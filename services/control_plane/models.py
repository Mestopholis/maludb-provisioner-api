"""Core domain models and repository functions.

Phase 01 scope: plans, nodes, projects. Identity models land in slice 2.

Dataclasses for shape plus module-level repository functions taking an explicit
connection -- no ORM, no session magic, no implicit global state (ADR-024).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from services.control_plane import db

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
