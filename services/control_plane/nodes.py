"""Node registry, capacity scoring, and placement.

ADR-003: nodes are pre-provisioned VMs that platform administrators manage
separately. The control plane schedules projects onto them; it never creates
one.

Capacity terms come from measurement, not guesswork -- see docs/CAPACITY.md.
The finding that matters here is that **connections bind before memory**: at
default settings a cluster saturates at roughly 24 warm projects while memory
would have allowed about 40. So warm and total project counts are tracked
separately, because only warm projects consume connections and worker memory
while every project consumes disk.

Warm accounting is structured but not yet enforced: worker state does not exist
until Phase 03. Total-project capacity is enforced today.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.control_plane import crypto, db, realtime

# A node whose health has not been reported within this window is not eligible
# for new projects. Stale metrics are indistinguishable from a dead node, and
# placing onto a dead node fails provisioning in a way that leaves debris.
HEALTH_STALE_AFTER = timedelta(minutes=5)

PLACEABLE_STATUS = "active"

# States from which a placement may be released. FAILED is deliberately absent:
# a project can reach FAILED from any operational state, including ones after
# the database was created, so the status alone cannot tell you whether
# anything exists on the node. release_placement checks database_name too.
RELEASABLE_STATUSES = ("REQUESTED", "PLACEMENT_RESERVED")

# Defaults used when a node's capacity_json omits a key. Deliberately
# conservative: docs/CAPACITY.md measured ~24 warm projects per cluster at
# PostgreSQL's default max_connections of 100.
DEFAULT_MAX_PROJECTS = 200
DEFAULT_MAX_WARM_PROJECTS = 20
DEFAULT_MIN_FREE_DISK_BYTES = 20 * 1024**3

# PostgreSQL's own defaults, used until a node reports its real settings.
# ADR-022 found connections rather than memory to be the binding constraint on
# how many tenants a node holds, so guessing high here would be the dangerous
# direction: it would let placement fill a node past the point where tenants
# that did nothing wrong start failing to connect.
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_RESERVED_CONNECTIONS = 3

# What the platform itself needs on a node beyond the reserved superuser slots:
# provisioning, measurement passes and health checks all connect. Held back so a
# node that is full of tenants can still be administered.
PLATFORM_CONNECTION_ALLOWANCE = 10

# PostgreSQL's default, and the reason Realtime's ceiling is the tightest of the
# three: ten slots against ADR-022's warm ceiling of roughly 24 projects, with
# one slot required per tenant database that uses Realtime and no multiplexing
# available (specs/realtime-replication-model.md, R1). A node reports its real
# figure through `realtime.record_readiness`.
DEFAULT_MAX_REPLICATION_SLOTS = 10


class PlacementError(RuntimeError):
    """No node could accept the project."""


@dataclass(frozen=True)
class NodeCapacity:
    node_id: int
    name: str
    node_pool: str
    max_projects: int
    max_warm_projects: int
    current_projects: int
    current_warm_projects: int
    free_disk_bytes: int | None
    min_free_disk_bytes: int
    # Connections, which ADR-022 identified as the real ceiling. Projected from
    # the plans of the projects actually warm on this node rather than from a
    # per-project average, because pool size is now a plan entitlement and a
    # node full of production projects is a very different shape from one full
    # of free ones.
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    reserved_connections: int = DEFAULT_RESERVED_CONNECTIONS
    projected_connections: int = 0
    # Replication slots, the third ceiling and the tightest one. Recorded on the
    # node by `realtime.record_readiness` rather than assumed, for the same
    # reason connections are: the defaults here are PostgreSQL's, and guessing
    # high would let placement commit slots the node cannot create.
    #
    # `realtime_ready` is False until a node has been checked, so a node nobody
    # has prepared refuses Realtime rather than accepting it and failing at
    # enablement -- ADR-031's pg_hba reject is not a thing to discover late.
    realtime_ready: bool = False
    max_replication_slots: int = DEFAULT_MAX_REPLICATION_SLOTS
    committed_slots: int = 0

    @property
    def project_headroom(self) -> int:
        return self.max_projects - self.current_projects

    @property
    def warm_headroom(self) -> int:
        return self.max_warm_projects - self.current_warm_projects

    @property
    def usable_connections(self) -> int:
        """What tenants may consume, once the platform has what it needs."""
        return max(0, self.max_connections - self.reserved_connections
                   - PLATFORM_CONNECTION_ALLOWANCE)

    @property
    def connection_headroom(self) -> int:
        return self.usable_connections - self.projected_connections

    @property
    def usable_replication_slots(self) -> int:
        """Slots tenants may hold, once the platform has kept what it needs."""
        return max(0, self.max_replication_slots - realtime.PLATFORM_SLOT_ALLOWANCE)

    @property
    def realtime_headroom(self) -> int:
        return self.usable_replication_slots - self.committed_slots

    def realtime_rejection_reason(self) -> str | None:
        """Why this node cannot take another *Realtime* project.

        Deliberately separate from `rejection_reason`. A node out of replication
        slots is still a perfectly good node for the many projects that do not
        want Realtime, and folding the slot ceiling into general placement would
        strand capacity ADR-022 measured as usable.
        """
        if not self.realtime_ready:
            return (
                "not prepared for Realtime; run `cp-manage node realtime-check` "
                "(ADR-031: wal_level, the pg_hba physical-replication reject, and a bounded "
                "max_slot_wal_keep_size are node preconditions, not tenant settings)"
            )
        if self.realtime_headroom <= 0:
            return (
                f"no replication slots left ({self.committed_slots} committed of "
                f"{self.usable_replication_slots} usable); one slot per tenant database, "
                "no multiplexing"
            )
        return None

    @property
    def can_accept_realtime(self) -> bool:
        return self.can_accept and self.realtime_rejection_reason() is None

    @property
    def utilisation(self) -> float:
        if self.max_projects <= 0:
            return 1.0
        return self.current_projects / self.max_projects

    def rejection_reason(self) -> str | None:
        """Why this node cannot take another project, or None if it can.

        Warm capacity and connection headroom were computed here from the start
        and never consulted, so ADR-022's ceiling was measured and unenforced --
        a node could be filled well past the point where tenants begin failing
        to connect, and nothing would have said so.
        """
        if self.project_headroom <= 0:
            return f"at project capacity ({self.current_projects}/{self.max_projects})"
        if self.warm_headroom <= 0:
            return (
                f"at warm capacity ({self.current_warm_projects}/{self.max_warm_projects}); "
                "ADR-022 measured connections as the binding constraint"
            )
        if self.connection_headroom <= 0:
            return (
                f"no connection headroom ({self.projected_connections} projected of "
                f"{self.usable_connections} usable)"
            )
        if self.free_disk_bytes is not None and self.free_disk_bytes < self.min_free_disk_bytes:
            return f"insufficient free disk ({self.free_disk_bytes} < {self.min_free_disk_bytes})"
        return None

    @property
    def can_accept(self) -> bool:
        return self.rejection_reason() is None


def _now() -> datetime:
    return datetime.now(UTC)


def _int_from(config: dict[str, Any], key: str, default: int) -> int:
    """Read an integer from node JSON, falling back on anything unusable.

    capacity_json and metrics_json are operator-supplied. A malformed value
    must not raise mid-placement, and must not be read as unlimited capacity --
    falling back to the conservative default is the safe direction.
    """
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value < 0:
        return default
    return int(value)


# -- registry --------------------------------------------------------------


def register_node(
    conn: psycopg.Connection,
    *,
    name: str,
    hostname: str,
    internal_host: str,
    node_pool: str = "shared",
    capacity: dict[str, Any] | None = None,
) -> int:
    """Register a node, or update its addresses if the name already exists."""
    row = db.one(
        conn,
        """
        INSERT INTO nodes (name, hostname, internal_host, node_pool, status, capacity_json)
        VALUES (%s, %s, %s, %s, 'maintenance', %s)
        ON CONFLICT (name) DO UPDATE
            SET hostname = EXCLUDED.hostname,
                internal_host = EXCLUDED.internal_host,
                node_pool = EXCLUDED.node_pool
        RETURNING id
        """,
        (name, hostname, internal_host, node_pool, psycopg.types.json.Jsonb(capacity or {})),
    )
    # New nodes start in 'maintenance', not 'active': an operator confirms a
    # node is genuinely ready before customer projects land on it.
    return int(row["id"])


def set_status(conn: psycopg.Connection, *, name: str, status: str) -> None:
    if status not in ("active", "draining", "maintenance", "unhealthy"):
        raise ValueError(f"unknown node status {status!r}")
    if db.execute(conn, "UPDATE nodes SET status = %s WHERE name = %s", (status, name)) == 0:
        raise ValueError(f"no node named {name!r}")


def record_health(conn: psycopg.Connection, *, name: str, metrics: dict[str, Any]) -> None:
    """Record a health report. Freshness is what gates placement."""
    if db.execute(
        conn,
        "UPDATE nodes SET metrics_json = %s, last_health_at = now() WHERE name = %s",
        (psycopg.types.json.Jsonb(metrics), name),
    ) == 0:
        raise ValueError(f"no node named {name!r}")


def capacity_of(conn: psycopg.Connection, node_id: int) -> NodeCapacity:
    row = db.one(
        conn,
        """
        SELECT n.id, n.name, n.node_pool, n.capacity_json, n.metrics_json,
               (SELECT count(*) FROM projects p
                 WHERE p.node_id = n.id AND p.deleted_at IS NULL) AS current_projects,
               -- Warm means a worker is actually running, not that the project
               -- is nominally active. A free project can be ACTIVE and asleep;
               -- ADR-022 measured that a slept project costs zero connections
               -- and zero RAM, and free-tier density rests entirely on that.
               -- Counting by status instead would charge every sleeping project
               -- against the connection ceiling it is not consuming.
               (SELECT count(*) FROM projects p
                 WHERE p.node_id = n.id AND p.deleted_at IS NULL
                   AND p.worker_state = 'RUNNING') AS current_warm,
               (SELECT count(*) FROM projects p
                 WHERE p.node_id = n.id AND p.deleted_at IS NULL
                   AND p.auth_worker_state = 'RUNNING') AS current_warm_auth
          FROM nodes n
         WHERE n.id = %s
        """,
        (node_id,),
    )
    if row is None:
        raise PlacementError(f"no node with id {node_id}")

    capacity = row["capacity_json"] or {}
    metrics = row["metrics_json"] or {}
    free_disk = metrics.get("free_disk_bytes")
    if isinstance(free_disk, bool) or not isinstance(free_disk, (int, float)):
        free_disk = None

    return NodeCapacity(
        # Anything other than an explicit true means unprepared. A malformed
        # value must read as "not ready": the failure mode of guessing wrong in
        # the other direction is a tenant holding a readable copy of the node.
        realtime_ready=capacity.get("realtime_ready") is True,
        max_replication_slots=_int_from(
            capacity, "max_replication_slots", DEFAULT_MAX_REPLICATION_SLOTS
        ),
        committed_slots=realtime.committed_slots(conn, node_id),
        max_connections=_int_from(capacity, "max_connections", DEFAULT_MAX_CONNECTIONS),
        reserved_connections=_int_from(
            capacity, "reserved_connections", DEFAULT_RESERVED_CONNECTIONS
        ),
        projected_connections=_projected_connections(conn, node_id),
        node_id=row["id"],
        name=row["name"],
        node_pool=row["node_pool"],
        max_projects=_int_from(capacity, "max_projects", DEFAULT_MAX_PROJECTS),
        max_warm_projects=_int_from(capacity, "max_warm_projects", DEFAULT_MAX_WARM_PROJECTS),
        current_projects=int(row["current_projects"]),
        current_warm_projects=int(row["current_warm"]),
        free_disk_bytes=int(free_disk) if free_disk is not None else None,
        min_free_disk_bytes=_int_from(capacity, "min_free_disk_bytes", DEFAULT_MIN_FREE_DISK_BYTES),
    )


def _projected_connections(conn: psycopg.Connection, node_id: int) -> int:
    """Connections the warm projects on this node are expected to hold.

    Summed from each project's own plan rather than from an average, because
    pool size became a plan entitlement in slice 1: a node full of production
    projects at a pool of 12 is a very different shape from one full of free
    projects at 3, and an average would describe neither.

    ADR-022 measured 4 backends per warm project at a pool size of 3 -- the pool
    plus one for the connection PostgREST holds outside it -- so the estimate is
    pool + 1, plus the auth role's fixed allowance where an Auth worker is
    running.
    """
    from services.control_plane import entitlements

    rows = db.query(
        conn,
        """
        SELECT p.worker_state, p.auth_worker_state, pl.code AS plan_code, pl.config_json
          FROM projects p LEFT JOIN plans pl ON pl.id = p.plan_id
         WHERE p.node_id = %s AND p.deleted_at IS NULL
           AND (p.worker_state = 'RUNNING' OR p.auth_worker_state = 'RUNNING')
        """,
        (node_id,),
    )
    total = 0
    for row in rows:
        allowed = entitlements.resolve(row["plan_code"], row["config_json"])
        if row["worker_state"] == "RUNNING":
            total += allowed.postgrest_pool_size + 1
        if row["auth_worker_state"] == "RUNNING":
            total += entitlements.AUTH_ROLE_CONNECTIONS
    return total


def record_node_limits(conn: psycopg.Connection, admin_conn: psycopg.Connection, *, name: str) -> dict:
    """Read a node's real connection settings and store them.

    Asked of the node rather than assumed, because the defaults here are
    PostgreSQL's and a production node will have been tuned. Guessing high is
    the dangerous direction: it lets placement fill a node past the point where
    tenants start failing to connect.
    """
    # Named columns, not positional: callers pass whichever admin connection
    # they already hold, and the provisioning ones use a dict row factory. The
    # same mistake cost a debugging round in slice 3.
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT current_setting('max_connections')::int AS max_conn, "
                    "current_setting('superuser_reserved_connections')::int AS reserved")
        row = cur.fetchone()
    limits = {"max_connections": int(row["max_conn"]), "reserved_connections": int(row["reserved"])}
    db.execute(
        conn,
        "UPDATE nodes SET capacity_json = capacity_json || %s::jsonb WHERE name = %s",
        (psycopg.types.json.Jsonb(limits), name),
    )
    conn.commit()
    return limits


def eligible_nodes(
    conn: psycopg.Connection,
    *,
    node_pool: str = "shared",
    needs_realtime: bool = False,
) -> list[NodeCapacity]:
    """Nodes that could accept a project, least utilised first.

    `needs_realtime` narrows the field to nodes prepared under ADR-031 with a
    replication slot still free. It defaults to False on purpose: Realtime is
    opt-in per project (slice 2), and defaulting it to the plan's entitlement
    would make every paid project refuse to place on the nodes that exist today.
    """
    rows = db.query(
        conn,
        """
        SELECT id FROM nodes
         WHERE status = %s AND node_pool = %s
           AND last_health_at IS NOT NULL AND last_health_at > %s
         ORDER BY id
        """,
        (PLACEABLE_STATUS, node_pool, _now() - HEALTH_STALE_AFTER),
    )
    candidates = [capacity_of(conn, row["id"]) for row in rows]
    accepts = (lambda c: c.can_accept_realtime) if needs_realtime else (lambda c: c.can_accept)
    return sorted((c for c in candidates if accepts(c)), key=lambda c: c.utilisation)


# -- placement -------------------------------------------------------------


def reserve_placement(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    node_pool: str = "shared",
    needs_realtime: bool = False,
) -> int:
    """Assign a project to a node, atomically.

    The capacity check and the assignment happen inside one transaction holding
    a row lock on the chosen node, so two concurrent provisioning runs cannot
    both see headroom and both take the last slot. Verified with a concurrency
    test rather than assumed.

    The same lock is what makes the replication-slot ceiling enforceable rather
    than merely measured: slots are a cluster-wide pool of ten, so two
    concurrent placements racing on the last one is not a hypothetical.
    """
    with conn.transaction():
        candidates = eligible_nodes(conn, node_pool=node_pool, needs_realtime=needs_realtime)
        if not candidates:
            raise PlacementError(
                f"no healthy node in pool {node_pool!r} can accept a "
                f"{'Realtime ' if needs_realtime else ''}project"
            )

        for candidate in candidates:
            # Lock the node, then re-read capacity under the lock: another
            # transaction may have filled it between selection and here.
            locked = db.one(conn, "SELECT id FROM nodes WHERE id = %s FOR UPDATE", (candidate.node_id,))
            if locked is None:
                continue
            confirmed = capacity_of(conn, candidate.node_id)
            if not (confirmed.can_accept_realtime if needs_realtime else confirmed.can_accept):
                continue

            updated = db.execute(
                conn,
                """
                UPDATE projects
                   SET node_id = %s, status = 'PLACEMENT_RESERVED'
                 WHERE id = %s AND node_id IS NULL AND deleted_at IS NULL
                """,
                (confirmed.node_id, project_id),
            )
            if updated == 0:
                raise PlacementError("project is already placed, or does not exist")
            return confirmed.node_id

        raise PlacementError(
            f"no healthy node in pool {node_pool!r} can accept a "
            f"{'Realtime ' if needs_realtime else ''}project"
        )


def release_placement(conn: psycopg.Connection, *, project_id: uuid.UUID) -> None:
    """Undo a reservation for a project that never got a database.

    Gated on the recorded fact -- whether a database exists -- rather than on
    the status label. An earlier version allowed release from `FAILED`, which
    was wrong: `specs/provisioning-state-machine.md` permits *any* operational
    state to reach `FAILED`, including states after `DATABASE_CREATING`. A
    project that failed during bootstrap therefore had a real database on a
    real node, and clearing `node_id` left it orphaned -- still holding
    customer data, no longer reachable by deletion, suspension or accounting.

    Failed projects that do hold a database need the explicit cleanup path in
    slice 4, which must drop the database before forgetting its node, never the
    reverse.
    """
    row = db.one(conn, "SELECT status, database_name FROM projects WHERE id = %s", (project_id,))
    if row is None:
        raise PlacementError("project does not exist")

    # The real precondition: nothing was created on the node yet.
    if row["database_name"] is not None:
        raise PlacementError(
            f"refusing to release placement: database {row['database_name']} exists on the node. "
            "Use the cleanup path, which drops the database before forgetting where it lives."
        )

    if row["status"] not in RELEASABLE_STATUSES:
        raise PlacementError(
            f"refusing to release placement for a project in {row['status']}: "
            "objects may already exist on the node"
        )
    db.execute(
        conn,
        "UPDATE projects SET node_id = NULL, status = 'REQUESTED' WHERE id = %s",
        (project_id,),
    )


# -- privileged node credentials -------------------------------------------


def set_admin_dsn(conn: psycopg.Connection, *, name: str, dsn: str, key_ring: crypto.KeyRing) -> None:
    """Store the privileged DSN used to provision on this node.

    Class B under ADR-023: envelope encrypted, never hashed, because
    provisioning must reproduce it to connect. Associated data binds the
    ciphertext to this node's row, so a DSN copied into another node's row
    fails to decrypt rather than silently pointing provisioning at the wrong
    cluster.
    """
    row = db.one(conn, "SELECT id FROM nodes WHERE name = %s", (name,))
    if row is None:
        raise ValueError(f"no node named {name!r}")
    sealed = key_ring.seal(dsn.encode(), aad=crypto.aad_for("nodes", "admin_ciphertext", str(row["id"])))
    db.execute(
        conn,
        """
        UPDATE nodes
           SET admin_ciphertext = %s, admin_nonce = %s, admin_key_version = %s
         WHERE id = %s
        """,
        (sealed.ciphertext, sealed.nonce, sealed.key_version, row["id"]),
    )


def admin_dsn(conn: psycopg.Connection, *, node_id: int, key_ring: crypto.KeyRing) -> str:
    """Recover the privileged DSN for a node.

    The returned value is a live credential: never log it, never include it in
    an error, never return it over the API.
    """
    row = db.one(
        conn,
        "SELECT admin_ciphertext, admin_nonce, admin_key_version FROM nodes WHERE id = %s",
        (node_id,),
    )
    if row is None:
        raise PlacementError(f"no node with id {node_id}")
    if row["admin_ciphertext"] is None:
        raise PlacementError(f"node {node_id} has no provisioning credential configured")
    sealed = crypto.SealedValue(
        ciphertext=bytes(row["admin_ciphertext"]),
        nonce=bytes(row["admin_nonce"]),
        key_version=row["admin_key_version"],
    )
    return key_ring.open(sealed, aad=crypto.aad_for("nodes", "admin_ciphertext", str(node_id))).decode()
