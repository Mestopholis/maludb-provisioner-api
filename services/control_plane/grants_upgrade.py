"""Bring a node's existing tenants to ADR-076's extension-function posture.

Grants slice 2. Bootstrap 014 gives the customer roles `EXECUTE` on extension
functions, which is only safe once the tenant's PostgREST refuses those functions
as RPC; before that it is ADR-018's finding -- `anon` calling `/rpc/gen_salt` --
reopened. New tenants get both at provisioning, before any worker exists. A
serving tenant has a worker already, and this is how it catches up.

## Why the check goes live through the database

PostgREST workers are node-local: the gateway starts them and renders their
files (`plans/active/deployment-topology.md`). This runs on the control plane,
which can neither rewrite those files nor reach a worker's loopback port. What it
can reach is the tenant database, and PostgREST reads configuration from there
too (`db-config = true`). So, per tenant:

1. **Bootstrap up to 013**, which creates the check and records a version from
   which the gateway's next render names it in the file as well.
2. **`pgrst.db_pre_request` on the tenant's authenticator, in its database**,
   naming the check, then `NOTIFY pgrst, 'reload config'`. Measured on PostgREST
   14.17 with a file that did not name the check: a running worker refused
   `gen_salt` 0.18 s after the notify; one whose listener had been killed
   reconnected and reloaded config on connect, live after 1.1 s; a fresh start
   read it immediately.
3. **Evidence, from `pg_stat_activity`**, after waiting past those figures: either
   no PostgREST is connected to the tenant -- it will read the setting when it
   starts -- or it holds a `LISTEN "pgrst"` connection, which is how the reload
   reaches it. Connections without a listener are a worker the reload cannot be
   shown to reach, and the run stops there with nothing granted.
4. **Bootstrap 014 and `verify`, in one transaction**, so a verification failure
   leaves the tenant without the grants rather than reporting them.

The evidence is weaker than an HTTP 403 from the worker itself and stronger than
assuming: it is what the control plane can observe, chosen by the repository
owner over a node-local run.

## Canary, batches, stopping

The shape of `extension_upgrade`, deliberately, and its node lock: one tenant on a
node's first run, then batches; the first failure stops the run with that tenant
rolled back; re-running is safe, and tenants that already have 014 are recorded
as current.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from services.control_plane import db, extension_upgrade, provisioning, restore, tenant_bootstrap
from services.control_plane.maludb import NODE_LOCK_NAMESPACE as _LOCK_NAMESPACE

log = logging.getLogger("maludb.grants_upgrade")

GRANTS_VERSION = "014_extension_function_grants"
CHECK_VERSION = "013_extension_rpc_check"

# Past both measured figures -- 0.18 s for a notify, 1.1 s for a listener that
# had to reconnect -- with room for a loaded node.
RELOAD_SECONDS = 3.0

DEFAULT_BATCH_SIZE = extension_upgrade.DEFAULT_BATCH_SIZE


class GrantsError(RuntimeError):
    """A tenant could not be shown safe to grant, or its grants did not verify."""


@dataclass
class TenantGrant:
    project_id: object
    project_ref: str
    status: str = "pending"
    canary: bool = False
    worker: str | None = None
    detail: str | None = None
    seconds: float = 0.0


@dataclass
class GrantsOutcome:
    node: str
    tenants: list[TenantGrant] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    canary_run: bool = False
    status: str = "pending"
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for t in self.tenants if t.status == status)


def worker_connections(admin_conn: psycopg.Connection, names: provisioning.TenantNames) -> tuple[int, int]:
    """(connections, listeners) the tenant's authenticator holds to its database.

    By role rather than by application name: nothing but PostgREST logs in as
    the authenticator, and counting every such connection is the conservative
    reading if something else ever did.
    """
    row = admin_conn.execute(
        """
        SELECT count(*) AS connections,
               count(*) FILTER (WHERE query ILIKE 'LISTEN %%') AS listeners
          FROM pg_stat_activity
         WHERE datname = %s AND usename = %s
        """,
        (names.database, names.authenticator),
    ).fetchone()
    return row[0], row[1]


def set_check_in_database(admin_conn: psycopg.Connection, names: provisioning.TenantNames) -> None:
    admin_conn.execute(
        sql.SQL("ALTER ROLE {role} IN DATABASE {database} SET pgrst.db_pre_request = {check}").format(
            role=sql.Identifier(names.authenticator),
            database=sql.Identifier(names.database),
            check=sql.Literal(tenant_bootstrap.RPC_CHECK_FUNCTION),
        )
    )


def grant_tenant(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    tenant_conn: psycopg.Connection,
    *,
    project_id,
    names: provisioning.TenantNames,
    tenant: TenantGrant,
    reload_seconds: float = RELOAD_SECONDS,
) -> None:
    """Steps 1-4 of the module docstring for one tenant. Raises on anything unsafe."""
    if GRANTS_VERSION in tenant_bootstrap.applied(tenant_conn):
        tenant.status = "current"
        return

    tenant_bootstrap.apply(tenant_conn)  # up to, never including, the held grants
    reached = tenant_bootstrap.applied_version(tenant_conn)
    db.execute(conn, "UPDATE projects SET bootstrap_version = %s WHERE id = %s", (reached, project_id))
    conn.commit()
    if CHECK_VERSION not in tenant_bootstrap.applied(tenant_conn):
        raise GrantsError(f"{CHECK_VERSION} is not applied; the check does not exist to point at")

    set_check_in_database(admin_conn, names)
    tenant_conn.execute("NOTIFY pgrst, 'reload config'")
    tenant_conn.commit()
    time.sleep(reload_seconds)

    connections, listeners = worker_connections(admin_conn, names)
    if connections == 0:
        tenant.worker = "not running; reads the setting when it starts"
    elif listeners:
        tenant.worker = f"{connections} connection(s), listening for reloads"
    else:
        tenant.worker = f"{connections} connection(s), no listener"
        raise GrantsError(
            "the tenant's PostgREST is connected without a LISTEN connection, so the config "
            "reload cannot be shown to have reached it; nothing was granted. Restart the "
            "worker on its node, then re-run"
        )

    tenant_bootstrap.apply_held(tenant_conn)
    db.execute(
        conn,
        "UPDATE projects SET bootstrap_version = %s WHERE id = %s",
        (tenant_bootstrap.applied_version(tenant_conn), project_id),
    )
    tenant.status = "upgraded"


def _record(conn: psycopg.Connection, *, node_id: int, tenant: TenantGrant) -> None:
    db.execute(
        conn,
        """
        INSERT INTO extension_grant_upgrades
            (project_id, node_id, status, canary, worker, detail, completed_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        """,
        (tenant.project_id, node_id, tenant.status, tenant.canary, tenant.worker, tenant.detail),
    )


def _canary_done(conn: psycopg.Connection, *, node_id: int) -> bool:
    return db.one(
        conn,
        "SELECT 1 AS done FROM extension_grant_upgrades WHERE node_id = %s AND status = 'upgraded' LIMIT 1",
        (node_id,),
    ) is not None


def _has_grants(admin_conn, database: str, open_) -> bool:
    tenant_conn = open_(admin_conn, database)
    try:
        return GRANTS_VERSION in tenant_bootstrap.applied(tenant_conn)
    finally:
        tenant_conn.close()


def upgrade_node(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    node_name: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    connect=None,
    reload_seconds: float = RELOAD_SECONDS,
) -> GrantsOutcome:
    """One canary or one batch across a node's tenants, stopping at a failure.

    `conn` is the control plane, `admin_conn` an autocommit superuser connection to
    the node, and `connect(admin_conn, database)` opens a tenant connection.
    """
    outcome = GrantsOutcome(node=node_name)
    open_ = connect or restore._connect_to  # noqa: SLF001
    if batch_size < 1:
        outcome.status, outcome.error = "refused", "batch size must be at least 1"
        return outcome

    problems = extension_upgrade.preflight(conn, node_name=node_name)
    if problems:
        outcome.status, outcome.error = "refused", "; ".join(problems)
        return outcome
    node = db.one(conn, "SELECT id FROM nodes WHERE name = %s", (node_name,))

    # The extension upgrade's lock. The hardening trigger that 014 replaces runs
    # on every ALTER EXTENSION, so the two must never interleave on a node.
    if not db.one(conn, "SELECT pg_try_advisory_lock(%s, %s) AS ok",
                  (_LOCK_NAMESPACE, node["id"]))["ok"]:
        outcome.status = "refused"
        outcome.error = f"an extension upgrade or grants run is already running on {node_name}"
        return outcome

    try:
        canary_run = not _canary_done(conn, node_id=node["id"])
        outcome.canary_run = canary_run
        allowance = 1 if canary_run else batch_size
        upgraded = 0

        projects = extension_upgrade.upgradable_projects(conn, node_id=node["id"])
        for index, project in enumerate(projects):
            tenant = TenantGrant(project["id"], project["project_ref"])
            names = provisioning.TenantNames.for_ref(project["project_ref"])

            if project["status"] not in extension_upgrade.UPGRADABLE_STATUSES:
                tenant.status = "skipped"
                tenant.detail = f"project is {project['status']}; run again once that settles"
                outcome.tenants.append(tenant)
                _record(conn, node_id=node["id"], tenant=tenant)
                conn.commit()
                continue

            if upgraded >= allowance:
                if _has_grants(admin_conn, names.database, open_):
                    tenant.status = "current"
                    outcome.tenants.append(tenant)
                    _record(conn, node_id=node["id"], tenant=tenant)
                    conn.commit()
                else:
                    outcome.left.append(project["project_ref"])
                continue

            started = time.monotonic()
            tenant_conn = None
            try:
                tenant_conn = open_(admin_conn, names.database)
                tenant_conn.autocommit = False
                tenant.canary = canary_run
                grant_tenant(conn, admin_conn, tenant_conn, project_id=project["id"], names=names,
                             tenant=tenant, reload_seconds=reload_seconds)
                if tenant.status == "upgraded":
                    upgraded += 1
                else:
                    tenant.canary = False
            except Exception as exc:  # noqa: BLE001 - reported and stops the run
                if tenant_conn is not None:
                    tenant_conn.rollback()
                conn.rollback()
                tenant.status = "failed"
                tenant.detail = f"{type(exc).__name__}: {exc}"
                outcome.stopped_at = project["project_ref"]
                outcome.left = [p["project_ref"] for p in projects[index + 1:]
                                if p["status"] in extension_upgrade.UPGRADABLE_STATUSES]
                log.warning("grants run on %s stopped at %s: %s",
                            node_name, project["project_ref"], tenant.detail)
            finally:
                if tenant_conn is not None:
                    tenant_conn.close()
                tenant.seconds = time.monotonic() - started

            outcome.tenants.append(tenant)
            _record(conn, node_id=node["id"], tenant=tenant)
            conn.commit()
            if tenant.status == "failed":
                break

        if outcome.stopped_at:
            outcome.status = "stopped"
        else:
            outcome.status = "complete"
            if canary_run and upgraded == 1 and outcome.left:
                outcome.notes.append(
                    "canary granted. Check its Data API refuses /rpc/gen_random_uuid, then "
                    f"re-run to grant in batches of {batch_size}"
                )
            elif outcome.left:
                outcome.notes.append(f"{len(outcome.left)} tenant(s) left; re-run to continue")
        return outcome
    finally:
        db.one(conn, "SELECT pg_advisory_unlock(%s, %s) AS ok", (_LOCK_NAMESPACE, node["id"]))
        conn.commit()
