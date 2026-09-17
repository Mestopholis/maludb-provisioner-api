"""Core domain models and repository functions.

Phase 01 scope: plans, nodes, projects. Identity models land in slice 2.

Dataclasses for shape plus module-level repository functions taking an explicit
connection -- no ORM, no session magic, no implicit global state (ADR-024).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from services.control_plane import db

log = logging.getLogger("maludb.models")

# Project reference character set. docs/TENANCY.md requires a strict set
# suitable for safe generated SQL identifiers; docs/ARCHITECTURE.md warns that
# unvalidated project names must never become identifiers.
PROJECT_REF_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
PROJECT_REF_LENGTH = 8


@dataclass(frozen=True)
class Plan:
    id: int
    code: str
    name: str
    is_active: bool
    config: dict[str, Any]


@dataclass(frozen=True)
class Node:
    id: int
    name: str
    hostname: str
    internal_host: str
    node_pool: str
    status: str
    capacity: dict[str, Any]
    metrics: dict[str, Any]
    last_health_at: datetime | None


@dataclass(frozen=True)
class Project:
    id: uuid.UUID
    org_id: uuid.UUID
    project_ref: str
    display_name: str
    plan_id: int
    node_id: int | None
    database_name: str | None
    status: str
    created_at: datetime


def is_valid_project_ref(value: str) -> bool:
    """Project refs are untrusted input until validated (AGENTS.md)."""
    return (
        isinstance(value, str)
        and len(value) == PROJECT_REF_LENGTH
        and all(character in PROJECT_REF_ALPHABET for character in value)
    )


def generate_project_ref() -> str:
    """Cryptographically generated, per AGENTS.md."""
    import secrets

    return "".join(secrets.choice(PROJECT_REF_ALPHABET) for _ in range(PROJECT_REF_LENGTH))


def database_name_for(project_ref: str) -> str:
    if not is_valid_project_ref(project_ref):
        raise ValueError(f"refusing to build a database name from an invalid project_ref: {project_ref!r}")
    return f"mldb_{project_ref}"


# --------------------------------------------------------------------------
# Repositories
# --------------------------------------------------------------------------


def list_plans(conn: psycopg.Connection, *, active_only: bool = True) -> list[Plan]:
    sql = "SELECT id, code, name, is_active, config_json FROM plans"
    params: tuple = ()
    if active_only:
        sql += " WHERE is_active = TRUE"
    sql += " ORDER BY id"
    return [
        Plan(id=r["id"], code=r["code"], name=r["name"], is_active=r["is_active"], config=r["config_json"])
        for r in db.query(conn, sql, params)
    ]


def get_plan_by_code(conn: psycopg.Connection, code: str) -> Plan | None:
    row = db.one(conn, "SELECT id, code, name, is_active, config_json FROM plans WHERE code = %s", (code,))
    if row is None:
        return None
    return Plan(id=row["id"], code=row["code"], name=row["name"], is_active=row["is_active"], config=row["config_json"])


def list_nodes(conn: psycopg.Connection, *, status: str | None = None) -> list[Node]:
    sql = """
        SELECT id, name, hostname, internal_host, node_pool, status,
               capacity_json, metrics_json, last_health_at
          FROM nodes
    """
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = %s"
        params = (status,)
    sql += " ORDER BY name"
    return [
        Node(
            id=r["id"],
            name=r["name"],
            hostname=r["hostname"],
            internal_host=r["internal_host"],
            node_pool=r["node_pool"],
            status=r["status"],
            capacity=r["capacity_json"],
            metrics=r["metrics_json"],
            last_health_at=r["last_health_at"],
        )
        for r in db.query(conn, sql, params)
    ]


# Column lists are written out in full rather than interpolated from a
# constant. The constant would be safe, but f-strings in SQL are a pattern this
# codebase should not normalise -- AGENTS.md makes SQL injection through
# generated identifiers a primary review concern, and Phase 02 will generate
# real identifiers from tenant metadata.


def _project(row: dict[str, Any]) -> Project:
    return Project(
        id=row["id"],
        org_id=row["org_id"],
        project_ref=row["project_ref"],
        display_name=row["display_name"],
        plan_id=row["plan_id"],
        node_id=row["node_id"],
        database_name=row["database_name"],
        status=row["status"],
        created_at=row["created_at"],
    )


def get_project_by_ref(conn: psycopg.Connection, project_ref: str) -> Project | None:
    # Validate before querying: a malformed ref is a client error, not a lookup.
    if not is_valid_project_ref(project_ref):
        return None
    row = db.one(
        conn,
        """
        SELECT id, org_id, project_ref, display_name, plan_id, node_id,
               database_name, status, created_at
          FROM projects
         WHERE project_ref = %s
        """,
        (project_ref,),
    )
    return _project(row) if row else None


def get_project(conn: psycopg.Connection, project_id: uuid.UUID) -> Project:
    """By id, for a caller that has just created or claimed one.

    The column list is spelled out rather than interpolated from a constant.
    Interpolating it is safe here and the linter cannot know that; a rule about
    building SQL from strings is one this repository wants loud rather than
    suppressed, since generated identifiers are a named review concern.
    """
    row = db.one(
        conn,
        """
        SELECT id, org_id, project_ref, display_name, plan_id, node_id,
               database_name, status, created_at
          FROM projects
         WHERE id = %s
        """,
        (project_id,),
    )
    if row is None:
        raise LookupError(f"no project with id {project_id}")
    return _project(row)


def project_by_idempotency_key(
    conn: psycopg.Connection, *, org_id: uuid.UUID, key: str
) -> Project | None:
    """The project a previous identical request created, if there was one."""
    row = db.one(
        conn,
        """
        SELECT id, org_id, project_ref, display_name, plan_id, node_id,
               database_name, status, created_at
          FROM projects
         WHERE org_id = %s AND idempotency_key = %s AND deleted_at IS NULL
        """,
        (org_id, key),
    )
    return _project(row) if row else None


def plan_by_code(conn: psycopg.Connection, code: str) -> Plan | None:
    row = db.one(
        conn,
        "SELECT id, code, name, is_active, config_json FROM plans "
        "WHERE code = %s AND is_active = TRUE",
        (code,),
    )
    if row is None:
        return None
    return Plan(id=row["id"], code=row["code"], name=row["name"],
                is_active=row["is_active"], config=row["config_json"])


def default_plan(conn: psycopg.Connection) -> Plan | None:
    """What a project gets when the caller names no plan.

    The free tier, by code, rather than "the first row": which plan a customer
    lands on unasked is a product decision and must not depend on insertion
    order. A deployment without a plan called `free` gets None and the caller
    turns it into an error, which is better than silently placing someone on
    whatever plan happens to sort first -- that could be the most expensive one.
    """
    return plan_by_code(conn, "free")


def count_projects_for_org(conn: psycopg.Connection, org_id: uuid.UUID) -> int:
    """Live projects an organization holds.

    Deleted ones do not count: a customer who created two, deleted one and
    cannot create another has been charged for a mistake they already corrected.
    """
    row = db.one(
        conn,
        "SELECT count(*) AS n FROM projects WHERE org_id = %s AND deleted_at IS NULL",
        (org_id,),
    )
    return row["n"]


def create_project(
    conn: psycopg.Connection,
    *,
    org_id: uuid.UUID,
    display_name: str,
    plan_id: int,
    requested_by: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    """Insert a REQUESTED project with a fresh reference.

    The reference is generated here and never taken from the caller. It becomes
    a database name, four role names and a hostname, so a customer-supplied one
    would be untrusted input reaching an SQL identifier and a DNS label at once
    -- `AGENTS.md` requires identifiers generated from project metadata to be
    validated or safely quoted, and the cheapest way to satisfy that is for the
    customer never to choose it.

    Collisions are retried rather than raised: 36^8 references make one
    unlikely, and "unlikely" is not "impossible" at the scale a free tier
    invites. A caller seeing a random failure they cannot act on is worse than
    one extra INSERT.
    """
    for _ in range(5):
        ref = generate_project_ref()
        # The identifier is generated here rather than by the database: the
        # table has no default, because every other caller of it so far has
        # been code that already had one.
        project_id = uuid.uuid4()
        try:
            with conn.transaction():
                db.execute(
                    conn,
                    """
                    INSERT INTO projects
                        (id, org_id, project_ref, display_name, plan_id, status,
                         requested_by, requested_at, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, 'REQUESTED', %s, now(), %s)
                    """,
                    (project_id, org_id, ref, display_name, plan_id, requested_by,
                     idempotency_key),
                )
            return project_id
        except psycopg.errors.UniqueViolation as exc:
            # A reference collision is retried; a repeated idempotency key is
            # the caller's own replay racing itself, and must not be turned
            # into a new project by a retry loop.
            if idempotency_key and "idempotency" in str(exc):
                raise
            continue
    raise RuntimeError("could not allocate an unused project reference")


def list_projects_for_org(conn: psycopg.Connection, org_id: uuid.UUID) -> list[Project]:
    rows = db.query(
        conn,
        """
        SELECT id, org_id, project_ref, display_name, plan_id, node_id,
               database_name, status, created_at
          FROM projects
         WHERE org_id = %s AND deleted_at IS NULL
         ORDER BY created_at
        """,
        (org_id,),
    )
    return [_project(r) for r in rows]


# -- deleting a project (free slice 10b) -----------------------------------
#
# The *request* lives here rather than in `jobs`, and that placement is ADR-038: the public
# application may not import node-side machinery, and `tests/test_control_plane_surfaces.py`
# enforces it. Marking a project and revoking its keys is control-plane work -- a status and
# some rows -- while destroying the tenant is the node superuser's, in the provisioner.


class DeletionRefused(RuntimeError):
    """The project cannot be marked for deletion; the message is written to be read by a customer."""


# What a project may be deleted from. One still being provisioned is refused: its own run would
# carry on against a database that had gone. It becomes deletable when it settles.
DELETABLE_STATES = (
    "REQUESTED", "PLACEMENT_RESERVED", "PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED",
    "FAILED", "RETRY_WAIT",
)

def request_deletion(
    conn: psycopg.Connection, *, project_id: uuid.UUID, requested_by: uuid.UUID | None
) -> bool:
    """Mark a project for deletion and stop it serving. False if it was already marked.

    Two things happen here rather than in the worker, because both must be true the moment the
    customer is answered: the status leaves `SERVING_STATUSES`, so the gateway stops routing to it,
    and every API key is revoked, so a key already in a client's hands stops working. The
    destruction itself is the worker's, on the node, with the superuser credential ADR-038 keeps
    off this path.
    """
    project = db.one(
        conn,
        "SELECT project_ref, status, delete_requested_at FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None:
        raise DeletionRefused("project does not exist")
    if project["delete_requested_at"] is not None:
        return False
    if project["status"] not in DELETABLE_STATES:
        raise DeletionRefused(
            f"refusing to delete a project in {project['status']}; it is mid-flight, and deletion "
            "waits for it to reach a settled state"
        )
    db.execute(
        conn,
        "UPDATE projects SET status = 'DELETING', delete_requested_at = now(), delete_requested_by = %s "
        " WHERE id = %s",
        (requested_by, project_id),
    )
    revoked = db.execute(
        conn,
        "UPDATE api_keys SET revoked_at = now() WHERE project_id = %s AND revoked_at IS NULL",
        (project_id,),
    )
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, %s, %s, 'project.delete_requested', %s)",
        (project_id, "user" if requested_by else "operator", requested_by,
         psycopg.types.json.Jsonb({"keys_revoked": revoked, "from_status": project["status"]})),
    )
    conn.commit()
    log.info("project %s marked for deletion (%d key(s) revoked)", project["project_ref"], revoked)
    return True
