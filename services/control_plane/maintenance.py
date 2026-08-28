"""The periodic passes, and the thing that runs them.

Three functions existed before this module and nothing called any of them:
`workers.idle_workers`, `storage.due_for_measurement`, and `jobs.due_for_retry`.
Each was written, tested, and inert. That is the shape most of Phase 05 was in
-- a measurement that enforces nothing -- and it matters most here, because
ADR-022 says free-tier economics rest **entirely** on workers sleeping when idle
and nothing was putting them to sleep.

Deliberately invoked rather than daemonised. `cp-manage maintenance run` under a
systemd timer or cron is a scheduler an operator can read, run by hand, and stop;
a long-lived process inside the control plane would be a second thing to
supervise and a second thing to notice has died. ADR-027 already took the same
position about worker supervision.

Every pass is safe to run concurrently with itself and safe to interrupt. A pass
that dies halfway leaves the projects it already handled handled, and the next
run picks up the rest -- which is why each returns what it did rather than
raising on the first project that fails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from services.control_plane import (
    auth_workers,
    backup,
    billing,
    crypto,
    db,
    entitlements,
    jobs,
    nodes,
    object_storage,
    plan_apply,
    provisioning,
    realtime,
    realtime_workers,
    reconcile,
    storage,
    storage_workers,
    subscriptions,
    workers,
)

log = logging.getLogger(__name__)

# How long a worker must be idle before it is slept. ADR-022 measured cold start
# at 320 ms for PostgREST and 175-268 ms for Auth, which is what makes an
# aggressive policy correct rather than a compromise: the cost of being wrong is
# a third of a second on the next request.
DEFAULT_IDLE_MINUTES = 15

# Realtime waits longer, and the asymmetry is the measurement rather than
# caution. Waking PostgREST costs a third of a second on one request; waking a
# Realtime instance means booting a BEAM and running its migrations, which is
# tens of seconds, and it is paid by a client opening a WebSocket rather than by
# a request that can be retried. An instance is also worth ~146 MB, so the
# saving is real -- this is where the two arguments balance, not where one wins.
REALTIME_IDLE_MINUTES = 60

# ADR-051's grace period, used only when a caller passes none. The real value is
# `config.billing_grace_days` and `cp-manage maintenance run` reads it from
# there -- this exists so that a caller that forgot cannot accidentally mean
# "no grace at all", which is the one wrong answer that costs a customer their
# plan on the first failed card.
DEFAULT_GRACE_DAYS = 14


@dataclass
class PassResult:
    """What a pass did. Failures are collected, not raised."""

    handled: int = 0
    failed: int = 0
    detail: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.detail.append(message)

    def __str__(self) -> str:
        return f"{self.handled} handled, {self.failed} failed"


def sleep_idle_workers(
    conn: psycopg.Connection,
    *,
    supervisor: workers.Supervisor,
    auth_supervisor: workers.Supervisor,
    realtime_supervisor: workers.Supervisor | None = None,
    idle_minutes: int = DEFAULT_IDLE_MINUTES,
    realtime_idle_minutes: int = REALTIME_IDLE_MINUTES,
) -> PassResult:
    """Stop workers nothing has used recently.

    The database and its data are untouched; only the process stops. ADR-005 and
    ADR-022: a slept project must remain a project, and free-tier density rests
    on the fact that a sleeping one costs zero connections and zero RAM.

    Auth and API workers sleep independently, because they are counted
    independently -- a project whose Data API is busy while nothing touches Auth
    should give the Auth worker back.

    Realtime sleeps the same way and matters most: ADR-034 measured an instance
    at ~146 MB against 31.8 MB for an entire warm project, so one idle Realtime
    is worth more than every other worker on the node put together. Its
    supervisor is optional because a node with no container runtime has no
    Realtime instances to stop, and a maintenance pass should not fail for
    lacking a supervisor it never needs.
    """
    result = PassResult()

    for project in workers.idle_workers(conn, idle_minutes=idle_minutes):
        try:
            workers.stop_worker(conn, project_id=project["id"], supervisor=supervisor)
            result.handled += 1
            result.note(f"slept api worker for {project['project_ref']}")
        except workers.WorkerError as exc:
            result.failed += 1
            log.warning("could not sleep api worker for %s: %s", project["project_ref"], exc)

    for project in auth_workers.idle_auth_workers(conn, idle_minutes=idle_minutes):
        try:
            auth_workers.stop_worker(
                conn, project_id=project["id"], supervisor=auth_supervisor
            )
            result.handled += 1
            result.note(f"slept auth worker for {project['project_ref']}")
        except auth_workers.AuthWorkerError as exc:
            result.failed += 1
            log.warning("could not sleep auth worker for %s: %s", project["project_ref"], exc)

    if realtime_supervisor is not None:
        for project in realtime_workers.idle_realtime_workers(
            conn, idle_minutes=realtime_idle_minutes
        ):
            try:
                realtime_workers.stop_worker(
                    conn, project_id=project["id"], supervisor=realtime_supervisor
                )
                result.handled += 1
                result.note(f"slept realtime worker for {project['project_ref']}")
            except (realtime_workers.RealtimeWorkerError, workers.WorkerError) as exc:
                result.failed += 1
                log.warning(
                    "could not sleep realtime worker for %s: %s", project["project_ref"], exc
                )

    return result


def measure_storage(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    limit: int = 50,
    connect_to_node=None,
) -> PassResult:
    """Measure projects and enforce their quotas, least recently measured first.

    Ordering by measurement age rather than by project id is what stops a pass
    with a limit re-measuring the same head of the list forever while the tail
    goes years without being looked at.
    """
    result = PassResult()
    connect_to_node = connect_to_node or _node_connections

    for project in storage.due_for_measurement(conn, limit=limit):
        if project["node_id"] is None:
            continue
        try:
            admin_conn, tenant_connect = connect_to_node(conn, project["node_id"], key_ring)
        except Exception as exc:  # noqa: BLE001 - one unreachable node must not stop the pass
            result.failed += 1
            log.warning("could not reach the node for %s: %s", project["project_ref"], type(exc).__name__)
            continue
        try:
            usage = storage.evaluate(
                conn, admin_conn, project_id=project["id"], tenant_connect=tenant_connect
            )
            result.handled += 1
            if usage.state != storage.OK:
                result.note(f"{project['project_ref']}: {usage.state} ({usage.fraction:.0%})")
        except storage.StorageError as exc:
            result.failed += 1
            log.warning("could not measure %s: %s", project["project_ref"], exc)
        finally:
            admin_conn.close()

    return result


def measure_object_storage(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    limit: int = 50,
    connect_to_node=None,
    config=None,
) -> PassResult:
    """ADR-056's held-bytes ceiling, re-measured and re-classified.

    Its own pass rather than a second query inside `measure_storage`, and its
    own `object_measured_at` cursor, because the two must be able to fail
    independently. They read different things -- `pg_database_size` on the node
    admin connection, `storage.objects` inside the tenant -- and a project whose
    tenant connection fails should not stop its database size being measured, or
    the reverse.

    Nothing is applied to the tenant here. `storage.measure_storage` revokes
    INSERT and UPDATE when a project is over; this only records a state, because
    object bytes arrive through the Storage API and slice 4's gateway is what
    refuses them. A pass that appeared to enforce and did not would be worse
    than one that plainly does not.

    `config` carries the object store, and passing it is what makes the figure
    trustworthy: without it the pass falls back to the tenant's own metadata,
    which a customer reaching `service_role` can rewrite. Optional rather than
    required so a deployment with no object store still runs the pass, and so a
    test can drive it without one.
    """
    result = PassResult()
    connect_to_node = connect_to_node or _node_connections

    if config is None or not config.storage_s3_endpoint:
        # Said out loud, because for four slices it was not. `run_all` defaults
        # `config` to None and the only production caller did not pass one, so
        # every measurement outside the suite silently took the fallback -- the
        # figure a customer who reaches `service_role` can rewrite, which is the
        # whole reason the store-side measurement was written. The fallback is
        # still correct on a node with no object store; reaching it without
        # saying so is what was wrong.
        result.note(
            "no object store configured: held bytes are the tenant's own metadata, "
            "which a project that can reach service_role can rewrite"
        )

    for project in object_storage.due_for_measurement(conn, limit=limit):
        if project["node_id"] is None:
            continue
        try:
            admin_conn, tenant_connect = connect_to_node(conn, project["node_id"], key_ring)
        except Exception as exc:  # noqa: BLE001 - one unreachable node must not stop the pass
            result.failed += 1
            log.warning(
                "could not reach the node for %s: %s", project["project_ref"], type(exc).__name__
            )
            continue
        try:
            usage = object_storage.evaluate(
                conn, project_id=project["id"], tenant_connect=tenant_connect,
                config=config,
            )
            result.handled += 1
            if usage.state != object_storage.OK:
                result.note(f"{project['project_ref']}: objects {usage.state} ({usage.fraction:.0%})")
        except (object_storage.ObjectStorageError, psycopg.Error) as exc:
            result.failed += 1
            log.warning(
                "could not measure objects for %s: %s", project["project_ref"], type(exc).__name__
            )
        finally:
            admin_conn.close()

    return result


def reconcile_storage_tenants(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    config=None,
    node_name: str | None = None,
) -> PassResult:
    """Re-register projects the node's storage worker has forgotten.

    `storage_workers.registered_projects` was written in slice 3 and called by
    nothing; this is the caller it was written for. The case is narrow and the
    failure is silent, which is the combination worth a pass: an ordinary
    container restart keeps its tenants, because they live in the node's
    multitenant database rather than in the process -- but a worker whose
    *metadata database* was rebuilt has forgotten every one of them, while the
    control plane still holds a `storage_registered_at` for each. Those
    projects then answer `400 TenantNotFound` to every Storage request, and
    nothing anywhere says why.

    Slice 4 considered doing this on the request path instead, by re-registering
    when the upstream answers `TenantNotFound`, and rejected it: `app.py` does
    not read an upstream's response body, and a control that depends on parsing
    somebody else's error strings breaks on somebody else's release note. This
    asks the admin API a question with a status code for an answer.

    **One node, and it is the local one.** Unlike every other pass here, the
    storage worker's control surface is not reachable over the network: ADR-058
    puts one instance on the node and its admin port is published on loopback
    only (`test_both_ports_are_published_on_loopback_only`), so this repairs the
    worker on the host it runs on. With a single storage node that is
    unambiguous and the pass resolves it; with more than one, only an operator
    knows which host this is, so the pass says so and does nothing rather than
    re-registering another node's tenants into this node's worker.

    Registration is a `PUT` and therefore idempotent, so a worker that has not
    forgotten anything is left exactly as it was.
    """
    result = PassResult()
    if config is None or not config.storage_s3_endpoint:
        # A deployment without object storage, not a broken one -- the same
        # answer `ensure_registered` gives, and it must not read as a failure.
        return result

    candidates = db.query(
        conn,
        """
        SELECT n.id, n.name FROM nodes n
         WHERE n.storage_secret_ciphertext IS NOT NULL
           AND EXISTS (SELECT 1 FROM projects p
                        WHERE p.node_id = n.id
                          AND p.deleted_at IS NULL
                          AND p.storage_registered_at IS NOT NULL)
         ORDER BY n.name
        """,
    )
    if node_name is not None:
        candidates = [node for node in candidates if node["name"] == node_name]
        if not candidates:
            result.note(f"no node named {node_name} has storage-registered projects")
            return result
    if not candidates:
        return result
    if len(candidates) > 1:
        result.note(
            "more than one node has storage-registered projects "
            f"({', '.join(node['name'] for node in candidates)}); the worker's admin API is "
            "on loopback, so name the local one with --node"
        )
        return result

    node = candidates[0]
    try:
        root = storage_workers.node_secret(conn, node_id=node["id"], key_ring=key_ring)
        if root is None:
            # Unreachable through the query above, which filters on that
            # column. Explicit anyway, because the alternative is `None` going
            # into HKDF and coming back out as an AttributeError one frame
            # deeper -- and because a later edit to the query would land here.
            result.note(f"{node['name']}: no storage secret; it has never run a worker")
            return result
        secrets_ = storage_workers.derived_secrets(root)
    except Exception as exc:  # noqa: BLE001 - never the message; it is key material
        result.failed += 1
        log.warning(
            "could not read the storage secret for node %s: %s", node["name"], type(exc).__name__
        )
        return result

    if not storage_workers.is_ready(
        admin_port=config.storage_admin_port, api_key=secrets_.admin_api_key
    ):
        # A stopped worker is not a rebuilt one, and registering into a
        # half-started one is the failure `is_ready` was written to prevent:
        # the container accepts connections before it has migrated the database
        # the tenant row goes into.
        result.note(f"{node['name']}: the storage worker is not ready; nothing reconciled")
        return result

    for project in storage_workers.registered_projects(conn, node_id=node["id"]):
        ref = project["project_ref"]
        try:
            if storage_workers.tenant_known(
                admin_port=config.storage_admin_port,
                api_key=secrets_.admin_api_key,
                project_ref=ref,
            ):
                result.handled += 1
                continue
            storage_workers.ensure_registered(
                conn,
                project_id=project["id"],
                project_ref=ref,
                node_id=node["id"],
                config=config,
                key_ring=key_ring,
            )
            conn.commit()
            result.handled += 1
            result.note(f"{ref}: the storage worker had forgotten it; re-registered")
        except Exception as exc:  # noqa: BLE001 - one bad project must not stop the pass
            # Never the message: `ensure_registered` builds a DSN carrying a
            # live password, and a driver error can echo the statement it came
            # from.
            result.failed += 1
            log.warning(
                "could not reconcile %s with the storage worker: %s", ref, type(exc).__name__
            )

    return result


def retry_failed_provisioning(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    platform_owner: str,
    limit: int = 20,
    connect_to_node=None,
) -> PassResult:
    """Resume projects whose provisioning failed and whose backoff has elapsed.

    `jobs.due_for_retry` has existed since Phase 02 slice 4 and nothing called
    it, so `RETRY_WAIT` was a state projects entered and never left without an
    operator typing a command.
    """
    result = PassResult()
    connect_to_node = connect_to_node or _node_connections

    for project in jobs.due_for_retry(conn, limit=limit):
        if project["node_id"] is None:
            continue
        try:
            admin_conn, tenant_connect = connect_to_node(conn, project["node_id"], key_ring)
        except Exception as exc:  # noqa: BLE001 - see above
            result.failed += 1
            log.warning("could not reach the node for %s: %s", project["project_ref"], type(exc).__name__)
            continue
        try:
            jobs.provision(
                conn,
                admin_conn,
                project_id=project["id"],
                key_ring=key_ring,
                platform_owner=platform_owner,
                tenant_connect=tenant_connect,
            )
            result.handled += 1
            result.note(f"provisioned {project['project_ref']}")
        except jobs.RetriesExhausted:
            # Not a failure of this pass. The project has been moved to FAILED
            # and needs an operator, which is what the retry cap is for.
            result.note(f"{project['project_ref']}: retries exhausted, needs cleanup")
        except Exception as exc:  # noqa: BLE001 - one bad project must not stop the pass
            result.failed += 1
            log.warning("retry failed for %s: %s", project["project_ref"], type(exc).__name__)
        finally:
            admin_conn.close()

    return result


def check_replication_slots(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    connect_to_node=None,
) -> PassResult:
    """Find replication slots that have stopped working, and say so.

    ADR-032 made invalidation a designed outcome rather than an edge case: a
    bounded `max_slot_wal_keep_size` is what stops one stalled consumer filling
    a node's disk, and the price is a slot that goes away. The project then
    receives no changes and **nothing in the connection says so**, which is why
    this pass exists at all. A contained failure nobody is told about is
    indistinguishable from data loss.

    Nodes with no Realtime projects are still checked when they are prepared for
    Realtime, because an unaccounted physical slot pins WAL exactly as a logical
    one does and no project would ever point the pass at it.
    """
    result = PassResult()
    connect_to_node = connect_to_node or _node_connections

    candidates = db.query(
        conn,
        """
        SELECT n.id, n.name FROM nodes n
         WHERE n.capacity_json ->> 'realtime_ready' = 'true'
            OR EXISTS (SELECT 1 FROM projects p
                        WHERE p.node_id = n.id AND p.realtime_enabled AND p.deleted_at IS NULL)
         ORDER BY n.name
        """,
    )

    for node in candidates:
        try:
            admin_conn, _ = connect_to_node(conn, node["id"], key_ring)
        except Exception as exc:  # noqa: BLE001 - one unreachable node must not stop the pass
            result.failed += 1
            log.warning("could not reach node %s: %s", node["name"], type(exc).__name__)
            continue
        try:
            report = realtime.reconcile_slots(
                conn, admin_conn, node_id=node["id"], node_name=node["name"]
            )
            result.handled += report.checked
            for ref in report.invalidated:
                result.note(
                    f"{ref}: replication slot invalidated -- not receiving changes, and "
                    "re-creating the slot resumes from the present without replaying the gap"
                )
            for ref in report.missing:
                result.note(f"{ref}: replication slot absent from {node['name']}")
            for ref in _realtime_past_its_plan(conn, node["id"]):
                # Reported, not acted on. `realtime.apply_plan` removes it, and
                # only from a provisioning run somebody triggered: entitlement
                # resolution falls back to the free tier for an unrecognised
                # plan code, so a background pass acting on this would take a
                # working capability away from a paying customer over a missing
                # key in a plan row. Phase 05 learned the same lesson the same
                # way -- report before enforcing.
                result.note(
                    f"{ref}: plan no longer includes Realtime, but the slot is still held; "
                    "the next provisioning run removes it"
                )
            for slot in report.unaccounted:
                # Not counted as a failure: an operator's own slot is legitimate.
                # It is reported because ADR-032 records that a role holding
                # REPLICATION can create a WAL-reserving physical slot through
                # ordinary SQL, which the ADR-031 pg_hba reject does not close.
                result.note(f"{node['name']}: unaccounted slot {slot}")
        except Exception as exc:  # noqa: BLE001 - see above
            result.failed += 1
            log.warning("slot check failed for node %s: %s", node["name"], type(exc).__name__)
        finally:
            admin_conn.close()

    return result


def check_backups(conn: psycopg.Connection) -> PassResult:
    """Say whether each node's backups exist, completed, and are recent enough.

    **A backup that has never been checked is not known to be a backup**, which
    is why this is in slice 1 rather than deferred to a later one. The failure
    this defends against is not a backup that errors -- an error is loud. It is
    ADR-067's measured one: an untuned pgBackRest backup of an *idle* cluster
    waits for a regular checkpoint that PostgreSQL never schedules, because it
    skips timed checkpoints when no WAL has been written. 15+ minutes at 0% CPU,
    `num_timed = 0` after forty minutes of uptime.

    ADR-022 rests free-tier economics on projects that sleep, so "a node whose
    tenants wrote nothing last night" is the platform's normal state and not an
    edge case. On such a node an untuned nightly backup produces no error, no
    exit code, and nothing in the repository -- so **silence is read here as
    failure**, never as "still going".

    Reads only the control plane. It does not open a connection to a node or to
    a repository, deliberately: the question it answers is "is the platform
    being told about backups, and are they recent", which is a question about
    the record. Whether the repository holds a *restorable* copy is a different
    question, only a restore answers it, and that is slice 2. Nothing here may
    ever be read as evidence of recoverability.
    """
    result = PassResult()

    for status in backup.node_status(conn):
        result.handled += 1
        problems = status.problems
        if not problems:
            continue
        # Counted as failed rather than merely noted. A node without a usable
        # backup is not an observation, and the recurring failure mode in this
        # repository is a green run that verified nothing.
        result.failed += 1
        for problem in problems:
            result.note(f"{status.name}: {problem}")

    return result


def _realtime_past_its_plan(conn: psycopg.Connection, node_id: int) -> list[str]:
    """Projects holding a replication slot their plan no longer entitles them to.

    A slot is one of ten on the node, so this is capacity spent on a capability
    nobody is paying for -- worth surfacing even though nothing here removes it.
    """
    from services.control_plane import entitlements

    rows = db.query(
        conn,
        """
        SELECT p.project_ref, pl.code AS plan_code, pl.config_json
          FROM projects p LEFT JOIN plans pl ON pl.id = p.plan_id
         WHERE p.node_id = %s AND p.realtime_enabled AND p.deleted_at IS NULL
         ORDER BY p.project_ref
        """,
        (node_id,),
    )
    return [
        row["project_ref"]
        for row in rows
        if entitlements.resolve(row["plan_code"], row["config_json"]).realtime_connections <= 0
    ]


def report_plan_drift(conn: psycopg.Connection, *, key_ring: crypto.KeyRing) -> PassResult:
    """Name the projects whose node disagrees with their plan. Change nothing.

    Reporting rather than correcting, and the reason is a control that already
    exists: `cp-manage project direct-sql --disable` lets an operator revoke a
    paid project's access during an incident, and that project's plan still
    says it is entitled. A reconciler on a timer would undo that within the
    hour -- a control cancelling a control, which is the failure this
    repository keeps finding rather than one it should add.

    So the pass says what diverged and which way it points, and
    `cp-manage project plan-apply` is what acts on it. `excess` is a project
    getting more than its plan grants, which is either that incident measure or
    a privilege nobody is paying for; `withheld` is a plan change that never
    reached the node, which before Phase 09 slice 0 was every plan change.
    """
    result = PassResult()
    by_node: dict[int, list[dict]] = {}
    for row in plan_apply.project_rows(conn):
        by_node.setdefault(row["node_id"], []).append(row)

    for node_id, rows in by_node.items():
        try:
            admin_conn, _ = _node_connections(conn, node_id, key_ring)
        except Exception as exc:  # noqa: BLE001 - a node being unreachable is a note, not a stop
            result.failed += len(rows)
            result.note(f"node {node_id} unreachable: {type(exc).__name__}")
            continue
        try:
            for row in rows:
                names = provisioning.TenantNames.for_ref(row["project_ref"])
                allowed = entitlements.resolve(row["plan_code"], row["config_json"])
                try:
                    report = plan_apply.inspect(admin_conn, names, allowed)
                except psycopg.Error as exc:
                    result.failed += 1
                    result.note(f"{row['project_ref']}: {exc.sqlstate}")
                    continue
                result.handled += 1
                if report.clean:
                    continue
                excess = sum(1 for d in report.divergences if d.direction == plan_apply.EXCESS)
                withheld = len(report.divergences) - excess
                missing = f", {len(report.missing_roles)} role(s) absent" if report.missing_roles else ""
                result.note(
                    f"{row['project_ref']} ({report.plan_code}): "
                    f"{withheld} withheld, {excess} excess{missing}"
                )
        finally:
            admin_conn.close()

    return result


def expire_billing_grace(
    conn: psycopg.Connection,
    *,
    grace_days: int,
    client=None,
) -> PassResult:
    """End the tolerance on failed payments that have run out of it (ADR-051).

    Runs immediately before `reconcile_subscriptions`, which is what moves the
    project to the free plan, and before `measure_storage`, which is what
    restricts it if the data it holds is now over the free quota. Three passes,
    in that order, so a customer whose grace ends is downgraded and restricted
    in the same run rather than over three.

    **Nothing here deletes anything, and nothing downstream of it does either.**
    That is acceptance criterion 4 satisfied by there being no code that could
    break it rather than by a check that could be removed.
    """
    result = PassResult()
    for outcome in billing.end_expired_grace(conn, client, grace_days=grace_days):
        if outcome.outcome == "deferred":
            # Not a failure of the pass. The customer keeps their plan a little
            # longer, which is the direction to be wrong in.
            result.note(f"{outcome.project_ref}: deferred -- {outcome.note}")
            continue
        result.handled += 1
        result.note(f"{outcome.project_ref}: {outcome.note}")
    return result


def reconcile_subscriptions(
    conn: psycopg.Connection, *, key_ring: crypto.KeyRing, connect_to_node=None
) -> PassResult:
    """Make paid-for plans true on their nodes (Phase 09 slice 4, ADR-053).

    **Why this pass exists rather than the webhook doing it.** Stripe posts from
    the internet, so the endpoint that receives an event runs in the public
    application -- and ADR-038 keeps node superuser credentials out of that
    process entirely. The webhook records what was paid for; this runs where the
    credentials live and makes it true. A customer's plan therefore arrives a
    pass later than their payment, which is seconds to a minute.

    **This is not the reconciler `report_plan_drift` refuses to be**, and the
    difference is worth being precise about because that refusal is a rule this
    repository keeps. That pass declines to correct *node-level* divergence,
    because `cp-manage project direct-sql --disable` is an operator's incident
    control and a timer that undid it would be a control cancelling a control.

    This pass corrects something else: a project whose **plan** does not match
    what is being paid for, and only when a specific billing event changed the
    answer -- `pending_reconciliation` is a queue of facts that arrived, not a
    sweep of everything that looks wrong. It goes through `plan_change`, which
    does nothing at all when the plan already matches, so an operator who
    revoked access during an incident is untouched: they changed the node, not
    the plan, and this never looks at the node.

    Pre-existing divergence with no event behind it stays where slice 3 put it,
    reported by `cp-manage subscription drift` for a person to decide about.

    `connect_to_node` is injectable on `measure_storage`'s precedent, so the
    pass can be exercised end to end against a real node without the test
    having to store an encrypted superuser DSN to get one.
    """
    result = PassResult()
    connect_to_node = connect_to_node or _node_connections
    expired = billing.expire_stale_checkouts(conn)
    if expired:
        result.note(f"{expired} stale checkout(s) closed")

    pending = subscriptions.pending_reconciliation(conn)
    if not pending:
        return result

    # Grouped by node so one connection serves every project on it, which is
    # what `report_plan_drift` does and for the same reason: a fleet-wide pass
    # opening a superuser connection per project is a connection storm.
    by_node: dict[int, list[dict]] = {}
    for row in pending:
        node = db.one(
            conn, "SELECT node_id FROM projects WHERE id = %s", (row["project_id"],)
        )
        if node is None or node["node_id"] is None:
            # Provisioning has not placed it yet. Left pending rather than
            # marked done: the fact is still true and still unapplied.
            result.failed += 1
            result.note(f"{row['project_ref']}: not placed on a node yet")
            continue
        by_node.setdefault(node["node_id"], []).append(row)

    for node_id, rows in by_node.items():
        try:
            admin_conn, _ = connect_to_node(conn, node_id, key_ring)
        except Exception as exc:  # noqa: BLE001 - a node being unreachable is a note, not a stop
            result.failed += len(rows)
            result.note(f"node {node_id} unreachable: {type(exc).__name__}")
            continue
        try:
            for row in rows:
                try:
                    outcome = subscriptions.reconcile(
                        conn, admin_conn, project_id=row["project_id"]
                    )
                except Exception as exc:  # noqa: BLE001 - one project, not the pass
                    conn.rollback()
                    result.failed += 1
                    # The type, not the message: a plan-change failure can carry
                    # a node's own error text, and this list is printed.
                    result.note(f"{row['project_ref']}: {type(exc).__name__}")
                    continue
                result.handled += 1
                if outcome.changed:
                    result.note(
                        f"{row['project_ref']}: {outcome.plan_code} -> "
                        f"{outcome.entitled_plan_code} ({row['state']})"
                    )
        finally:
            admin_conn.close()
    return result


def reconcile_objects(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    config=None,
    limit: int = 20,
    connect_to_node=None,
) -> PassResult:
    """Compare each project's object metadata with the platform bucket (ADR-069).

    **Reports; deletes nothing.** There is no collection step here and adding
    one is a decision with its own ADR: both populations are consequences of a
    failure somewhere else, and a pass that tidied up at three in the morning
    would destroy the evidence of whatever produced them.

    Bounded per run and ordered least-recently-reconciled first. This pass costs
    a bucket listing and a full read of `storage.objects` per project, where the
    measurement pass next door costs one number, so its limit is smaller and its
    coverage is a cycle rather than a sweep.

    A project whose store could not be read is counted as failed and its row is
    left untouched, so it stays at the front of the queue instead of being
    marked clean and sorting to the back.
    """
    result = PassResult()
    connect_to_node = connect_to_node or _node_connections

    if config is None or not config.storage_s3_endpoint:
        result.note(
            "no object store configured: object metadata was not compared with anything, "
            "so nothing here says a project's files exist"
        )
        return result

    for project in reconcile.due_for_reconciliation(conn, limit=limit):
        try:
            admin_conn, tenant_connect = connect_to_node(conn, project["node_id"], key_ring)
        except Exception as exc:  # noqa: BLE001 - one unreachable node must not stop the pass
            result.failed += 1
            log.warning(
                "could not reach the node for %s: %s", project["project_ref"], type(exc).__name__
            )
            continue
        try:
            with tenant_connect(project["database_name"]) as tenant_conn:
                report = reconcile.reconcile(
                    config, tenant_conn, project_ref=project["project_ref"]
                )
            if not report.store_readable:
                result.failed += 1
                result.note(f"{project['project_ref']}: {report.problems()[0]}")
                continue
            reconcile.record(conn, project_id=project["id"], report=report)
            conn.commit()
            result.handled += 1
            for problem in report.problems():
                result.note(f"{project['project_ref']}: {problem}")
            # In-flight uploads are noted rather than counted as a problem: a
            # busy project is not a broken one, and the bytes are invisible to
            # the quota, which is why they are worth saying at all.
            if report.in_flight:
                result.note(
                    f"{project['project_ref']}: {len(report.in_flight)} incomplete multipart "
                    "upload(s) hold bytes the object listing and the quota do not see"
                )
        except (reconcile.ReconcileError, psycopg.Error) as exc:
            result.failed += 1
            log.warning(
                "could not reconcile %s: %s", project["project_ref"], type(exc).__name__
            )
        finally:
            admin_conn.close()

    return result


def _node_connections(conn: psycopg.Connection, node_id: int, key_ring: crypto.KeyRing):
    """A superuser connection to a node, and a factory for its tenant databases.

    The DSN is a live credential: decrypted here, handed straight to psycopg,
    and never logged or returned.
    """
    dsn = nodes.admin_dsn(conn, node_id=node_id, key_ring=key_ring)
    admin_conn = psycopg.connect(dsn)

    def tenant_connect(database: str):
        parsed = psycopg.conninfo.conninfo_to_dict(dsn)
        parsed["dbname"] = database
        return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)

    return admin_conn, tenant_connect


def run_all(
    conn: psycopg.Connection,
    *,
    key_ring: crypto.KeyRing,
    platform_owner: str,
    supervisor: workers.Supervisor,
    auth_supervisor: workers.Supervisor,
    realtime_supervisor: workers.Supervisor | None = None,
    idle_minutes: int = DEFAULT_IDLE_MINUTES,
    grace_days: int | None = None,
    billing_client=None,
    config=None,
    storage_node: str | None = None,
) -> dict[str, PassResult]:
    """Every pass, in an order chosen so each sees the others' work.

    Retries first, so a project that provisions successfully is measured in the
    same run rather than waiting for the next. Sleep last, so a worker started
    by a retry is not immediately slept for having no recent activity -- it has
    none, because it has only just started.
    """
    return {
        "retry": retry_failed_provisioning(
            conn, key_ring=key_ring, platform_owner=platform_owner
        ),
        # The billing passes come before storage, and the order is the design.
        # Grace expiring cancels a subscription; reconciliation moves that
        # project to the free plan; and only then does measuring it against the
        # *free* quota mean anything. Measuring first would leave a downgraded
        # project unrestricted until the next run -- correct eventually, and
        # confusing to anybody reading one run's output.
        "grace": expire_billing_grace(
            conn,
            grace_days=grace_days if grace_days is not None else DEFAULT_GRACE_DAYS,
            client=billing_client,
        ),
        # Before the drift report, so a plan a customer has just paid for is
        # applied in this run and does not show up in the same run's report as
        # divergence that needs a human.
        "billing": reconcile_subscriptions(conn, key_ring=key_ring),
        "storage": measure_storage(conn, key_ring=key_ring),
        # Beside database storage and after billing, for the same reason: a
        # project moved to the free plan by reconciliation must be measured
        # against the *free* object ceiling in the same run, not the next one.
        "object_storage": measure_object_storage(conn, key_ring=key_ring, config=config),
        # After the measurement rather than before it, because the two want
        # opposite things from a worker that has forgotten its tenants: the
        # measurement reads the object store and the tenant database directly
        # and is unaffected, while this one is the repair. Running it first
        # would only mean a re-registration nothing in the same run needed.
        "storage_tenants": reconcile_storage_tenants(
            conn, key_ring=key_ring, config=config, node_name=storage_node
        ),
        "slots": check_replication_slots(conn, key_ring=key_ring),
        # Reads the control plane only, so it neither needs a node to be
        # reachable nor is affected by one that is not. Placed after the passes
        # that touch nodes so that a run's output reads in the order an operator
        # would investigate: what broke on the nodes, then whether the thing
        # that would let them rebuild one is in place.
        "backups": check_backups(conn),
        # After the object *measurement* pass and for a different question:
        # that one asks how much a project is holding, this one asks whether
        # what it claims to hold is actually there. Slice 3 made it urgent --
        # a point-in-time restore returns metadata to the past while the
        # bucket stays present-day, so the two are guaranteed to disagree
        # after the one operation this phase exists to provide.
        "objects": reconcile_objects(conn, key_ring=key_ring, config=config),
        # After the retry pass, so a project that has just finished provisioning
        # is compared in its settled state rather than mid-flight.
        "plan_drift": report_plan_drift(conn, key_ring=key_ring),
        "sleep": sleep_idle_workers(
            conn, supervisor=supervisor, auth_supervisor=auth_supervisor,
            realtime_supervisor=realtime_supervisor, idle_minutes=idle_minutes,
        ),
    }


def unenforced_capacity(conn: psycopg.Connection) -> list[dict]:
    """Nodes already past a ceiling, for a report before enforcement bites.

    Enforcing warm capacity and connection headroom changes placement behaviour
    for nodes that currently accept projects. That wants a report first: a node
    over its ceiling today will start refusing, and an operator should learn
    that from a command rather than from a failed provisioning run.
    """
    over = []
    for row in db.query(conn, "SELECT id FROM nodes ORDER BY name"):
        capacity = nodes.capacity_of(conn, row["id"])
        reason = capacity.rejection_reason()
        # A node that has already committed slots and cannot take another is
        # over a ceiling even though it still accepts ordinary projects. Left
        # out, the report would describe such a node as healthy right up to the
        # enablement that fails.
        realtime_reason = (
            capacity.realtime_rejection_reason() if capacity.committed_slots else None
        )
        if reason or realtime_reason:
            over.append({"name": capacity.name, "reason": reason or realtime_reason,
                         "warm": capacity.current_warm_projects,
                         "projected_connections": capacity.projected_connections,
                         "usable_connections": capacity.usable_connections,
                         "committed_slots": capacity.committed_slots,
                         "usable_replication_slots": capacity.usable_replication_slots})
    return over


def sleepable_now(conn: psycopg.Connection, *, idle_minutes: int = DEFAULT_IDLE_MINUTES) -> int:
    """How many workers a sleep pass would stop. For a dry run."""
    return (
        len(workers.idle_workers(conn, idle_minutes=idle_minutes))
        + len(auth_workers.idle_auth_workers(conn, idle_minutes=idle_minutes))
        + len(realtime_workers.idle_realtime_workers(conn, idle_minutes=REALTIME_IDLE_MINUTES))
    )


__all__ = [
    "DEFAULT_IDLE_MINUTES",
    "PassResult",
    "check_replication_slots",
    "measure_object_storage",
    "measure_storage",
    "retry_failed_provisioning",
    "run_all",
    "sleep_idle_workers",
    "sleepable_now",
    "unenforced_capacity",
]
