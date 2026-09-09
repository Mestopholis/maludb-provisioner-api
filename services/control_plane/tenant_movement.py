"""Move one tenant database to another node.

Phase 11 slice 7, implementing ADR-066. A move is an operator command with a
named project and target node. Reports may say a move is needed; they never
start one.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from psycopg import sql

from services.control_plane import crypto, db, entitlements, models, nodes, provisioning, restore

log = logging.getLogger("maludb.tenant_movement")


class MovementError(RuntimeError):
    """A tenant move could not be performed, or could not be trusted."""


MOVABLE_STATUSES = ("PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED")
STOPPED = "STOPPED"


@dataclass(frozen=True)
class MoveTarget:
    move_id: int
    project_id: uuid.UUID
    project_ref: str
    source_node_id: int
    source_node_name: str
    target_node_id: int
    target_node_name: str
    database: str
    original_status: str


@dataclass
class MoveOutcome:
    move_id: int
    project_ref: str
    source_node_id: int
    target_node_id: int
    database: str
    original_status: str
    status: str = "running"
    ownership: restore.OwnershipReport | None = None
    dump_seconds: float = 0.0
    load_seconds: float = 0.0
    total_seconds: float = 0.0
    dump_bytes: int = 0
    source_cleaned: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def _project_for_move(
    conn: psycopg.Connection,
    *,
    project_ref: str,
    source_node: str | None,
    target_node: str,
) -> dict:
    if not models.is_valid_project_ref(project_ref):
        raise MovementError(f"invalid project ref {project_ref!r}")
    row = db.one(
        conn,
        """
        SELECT p.id, p.project_ref, p.status, p.node_id, p.database_name,
               p.worker_state, p.auth_worker_state, p.realtime_worker_state,
               p.realtime_enabled, pl.code AS plan_code, pl.config_json,
               src.name AS source_node_name, src.node_pool AS source_pool,
               tgt.id AS target_node_id, tgt.name AS target_node_name,
               tgt.status AS target_status, tgt.node_pool AS target_pool,
               tgt.last_health_at AS target_last_health_at
          FROM projects p
          JOIN nodes src ON src.id = p.node_id
          JOIN nodes tgt ON tgt.name = %s
          LEFT JOIN plans pl ON pl.id = p.plan_id
         WHERE p.project_ref = %s AND p.deleted_at IS NULL
        """,
        (target_node, project_ref),
    )
    if row is None:
        raise MovementError(f"no project {project_ref!r} with target node {target_node!r}")
    if source_node is not None and row["source_node_name"] != source_node:
        raise MovementError(
            f"{project_ref} is on {row['source_node_name']}, not {source_node}. "
            "The source node is an explicit guard on the operation"
        )
    expected_database = models.database_name_for(project_ref)
    if row["database_name"] != expected_database:
        raise MovementError(
            f"recorded database {row['database_name']} does not match {expected_database}; "
            "refusing to copy or drop anything"
        )
    if row["status"] not in MOVABLE_STATUSES:
        raise MovementError(
            f"refusing to move a project in {row['status']}; movable states are "
            + ", ".join(MOVABLE_STATUSES)
        )
    if row["node_id"] == row["target_node_id"]:
        raise MovementError(f"{project_ref} is already on {target_node}")
    if row["target_status"] != nodes.PLACEABLE_STATUS:
        raise MovementError(
            f"target node {target_node} is {row['target_status']}, not {nodes.PLACEABLE_STATUS}"
        )
    if (
        row["target_last_health_at"] is None
        or row["target_last_health_at"] <= datetime.now(UTC) - nodes.HEALTH_STALE_AFTER
    ):
        raise MovementError(
            f"target node {target_node} has no fresh health report; record health before moving"
        )
    if row["realtime_enabled"]:
        raise MovementError(
            "Realtime-enabled projects carry node-local replication and metadata state; "
            "disable or recover Realtime before moving this project"
        )
    running = [
        name
        for name in ("worker_state", "auth_worker_state", "realtime_worker_state")
        if row[name] != STOPPED
    ]
    if running:
        raise MovementError(
            "stop project workers before moving; still not stopped: " + ", ".join(running)
        )

    allowed = entitlements.resolve(row["plan_code"], row["config_json"])
    if row["target_pool"] != allowed.node_pool:
        raise MovementError(
            f"target node {target_node} is in pool {row['target_pool']!r}, but plan "
            f"{allowed.plan_code} is entitled to {allowed.node_pool!r}"
        )
    capacity = nodes.capacity_of(conn, row["target_node_id"])
    reason = capacity.rejection_reason()
    if reason:
        raise MovementError(f"target node {target_node} cannot accept the project: {reason}")
    return row


def begin(
    conn: psycopg.Connection,
    *,
    project_ref: str,
    source_node: str | None,
    target_node: str,
) -> MoveTarget:
    """Record the move and put the project in a non-serving status."""
    with conn.transaction():
        row = _project_for_move(
            conn, project_ref=project_ref, source_node=source_node, target_node=target_node
        )
        locked = db.one(conn, "SELECT id FROM projects WHERE id = %s FOR UPDATE", (row["id"],))
        if locked is None:
            raise MovementError(f"project {project_ref} disappeared during move setup")
        open_move = db.one(
            conn,
            "SELECT id FROM tenant_moves WHERE project_id = %s AND status = 'running'",
            (row["id"],),
        )
        if open_move is not None:
            raise MovementError(f"project {project_ref} already has move {open_move['id']} running")
        db.execute(conn, "UPDATE projects SET status = 'MOVING' WHERE id = %s", (row["id"],))
        move_id = _start(
            conn,
            project_id=row["id"],
            source_node_id=row["node_id"],
            target_node_id=row["target_node_id"],
            database=row["database_name"],
            original_status=row["status"],
        )
    return MoveTarget(
        move_id=move_id,
        project_id=row["id"],
        project_ref=row["project_ref"],
        source_node_id=row["node_id"],
        source_node_name=row["source_node_name"],
        target_node_id=row["target_node_id"],
        target_node_name=row["target_node_name"],
        database=row["database_name"],
        original_status=row["status"],
    )


def _credentials(
    conn: psycopg.Connection, *, project_id: uuid.UUID, key_ring: crypto.KeyRing
) -> dict[str, str]:
    credentials = {}
    for credential_type in (
        "db_authenticator",
        "db_auth",
        "db_admin",
        "db_executor",
        "db_client",
        "db_storage",
    ):
        credentials[credential_type] = provisioning.load_credential(
            conn, project_id=project_id, credential_type=credential_type, key_ring=key_ring
        )
    return credentials


def prepare_target_roles(
    conn: psycopg.Connection,
    target_admin: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    names: provisioning.TenantNames,
    key_ring: crypto.KeyRing,
) -> entitlements.Entitlements:
    allowed = entitlements.for_project(conn, project_id)
    secrets = _credentials(conn, project_id=project_id, key_ring=key_ring)
    provisioning.ensure_shared_roles(target_admin)
    provisioning.create_roles(
        target_admin,
        names,
        passwords={
            "authenticator": secrets["db_authenticator"],
            "auth": secrets["db_auth"],
            "admin": secrets["db_admin"],
        },
        connection_limits=allowed.connection_limits(),
    )
    provisioning.create_executor_role(
        target_admin, names, password=secrets["db_executor"]
    )
    provisioning.create_client_role(
        target_admin,
        names,
        password=secrets["db_client"],
        connection_limit=allowed.database_connections,
    )
    provisioning.create_storage_role(
        target_admin, names, password=secrets["db_storage"]
    )
    target_admin.commit()
    return allowed


def finish_target_database(
    target_admin: psycopg.Connection,
    names: provisioning.TenantNames,
    *,
    allowed: entitlements.Entitlements,
) -> None:
    provisioning.lock_down_database(target_admin, names)
    provisioning.grant_executor_connect(target_admin, names)
    provisioning.grant_client_connect(target_admin, names)
    provisioning.grant_storage_connect(target_admin, names)
    provisioning.apply_plan_settings(target_admin, names, settings=allowed.postgres_settings())
    provisioning.set_direct_sql_access(
        target_admin,
        names,
        enabled=allowed.direct_database_access,
        connection_limit=allowed.database_connections,
    )
    target_admin.commit()


def freeze_source(
    source_admin: psycopg.Connection,
    names: provisioning.TenantNames,
) -> None:
    provisioning.set_direct_sql_access(source_admin, names, enabled=False, connection_limit=0)
    _terminate_tenant_backends(source_admin, names)


def restore_source_access(
    source_admin: psycopg.Connection,
    names: provisioning.TenantNames,
    *,
    connection_limit: int,
) -> None:
    provisioning.set_direct_sql_access(
        source_admin, names, enabled=True, connection_limit=connection_limit
    )


def _terminate_tenant_backends(
    admin_conn: psycopg.Connection, names: provisioning.TenantNames
) -> None:
    roles = [
        names.authenticator,
        names.auth,
        names.admin,
        names.executor,
        names.client,
        names.storage,
    ]
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
              FROM pg_stat_activity
             WHERE datname = %s
               AND usename = ANY(%s)
               AND pid <> pg_backend_pid()
            """,
            (names.database, roles),
        )
    admin_conn.commit()


def dump_from_source(
    source_admin: psycopg.Connection,
    *,
    database: str,
    dump_path: str,
    run_as: str,
) -> tuple[float, int]:
    if models.database_name_for(restore.tenant_ref_of(database)) != database:
        raise MovementError(f"{database!r} is not a name this platform generates")
    started = time.monotonic()
    restore._run(["rm", "-f", dump_path], sudo=True)  # noqa: SLF001
    proc = restore._as_owner(  # noqa: SLF001
        ["pg_dump", "-p", str(restore._port_of(source_admin)), "-Fc", "-f", dump_path, database],
        run_as=run_as,
        timeout=restore.PROMOTION_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise MovementError(f"pg_dump of {database} failed: {restore._tail(proc.stderr or proc.stdout)}")  # noqa: SLF001
    restore._run(["chmod", "600", dump_path], sudo=True)  # noqa: SLF001
    size = restore._run(["stat", "-c", "%s", dump_path], sudo=True)  # noqa: SLF001
    return time.monotonic() - started, int((size.stdout or "0").strip() or 0)


def clean_source(
    source_admin: psycopg.Connection,
    names: provisioning.TenantNames,
) -> None:
    source_admin.commit()
    previous = source_admin.autocommit
    source_admin.autocommit = True
    try:
        source_admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(names.database)
            )
        )
    finally:
        source_admin.autocommit = previous
    for role in (
        names.authenticator,
        names.auth,
        names.admin,
        names.executor,
        names.client,
        names.storage,
    ):
        if provisioning.role_exists(source_admin, role):
            source_admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    source_admin.commit()


def move_tenant(
    conn: psycopg.Connection,
    source_admin: psycopg.Connection,
    target_admin: psycopg.Connection,
    *,
    project_ref: str,
    source_node: str | None,
    target_node: str,
    key_ring: crypto.KeyRing,
    platform_owner: str = "postgres",
    run_as: str = "postgres",
    tenant_connect=None,
) -> MoveOutcome:
    """Move one project to another node, preserving its public identity."""
    target = begin(
        conn, project_ref=project_ref, source_node=source_node, target_node=target_node
    )
    names = provisioning.TenantNames.for_ref(target.project_ref)
    outcome = MoveOutcome(
        move_id=target.move_id,
        project_ref=target.project_ref,
        source_node_id=target.source_node_id,
        target_node_id=target.target_node_id,
        database=target.database,
        original_status=target.original_status,
    )
    started = time.monotonic()
    dump_path = (
        f"{restore.prepare_dump_dir(run_as=run_as)}/"
        f"{target.database}_move_{target.move_id}.dump"
    )
    allowed = entitlements.for_project(conn, target.project_id)
    direct_sql_was_enabled = allowed.direct_database_access
    try:
        freeze_source(source_admin, names)
        outcome.dump_seconds, outcome.dump_bytes = dump_from_source(
            source_admin, database=target.database, dump_path=dump_path, run_as=run_as
        )
        target_allowed = prepare_target_roles(
            conn, target_admin, project_id=target.project_id, names=names, key_ring=key_ring
        )
        outcome.load_seconds = restore.load_into_target(
            target_admin,
            names,
            dump_path=dump_path,
            target_database=target.database,
            owner=platform_owner,
            run_as=run_as,
            allow_live_name=True,
        )
        finish_target_database(target_admin, names, allowed=target_allowed)
        connect = tenant_connect or restore._connect_to  # noqa: SLF001
        with connect(target_admin, target.database) as tenant_conn:
            outcome.ownership = restore.verify_ownership(
                tenant_conn, target_admin, names, database=target.database
            )
        if outcome.ownership is None or not outcome.ownership.verified:
            raise MovementError(
                "target ownership did not verify: "
                + (outcome.ownership.detail if outcome.ownership else "not checked")
            )
        db.execute(
            conn,
            "UPDATE projects SET node_id = %s, status = %s WHERE id = %s AND status = 'MOVING'",
            (target.target_node_id, target.original_status, target.project_id),
        )
        conn.commit()
        try:
            clean_source(source_admin, names)
            outcome.source_cleaned = True
        except Exception as exc:  # noqa: BLE001 - moved, but source cleanup must be reported
            outcome.notes.append(f"source cleanup failed: {type(exc).__name__}: {exc}")
            log.warning("source cleanup for %s failed: %s", project_ref, exc)
        outcome.status = "complete"
    except Exception as exc:  # noqa: BLE001 - recorded and project restored to serving state
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        db.execute(
            conn,
            "UPDATE projects SET status = %s WHERE id = %s AND status = 'MOVING'",
            (target.original_status, target.project_id),
        )
        conn.commit()
        if direct_sql_was_enabled:
            try:
                restore_source_access(
                    source_admin, names, connection_limit=allowed.database_connections
                )
            except Exception as restore_exc:  # noqa: BLE001
                outcome.notes.append(
                    f"source direct SQL access was not restored: {type(restore_exc).__name__}"
                )
        log.warning("move of %s failed: %s", project_ref, outcome.error)
    finally:
        outcome.total_seconds = time.monotonic() - started
        restore._run(["rm", "-f", dump_path], sudo=True)  # noqa: SLF001
        _finish(conn, outcome)
    return outcome


def _start(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    source_node_id: int,
    target_node_id: int,
    database: str,
    original_status: str,
) -> int:
    row = db.one(
        conn,
        """
        INSERT INTO tenant_moves
            (project_id, source_node_id, target_node_id, source_database,
             target_database, original_status)
        VALUES (%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (project_id, source_node_id, target_node_id, database, database, original_status),
    )
    return int(row["id"])


def _finish(conn: psycopg.Connection, outcome: MoveOutcome) -> None:
    db.execute(
        conn,
        """
        UPDATE tenant_moves
           SET status = %s, finished_at = now(), ownership_verified = %s,
               ownership_detail = %s, elapsed_seconds = %s, dump_bytes = %s,
               source_cleaned = %s, error = %s
         WHERE id = %s
        """,
        (
            outcome.status,
            outcome.ownership.verified if outcome.ownership else None,
            outcome.ownership.detail if outcome.ownership else None,
            round(outcome.total_seconds, 2),
            outcome.dump_bytes or None,
            outcome.source_cleaned,
            outcome.error,
            outcome.move_id,
        ),
    )
    conn.commit()


def history(conn: psycopg.Connection, *, project_id: uuid.UUID | None = None) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT m.id, p.project_ref, src.name AS source_node, tgt.name AS target_node,
               m.source_database, m.target_database, m.original_status, m.status,
               m.ownership_verified, m.ownership_detail, m.elapsed_seconds,
               m.dump_bytes, m.source_cleaned, m.started_at, m.finished_at, m.error
          FROM tenant_moves m
          JOIN projects p ON p.id = m.project_id
          JOIN nodes src ON src.id = m.source_node_id
          JOIN nodes tgt ON tgt.id = m.target_node_id
         WHERE %s::uuid IS NULL OR m.project_id = %s::uuid
         ORDER BY m.started_at DESC
        """,
        (project_id, project_id),
    )


def drain_report(conn: psycopg.Connection, *, node_name: str) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT p.project_ref, p.status, p.database_name, pl.code AS plan_code
          FROM projects p
          JOIN nodes n ON n.id = p.node_id
          LEFT JOIN plans pl ON pl.id = p.plan_id
         WHERE n.name = %s AND p.deleted_at IS NULL
         ORDER BY p.project_ref
        """,
        (node_name,),
    )


__all__ = [
    "MOVABLE_STATUSES",
    "MovementError",
    "MoveOutcome",
    "MoveTarget",
    "begin",
    "clean_source",
    "drain_report",
    "dump_from_source",
    "finish_target_database",
    "freeze_source",
    "history",
    "move_tenant",
    "prepare_target_roles",
]
