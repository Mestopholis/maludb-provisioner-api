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
from psycopg.rows import dict_row

from services.control_plane import (
    crypto,
    db,
    entitlements,
    extension_data,
    extension_pins,
    models,
    nodes,
    provisioning,
    restore,
)

log = logging.getLogger("maludb.tenant_movement")


class MovementError(RuntimeError):
    """A tenant move could not be performed, or could not be trusted."""


MOVABLE_STATUSES = ("PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED")
STOPPED = "STOPPED"


# Every role that can hold `CONNECT` on a tenant database. Enumerated from
# `TenantNames` rather than discovered, so a role added later is a change here
# too. `replicator` is in the list even though `_project_for_move` refuses a
# Realtime-enabled project: turning Realtime off does not always take the role
# with it, and a freeze that skipped it would leave a way into a frozen tenant.
def tenant_roles(names: provisioning.TenantNames) -> tuple[str, ...]:
    return (
        names.authenticator,
        names.auth,
        names.admin,
        names.executor,
        names.client,
        names.replicator,
        names.storage,
        names.vectors,
    )


def _one(node_conn: psycopg.Connection, sql_text: str, params: tuple = ()) -> dict | None:
    """One row from a *node* connection.

    Node connections are opened with a bare `psycopg.connect` by every caller --
    the CLI, the maintenance pass, the tests -- so they return tuples, and
    `db.one` (which assumes the pool's `dict_row`) raises `TypeError: tuple
    indices must be integers`.
    """
    with node_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql_text, params)
        return cur.fetchone()


def cluster_identity(conn: psycopg.Connection) -> int:
    """This cluster's unique identifier, from its control file.

    The thing that makes "these are two different nodes" checkable rather than
    assumed. A host and port can be spelled two ways for one cluster, and two
    `nodes` rows can address one; this cannot.
    """
    row = _one(conn, "SELECT system_identifier FROM pg_control_system()")
    if row is None:
        raise MovementError("the cluster did not report a system identifier")
    return int(row["system_identifier"])


def moved_aside_name(names: provisioning.TenantNames, when: datetime) -> str:
    """What the source database is renamed to. Never dropped."""
    stamp = when.strftime("%Y%m%d%H%M%S")
    candidate = f"{names.database}_pre_move_{stamp}"
    # PostgreSQL truncates identifiers at 63 bytes, and a silently truncated
    # name could collide with another project's. Refuse instead.
    if len(candidate) > 63:
        raise MovementError(f"the retained name {candidate!r} would be truncated")
    return candidate


@dataclass
class Freeze:
    """A frozen tenant, and exactly what has to be given back to unfreeze it."""

    database: str
    # The roles that held CONNECT when the freeze was taken. Release restores
    # these and nothing else: a blanket `GRANT CONNECT TO PUBLIC` would leave the
    # tenant more open than the move found it, which is a quiet privilege
    # escalation performed by a recovery step.
    had_connect: tuple[str, ...] = ()
    public_had_connect: bool = False
    terminated: int = 0
    frozen_at: datetime | None = None

    @property
    def seconds_held(self) -> float:
        if self.frozen_at is None:
            return 0.0
        return (datetime.now(UTC) - self.frozen_at).total_seconds()


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
    # The source is retained, not cleaned: `retire_source` renames it aside and
    # drops nothing. `retained_database` is the name to rename back if the move
    # has to be undone.
    source_retained: bool = False
    retained_database: str | None = None
    frozen: Freeze | None = None
    still_frozen: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "complete"

    @property
    def freeze_seconds(self) -> float:
        return self.frozen.seconds_held if self.frozen else 0.0


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
    # ADR-075, and not covered by the target merely agreeing with its own pins: a
    # move `pg_restore`s a dump whose `CREATE EXTENSION` carries no version, so the
    # tenant arrives at the target's. Lower silently downgrades it; higher lands it
    # ahead of the upgrade run (pinning slice 0, finding 6).
    pin_refusal = extension_pins.move_refusal(
        conn, source_node_id=row["node_id"], target_node_id=row["target_node_id"]
    )
    if pin_refusal:
        raise MovementError(f"cannot move {project_ref} to {target_node}: {pin_refusal}")
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
    # Before the load, not after: the dump grants on maludb_core's vector tables
    # to this role, and `pg_restore` drops a grant to a role the target lacks
    # (ADR-077). NOLOGIN and passwordless, so nothing is read from the vault.
    vectors = db.one(conn, "SELECT maludb_vectors_enabled FROM projects WHERE id = %s", (project_id,))
    if vectors and vectors["maludb_vectors_enabled"]:
        provisioning.create_vectors_role(target_admin, names)
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


def _has_connect(admin_conn: psycopg.Connection, database: str, role: str) -> bool:
    row = _one(
        admin_conn,
        "SELECT has_database_privilege(%s, %s, 'CONNECT') AS ok",
        (role, database),
    )
    return bool(row and row["ok"])


def _role_exists(admin_conn: psycopg.Connection, role: str) -> bool:
    return _one(admin_conn, "SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (role,)) is not None


def roles_refusal(target_admin: psycopg.Connection, names: provisioning.TenantNames) -> str | None:
    """Why the destination cannot take the load yet, after its roles were prepared.

    `prepare_target_roles` creating them is not taken on trust: a role missing at
    load time is ADR-059's finding, and it is checked here, before the freeze.
    """
    absent = restore.missing_roles(target_admin, names)
    if not absent:
        return None
    return (
        "the destination is missing this tenant's roles: " + ", ".join(absent) + ". "
        "Loading without them completes with 'errors ignored' and silently reassigns "
        "the auth and storage schemas to the platform superuser (ADR-059)"
    )


def preflight(
    source_admin: psycopg.Connection,
    target_admin: psycopg.Connection,
    names: provisioning.TenantNames,
) -> list[str]:
    """Everything that should stop a move before anything is written anywhere.

    Before the freeze, deliberately. Each of these is cheap to check and
    expensive to discover halfway through -- a customer is offline for the whole
    of a move, and a move that fails on a missing role has spent that downtime
    for nothing.

    **The tenant's roles are not checked here**, because on a destination that
    has never had the tenant -- which is every ordinary move -- they do not exist
    yet. They used to be, which refused every such move before it started; the
    tests stubbed this function and never saw it. `move_tenant` creates the roles
    once this has passed, then `roles_refusal` checks them, still before the
    freeze. The order matters: creating roles resets their passwords, and doing
    that before the cluster-identity check below had proven the destination is not
    the source would write to the live cluster on a mis-registered node.

    `_project_for_move` already refuses a target whose `nodes` row is the
    project's own. That is a control-plane check on two rows; this is the
    physical one, and they are not the same claim. Two rows can address one
    cluster through a copy-pasted DSN or a node re-registered under a new name.
    """
    problems: list[str] = []

    if cluster_identity(source_admin) == cluster_identity(target_admin):
        problems.append(
            "the source and destination are the same cluster. A move that loaded "
            "here would write the tenant's dump over the tenant's own live "
            "database, which is the one failure in this module with no recovery"
        )

    existing = _one(
        target_admin,
        "SELECT 1 AS x FROM pg_database WHERE datname = %s",
        (names.database,),
    )
    if existing:
        problems.append(
            f"the destination already has a database named {names.database}. A move does "
            "not write into an existing database; remove or rename it first"
        )

    source_has = _one(
        source_admin, "SELECT 1 AS x FROM pg_database WHERE datname = %s", (names.database,)
    )
    if not source_has:
        problems.append(f"the source has no database named {names.database}")

    return problems


def freeze(admin_conn: psycopg.Connection, names: provisioning.TenantNames) -> Freeze:
    """Stop every tenant session, and stop new ones starting (ADR-071).

    Records what it took away before taking it, so the release gives back exactly
    that. Terminating comes *after* revoking, in that order and not the other:
    terminate first and a connection pool reconnects into the gap.

    `REVOKE CONNECT` rather than a privilege revocation, because privilege
    revocation leaves DDL, sequence advancement, `DELETE` and `SECURITY DEFINER`
    functions able to change state under the copy. A session that cannot exist
    cannot write. It is also broader than turning direct SQL access off, which
    only reaches the roles that had it -- and so froze nothing at all for a
    project that never had direct access to begin with.

    The platform's own connection is unaffected: this runs on the node's admin
    connection, which is to `postgres` rather than to the tenant database, and
    the platform owner's `CONNECT` is never revoked. `pg_dump` still works.
    """
    state = Freeze(database=names.database)
    roles = [r for r in tenant_roles(names) if _role_exists(admin_conn, r)]
    state.had_connect = tuple(r for r in roles if _has_connect(admin_conn, names.database, r))
    state.public_had_connect = _has_connect(admin_conn, names.database, "public")

    target = sql.Identifier(names.database)
    if state.public_had_connect:
        admin_conn.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {db} FROM PUBLIC").format(db=target)
        )
    for role in state.had_connect:
        admin_conn.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {db} FROM {role}").format(
                db=target, role=sql.Identifier(role)
            )
        )

    # Whatever was already connected. Counted rather than assumed, because the
    # number is the difference between "the workers were stopped first" and "a
    # supervised worker is about to notice and be refused".
    row = _one(
        admin_conn,
        "SELECT count(*) AS n FROM ("
        "  SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "   WHERE datname = %s AND pid <> pg_backend_pid()"
        ") AS t",
        (names.database,),
    )
    state.terminated = int(row["n"]) if row else 0
    state.frozen_at = datetime.now(UTC)
    admin_conn.commit()
    log.info(
        "froze %s: revoked CONNECT from %d role(s), terminated %d backend(s)",
        names.database, len(state.had_connect), state.terminated,
    )
    return state


def release(admin_conn: psycopg.Connection, state: Freeze) -> None:
    """Give back exactly what the freeze took, and nothing more."""
    target = sql.Identifier(state.database)
    if state.public_had_connect:
        admin_conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {db} TO PUBLIC").format(db=target))
    for role in state.had_connect:
        if not _role_exists(admin_conn, role):
            continue
        admin_conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=target, role=sql.Identifier(role)
            )
        )
    admin_conn.commit()
    log.info("released %s after %.1fs", state.database, state.seconds_held)


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


def retire_source(
    source_admin: psycopg.Connection,
    names: provisioning.TenantNames,
    *,
    when: datetime | None = None,
) -> str:
    """Rename the source database aside. It is never dropped, and neither are the roles.

    This used to be a `DROP DATABASE ... WITH (FORCE)` followed by `DROP ROLE`,
    which made a move irreversible at the exact moment it was least proven --
    immediately after the first repointing of customer traffic to a copy that has
    existed for seconds. A move that turns out to have been wrong is now undone
    by renaming this back and repointing one column.

    The roles stay for the same reason: they are what the retained database's
    `auth` and `storage` schemas are owned by, and dropping them would leave the
    retained copy unrestorable (ADR-059) -- so the rollback path would survive
    the database and not the thing that makes it loadable.

    Reclaiming the disk is a separate, later, deliberate act. The same choice
    `restore.activate` makes, for the same reason, and the one AGENTS.md asks for
    when it says destructive cleanup requires explicit state checks.

    **The retained database stays frozen**, and that is deliberate rather than an
    oversight of the rename. Its roles still exist on this node, so a released
    copy would answer a customer's old DSN with a writable stale database while
    the live tenant serves from somewhere else. Undoing a move is therefore
    "rename back, then `cp-manage node release-freeze`", which restores the same
    recorded grants.
    """
    retained = moved_aside_name(names, when or datetime.now(UTC))
    source_admin.commit()
    previous = source_admin.autocommit
    source_admin.autocommit = True
    try:
        source_admin.execute(
            sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                sql.Identifier(names.database), sql.Identifier(retained)
            )
        )
    finally:
        source_admin.autocommit = previous
    log.info("retained the source of %s as %s", names.database, retained)
    return retained


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
    frozen: Freeze | None = None
    try:
        # Before the freeze, and before anything is dumped. The customer is
        # offline from the freeze onward, so every cheap refusal is spent here
        # where it costs no downtime.
        problems = preflight(source_admin, target_admin, names)
        if problems:
            raise MovementError(
                "refusing to move " + target.project_ref + ": " + "; ".join(problems)
            )
        # Only once preflight has proven the destination is another cluster:
        # creating the roles resets their passwords. Idempotent, so a retried
        # move arrives here with them already present.
        target_allowed = prepare_target_roles(
            conn, target_admin, project_id=target.project_id, names=names, key_ring=key_ring
        )
        refusal = roles_refusal(target_admin, names)
        if refusal:
            raise MovementError("refusing to move " + target.project_ref + ": " + refusal)
        frozen = freeze(source_admin, names)
        outcome.frozen = frozen
        outcome.dump_seconds, outcome.dump_bytes = dump_from_source(
            source_admin, database=target.database, dump_path=dump_path, run_as=run_as
        )
        outcome.load_seconds = restore.load_into_target(
            target_admin,
            names,
            dump_path=dump_path,
            target_database=target.database,
            owner=platform_owner,
            run_as=run_as,
        )
        connect = tenant_connect or restore._connect_to  # noqa: SLF001
        # `pg_dump` left out every row maludb_core stores (ADR-077 decision 8), so
        # the vector store is carried beside it -- before the ownership check and
        # well before the repointing below, so a carry that cannot be exact fails
        # the move while the source is still the only live copy.
        with connect(source_admin, target.database) as source_db, \
                connect(target_admin, target.database) as target_db:
            carried = extension_data.carry(extension_data.ConnectionSource(source_db), target_db)
        outcome.notes.append(f"carried {carried.total} maludb_core vector row(s)")
        finish_target_database(target_admin, names, allowed=target_allowed)
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
            outcome.retained_database = retire_source(source_admin, names)
            outcome.source_retained = True
        except Exception as exc:  # noqa: BLE001 - moved, but source retirement must be reported
            outcome.notes.append(f"source retirement failed: {type(exc).__name__}: {exc}")
            log.warning("source retirement for %s failed: %s", project_ref, exc)
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
        # The source is unfrozen, and that is safe for a reason worth stating
        # rather than assuming: every exception that reaches here was raised
        # before the repointing `UPDATE`, because the only steps after that
        # commit are the retirement -- which handles its own failures above --
        # and an assignment. So the destination has never served traffic, the
        # source is still the only live copy, and leaving it frozen would take a
        # tenant offline to recover from a move that changed nothing.
        #
        # If the release itself fails the tenant really is stranded, and the
        # outcome says so by name so an operator can finish it with
        # `cp-manage node release-freeze`.
        if frozen is not None:
            try:
                release(source_admin, frozen)
                outcome.notes.append(
                    f"source unfrozen: CONNECT restored to {len(frozen.had_connect)} role(s)"
                )
            except Exception as release_exc:  # noqa: BLE001
                outcome.still_frozen = True
                outcome.notes.append(
                    f"THE SOURCE IS STILL FROZEN: release failed with "
                    f"{type(release_exc).__name__}: {release_exc}. The tenant cannot accept "
                    f"connections until `cp-manage node release-freeze --database "
                    f"{names.database}` succeeds"
                )
                log.error("could not unfreeze %s after a failed move", names.database)
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
               source_cleaned = %s, retained_database = %s, still_frozen = %s,
               frozen_roles = %s, frozen_public = %s, error = %s
         WHERE id = %s
        """,
        (
            outcome.status,
            outcome.ownership.verified if outcome.ownership else None,
            outcome.ownership.detail if outcome.ownership else None,
            round(outcome.total_seconds, 2),
            outcome.dump_bytes or None,
            outcome.source_retained,
            outcome.retained_database,
            outcome.still_frozen,
            # Recorded whether or not the release succeeded: it is the only
            # record of what the freeze took, and inferring it afterwards from
            # "roles lacking CONNECT" would hand it to roles that never had it.
            list(outcome.frozen.had_connect) if outcome.frozen else None,
            bool(outcome.frozen.public_had_connect) if outcome.frozen else False,
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
               m.dump_bytes, m.source_cleaned, m.retained_database, m.still_frozen,
               m.frozen_roles, m.frozen_public, m.started_at, m.finished_at, m.error
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
    "Freeze",
    "MovementError",
    "MoveOutcome",
    "MoveTarget",
    "begin",
    "cluster_identity",
    "drain_report",
    "dump_from_source",
    "finish_target_database",
    "freeze",
    "history",
    "move_tenant",
    "moved_aside_name",
    "preflight",
    "prepare_target_roles",
    "roles_refusal",
    "release",
    "retire_source",
    "tenant_roles",
]
