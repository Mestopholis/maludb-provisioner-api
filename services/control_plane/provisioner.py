"""The provisioner worker (ADR-038).

The process that holds node superuser credentials, so that the process bound to
the internet does not. Everything else about it follows from that one sentence.

A customer asking for a project writes a row and reserves a place on a node;
this claims that row and does the work `cp-manage project retry` has always
done -- `jobs.provision`, which Phase 02 built resumable and which
`provisioning_jobs` has always recorded attempt by attempt. Nothing here is a
new provisioning path. What is new is that a customer can start one.

**Claiming, not scanning.** Work is taken with `SELECT ... FOR UPDATE SKIP
LOCKED`, so two workers on one control plane take different projects rather
than the same one twice. The database is the queue: adding a broker to carry
messages between two processes that already share a transactional database
would be a second source of truth about what has been provisioned, and the
first thing it would do is disagree.

**Failures belong to the project, not to the worker.** `jobs.provision` already
records an attempt, sanitises the error, sets `RETRY_WAIT` with a time and
counts consecutive failures against a cap. So a failure here is logged and the
loop moves on: a worker that stopped on a bad project would let one customer's
broken request halt provisioning for everybody else's.

Run it with `python -m services.control_plane.provisioner`, or under the
systemd unit in `deploy/`. It needs `MALUDB_NODE_ADMIN_DSN` only for nodes whose
admin credential has not been stored; ordinarily it reads each node's own
credential from the node row, which is why it must never run where the public
application runs.
"""

from __future__ import annotations

import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass

import psycopg

from services.control_plane import config as config_module
from services.control_plane import crypto, db, jobs, maludb, maludb_jobs, maludb_memory, maludb_vectors, nodes
from services.control_plane import logging as cp_logging

log = logging.getLogger(__name__)

# How long to wait when there was nothing to do. Short enough that a customer
# watching a spinner does not notice it, long enough that an idle platform is
# not asking the database a question every few milliseconds.
IDLE_SLEEP_SECONDS = 2.0

# What a claim is allowed to be: a project someone asked for, or one whose
# retry time has come. DELETING and DELETED are excluded by the status list and
# `deleted_at` by the predicate, because provisioning a project that is being
# torn down would race the teardown for the same roles.
CLAIMABLE_STATUSES = ("REQUESTED", "PLACEMENT_RESERVED", "RETRY_WAIT")


@dataclass(frozen=True)
class Claim:
    project_id: uuid.UUID
    project_ref: str
    node_id: int


def claim_one(conn: psycopg.Connection) -> Claim | None:
    """Take the project that has waited longest, or return None.

    `FOR UPDATE SKIP LOCKED` is what makes a second worker safe. The row stays
    locked for the life of the caller's transaction, so the caller commits
    before doing any node work -- holding a control-plane transaction open for
    the nine seconds a bootstrap takes would be a lock on the project row for
    every reader of it, including the customer polling for status.
    """
    row = db.one(
        conn,
        """
        SELECT id, project_ref, node_id
          FROM projects
         WHERE deleted_at IS NULL
           AND node_id IS NOT NULL
           AND status = ANY(%s)
           AND (retry_after IS NULL OR retry_after <= now())
           AND provisioning_failures < %s
         ORDER BY requested_at NULLS FIRST, created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1
        """,
        (list(CLAIMABLE_STATUSES), jobs.MAX_ATTEMPTS),
    )
    if row is None:
        return None
    return Claim(project_id=row["id"], project_ref=row["project_ref"], node_id=row["node_id"])


def provision_claim(claim: Claim, *, key_ring: crypto.KeyRing, platform_owner: str) -> bool:
    """Do the node work for one claimed project. True if it reached ACTIVE.

    The node's admin credential is decrypted here, handed straight to psycopg
    and never logged -- the same handling `cp-manage` uses, in the process ADR
    -038 says is allowed to have it.
    """
    with db.connection() as conn:
        dsn = nodes.admin_dsn(conn, node_id=claim.node_id, key_ring=key_ring)

    def tenant_connect(database: str):
        parsed = psycopg.conninfo.conninfo_to_dict(dsn)
        parsed["dbname"] = database
        return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)

    admin_conn = psycopg.connect(dsn)
    try:
        with db.connection() as conn:
            jobs.provision(
                conn,
                admin_conn,
                project_id=claim.project_id,
                key_ring=key_ring,
                platform_owner=platform_owner,
                tenant_connect=tenant_connect,
            )
        log.info(
            "provisioned project %s", claim.project_ref,
            extra={"extra_fields": {"project_ref": claim.project_ref}},
        )
        return True
    except jobs.RetriesExhausted:
        # Already recorded against the project, and not this worker's to solve:
        # an operator cleans it up, which resets the count.
        log.warning("project %s has exhausted its provisioning attempts", claim.project_ref)
        return False
    except Exception:  # noqa: BLE001 - one project's failure must not stop the rest
        # jobs.provision has already written the attempt, sanitised the error
        # and set the retry time. Logged without the exception's own text,
        # because that text has reached this process from a node and is the one
        # thing here that could carry a credential into a log line.
        log.error(
            "provisioning failed for project %s; recorded against the project",
            claim.project_ref,
            extra={"extra_fields": {"project_ref": claim.project_ref}},
        )
        return False
    finally:
        admin_conn.close()


def run_once(*, key_ring: crypto.KeyRing, platform_owner: str) -> bool:
    """Claim and provision one project. False when there was nothing to do."""
    with db.connection() as conn:
        claim = claim_one(conn)
        # Committed before the node work starts, releasing the row lock. The
        # project's own status is what stops a second worker picking it up:
        # `jobs.provision` moves it out of the claimable statuses as its first
        # act, and `provisioning_jobs_one_open_per_project` refuses a second
        # open attempt even if two workers raced the gap.
        conn.commit()
    if claim is None:
        return False
    provision_claim(claim, key_ring=key_ring, platform_owner=platform_owner)
    return True


def run_maludb_once(*, key_ring: crypto.KeyRing) -> bool:
    """Claim and do one MaluDB data-model request. False when there was nothing to do.

    Phase 12 slice 4 (ADR-074), and vector compartments since compartments slice 2b
    (ADR-077). Here rather than in the public application
    because both kinds run as the node superuser (ADR-038).

    **What reaches the customer is chosen here.** A job's `detail` is shown by
    the status route, so a `MaludbError` -- the platform's own refusal, written to
    be read by a customer, like a squatted schema name -- is passed through, and
    anything else becomes a generic sentence. The exception's own text has come
    from a node and is the one thing that could carry an internal detail to a
    customer, the same reason `provision_claim` keeps it out of the log.
    """
    with db.connection() as conn:
        job = maludb_jobs.claim(conn)
        # Committed before the node work, as `run_once` does: the job is marked
        # running, which is what keeps a second worker off it.
        conn.commit()
    if job is None:
        return False

    try:
        with db.connection() as conn:
            dsn = nodes.admin_dsn(conn, node_id=job["node_id"], key_ring=key_ring)

        def tenant_connect(database: str):
            parsed = psycopg.conninfo.conninfo_to_dict(dsn)
            parsed["dbname"] = database
            return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)

        with db.connection() as conn:
            if job["kind"] == maludb_jobs.KIND_MEMORY_SPACES:
                built = maludb_memory.build_pending(conn, project_id=job["project_id"],
                                                    tenant_connect=tenant_connect, key_ring=key_ring)
                result = {"created": built.created, "failed": built.failed}
            elif job["kind"] == maludb_jobs.KIND_VECTORS_ENABLE:
                maludb_vectors.enable(conn, project_id=job["project_id"], tenant_connect=tenant_connect)
                result = {"enabled": True}
            elif job["kind"] == maludb_jobs.KIND_VECTORS_DISABLE:
                maludb_vectors.disable(conn, project_id=job["project_id"], tenant_connect=tenant_connect)
                result = {"withdrawn": True}
            elif job["kind"] == maludb_jobs.KIND_DISABLE:
                maludb.disable(conn, project_id=job["project_id"], tenant_connect=tenant_connect)
                result = {"withdrawn": True}
            elif job["kind"] == maludb_jobs.KIND_ENABLE:
                done = maludb.enable(conn, project_id=job["project_id"], tenant_connect=tenant_connect)
                result = {"memory_schema_version": done.memory_schema_version}
                if done.copy is not None:
                    result.update(relations=done.copy.relations, nodes=done.copy.nodes,
                                  edges=done.copy.edges)
            else:
                copied = maludb.refresh(conn, project_id=job["project_id"],
                                        tenant_connect=tenant_connect)
                result = {"relations": copied.relations, "nodes": copied.nodes,
                          "edges": copied.edges}
            maludb_jobs.finish(conn, job["id"], succeeded=True, result=result)
            conn.commit()
        log.info("maludb %s done for project %s", job["kind"], job["project_ref"],
                 extra={"extra_fields": {"project_ref": job["project_ref"]}})
    except maludb.MaludbError as exc:
        with db.connection() as conn:
            # Refused, not broken: counted against the plan's limit, because a
            # refusal is something a customer can cause after superuser work
            # has started.
            maludb_jobs.finish(conn, job["id"], succeeded=False, detail=str(exc), refused=True)
            conn.commit()
        log.info("maludb %s refused for project %s", job["kind"], job["project_ref"])
    except Exception as exc:  # noqa: BLE001 - one project's failure must not stop the rest
        with db.connection() as conn:
            maludb_jobs.finish(
                conn, job["id"], succeeded=False,
                detail="the platform could not complete this request; it has been logged "
                       "and can be asked for again",
            )
            conn.commit()
        log.error("maludb %s failed for project %s (%s)", job["kind"], job["project_ref"],
                  type(exc).__name__,
                  extra={"extra_fields": {"project_ref": job["project_ref"]}})
    return True


def main() -> int:
    cfg = config_module.load()
    cp_logging.configure()
    platform_owner = os.environ.get("MALUDB_PLATFORM_OWNER", "").strip()
    if not platform_owner:
        # Fail closed rather than guess. Owning a tenant database is what makes
        # the platform able to take it back; a wrong owner produces databases
        # nobody can administer.
        log.error("MALUDB_PLATFORM_OWNER is required for the provisioner")
        return 2

    db.init_pool(cfg.database_url)
    key_ring = crypto.KeyRing(cfg.kek)
    with db.connection() as conn:
        key_ring.load(conn)

    stopping = False

    def stop(signum, _frame) -> None:  # noqa: ANN001 - signal handler signature
        nonlocal stopping
        stopping = True
        log.info("provisioner stopping on signal %s", signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info(
        "provisioner started",
        extra={"extra_fields": {"database": cfg.safe_database_dsn}},
    )
    try:
        while not stopping:
            # A project at a time, and the loop asks again immediately when it
            # found one: a queue that emptied slowly would leave the last
            # customer of a busy minute waiting for a poll interval that exists
            # for the idle case.
            # Alternated rather than drained one after the other, so a burst of
            # project creations cannot starve a customer's refresh, or the
            # reverse.
            provisioned = run_once(key_ring=key_ring, platform_owner=platform_owner)
            requested = run_maludb_once(key_ring=key_ring)
            if not provisioned and not requested:
                time.sleep(IDLE_SLEEP_SECONDS)
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
