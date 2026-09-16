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

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.control_plane import crypto, db
from services.control_plane.node_capacity import (  # noqa: F401 - re-exported; see node_capacity
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_PROJECTS,
    DEFAULT_MAX_REPLICATION_SLOTS,
    DEFAULT_MAX_WARM_PROJECTS,
    DEFAULT_MIN_FREE_DISK_BYTES,
    DEFAULT_RESERVED_CONNECTIONS,
    PLATFORM_CONNECTION_ALLOWANCE,
    NodeCapacity,
    PlacementError,
    _int_from,
    _projected_connections,
    capacity_of,
)

# A node whose health has not been reported within this window is not eligible
# for new projects. Stale metrics are indistinguishable from a dead node, and
# placing onto a dead node fails provisioning in a way that leaves debris.
HEALTH_STALE_AFTER = timedelta(minutes=5)

PLACEABLE_STATUS = "active"

# ADR-065. The pool every tier is entitled to as shipped, and the value the
# `nodes` column has defaulted to since migration 0002.
DEFAULT_POOL = "shared"

# A pool name is an identifier in a `WHERE node_pool = %s` comparison and the
# thing that decides which hardware a customer lands on. `entitlements` applies
# the same pattern to the plan's side; the two must agree or the comparison
# silently matches nothing.
_POOL = re.compile(r"\A[a-z][a-z0-9_-]{0,49}\Z")


def checked_pool(name: str) -> str:
    """Normalise and validate a pool name, or refuse it.

    Refuses rather than falling back, unlike the entitlement side. The
    asymmetry is deliberate: a bad value in `plans.config_json` is read on a
    request path where raising would refuse a customer for an operator's typo,
    while this runs in `cp-manage node register`, where an operator is present
    and a clear error is the most useful thing to hand them.
    """
    candidate = (name or "").strip().lower()
    if not _POOL.match(candidate):
        raise ValueError(
            f"unusable node pool {name!r}: lower-case letters, digits, hyphen and "
            "underscore, starting with a letter, at most 50 characters"
        )
    return candidate

# States from which a placement may be released. FAILED is deliberately absent:
# a project can reach FAILED from any operational state, including ones after
# the database was created, so the status alone cannot tell you whether
# anything exists on the node. release_placement checks database_name too.
RELEASABLE_STATUSES = ("REQUESTED", "PLACEMENT_RESERVED")

def _now() -> datetime:
    return datetime.now(UTC)


# -- registry --------------------------------------------------------------


# What `node register` sets in `capacity_json`. The rest of that column is written
# by checks -- `extension_check`, `realtime_ready`, backup policy -- so registration
# accepts only these, and cannot overwrite a check's result.
OPERATOR_CAPACITY_KEYS = frozenset({"max_projects", "max_warm_projects", "min_free_disk_bytes"})


def register_node(
    conn: psycopg.Connection,
    *,
    name: str,
    hostname: str,
    internal_host: str,
    node_pool: str = "shared",
    capacity: dict[str, Any] | None = None,
) -> int:
    """Register a node, or update an existing one's addresses, pool and given capacity.

    An existing node keeps its status and every capacity key not given here. Re-running
    registration used to update only the addresses and pool, so a changed capacity flag
    was accepted and silently dropped.
    """
    capacity = capacity or {}
    unknown = sorted(set(capacity) - OPERATOR_CAPACITY_KEYS)
    if unknown:
        raise ValueError(f"not a capacity setting: {', '.join(unknown)}")
    # Normalised with the same rule `entitlements` applies to the plan's side of
    # this comparison. Without it the two halves can disagree in a way that is
    # invisible and total: an operator who registers `Production` and entitles a
    # plan to `Production` gets an entitlement lowercased to `production`, a node
    # that stayed `Production`, no match, and every project on that plan refused
    # with "no healthy node in pool". Fails closed, which is right, and gives no
    # hint that the two spellings are the problem.
    node_pool = checked_pool(node_pool)
    row = db.one(
        conn,
        """
        INSERT INTO nodes (name, hostname, internal_host, node_pool, status, capacity_json)
        VALUES (%s, %s, %s, %s, 'maintenance', %s)
        ON CONFLICT (name) DO UPDATE
            SET hostname = EXCLUDED.hostname,
                internal_host = EXCLUDED.internal_host,
                node_pool = EXCLUDED.node_pool,
                capacity_json = nodes.capacity_json || EXCLUDED.capacity_json
        RETURNING id
        """,
        (name, hostname, internal_host, node_pool, psycopg.types.json.Jsonb(capacity)),
    )
    # New nodes start in 'maintenance', not 'active': an operator confirms a
    # node is genuinely ready before customer projects land on it.
    return int(row["id"])


def set_status(conn: psycopg.Connection, *, name: str, status: str) -> None:
    if status not in ("active", "draining", "maintenance", "unhealthy"):
        raise ValueError(f"unknown node status {status!r}")
    if db.execute(conn, "UPDATE nodes SET status = %s WHERE name = %s", (status, name)) == 0:
        raise ValueError(f"no node named {name!r}")


def release_gateway_role(conn: psycopg.Connection, *, name: str) -> str | None:
    """Give up this node's claim on a gateway role, returning what it held.

    A lost node has no gateway, and `nodes.gateway_role` is UNIQUE because it is
    one node's identity (ADR-072). Left set, it makes the rebuilt node
    ungrantable: `cp-manage gateway grant --role gw --node <new>` refuses,
    correctly, because that role still serves a node -- and tells the operator
    to give the new node its own role, which is the wrong advice at the one
    moment they are following a disaster runbook. The identity moves with the
    tenants.
    """
    # Read then write, rather than `UPDATE ... RETURNING gateway_role`: that
    # returns the *new* value, which is the NULL we just set. `RETURNING OLD`
    # arrives in PostgreSQL 18 and this targets 17.
    row = db.one(conn, "SELECT gateway_role FROM nodes WHERE name = %s", (name,))
    if row is None:
        raise ValueError(f"no node named {name!r}")
    db.execute(conn, "UPDATE nodes SET gateway_role = NULL WHERE name = %s", (name,))
    return row["gateway_role"]


def record_health(conn: psycopg.Connection, *, name: str, metrics: dict[str, Any]) -> None:
    """Record a health report. Freshness is what gates placement.

    Merged into `metrics_json`, not written over it: the same column carries what
    `node realtime-check` and `node backup-check` recorded, and a report used to
    erase both. `report_node_health()` (migration 0050) merges the same way.
    """
    if db.execute(
        conn,
        "UPDATE nodes SET metrics_json = coalesce(metrics_json, '{}'::jsonb) || %s, last_health_at = now() "
        "WHERE name = %s",
        (psycopg.types.json.Jsonb(metrics), name),
    ) == 0:
        raise ValueError(f"no node named {name!r}")


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

    The pool is normalised here, at the one place the comparison is actually
    made, so every caller agrees regardless of how it spelled the name. Leniently
    -- lower-cased and stripped, never raising -- because both sides that produce
    a pool name have already validated it: `checked_pool` at registration and
    `entitlements` on the plan. A raise here would be a new way to refuse a
    customer on a request path, which is not what a spelling difference deserves.
    """
    node_pool = (node_pool or DEFAULT_POOL).strip().lower()
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
    node_pool = (node_pool or DEFAULT_POOL).strip().lower()
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


@dataclass(frozen=True)
class PoolReport:
    """What pools exist, what each plan is entitled to, and where the two disagree.

    ADR-065 makes the pool a plan entitlement, and ships every tier as `shared`
    so that upgrading changes nothing. The risk that creates is the opposite of
    an outage and just as real: a deployment can believe it has separated
    production from free and have done no such thing, because the mechanism is
    present and switched off. So this reports the *entitlement* alongside the
    pools that actually exist, and says plainly when a plan is entitled to share
    hardware with the free tier.
    """

    pools: dict[str, int]
    plan_pools: dict[str, str]
    # Projects sitting in a pool their plan no longer entitles them to -- a plan
    # change is the usual cause. Reported and never moved: moving a tenant is
    # slice 7 and ADR-066 makes it operator-initiated.
    misplaced: list[dict]

    @property
    def entitled_pools(self) -> set[str]:
        return set(self.plan_pools.values())

    def problems(self) -> list[str]:
        issues = []
        for pool in sorted(self.entitled_pools - set(self.pools)):
            plans = sorted(c for c, p in self.plan_pools.items() if p == pool)
            issues.append(
                f"pool {pool!r} has no node, and {', '.join(plans)} is entitled to it. "
                "A project on that plan cannot be placed at all -- creating one answers "
                "503 rather than landing somewhere else, which is deliberate (ADR-065): "
                "a control that quietly placed it beside the free tier would be worse "
                "than a refusal"
            )
        if self.misplaced:
            issues.append(
                f"{len(self.misplaced)} project(s) are on a node whose pool their plan no "
                "longer entitles them to; a plan change does not move a tenant, and moving "
                "one is a separate operation"
            )
        return issues

    def notes(self) -> list[str]:
        notes = []
        sharing = sorted(c for c, p in self.plan_pools.items() if p == DEFAULT_POOL)
        if len(sharing) == len(self.plan_pools) and len(sharing) > 1:
            notes.append(
                f"every plan is entitled to the {DEFAULT_POOL!r} pool, so no separation is "
                "in effect: a production project and a free project can land on the same "
                "node. That is ADR-065's shipped default and not a fault -- set `node_pool` "
                "in a plan's config_json and register nodes in that pool to separate them"
            )
        elif sharing:
            notes.append(
                f"{', '.join(sharing)} share the {DEFAULT_POOL!r} pool with each other"
            )
        return notes


def pool_report(conn: psycopg.Connection) -> PoolReport:
    """Pools as configured, pools as entitled, and the projects between them."""
    from services.control_plane import entitlements

    pools: dict[str, int] = {
        row["node_pool"]: row["n"]
        for row in db.query(
            conn,
            "SELECT node_pool, count(*) AS n FROM nodes WHERE status <> 'retired' "
            " GROUP BY node_pool ORDER BY node_pool",
        )
    }
    plan_pools = {
        row["code"]: entitlements.resolve(row["code"], row["config_json"]).node_pool
        for row in db.query(conn, "SELECT code, config_json FROM plans WHERE is_active")
    }
    misplaced = db.query(
        conn,
        """
        SELECT pr.project_ref, p.code AS plan_code, n.name AS node_name, n.node_pool
          FROM projects pr
          JOIN nodes n ON n.id = pr.node_id
          LEFT JOIN plans p ON p.id = pr.plan_id
         WHERE pr.deleted_at IS NULL AND pr.node_id IS NOT NULL
         ORDER BY pr.project_ref
        """,
    )
    wrong = [
        row
        for row in misplaced
        if plan_pools.get(row["plan_code"], DEFAULT_POOL) != row["node_pool"]
    ]
    return PoolReport(pools=pools, plan_pools=plan_pools, misplaced=wrong)


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
