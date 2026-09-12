"""Upgrade `maludb_core` across a node's tenants: a canary, then batches (ADR-074).

Phase 12 slice 1. ADR-015 puts `maludb_core` in every tenant database, so an
upgrade is a per-database operation repeated across the fleet, and once Phase 12
lets customers depend on the extension's features it changes an API they use.
ADR-074 decision 5 makes it operator-run: nothing here runs on a timer, because
an automatic schema change to every customer database is the data-changing
control plane ADR-066 exists to prevent.

## A failed tenant stays on its previous version, and that is the design

Each tenant is upgraded inside **one transaction** that also verifies it:
`ALTER EXTENSION`, then the properties that matter, then `COMMIT` -- or
`ROLLBACK`. PostgreSQL has no general extension downgrade, so a check run *after*
committing could only report a broken tenant, not undo one. Inside the
transaction a failure leaves the tenant exactly where it was.

Verified, as outcomes rather than as statements that ran:

- the installed version is the target, and `maludb_core_version()` agrees;
- `tenant_bootstrap.verify` still passes. That is ADR-018's revoke -- a fleet-wide
  `ALTER EXTENSION` that adds a function to `public` is the exact case ADR-018
  names -- plus ADR-045's allowlist trigger;
- where the project has a **platform-owned** memory schema, `enable_memory_schema`
  is re-run and the data-model facades exist afterwards. Phase 12 slice 0 found
  that `ALTER EXTENSION` does not rebuild an enabled schema, so without this every
  upgrade would silently strand enabled projects on old facades.

## Why "platform-owned" is checked, and why it does not stop the run

The tenant admin holds `CREATE ON DATABASE` (bootstrap 010), so a customer can
create a schema named `maludb_memory` themselves. Running `enable_memory_schema`
into it would build superuser-owned `SECURITY DEFINER` functions inside a schema
the customer owns. So a schema is re-enabled only if a superuser owns it.

A customer-owned one is **skipped and noted, not failed.** Failing would let any
customer halt a node's upgrade -- including a security release -- by creating a
schema with the right name.

## Canary, batches, and stopping

The first run for a target version on a node upgrades **one** tenant and stops,
so an operator looks at a real upgraded tenant before any other is touched. Later
runs take up to `batch_size` more. The first failure stops the run; nothing after
it is attempted. Re-running is safe: tenants already at the target are recorded
as current and cost nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from services.control_plane import db, provisioning, restore, tenant_bootstrap
from services.control_plane.maludb import (
    DATAMODEL_FACADES,
    DATAMODEL_SINCE,
    MEMORY_SCHEMA,
    memory_schema_owner,
)
from services.control_plane.maludb import NODE_LOCK_NAMESPACE as _LOCK_NAMESPACE
from services.control_plane.maludb import version_tuple as _version

log = logging.getLogger("maludb.extension_upgrade")

EXTENSION = "maludb_core"


# Tenants in these states have a database that is not being changed by anything
# else. Anything mid-provisioning, mid-move or mid-deletion is left for a later
# run rather than raced.
UPGRADABLE_STATUSES = ("PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED")

# Node states in which an upgrade is refused outright. `maintenance` is allowed:
# it is the natural state to upgrade in.
REFUSED_NODE_STATUSES = ("draining", "unhealthy")

DEFAULT_BATCH_SIZE = 10

# One upgrade per node at a time, held exclusively on the control plane.
# Enablement takes the same key shared (`maludb.NODE_LOCK_NAMESPACE`), so it
# never runs while an upgrade is re-enabling schemas on the node.


class UpgradeError(RuntimeError):
    """A tenant could not be upgraded and verified, so it was rolled back."""


@dataclass
class TenantUpgrade:
    project_id: object
    project_ref: str
    database: str
    from_version: str | None = None
    to_version: str | None = None
    status: str = "pending"
    canary: bool = False
    detail: str | None = None
    memory_schema_version: str | None = None
    seconds: float = 0.0


@dataclass
class UpgradeOutcome:
    node: str
    target_version: str | None = None
    tenants: list[TenantUpgrade] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    canary_run: bool = False
    status: str = "pending"
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "complete"

    def count(self, status: str) -> int:
        return sum(1 for t in self.tenants if t.status == status)


# --------------------------------------------------------------------------
# Before touching anything


def preflight(conn: psycopg.Connection, *, node_name: str) -> list[str]:
    """Everything that should stop an upgrade before any tenant is connected to."""
    node = db.one(conn, "SELECT id, status FROM nodes WHERE name = %s", (node_name,))
    if node is None:
        return [f"no node named {node_name!r}"]

    problems: list[str] = []
    if node["status"] in REFUSED_NODE_STATUSES:
        problems.append(
            f"{node_name} is {node['status']}. A draining node is being emptied and an "
            "unhealthy one may be lost; upgrade after it is active or in maintenance"
        )
    moving = db.query(
        conn,
        "SELECT project_ref FROM projects WHERE node_id = %s AND status = 'MOVING' "
        "AND deleted_at IS NULL ORDER BY project_ref",
        (node["id"],),
    )
    if moving:
        problems.append(
            "a tenant move is in progress on this node ("
            + ", ".join(r["project_ref"] for r in moving)
            + "). A move copies a database and checks its schema posture against the "
            "source; changing the extension underneath it invalidates that check"
        )
    restoring = db.query(
        conn,
        "SELECT p.project_ref FROM tenant_restores r JOIN projects p ON p.id = r.project_id "
        "WHERE r.node_id = %s AND r.status = 'running' ORDER BY p.project_ref",
        (node["id"],),
    )
    if restoring:
        problems.append(
            "a restore is running on this node ("
            + ", ".join(r["project_ref"] for r in restoring)
            + "). Wait for it to finish"
        )
    return problems


def available_target(admin_conn: psycopg.Connection, requested: str | None) -> str:
    """The version to upgrade to: the one asked for, or the node's default.

    Each node installs whatever its OS packages provide, so the default is read
    from the node rather than assumed to match another's.
    """
    with admin_conn.cursor() as cur:
        if requested is None:
            cur.execute(
                "SELECT default_version FROM pg_available_extensions WHERE name = %s",
                (EXTENSION,),
            )
            row = cur.fetchone()
            if row is None:
                raise UpgradeError(f"{EXTENSION} is not available on this node at all")
            return row[0]
        cur.execute(
            "SELECT 1 FROM pg_available_extension_versions WHERE name = %s AND version = %s",
            (EXTENSION, requested),
        )
        if cur.fetchone() is None:
            raise UpgradeError(
                f"{EXTENSION} {requested} is not installed on this node's packages; "
                "install it on the node before asking the fleet to move to it"
            )
        return requested


def upgradable_projects(conn: psycopg.Connection, *, node_id: int) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT id, project_ref, database_name, status
          FROM projects
         WHERE node_id = %s AND deleted_at IS NULL
         ORDER BY project_ref
        """,
        (node_id,),
    )


# --------------------------------------------------------------------------
# One tenant


def installed_version(tenant_conn: psycopg.Connection) -> str | None:
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = %s", (EXTENSION,))
        row = cur.fetchone()
    return None if row is None else row[0]


def upgrade_tenant(tenant_conn: psycopg.Connection, *, target: str, tenant: TenantUpgrade) -> None:
    """Upgrade and verify one tenant in a single transaction, or roll it back.

    `tenant_conn` must not be in autocommit: the whole point is that the update
    and its verification commit together or not at all.
    """
    if tenant_conn.autocommit:
        raise UpgradeError("upgrade_tenant needs a transactional connection")

    tenant.from_version = installed_version(tenant_conn)
    tenant.to_version = target
    tenant_conn.rollback()
    if tenant.from_version is None:
        raise UpgradeError(f"{EXTENSION} is not installed in {tenant.database} (ADR-015)")
    if tenant.from_version == target:
        tenant.status = "current"
        return

    try:
        # The version is a value, but ALTER EXTENSION takes it as a literal.
        # `available_target` has already confirmed it names a real package
        # version, and it is quoted here rather than trusted.
        tenant_conn.execute(
            sql.SQL("ALTER EXTENSION {ext} UPDATE TO {version}").format(
                ext=sql.Identifier(EXTENSION),
                version=sql.Literal(target),
            )
        )

        now_installed = installed_version(tenant_conn)
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT maludb_core.maludb_core_version()")
            reported = cur.fetchone()[0]
        if now_installed != target or reported != target:
            raise UpgradeError(
                f"after ALTER EXTENSION the tenant reports {now_installed} installed and "
                f"maludb_core_version() = {reported}, not {target}"
            )

        # ADR-018 and ADR-045, checked inside the transaction so a failure here
        # is undone rather than merely reported.
        tenant_bootstrap.verify(tenant_conn)

        schema = memory_schema_owner(tenant_conn)
        if schema is not None:
            owned_by_superuser, owner = schema
            if not owned_by_superuser:
                tenant.detail = (
                    f"{MEMORY_SCHEMA} exists but is owned by {owner}, not the platform; "
                    "not re-enabled, because that would put superuser-owned SECURITY "
                    "DEFINER functions in a customer's schema"
                )
            else:
                with tenant_conn.cursor() as cur:
                    cur.execute(
                        "SELECT enabled_version FROM maludb_core.enable_memory_schema(%s)",
                        (MEMORY_SCHEMA,),
                    )
                    tenant.memory_schema_version = cur.fetchone()[0]
                    cur.execute(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                        "ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.proname = ANY(%s)",
                        (MEMORY_SCHEMA, list(DATAMODEL_FACADES)),
                    )
                    present = cur.fetchone()[0]
                if tenant.memory_schema_version != target:
                    raise UpgradeError(
                        f"enable_memory_schema reported {tenant.memory_schema_version}, "
                        f"not {target}"
                    )
                if _version(target) >= DATAMODEL_SINCE and present < len(DATAMODEL_FACADES):
                    raise UpgradeError(
                        f"{MEMORY_SCHEMA} was re-enabled but has {present} of "
                        f"{len(DATAMODEL_FACADES)} data-model facades"
                    )

        tenant_conn.commit()
        tenant.status = "upgraded"
    except Exception:
        tenant_conn.rollback()
        raise


# --------------------------------------------------------------------------
# A node


def _record(conn: psycopg.Connection, *, node_id: int, tenant: TenantUpgrade) -> None:
    db.execute(
        conn,
        """
        INSERT INTO extension_upgrades
            (project_id, node_id, extension, from_version, to_version, status, canary,
             detail, memory_schema_version, completed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        """,
        (tenant.project_id, node_id, EXTENSION, tenant.from_version, tenant.to_version,
         tenant.status, tenant.canary, tenant.detail, tenant.memory_schema_version),
    )


def _already_at(admin_conn: psycopg.Connection, database: str, target: str, open_) -> bool:
    """Whether a tenant already has the target version. Reads only."""
    tenant_conn = open_(admin_conn, database)
    try:
        return installed_version(tenant_conn) == target
    finally:
        tenant_conn.close()


def _canary_done(conn: psycopg.Connection, *, node_id: int, target: str) -> bool:
    """Whether some tenant on this node already took this version and verified."""
    return db.one(
        conn,
        "SELECT 1 AS done FROM extension_upgrades WHERE node_id = %s AND to_version = %s "
        "AND status = 'upgraded' LIMIT 1",
        (node_id, target),
    ) is not None


def upgrade_node(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    node_name: str,
    to_version: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    connect=None,
) -> UpgradeOutcome:
    """Run one canary or one batch across a node's tenants, stopping at a failure.

    `conn` is the control plane; `admin_conn` a superuser connection to the node.
    `connect(admin_conn, database)` opens a transactional tenant connection, and
    exists so a test can drive this without a second cluster.
    """
    outcome = UpgradeOutcome(node=node_name)
    open_ = connect or restore._connect_to  # noqa: SLF001
    if batch_size < 1:
        outcome.status, outcome.error = "refused", "batch size must be at least 1"
        return outcome

    problems = preflight(conn, node_name=node_name)
    if problems:
        outcome.status, outcome.error = "refused", "; ".join(problems)
        return outcome
    node = db.one(conn, "SELECT id FROM nodes WHERE name = %s", (node_name,))

    got_lock = db.one(
        conn, "SELECT pg_try_advisory_lock(%s, %s) AS ok", (_LOCK_NAMESPACE, node["id"])
    )["ok"]
    if not got_lock:
        outcome.status = "refused"
        outcome.error = f"another extension upgrade is already running on {node_name}"
        return outcome

    try:
        try:
            outcome.target_version = available_target(admin_conn, to_version)
        except UpgradeError as exc:
            outcome.status, outcome.error = "refused", str(exc)
            return outcome

        canary_run = not _canary_done(conn, node_id=node["id"], target=outcome.target_version)
        outcome.canary_run = canary_run
        allowance = 1 if canary_run else batch_size
        upgraded = 0

        projects = upgradable_projects(conn, node_id=node["id"])
        for index, project in enumerate(projects):
            tenant = TenantUpgrade(project["id"], project["project_ref"], project["database_name"])

            if project["status"] not in UPGRADABLE_STATUSES:
                tenant.status = "skipped"
                tenant.to_version = outcome.target_version
                tenant.detail = f"project is {project['status']}; upgrade it once that settles"
                outcome.tenants.append(tenant)
                _record(conn, node_id=node["id"], tenant=tenant)
                conn.commit()
                continue

            if upgraded >= allowance:
                # The allowance is spent, but "left" must mean work remains. On a
                # node where most tenants already took this version, stopping here
                # would report nearly all of them as outstanding. So look -- read
                # only, no transaction held -- and record what is already current.
                if _already_at(admin_conn, project["database_name"], outcome.target_version, open_):
                    tenant.status = "current"
                    tenant.from_version = tenant.to_version = outcome.target_version
                    outcome.tenants.append(tenant)
                    _record(conn, node_id=node["id"], tenant=tenant)
                    conn.commit()
                else:
                    outcome.left.append(project["project_ref"])
                continue

            started = time.monotonic()
            tenant_conn = None
            try:
                tenant_conn = open_(admin_conn, project["database_name"])
                tenant_conn.autocommit = False
                tenant.canary = canary_run
                upgrade_tenant(tenant_conn, target=outcome.target_version, tenant=tenant)
                if tenant.status == "upgraded":
                    upgraded += 1
                    if tenant.memory_schema_version is not None:
                        db.execute(
                            conn,
                            "UPDATE projects SET maludb_memory_schema_version = %s WHERE id = %s",
                            (tenant.memory_schema_version, project["id"]),
                        )
                    # What the tenant has now, read back rather than assumed.
                    versions = provisioning.installed_extensions(tenant_conn)
                    tenant_conn.rollback()
                    db.execute(
                        conn,
                        "UPDATE projects SET extension_versions = %s WHERE id = %s",
                        (Jsonb(versions), project["id"]),
                    )
                else:
                    tenant.canary = False
            except Exception as exc:  # noqa: BLE001 - reported and stops the run
                tenant.status = "failed"
                tenant.detail = f"{type(exc).__name__}: {exc}"
                outcome.stopped_at = project["project_ref"]
                outcome.left = [p["project_ref"] for p in projects[index + 1:]
                                if p["status"] in UPGRADABLE_STATUSES]
                log.warning("extension upgrade of %s stopped at %s: %s",
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
                    "canary verified. Inspect it, then re-run to upgrade in batches of "
                    f"{batch_size}"
                )
            elif outcome.left:
                outcome.notes.append(f"{len(outcome.left)} tenant(s) left; re-run to continue")
        return outcome
    finally:
        db.one(conn, "SELECT pg_advisory_unlock(%s, %s) AS ok", (_LOCK_NAMESPACE, node["id"]))
        conn.commit()


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MEMORY_SCHEMA",
    "TenantUpgrade",
    "UpgradeError",
    "UpgradeOutcome",
    "available_target",
    "preflight",
    "upgrade_node",
    "upgrade_tenant",
]
