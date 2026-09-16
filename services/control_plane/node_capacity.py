"""Node capacity: what a node holds against its ceilings (docs/CAPACITY.md, ADR-022).

Split out of `nodes` for ADR-082 slice 3c. The operator console reports capacity and
must not import `nodes`, which unwraps node superuser credentials with the KEK. This
module reads counts and recorded readiness only, and imports nothing that reaches a
credential: `db`, `entitlements`, `extension_pins` and `realtime`'s slot arithmetic.
`nodes` re-exports every name, so placement and `cp-manage` are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from services.control_plane import db, extension_pins, realtime

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
    # Phase 11 slice 1. Recorded by `backup.record_readiness`, and deliberately
    # NOT consulted by `rejection_reason`: a node with no backup is a node with
    # a real problem, and it is still a perfectly good node for the projects
    # already on it. Refusing placement on it would strand capacity to punish an
    # operator for something a report can tell them, and this repository has
    # made the report-before-enforcing mistake in the other direction (Phase 05)
    # and learned from it. `maintenance.check_backups` is what raises it.
    #
    # False until a node has been checked, on `realtime_ready`'s reasoning: a
    # node nobody has prepared reads as unprepared rather than as fine.
    backup_ready: bool = False
    # ADR-075 decision 4, from `extension_pins.rejection_reason`: why this node's
    # extensions disagree with its pins -- no pin, never checked, a mismatch, or
    # backends still running a replaced library. Placement, moves in and
    # restores all refuse on it; the node's existing tenants keep serving.
    # Defaulting to a refusal, on `realtime_ready`'s reasoning: a NodeCapacity
    # built without reading the pins must not read as a node that agrees.
    extension_refusal: str | None = "extension pins were not read"

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
        if self.extension_refusal:
            return self.extension_refusal
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
        extension_refusal=extension_pins.rejection_reason(
            extension_pins.pins(conn, row["id"]), capacity.get("extension_check")
        ),
        realtime_ready=capacity.get("realtime_ready") is True,
        backup_ready=capacity.get("backup_ready") is True,
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
