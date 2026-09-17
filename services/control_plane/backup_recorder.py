"""A node's backup recorder role, and the permission model applied to it (ADR-086).

pgBackRest runs on the node, as the cluster's owner. Phase 11 recorded what it did through
the control plane's own role, from the same process -- which only works where the control
plane's database and the data directory share a host. On the two-machine deployment the node
records through this role instead: a login role mapped to one node (`nodes.backup_recorder_role`)
that may execute three functions (migration 0058) and holds no table privilege. Which node is
decided inside the database from the login role, as for the health reporter (ADR-080).

This module is the control plane's half: what the role is granted, which roles must not
become a recorder, and the catalogue check that proves the grant is no wider than the model.
The node's half -- running pgBackRest and calling the functions -- is slice 7b.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

from services.control_plane import admin_grants, db, memory_worker_grants, node_reporter

FUNCTIONS = (
    "public.start_node_backup(text)",
    "public.finish_node_backup(bigint, text, text, bigint, bigint, text, text, text)",
    "public.record_node_backup_check(jsonb)",
)


def statements(role: str) -> list[sql.Composed]:
    """Everything a recorder is granted: the schema, and the three functions."""
    ident = sql.Identifier(role)
    return [sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident)] + [
        sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(function), ident) for function in FUNCTIONS
    ]


def refusal(conn: psycopg.Connection, *, role: str, node: str) -> str | None:
    """Why this role must not record backups for this node, or None. Expects `dict_row` (the pool's).

    Every other narrowed role on the platform is refused, and the reason is the same each time:
    a role holding two models holds their union. A gateway that could also record backups could
    mark its own node backed up; a reporter that could would make the narrowest role on the
    platform twice as wide as it was reviewed as.
    """
    row = db.one(conn, "SELECT rolsuper, rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = %s", (role,))
    if row is None:
        return f"no role named {role!r}; create it first as a superuser: CREATE ROLE {role} LOGIN PASSWORD '<strong>'"
    if row["rolsuper"]:
        return f"role {role!r} is a superuser; a recorder must hold nothing but the three functions"
    if not row["rolcanlogin"]:
        return f"role {role!r} cannot log in; the recorder connects as it, and identity is the login (session_user)"
    if db.one(conn, "SELECT pg_catalog.pg_has_role(%s, c.relowner, 'MEMBER') AS yes FROM pg_catalog.pg_class c "
                    "WHERE c.oid = 'public.nodes'::regclass", (role,))["yes"]:
        return f"role {role!r} owns the control plane's tables (or is a member of their owner)"
    gateway = db.one(conn, "SELECT name FROM nodes WHERE gateway_role = %s", (role,))
    if gateway is not None:
        return f"role {role!r} is the gateway role of node {gateway['name']!r}; give the recorder its own role"
    reporter = db.one(conn, "SELECT name FROM nodes WHERE health_reporter_role = %s", (role,))
    if reporter is not None:
        return f"role {role!r} is the health reporter of node {reporter['name']!r}; give the recorder its own role"
    groups = [memory_worker_grants.GROUP_ROLE, memory_worker_grants.EMBEDDER_GROUP_ROLE, admin_grants.GROUP_ROLE]
    member = db.one(conn, "SELECT g.rolname FROM pg_catalog.pg_roles g WHERE g.rolname = ANY(%s) "
                          "AND pg_catalog.pg_has_role(%s, g.oid, 'MEMBER') LIMIT 1", (groups, role))
    if member is not None:
        return f"role {role!r} is a member of {member['rolname']}; give the recorder its own role"
    clash = db.one(conn, "SELECT name FROM nodes WHERE backup_recorder_role = %s AND name <> %s", (role, node))
    if clash is not None:
        return f"role {role!r} already records backups for node {clash['name']!r}; one role per node (ADR-086)"
    return None


def wider_than_the_model(conn: psycopg.Connection, role: str) -> list[str]:
    """Every privilege on a control-plane table or view this role holds. The model grants none."""
    return node_reporter.wider_than_the_model(conn, role)


def missing_functions(conn: psycopg.Connection, role: str) -> list[str]:
    """The functions of the model this role cannot execute."""
    return [
        function for function in FUNCTIONS
        if not db.one(conn, "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE') AS yes",
                      (role, function))["yes"]
    ]


__all__ = ["FUNCTIONS", "missing_functions", "refusal", "statements", "wider_than_the_model"]
