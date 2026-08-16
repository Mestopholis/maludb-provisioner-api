"""Storage accounting, and what happens when a project exceeds its quota.

Three states, and the transitions between them are the whole module: `ok`,
`warning`, and `restricted`. A project reaches `restricted` by exceeding its
plan's storage entitlement, and leaves it by getting back under.

**Restriction revokes `INSERT` and `UPDATE` from `anon` and `authenticated`, and
nothing else.** That combination is deliberate:

- `SELECT` stays, so a customer over quota can still read and export their data.
  Cutting off reads punishes them for a condition they need to see in order to
  fix.
- `DELETE` and `TRUNCATE` stay, because they are how a customer gets back under
  the limit. Revoking them would make the restriction a trap that only support
  can unlock.
- `service_role` is untouched. It is reachable only from the project's own
  backend, and a customer's cleanup job is the most likely thing to use it.

The alternative considered was `default_transaction_read_only` on the database,
which is simpler and complete -- and also stops the tenant's own migrations and
anything holding direct SQL, which turns a storage problem into an outage.

Growth continues to be possible through direct SQL on paid plans. That is
accepted: ADR-009 makes governance layered precisely because no single layer is
sufficient, and the layer that bounds a paid tenant's disk is node capacity
management rather than a grant.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from services.control_plane import db, entitlements

log = logging.getLogger(__name__)

# Fallback only, for projects provisioned before the baseline was recorded per
# project. Quotas are net of the baseline: a 100 MB quota that counts the
# extension is a 77 MB quota with 100 printed on it (ADR-015).
#
# Deliberately *below* the 23,639,731 bytes measured on the development node.
# Under-subtracting over-reports a customer's usage a little; over-subtracting
# hides it, and a baseline read as larger than reality floors every project to
# zero usage -- which is exactly what a first draft of this module did, with the
# constant set to 23 MiB against a real baseline of 22.5.
BASELINE_BYTES = 20 * 1024 * 1024

# Where a project is told it is running out, while it can still do something
# about it. Not a plan entitlement -- it is the same proportion for everyone,
# and a plan that set it to 100% would make the warning arrive with the
# restriction.
WARNING_FRACTION = 0.8

OK = "ok"
WARNING = "warning"
RESTRICTED = "restricted"

# What a restricted project loses. INSERT and UPDATE grow a database; the rest
# are how a customer reads, exports, and shrinks it.
RESTRICTED_PRIVILEGES = ("INSERT", "UPDATE")

# Restriction applies to the roles a customer's *application* reaches the
# database through. service_role is left alone -- see the module docstring.
RESTRICTED_ROLES = ("anon", "authenticated")


class StorageError(RuntimeError):
    """Storage could not be measured or enforced."""


@dataclass(frozen=True)
class Usage:
    """What a project is using, and what that means."""

    gross_bytes: int
    billable_bytes: int
    quota_bytes: int
    state: str

    @property
    def fraction(self) -> float:
        if self.quota_bytes <= 0:
            return 0.0
        return self.billable_bytes / self.quota_bytes


def measure(admin_conn: psycopg.Connection, database: str) -> int:
    """The database's size on disk, as PostgreSQL reports it."""
    # Named, not positional: callers pass whichever connection they already
    # hold, and the provisioning ones use a dict row factory.
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_database_size(%s) AS bytes", (database,))
        row = cur.fetchone()
    if row is None or row["bytes"] is None:
        raise StorageError(f"no such database {database}")
    return int(row["bytes"])


def classify(gross_bytes: int, quota_bytes: int, *, baseline_bytes: int | None = None) -> Usage:
    """Turn a raw size into a state.

    Net of the baseline, and floored at zero: a freshly provisioned project
    weighs about the baseline, and reporting it as using a negative amount would
    be worse than useless on a dashboard.
    """
    baseline = BASELINE_BYTES if baseline_bytes is None else baseline_bytes
    billable = max(0, gross_bytes - baseline)
    if quota_bytes <= 0:
        state = OK
    elif billable >= quota_bytes:
        state = RESTRICTED
    elif billable >= quota_bytes * WARNING_FRACTION:
        state = WARNING
    else:
        state = OK
    return Usage(
        gross_bytes=gross_bytes,
        billable_bytes=billable,
        quota_bytes=quota_bytes,
        state=state,
    )


def _apply(tenant_conn: psycopg.Connection, *, revoke: bool) -> None:
    """Revoke or restore write privileges in the tenant's exposed schema.

    Two halves, and missing either leaves a hole. Existing tables are the
    obvious one. Default privileges are the one that bites: bootstrap 004 grants
    `ALL` on future tables, so a restricted project that creates a table would
    otherwise get a fully writable one and walk straight back out of its
    restriction.

    Applied for every role that creates tables in the tenant -- the platform
    owner during bootstrap, and the project's own admin afterwards -- because
    `ALTER DEFAULT PRIVILEGES` only affects objects created by the role it names.
    """
    verb = sql.SQL("REVOKE") if revoke else sql.SQL("GRANT")
    direction = sql.SQL("FROM") if revoke else sql.SQL("TO")
    privileges = sql.SQL(", ").join(sql.SQL(p) for p in RESTRICTED_PRIVILEGES)
    roles = sql.SQL(", ").join(sql.Identifier(r) for r in RESTRICTED_ROLES)

    tenant_conn.execute(
        sql.SQL("{verb} {privs} ON ALL TABLES IN SCHEMA public {direction} {roles}").format(
            verb=verb, privs=privileges, direction=direction, roles=roles
        )
    )

    with tenant_conn.cursor() as cur:
        # Every role that owns or could create tables here. Discovered rather
        # than assumed: the set differs between a tenant provisioned by Phase 02
        # and one an operator has touched since.
        cur.execute(
            """
            SELECT DISTINCT pg_get_userbyid(c.relowner) AS owner
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            UNION
            SELECT current_database() || '_admin'
            """
        )
        owners = [row[0] for row in cur.fetchall()]

    for owner in owners:
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (owner,))
            if cur.fetchone() is None:
                continue
        tenant_conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                "{verb} {privs} ON TABLES {direction} {roles}"
            ).format(
                owner=sql.Identifier(owner), verb=verb, privs=privileges,
                direction=direction, roles=roles,
            )
        )
    tenant_conn.commit()


def restrict(tenant_conn: psycopg.Connection) -> None:
    """Stop a project's application writing. Reads, deletes and truncates stay."""
    _apply(tenant_conn, revoke=True)


def release(tenant_conn: psycopg.Connection) -> None:
    """Give the writes back."""
    _apply(tenant_conn, revoke=False)


def _audit(conn: psycopg.Connection, project_id: uuid.UUID, event_type: str, usage: Usage) -> None:
    """A restriction is customer-visible and feels irreversible in the moment.

    `docs/ACCOUNTS.md` requires actions to be attributable and visible to the
    customer; this one has no human actor, so it is recorded as `system` with
    the numbers that produced the decision -- enough to answer "why was I cut
    off" without anyone re-deriving it.
    """
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
        "VALUES (%s, 'system', %s, %s)",
        (
            project_id,
            event_type,
            psycopg.types.json.Jsonb(
                {
                    "gross_bytes": usage.gross_bytes,
                    "billable_bytes": usage.billable_bytes,
                    "quota_bytes": usage.quota_bytes,
                    "fraction": round(usage.fraction, 4),
                }
            ),
        ),
    )


def evaluate(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> Usage:
    """Measure one project, record it, and enforce the result.

    Idempotent: a project already restricted and still over quota is left alone
    rather than re-revoked, so a measurement pass that runs every few minutes
    does not write an audit event every few minutes.
    """
    project = db.one(
        conn,
        "SELECT project_ref, database_name, storage_state, storage_baseline_bytes FROM projects "
        " WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None:
        raise StorageError("project does not exist")
    if project["database_name"] is None:
        raise StorageError("project has no database to measure")

    quota = entitlements.for_project(conn, project_id).database_storage_bytes
    usage = classify(
        measure(admin_conn, project["database_name"]),
        quota,
        baseline_bytes=project["storage_baseline_bytes"],
    )

    db.execute(
        conn,
        "UPDATE projects SET database_bytes = %s, database_measured_at = now(), "
        "storage_state = %s WHERE id = %s",
        (usage.gross_bytes, usage.state, project_id),
    )

    previous = project["storage_state"]
    if usage.state != previous:
        if usage.state == RESTRICTED:
            with tenant_connect(project["database_name"]) as tenant_conn:
                restrict(tenant_conn)
            db.execute(
                conn,
                "UPDATE projects SET storage_restricted_at = now() WHERE id = %s",
                (project_id,),
            )
            _audit(conn, project_id, "storage.restricted", usage)
            log.warning("project %s restricted: over storage quota", project["project_ref"])
        elif previous == RESTRICTED:
            with tenant_connect(project["database_name"]) as tenant_conn:
                release(tenant_conn)
            db.execute(
                conn,
                "UPDATE projects SET storage_restricted_at = NULL WHERE id = %s",
                (project_id,),
            )
            _audit(conn, project_id, "storage.released", usage)
            log.info("project %s released: back under storage quota", project["project_ref"])
        elif usage.state == WARNING:
            _audit(conn, project_id, "storage.warning", usage)

    conn.commit()
    return usage


def due_for_measurement(conn: psycopg.Connection, *, limit: int = 50) -> list[dict]:
    """Projects a measurement pass should look at, least recently measured first."""
    return db.query(
        conn,
        """
        SELECT id, project_ref, node_id, database_name, database_measured_at
          FROM projects
         WHERE database_name IS NOT NULL AND deleted_at IS NULL
           AND status IN ('PROVISIONED', 'ACTIVE')
         ORDER BY database_measured_at NULLS FIRST
         LIMIT %s
        """,
        (limit,),
    )
