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
    billing,
    crypto,
    db,
    entitlements,
    jobs,
    nodes,
    plan_apply,
    provisioning,
    realtime,
    realtime_workers,
    storage,
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
        "slots": check_replication_slots(conn, key_ring=key_ring),
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
    "measure_storage",
    "retry_failed_provisioning",
    "run_all",
    "sleep_idle_workers",
    "sleepable_now",
    "unenforced_capacity",
]
