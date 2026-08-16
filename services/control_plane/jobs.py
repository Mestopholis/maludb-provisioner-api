"""Provisioning as a resumable sequence of steps.

`specs/provisioning-state-machine.md` requires that each state handler be safe
to re-run or explicitly detect that its work is already done. Slice 3 left
provisioning as one linear function that refused outright if the project
already had a database, which made a failed run terminal: the tenant kept its
roles and its database, and nothing could move it forward or take it back.

The shape here is a list of steps, each with a `done` predicate that asks the
node what is actually true rather than trusting `projects.status`. Status says
what the control plane believed when it last wrote a row; a step that died
midway leaves the two disagreeing, and the node is the one telling the truth.

Retries resume at the first step that is not done. Nothing is dropped to get
back to a clean state -- see `cleanup`, and the data-safety invariant it
enforces.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from services.control_plane import crypto, db, entitlements, provisioning, tenant_bootstrap
from services.control_plane.provisioning import ProvisioningError, TenantNames

log = logging.getLogger(__name__)

# Five attempts at roughly 2, 4, 8, 16 seconds. Provisioning failures are
# usually either instant (a name collision) or persistent (a node that is
# down); a longer ladder mostly delays the operator finding out.
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (2, 4, 8, 16)


class RetriesExhausted(ProvisioningError):
    """The project has failed too many times to retry automatically."""


@dataclass
class Run:
    """Everything a step needs. Holds no plaintext secret between steps."""

    conn: psycopg.Connection
    admin_conn: psycopg.Connection
    project_id: uuid.UUID
    project_ref: str
    names: TenantNames
    key_ring: crypto.KeyRing
    platform_owner: str
    tenant_connect: Callable[[str], psycopg.Connection]
    plan_settings: dict[str, Any] = field(default_factory=dict)
    connection_limits: dict[str, int] = field(default_factory=dict)
    extension_versions: dict[str, str] = field(default_factory=dict)
    # Supplied only by callers that can supervise workers on this node, which
    # is an operator command and not, for instance, a test provisioning a
    # tenant. Without it a plan downgrade still takes the slots and the
    # replicator role back; what it cannot do is stop the container first.
    realtime_supervisor: Any | None = None


@dataclass(frozen=True)
class Step:
    status: str
    done: Callable[[Run], bool]
    run: Callable[[Run], None]


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def _roles_done(run: Run) -> bool:
    """Roles exist *and* their passwords are recoverable.

    Both halves matter. Roles without stored credentials is the state that
    strands a tenant: the passwords were generated in memory and lost, so
    nothing can authenticate and no amount of retrying the later steps helps.
    Reporting that as not-done sends the retry back through the password reset.
    """
    for role in (run.names.authenticator, run.names.auth, run.names.admin):
        if not provisioning.role_exists(run.admin_conn, role):
            return False
    stored = db.one(
        run.conn,
        """
        SELECT count(*) AS live FROM project_credentials
         WHERE project_id = %s AND revoked_at IS NULL
           AND credential_type IN ('db_authenticator', 'db_auth', 'db_admin')
        """,
        (run.project_id,),
    )
    return stored["live"] == 3


def _create_roles(run: Run) -> None:
    passwords = {
        "authenticator": provisioning.generate_password(),
        "auth": provisioning.generate_password(),
        "admin": provisioning.generate_password(),
    }
    try:
        provisioning.ensure_shared_roles(run.admin_conn)
        provisioning.create_roles(
            run.admin_conn,
            run.names,
            passwords=passwords,
            connection_limits=(
                run.connection_limits
                or entitlements.for_project(run.conn, run.project_id).connection_limits()
            ),
        )
        # Persisted before the node work is committed. If the commit below
        # fails the roles do not exist, which _roles_done reports honestly; if
        # the credential write failed instead, the same predicate sends the
        # next attempt back here to reset the passwords.
        for credential_type, role in (
            ("db_authenticator", run.names.authenticator),
            ("db_auth", run.names.auth),
            ("db_admin", run.names.admin),
        ):
            provisioning.store_credential(
                run.conn,
                project_id=run.project_id,
                credential_type=credential_type,
                role_name=role,
                secret=passwords[credential_type.removeprefix("db_")],
                key_ring=run.key_ring,
            )
        run.conn.commit()
        run.admin_conn.commit()
    finally:
        passwords.clear()


def _database_done(run: Run) -> bool:
    if not provisioning.database_exists(run.admin_conn, run.names.database):
        return False
    project = db.one(
        run.conn, "SELECT database_name FROM projects WHERE id = %s", (run.project_id,)
    )
    return project["database_name"] == run.names.database


def _create_database(run: Run) -> None:
    provisioning.create_database(run.admin_conn, run.names, owner=run.platform_owner)
    # Recorded as soon as it exists. A database the control plane has forgotten
    # is one cleanup will not find and nothing will ever reclaim (slice 1).
    db.execute(
        run.conn,
        "UPDATE projects SET database_name = %s WHERE id = %s",
        (run.names.database, run.project_id),
    )
    run.conn.commit()

    provisioning.lock_down_database(run.admin_conn, run.names)
    # Resolved from the project's plan rather than passed in. An earlier caller
    # that forgot to build the dict provisioned a tenant with no settings at
    # all, and nothing said so.
    allowed = entitlements.for_project(run.conn, run.project_id)
    provisioning.apply_plan_settings(
        run.admin_conn, run.names, settings=run.plan_settings or allowed.postgres_settings()
    )
    # ADR-005: direct SQL is a paid capability, and the plan is what says so.
    # Applied here rather than left to an operator, because a paid project whose
    # admin role stayed NOLOGIN would be sold a capability it did not have.
    provisioning.set_direct_sql_access(
        run.admin_conn, run.names, enabled=allowed.direct_database_access,
        connection_limit=allowed.database_connections,
    )
    run.admin_conn.commit()


def _bootstrap_done(run: Run) -> bool:
    project = db.one(
        run.conn, "SELECT bootstrap_version FROM projects WHERE id = %s", (run.project_id,)
    )
    return project["bootstrap_version"] == tenant_bootstrap.latest_version()


def _bootstrap(run: Run) -> None:
    with run.tenant_connect(run.names.database) as tenant_conn:
        run.extension_versions = provisioning.install_extension(tenant_conn)
        tenant_bootstrap.bootstrap_project(run.conn, tenant_conn, project_id=run.project_id)


def _record_storage_baseline(run: Run) -> None:
    """What this tenant weighs before it holds any customer data.

    Taken here because this is the last moment it is true: the project has its
    extension and its bootstrap and nothing else. Recorded per project rather
    than assumed from a constant, for the same reason ADR-015 records the
    extension version per project -- the figure moves with what the node's
    packages happen to provide.
    """
    with run.tenant_connect(run.names.database) as tenant_conn:
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            baseline = int(cur.fetchone()[0])
    db.execute(
        run.conn,
        "UPDATE projects SET storage_baseline_bytes = coalesce(storage_baseline_bytes, %s) "
        "WHERE id = %s",
        (baseline, run.project_id),
    )
    run.conn.commit()


def _apply_realtime_plan(run: Run) -> None:
    """Take Realtime away from a project whose plan no longer includes it.

    The mirror of `set_direct_sql_access` above, and only the removing half:
    enabling Realtime creates a role holding `REPLICATION` (ADR-031), which
    should be a decision rather than a side effect of a billing change. Removing
    it is not optional though -- the node is holding one of ten replication
    slots for this project, and a plan that says no while the node says yes is
    capacity spent on a capability nobody is paying for.

    Never fatal to provisioning. A tenant that is otherwise correct must not be
    left unprovisioned because a slot could not be dropped; the maintenance pass
    reports the slot either way, and the plan is re-applied on the next run.
    """
    from services.control_plane import realtime

    try:
        realtime.apply_plan(
            run.conn, run.admin_conn, project_id=run.project_id,
            tenant_connect=run.tenant_connect,
            supervisor=run.realtime_supervisor, key_ring=run.key_ring,
        )
    except Exception as exc:  # noqa: BLE001 - see above
        log.warning(
            "project %s: could not apply the plan's Realtime entitlement: %s",
            run.project_id, type(exc).__name__,
        )


def _validate(run: Run) -> None:
    """Always re-run. It is a check, it is cheap, and its whole purpose is to
    be the thing standing between a half-provisioned tenant and a customer."""
    _record_storage_baseline(run)
    _apply_realtime_plan(run)
    provisioning.verify_isolation(run.admin_conn, run.names)
    with run.tenant_connect(run.names.database) as tenant_conn:
        tenant_bootstrap.verify(tenant_conn)
        if not run.extension_versions:
            run.extension_versions = provisioning.installed_extensions(tenant_conn)

    db.execute(
        run.conn,
        """
        UPDATE projects
           SET status = 'PROVISIONED', extension_versions = %s, provisioned_at = now(),
               retry_after = NULL, failed_at = NULL, provisioning_failures = 0
         WHERE id = %s
        """,
        (psycopg.types.json.Jsonb(run.extension_versions), run.project_id),
    )
    run.conn.commit()


STEPS: tuple[Step, ...] = (
    Step("ROLES_CREATING", _roles_done, _create_roles),
    Step("DATABASE_CREATING", _database_done, _create_database),
    Step("BOOTSTRAPPING", _bootstrap_done, _bootstrap),
    Step("VALIDATING", lambda run: False, _validate),
)


# --------------------------------------------------------------------------
# Job bookkeeping
# --------------------------------------------------------------------------


def _sanitise(exc: BaseException, *, step: str) -> tuple[str, str]:
    """A code and a detail that are safe to store and to log.

    Driver text is never used. `CREATE ROLE ... PASSWORD 'literal'` appears
    verbatim in psycopg's error message, and provisioning_jobs is an operator-
    readable table -- a leak there is a leak into every dashboard and support
    transcript that reads it. SQLSTATE carries the diagnosis without the
    statement.
    """
    if isinstance(exc, psycopg.Error):
        sqlstate = getattr(exc, "sqlstate", None) or "unknown"
        return f"postgres.{sqlstate}", f"{type(exc).__name__} at step {step} (SQLSTATE {sqlstate})"
    if isinstance(exc, ProvisioningError):
        # Ours, and constructed from names rather than driver output.
        return "provisioning.failed", f"{exc} (step {step})"
    return "internal", f"{type(exc).__name__} at step {step}"


def _open_job(conn: psycopg.Connection, project_id: uuid.UUID, attempt: int, state: str) -> uuid.UUID:
    """Claim the right to provision this project.

    A partial unique index allows one open job per project, so two workers that
    pick up the same project race here and exactly one wins. The loser must not
    proceed: concurrent runs would both reset the role passwords, and whichever
    committed second would leave the other's stored credential pointing at a
    password that no longer works.
    """
    job_id = uuid.uuid4()
    try:
        db.execute(
            conn,
            "INSERT INTO provisioning_jobs (id, project_id, state, attempt) VALUES (%s, %s, %s, %s)",
            (job_id, project_id, state, attempt),
        )
        conn.commit()
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        raise ProvisioningError(
            f"a provisioning run is already in progress for project {project_id}"
        ) from None
    return job_id


def _advance_job(conn: psycopg.Connection, job_id: uuid.UUID, state: str) -> None:
    db.execute(
        conn,
        "UPDATE provisioning_jobs SET state = %s, updated_at = now() WHERE id = %s",
        (state, job_id),
    )
    conn.commit()


def _close_job(
    conn: psycopg.Connection,
    job_id: uuid.UUID,
    *,
    state: str,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    db.execute(
        conn,
        """
        UPDATE provisioning_jobs
           SET state = %s, error_code = %s, error_detail = %s,
               updated_at = now(), completed_at = now()
         WHERE id = %s
        """,
        (state, error_code, error_detail, job_id),
    )
    conn.commit()


def next_attempt(conn: psycopg.Connection, project_id: uuid.UUID) -> int:
    row = db.one(
        conn,
        "SELECT coalesce(max(attempt), 0) AS last FROM provisioning_jobs WHERE project_id = %s",
        (project_id,),
    )
    return row["last"] + 1


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


def provision(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    platform_owner: str,
    tenant_connect: Callable[[str], psycopg.Connection],
    plan_settings: dict[str, Any] | None = None,
    connection_limits: dict[str, int] | None = None,
    realtime_supervisor: Any | None = None,
) -> TenantNames:
    """Provision a project, or resume one that failed partway.

    Safe to call repeatedly. Steps whose work is already done on the node are
    skipped, so a completed project re-validates and returns rather than
    erroring, and a half-built one continues from where it stopped.
    """
    project = db.one(
        conn,
        "SELECT project_ref, status, provisioning_failures FROM projects WHERE id = %s",
        (project_id,),
    )
    if project is None:
        raise ProvisioningError("project does not exist")
    if project["status"] in ("DELETING", "DELETED"):
        raise ProvisioningError("project is being deleted")

    # Consecutive failures, not rows in provisioning_jobs. The job table is the
    # audit trail and grows forever; capping on it would mean a project that was
    # cleaned up -- everything reclaimed, back to REQUESTED, nothing left on any
    # node -- could still never be provisioned again.
    if project["provisioning_failures"] >= MAX_ATTEMPTS:
        raise RetriesExhausted(
            f"project has failed {project['provisioning_failures']} times in a row; "
            "provisioning will not retry automatically. Clean it up to reset."
        )

    attempt = next_attempt(conn, project_id)

    run = Run(
        conn=conn,
        admin_conn=admin_conn,
        project_id=project_id,
        project_ref=project["project_ref"],
        names=TenantNames.for_ref(project["project_ref"]),
        key_ring=key_ring,
        platform_owner=platform_owner,
        tenant_connect=tenant_connect,
        plan_settings=plan_settings or {},
        connection_limits=connection_limits or {},
        realtime_supervisor=realtime_supervisor,
    )

    job_id = _open_job(conn, project_id, attempt, STEPS[0].status)
    current = STEPS[0].status
    try:
        for step in STEPS:
            current = step.status
            if step.done(run):
                log.info("project %s: step %s already satisfied, skipping", project_id, step.status)
                continue
            _advance_job(conn, job_id, step.status)
            db.execute(conn, "UPDATE projects SET status = %s WHERE id = %s", (step.status, project_id))
            conn.commit()
            step.run(run)
    except BaseException as exc:
        code, detail = _sanitise(exc, step=current)
        _fail(conn, project_id, job_id, attempt=attempt, code=code, detail=detail)
        log.error("provisioning attempt %s failed for project %s: %s", attempt, project_id, code)
        if not isinstance(exc, Exception):
            # KeyboardInterrupt and SystemExit are recorded, because leaving the
            # job open would block every future attempt, but never reinterpreted
            # as a provisioning failure.
            raise
        if isinstance(exc, ProvisioningError):
            raise
        raise ProvisioningError(f"provisioning failed at {current} for project {project_id}") from None

    _close_job(conn, job_id, state="PROVISIONED")
    return run.names


def _fail(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    attempt: int,
    code: str,
    detail: str,
) -> None:
    """Record the failure and decide whether another attempt is allowed.

    RETRY_WAIT rather than FAILED while attempts remain, with a time attached:
    RETRY_WAIT without one is a project that gets retried immediately and fails
    immediately. FAILED is not a licence to delete anything -- the project may
    hold a database by now, and `cleanup` is the only thing that touches it.
    """
    conn.rollback()
    _close_job(conn, job_id, state="FAILED", error_code=code, error_detail=detail)

    failures = db.one(
        conn,
        "UPDATE projects SET provisioning_failures = provisioning_failures + 1 "
        "WHERE id = %s RETURNING provisioning_failures",
        (project_id,),
    )["provisioning_failures"]

    if failures < MAX_ATTEMPTS:
        backoff = RETRY_BACKOFF_SECONDS[min(failures - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        db.execute(
            conn,
            """
            UPDATE projects
               SET status = 'RETRY_WAIT', failed_at = now(),
                   retry_after = now() + make_interval(secs => %s)
             WHERE id = %s
            """,
            (backoff, project_id),
        )
    else:
        db.execute(
            conn,
            "UPDATE projects SET status = 'FAILED', failed_at = now(), retry_after = NULL WHERE id = %s",
            (project_id,),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------

# Schemas the platform creates itself. Anything outside these, and not owned by
# an extension, was put there by the tenant.
_PLATFORM_SCHEMAS = ("pg_catalog", "information_schema", "maludb_platform", "auth")

CLEANABLE_STATES = ("FAILED", "RETRY_WAIT")


@dataclass(frozen=True)
class CleanupReport:
    """What cleanup did, and — more usefully — what it refused to do."""

    project_id: uuid.UUID
    dropped_roles: tuple[str, ...] = ()
    dropped_database: str | None = None
    retained_database: str | None = None
    refused_because: str | None = None

    @property
    def dropped_anything(self) -> bool:
        return bool(self.dropped_roles) or self.dropped_database is not None


def customer_object_count(tenant_conn: psycopg.Connection) -> int:
    """Relations the tenant created, excluding everything the platform made.

    Deliberately counts rather than samples, and deliberately errs towards
    finding something: a false positive costs an operator a manual look, a
    false negative destroys a customer's data.
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
               AND n.nspname <> ALL(%s)
               AND n.nspname NOT LIKE 'pg\\_%%'
               AND NOT EXISTS (
                     SELECT 1 FROM pg_depend d
                      WHERE d.objid = c.oid AND d.deptype = 'e')
            """,
            (list(_PLATFORM_SCHEMAS),),
        )
        return cur.fetchone()[0]


def cleanup(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect: Callable[[str], psycopg.Connection],
    allow_database_drop: bool = False,
) -> CleanupReport:
    """Reclaim what a failed provisioning run left behind.

    The invariant from `specs/provisioning-state-machine.md`: once a project may
    contain customer data, cleanup must never drop the database merely to
    restore desired state. So this drops nothing by default, and even when told
    it may, it still refuses if the database was ever handed over or holds a
    single object the tenant created.

    Roles are only removed once the database is gone. Dropping them first would
    leave a database whose grants name roles that no longer exist and whose
    owner may have vanished — reachable by nobody, reclaimable by nothing.
    """
    project = db.one(
        conn,
        "SELECT project_ref, status, database_name, provisioned_at FROM projects WHERE id = %s",
        (project_id,),
    )
    if project is None:
        raise ProvisioningError("project does not exist")

    # Explicit state check, per AGENTS.md. A PROVISIONED or ACTIVE project is
    # not a cleanup candidate at any privilege level; deletion is its own path.
    if project["status"] not in CLEANABLE_STATES:
        raise ProvisioningError(
            f"refusing to clean up a project in {project['status']}; "
            f"cleanup handles {' and '.join(CLEANABLE_STATES)} only"
        )

    # A retry worker may be mid-flight on this project right now. Cleanup would
    # drop the database out from under it, and the run would carry on against
    # something that no longer exists.
    open_job = db.one(
        conn,
        "SELECT attempt, state FROM provisioning_jobs WHERE project_id = %s AND completed_at IS NULL",
        (project_id,),
    )
    if open_job is not None:
        raise ProvisioningError(
            f"a provisioning run is in progress for this project (attempt {open_job['attempt']}, "
            f"{open_job['state']}); wait for it to finish or fail before cleaning up"
        )

    names = TenantNames.for_ref(project["project_ref"])
    database = project["database_name"]

    # Defence in depth on the one operation here that destroys data. The name is
    # composed with sql.Identifier so this is not an injection concern -- it is
    # that a database_name which disagrees with the project's own ref means
    # dropping some other tenant's database. Nothing should ever write one, so
    # finding one is a reason to stop rather than to proceed carefully.
    if database is not None and database != names.database:
        raise ProvisioningError(
            f"recorded database {database} does not match the name this project's ref derives "
            f"({names.database}); refusing to drop anything"
        )

    if database is not None:
        if not allow_database_drop:
            return CleanupReport(
                project_id=project_id,
                retained_database=database,
                refused_because=(
                    "the project has a database; cleanup does not drop one unless explicitly "
                    "allowed to, and will still refuse if it holds tenant objects"
                ),
            )
        if project["provisioned_at"] is not None:
            return CleanupReport(
                project_id=project_id,
                retained_database=database,
                refused_because="the project reached PROVISIONED, so the database was handed over",
            )
        if provisioning.database_exists(admin_conn, database):
            with tenant_connect(database) as tenant_conn:
                objects = customer_object_count(tenant_conn)
            if objects:
                return CleanupReport(
                    project_id=project_id,
                    retained_database=database,
                    refused_because=f"the database holds {objects} tenant-created objects",
                )
            _drop_database(admin_conn, database)

        db.execute(conn, "UPDATE projects SET database_name = NULL WHERE id = %s", (project_id,))
        conn.commit()

    dropped = _drop_roles(admin_conn, names)

    # Completes the path `nodes.release_placement` points at: it refuses to
    # forget a project's node while a database exists, and directs callers here
    # to "drop the database before forgetting where it lives". Its status guard
    # cannot admit FAILED — slice 2 established that a FAILED project may hold a
    # database — so the release happens here, having just established the
    # stronger fact directly: the node holds nothing for this project.
    db.execute(
        conn,
        "UPDATE projects SET node_id = NULL, status = 'REQUESTED', retry_after = NULL, "
        "failed_at = NULL, provisioning_failures = 0 WHERE id = %s",
        (project_id,),
    )
    conn.commit()

    log.info("cleanup for project %s dropped database=%s roles=%s", project_id, database, len(dropped))
    return CleanupReport(
        project_id=project_id,
        dropped_roles=dropped,
        dropped_database=database,
    )


def _drop_database(admin_conn: psycopg.Connection, database: str) -> None:
    admin_conn.commit()
    previous = admin_conn.autocommit
    admin_conn.autocommit = True
    try:
        admin_conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database)
            )
        )
    finally:
        admin_conn.autocommit = previous


def _drop_roles(admin_conn: psycopg.Connection, names: TenantNames) -> tuple[str, ...]:
    """Per-tenant roles only. The shared names are cluster-wide and belong to
    every other tenant on the node."""
    dropped = []
    for role in (names.authenticator, names.auth, names.admin):
        if provisioning.role_exists(admin_conn, role):
            admin_conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
            )
            dropped.append(role)
    admin_conn.commit()
    return tuple(dropped)


def due_for_retry(conn: psycopg.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """Projects a retry worker should pick up. Ordered oldest failure first."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, project_ref, node_id, status, failed_at
              FROM projects
             WHERE status = 'RETRY_WAIT'
               AND (retry_after IS NULL OR retry_after <= now())
               AND deleted_at IS NULL
             ORDER BY failed_at
             LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()
