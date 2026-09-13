"""MaluDB-native features for a project: the data-model graph (ADR-074).

Phase 12 slice 2 turns the surface on; slice 3 gives a customer something to
read; slice 4 adds the route they call to ask for a refresh.

## What "enabled" means, and what it does not

`maludb_core` is installed in every tenant database whether or not anything here
runs (ADR-015). Enabling builds the extension's memory-schema facades into one
fixed, **platform-owned** schema, `maludb_memory`, and records that the project
has them. ADR-074 amended ADR-015 to allow exactly this flag: the extension is
platform, the surface on top of it is opt-in.

**The facades are never exposed; a copy of what they produce is.** They are
`SECURITY DEFINER`, owned by the node superuser, and Phase 12 slice 0 found them
closed to every customer role by the extension's own ACLs -- checked again on
every enablement rather than trusted, because an upstream release could change
it. What a customer reads is ADR-074's amended decision 3: the platform runs the
refresh over its own connection and copies the result into ordinary tables in a
second platform-owned schema, `maludb`, which PostgREST serves to `service_role`
only.

## How `maludb` reaches PostgREST

Through PostgREST's **in-database configuration**: `pgrst.db_schemas` set on the
project's authenticator role, in the project's database, then `NOTIFY pgrst,
'reload config'` and `'reload schema'`. Measured in slice 3 rather than assumed:
it overrides the rendered config file, survives a PostgREST restart with that
file unchanged, applies in 0.30 s, and is withdrawn as fast by a `RESET`. No
customer role can set it -- `ALTER ROLE` on the authenticator and `ALTER
DATABASE` both refused from the SQL console's roles.

The alternative the plan first named, rewriting the worker's config file, does
not work from here: that file is written on the node by the gateway when it
wakes a worker, and a running worker would keep the old schema list until it
next slept.

## Why this is an operator command in this slice

Enabling runs as the node superuser. ADR-038 forbids the internet-facing
application from holding node credentials, so a customer request can only
*enqueue* this for a worker -- and the queue arrives in slice 4, beside the
refresh route that needs it. Until then this follows Realtime, whose
enablement has always been `cp-manage`.

## The squatting refusal

The tenant admin holds `CREATE ON DATABASE` (bootstrap 010), so a customer can
create a schema called `maludb_memory` before the platform does. Running
`enable_memory_schema` into it would put superuser-owned `SECURITY DEFINER`
functions inside a schema the customer owns. Enabling refuses, and says what the
customer can do about it -- otherwise the refusal is a feature that silently
will not turn on.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements, provisioning, workers

log = logging.getLogger("maludb.maludb")

# ADR-074 decision 2's fixed, platform-owned schema.
MEMORY_SCHEMA = "maludb_memory"

# Where the copy a customer reads lives, and the name PostgREST serves it under.
COPY_SCHEMA = "maludb"
COPY_TABLES = ("datamodel_relations", "datamodel_nodes", "datamodel_edges")

# pg_class.relkind -> what a customer calls it.
_RELATION_KINDS = {
    "r": "table", "p": "partitioned table", "v": "view",
    "m": "materialized view", "f": "foreign table",
}

# The facades that make up the data-model graph. They arrived in 0.104.0; a
# schema enabled at an older version has none to find.
DATAMODEL_FACADES = ("maludb_datamodel_describe", "maludb_datamodel_refresh")
DATAMODEL_SINCE = (0, 104, 0)

# States in which a project's database exists and nothing else is changing it.
ENABLEABLE_STATUSES = ("PROVISIONED", "ACTIVE")

# Wider than enabling, on purpose. Turning the surface off has to work for a
# project that is paused or suspended -- that is exactly when an operator may
# want its structure off the Data API -- and only needs the database to exist.
DISABLEABLE_STATUSES = ("PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED")

# One advisory-lock key per node, shared with `extension_upgrade`. An upgrade
# takes it exclusively; an enablement takes it shared. So enablements on
# different projects do not wait on each other, and neither runs while an
# upgrade is re-enabling schemas on the same node -- which could otherwise read
# the memory schema as absent a moment before this commits it, and strand it on
# the old facades.
NODE_LOCK_NAMESPACE = 0x4D455855  # "MEXU"

AUDIT_ENABLED = "maludb.datamodel.enabled"
AUDIT_DISABLED = "maludb.datamodel.disabled"


class MaludbError(RuntimeError):
    """The data-model graph could not be enabled, and nothing was left half-built."""


@dataclass
class CopyResult:
    relations: int
    nodes: int
    edges: int


@dataclass
class Enablement:
    project_ref: str
    changed: bool
    memory_schema_version: str
    detail: str
    copy: CopyResult | None = None


def version_tuple(text: str) -> tuple[int, ...]:
    """`0.104.0` as a comparable tuple. Unparseable parts compare as zero."""
    return tuple(int(part) if part.isdigit() else 0 for part in text.split("."))


def schema_owner(tenant_conn: psycopg.Connection, schema: str) -> tuple[bool, str] | None:
    """Whether a schema exists, and whether a superuser owns it.

    None when there is no such schema; otherwise (owned_by_superuser, owner).
    Customer roles are never superusers (docs/MALUDB.md), which is what makes
    `rolsuper` the right test for "the platform made this".
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolsuper, r.rolname FROM pg_namespace n "
            "JOIN pg_roles r ON r.oid = n.nspowner WHERE n.nspname = %s",
            (schema,),
        )
        row = cur.fetchone()
    return None if row is None else (bool(row[0]), row[1])


def memory_schema_owner(tenant_conn: psycopg.Connection) -> tuple[bool, str] | None:
    return schema_owner(tenant_conn, MEMORY_SCHEMA)


def _refuse_squatted(tenant_conn: psycopg.Connection, schema: str) -> bool:
    """Refuse a customer-owned schema of a platform name; return whether it exists."""
    owner = schema_owner(tenant_conn, schema)
    if owner is not None and not owner[0]:
        raise MaludbError(
            f"a schema named {schema} already exists in this project and belongs to "
            f"{owner[1]}, not the platform. The data-model graph is built into that name, and "
            "building it into a schema the project owns would put platform objects where the "
            f"project can change them. Rename or drop the project's {schema} schema, "
            "then enable again."
        )
    return owner is not None


def datamodel_facades_present(tenant_conn: psycopg.Connection) -> int:
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s AND p.proname = ANY(%s)",
            (MEMORY_SCHEMA, list(DATAMODEL_FACADES)),
        )
        return cur.fetchone()[0]


def customer_roles(names) -> tuple[str, ...]:
    """Every role a customer can act as, directly or by `SET ROLE`.

    The authenticator reaches the three shared API roles; the executor and the
    client reach the project admin (Phase 12 slice 0 walked the memberships).
    """
    return ("anon", "authenticated", "service_role", names.authenticator, names.admin,
            names.executor, names.client)


def _project(conn: psycopg.Connection, project_id: uuid.UUID) -> dict:
    project = db.one(
        conn,
        """
        SELECT id, project_ref, node_id, database_name, status,
               maludb_datamodel_enabled, maludb_datamodel_enabled_at,
               maludb_memory_schema_version, maludb_vectors_enabled
          FROM projects WHERE id = %s AND deleted_at IS NULL
        """,
        (project_id,),
    )
    if project is None:
        raise MaludbError("project does not exist")
    if project["database_name"] is None or project["node_id"] is None:
        raise MaludbError("project has no database yet; provision it before enabling this")
    return project


def enable(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> Enablement:
    """Turn the data-model graph on for one project.

    Cheapest refusal first, and nothing in the tenant database until every
    refusal that does not need it has passed. The tenant work is one transaction
    and the control-plane record is written only after it commits, so a failure
    at any point leaves either nothing built or a built schema with no record --
    and a re-run finishes the second, which is what "safely retryable" has to
    mean (AGENTS.md).
    """
    project = _project(conn, project_id)
    if project["status"] not in ENABLEABLE_STATUSES:
        raise MaludbError(
            f"project is {project['status']}; enable it once that operation has finished"
        )
    allowed = entitlements.for_project(conn, project_id)
    if not allowed.maludb_datamodel:
        raise MaludbError(
            "this project's plan does not include the data-model graph "
            "(maludb_datamodel is false). Change the plan rather than enabling it here."
        )

    locked = db.one(
        conn, "SELECT pg_try_advisory_lock_shared(%s, %s) AS ok",
        (NODE_LOCK_NAMESPACE, project["node_id"]),
    )["ok"]
    conn.commit()
    if not locked:
        raise MaludbError(
            "an extension upgrade is running on this project's node; enable it once that finishes"
        )

    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        tenant_conn = tenant_connect(project["database_name"])
        try:
            tenant_conn.autocommit = False
            version, copied = _build(tenant_conn, names)
            tenant_conn.commit()
        except Exception:
            tenant_conn.rollback()
            raise
        finally:
            tenant_conn.close()

        was_enabled = bool(project["maludb_datamodel_enabled"])
        db.execute(
            conn,
            """
            UPDATE projects
               SET maludb_datamodel_enabled = TRUE,
                   maludb_datamodel_enabled_at = coalesce(maludb_datamodel_enabled_at, now()),
                   maludb_memory_schema_version = %s
             WHERE id = %s
            """,
            (version, project_id),
        )
        if not was_enabled:
            db.execute(
                conn,
                "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
                "VALUES (%s, 'system', %s, %s)",
                (project_id, AUDIT_ENABLED, Jsonb({"memory_schema_version": version})),
            )
        conn.commit()
    finally:
        db.one(conn, "SELECT pg_advisory_unlock_shared(%s, %s) AS ok",
               (NODE_LOCK_NAMESPACE, project["node_id"]))
        conn.commit()

    return Enablement(
        project_ref=project["project_ref"],
        changed=not was_enabled,
        memory_schema_version=version,
        detail="enabled" if not was_enabled else "already enabled",
        copy=copied,
    )


def _build(tenant_conn: psycopg.Connection, names) -> tuple[str, CopyResult]:
    """Build the memory schema, the copy and its exposure, in the caller's transaction."""
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'maludb_core'")
        row = cur.fetchone()
    if row is None:
        raise MaludbError("maludb_core is not installed in this tenant database (ADR-015)")
    if version_tuple(row[0]) < DATAMODEL_SINCE:
        raise MaludbError(
            f"this tenant has maludb_core {row[0]}; the data-model graph needs "
            f"{'.'.join(map(str, DATAMODEL_SINCE))} or later. Run `cp-manage extension "
            "upgrade` for its node first"
        )

    # Both platform names, before either is built into: a refusal after the
    # memory schema is enabled would still roll back, but it would have run
    # superuser code for nothing.
    memory_exists = _refuse_squatted(tenant_conn, MEMORY_SCHEMA)
    _refuse_squatted(tenant_conn, COPY_SCHEMA)
    if not memory_exists:
        # Created here, over the superuser connection, so the platform owns it --
        # which is the property the check above exists to protect.
        tenant_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(MEMORY_SCHEMA)))

    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT enabled_version FROM maludb_core.enable_memory_schema(%s)", (MEMORY_SCHEMA,)
        )
        version = cur.fetchone()[0]

    present = datamodel_facades_present(tenant_conn)
    if present < len(DATAMODEL_FACADES):
        raise MaludbError(
            f"{MEMORY_SCHEMA} was enabled but has {present} of {len(DATAMODEL_FACADES)} "
            "data-model facades"
        )

    _ensure_copy_schema(tenant_conn)
    copied = copy_graph(tenant_conn)
    _expose(tenant_conn, names)
    _assert_reach(tenant_conn, names)
    return version, copied


def _ensure_copy_schema(tenant_conn: psycopg.Connection) -> None:
    """The copy's schema and tables, platform-owned, readable by `service_role` alone.

    Idempotent, and the grants are re-stated every time rather than only on
    creation: they are the control, and a re-run is how a drifted one is put
    back.
    """
    schema = sql.Identifier(COPY_SCHEMA)
    tenant_conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    # Re-checked after the create, not only before it. `IF NOT EXISTS` is silent
    # when the schema appeared in between -- a customer creating `maludb` from the
    # SQL console in that window would otherwise have the platform build and
    # expose its copy inside a schema the customer owns. Inside the transaction,
    # so the refusal rolls everything back.
    owner = schema_owner(tenant_conn, COPY_SCHEMA)
    if owner is None or not owner[0]:
        raise MaludbError(
            f"{COPY_SCHEMA} is not owned by the platform after creating it"
            + (f" (it belongs to {owner[1]})" if owner else "")
            + "; refusing to build the copy into it"
        )
    tenant_conn.execute(sql.SQL(
        """
        CREATE TABLE IF NOT EXISTS {s}.datamodel_nodes (
            node_id       BIGINT PRIMARY KEY,
            node_type     TEXT NOT NULL,
            name          TEXT NOT NULL,
            refreshed_at  TIMESTAMPTZ NOT NULL
        )
        """).format(s=schema))
    tenant_conn.execute(sql.SQL(
        """
        CREATE TABLE IF NOT EXISTS {s}.datamodel_edges (
            source_node_id  BIGINT NOT NULL REFERENCES {s}.datamodel_nodes (node_id) ON DELETE CASCADE,
            relationship    TEXT NOT NULL,
            target_node_id  BIGINT NOT NULL REFERENCES {s}.datamodel_nodes (node_id) ON DELETE CASCADE,
            provenance      TEXT,
            refreshed_at    TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (source_node_id, relationship, target_node_id)
        )
        """).format(s=schema))
    tenant_conn.execute(sql.SQL(
        """
        CREATE TABLE IF NOT EXISTS {s}.datamodel_relations (
            schema_name    TEXT NOT NULL,
            relation_name  TEXT NOT NULL,
            kind           TEXT NOT NULL,
            description    JSONB NOT NULL,
            refreshed_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (schema_name, relation_name)
        )
        """).format(s=schema))
    for table in COPY_TABLES:
        tenant_conn.execute(sql.SQL("COMMENT ON TABLE {s}.{t} IS {c}").format(
            s=schema, t=sql.Identifier(table),
            c=sql.Literal(
                "MaluDB data-model graph, as of refreshed_at. A copy made by the platform "
                "(ADR-074); it does not change until the next refresh."
            ),
        ))
    tenant_conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema))
    tenant_conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(schema))
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO service_role").format(schema))
    tenant_conn.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO service_role").format(schema)
    )


def copy_graph(tenant_conn: psycopg.Connection) -> CopyResult:
    """Refresh the graph and replace the copy, inside the caller's transaction.

    Replace, not merge: the old rows are deleted and the new ones inserted in one
    transaction, so a reader sees one refresh or the next and never half of each.
    `DELETE` rather than `TRUNCATE`, because `TRUNCATE` takes an exclusive lock
    that would stall a customer's read for the length of the refresh.

    **What is left out, and why.** The refresh introspects all of `public`, and
    ADR-018 leaves `maludb_core`'s 373 functions there -- in a tenant with three
    tables of its own, 334 of 338 nodes were the extension's. A routine node is
    dropped only when *every* function of that name in its schema belongs to an
    extension, so a customer function that shares a name with one of the
    extension's is kept. Relations that belong to an extension are left out the
    same way. Routines are named without signatures by the extension, which is
    why the test is by name.

    Statements, never stored views or functions over the facades: re-running
    `enable_memory_schema` on an extension upgrade drops and recreates its
    objects, and a tracked dependency on them would be cascade-dropped with them.
    """
    prefix = "datamodel/"
    tenant_conn.execute(
        "SELECT maludb_memory.maludb_datamodel_refresh('datamodel', ARRAY['public']::name[])"
    )
    tenant_conn.execute("DELETE FROM maludb.datamodel_edges")
    tenant_conn.execute("DELETE FROM maludb.datamodel_nodes")
    tenant_conn.execute("DELETE FROM maludb.datamodel_relations")

    tenant_conn.execute(
        """
        INSERT INTO maludb.datamodel_nodes (node_id, node_type, name, refreshed_at)
        SELECT s.subject_id, s.subject_type, substr(s.canonical_name, %(start)s), now()
          FROM maludb_memory.maludb_subject s
         WHERE left(s.canonical_name, %(len)s) = %(prefix)s
           AND s.subject_type <> 'graph_namespace'
           AND s.archived_at IS NULL
           AND NOT (
                 s.subject_type = 'db_routine'
             AND substr(s.canonical_name, %(start)s) IN (
                   SELECT n.nspname || '.' || p.proname
                     FROM pg_catalog.pg_proc p
                     JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                     LEFT JOIN pg_catalog.pg_depend d
                            ON d.classid = 'pg_catalog.pg_proc'::regclass
                           AND d.objid = p.oid AND d.deptype = 'e'
                    GROUP BY n.nspname, p.proname
                   HAVING bool_and(d.objid IS NOT NULL)))
        """,
        {"start": len(prefix) + 1, "len": len(prefix), "prefix": prefix},
    )
    tenant_conn.execute(
        """
        INSERT INTO maludb.datamodel_edges
               (source_node_id, relationship, target_node_id, provenance, refreshed_at)
        SELECT DISTINCT ON (e.source_id, e.rel, e.target_id)
               e.source_id, e.rel, e.target_id, e.provenance, now()
          FROM maludb_memory.maludb_edge e
         WHERE e.source_kind = 'subject' AND e.target_kind = 'subject'
           AND e.source_id IN (SELECT node_id FROM maludb.datamodel_nodes)
           AND e.target_id IN (SELECT node_id FROM maludb.datamodel_nodes)
         ORDER BY e.source_id, e.rel, e.target_id, e.provenance
        """
    )
    kind = " ".join(
        f"WHEN '{code}' THEN '{name}'" for code, name in _RELATION_KINDS.items()
    )
    tenant_conn.execute(
        f"""
        INSERT INTO maludb.datamodel_relations
               (schema_name, relation_name, kind, description, refreshed_at)
        SELECT n.nspname, c.relname, CASE c.relkind {kind} END,
               maludb_memory.maludb_datamodel_describe(format('%%I.%%I', n.nspname, c.relname)),
               now()
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = ANY(%s)
           AND NOT EXISTS (
                 SELECT 1 FROM pg_catalog.pg_depend d
                  WHERE d.classid = 'pg_catalog.pg_class'::regclass
                    AND d.objid = c.oid AND d.deptype = 'e')
        """,  # noqa: S608 - `kind` is built from the module's own constant table
        (list(_RELATION_KINDS),),
    )

    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM maludb.datamodel_relations), "
            "       (SELECT count(*) FROM maludb.datamodel_nodes), "
            "       (SELECT count(*) FROM maludb.datamodel_edges)"
        )
        relations, nodes, edges = cur.fetchone()
    return CopyResult(relations=relations, nodes=nodes, edges=edges)


def _expose(tenant_conn: psycopg.Connection, names) -> None:
    """Add `maludb` to what this project's PostgREST serves, in the database.

    `pgrst.db_schemas` on the authenticator, in this database only, overrides the
    rendered config file and survives a PostgREST restart. Set inside the
    enablement's transaction, and both notifications are delivered at commit --
    so a failed enablement exposes nothing, and a successful one takes effect
    without waiting for a worker to sleep and wake.
    """
    schemas = f"{workers.DEFAULT_EXPOSED_SCHEMA}, {COPY_SCHEMA}"
    tenant_conn.execute(
        sql.SQL("ALTER ROLE {role} IN DATABASE {database} SET pgrst.db_schemas = {value}").format(
            role=sql.Identifier(names.authenticator),
            database=sql.Identifier(names.database),
            value=sql.Literal(schemas),
        )
    )
    tenant_conn.execute("NOTIFY pgrst, 'reload config'")
    tenant_conn.execute("NOTIFY pgrst, 'reload schema'")


def _assert_reach(tenant_conn: psycopg.Connection, names) -> None:
    """Refuse unless exactly the intended roles can reach what was built.

    The memory schema: no customer role at all (slice 0 found it closed; an
    upstream release could open it). The copy: `service_role` may read, nobody
    but the platform may write, and no other customer role may even look. Checked
    inside the transaction, so a violation rolls everything back rather than
    being found by a customer.
    """
    with tenant_conn.cursor() as cur:
        # Only roles that exist. `has_schema_privilege` raises on a role that does
        # not, and one that does not exist can reach nothing -- while provisioning
        # creates the executor and client roles in steps of their own, so a
        # project can briefly have neither.
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                    (list(customer_roles(names)),))
        present = {row[0] for row in cur.fetchall()}
        if "service_role" not in present:
            raise MaludbError("service_role does not exist on this node; the copy has no reader")
        for role in (r for r in customer_roles(names) if r in present):
            cur.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, MEMORY_SCHEMA))
            if cur.fetchone()[0]:
                raise MaludbError(
                    f"{role} can use {MEMORY_SCHEMA} after enabling. The facades there run as "
                    "the node superuser and must be reachable by no customer role; refusing"
                )
            cur.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, COPY_SCHEMA))
            copy_usage = cur.fetchone()[0]
            if role == "service_role" and not copy_usage:
                raise MaludbError("service_role cannot use the copy schema; the grants did not take")
            if role != "service_role" and copy_usage:
                raise MaludbError(
                    f"{role} can use {COPY_SCHEMA}. The copy describes every table in the "
                    "project, including ones that role cannot read; only service_role may"
                )
            for table in COPY_TABLES:
                qualified = f"{COPY_SCHEMA}.{table}"
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, 'INSERT') "
                    "    OR has_table_privilege(%s, %s, 'UPDATE') "
                    "    OR has_table_privilege(%s, %s, 'DELETE') "
                    "    OR has_table_privilege(%s, %s, 'TRUNCATE')",
                    (role, qualified) * 4,
                )
                if cur.fetchone()[0]:
                    raise MaludbError(f"{role} can write {qualified}; only the platform may")


def disable(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> Enablement:
    """Turn the data-model graph off by withdrawing it. Nothing is dropped.

    `maludb` comes off the project's Data API -- the one in-database setting
    enablement wrote is reset, and PostgREST is told to reload -- and the project
    is recorded as not enabled, which stops refreshes and makes the gateway
    refuse the schema by name. The memory schema and the copy stay where they
    are: this is "off", not "delete", because a later MaluDB surface would keep
    real data in the memory schema and turning a feature off must never be what
    destroys it. Dropping them is a separate decision.

    **No entitlement check.** A project whose plan has lost the feature must still
    be able to switch it off; refusing that would leave its structure published
    because of a billing change.

    Idempotent: a project already off has the setting reset again, which heals a
    project whose record and database disagree, and records nothing new.
    """
    project = _project(conn, project_id)
    if project["status"] not in DISABLEABLE_STATUSES:
        raise MaludbError(
            f"project is {project['status']}; disable it once that operation has finished"
        )

    locked = db.one(
        conn, "SELECT pg_try_advisory_lock_shared(%s, %s) AS ok",
        (NODE_LOCK_NAMESPACE, project["node_id"]),
    )["ok"]
    conn.commit()
    if not locked:
        raise MaludbError(
            "an extension upgrade is running on this project's node; disable it once that finishes"
        )
    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        # `maludb` is served while any MaluDB feature is on (ADR-077 decision 6).
        # With vector compartments still enabled, withdrawing it here would take
        # their wrappers off the Data API along with the graph.
        if not project["maludb_vectors_enabled"]:
            tenant_conn = tenant_connect(project["database_name"])
            try:
                tenant_conn.autocommit = False
                _withdraw(tenant_conn, names)
                tenant_conn.commit()
            except Exception:
                tenant_conn.rollback()
                raise
            finally:
                tenant_conn.close()

        was_enabled = bool(project["maludb_datamodel_enabled"])
        db.execute(conn, "UPDATE projects SET maludb_datamodel_enabled = FALSE WHERE id = %s",
                   (project_id,))
        if was_enabled:
            db.execute(
                conn,
                "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
                "VALUES (%s, 'system', %s, %s)",
                (project_id, AUDIT_DISABLED, Jsonb({})),
            )
        conn.commit()
    finally:
        db.one(conn, "SELECT pg_advisory_unlock_shared(%s, %s) AS ok",
               (NODE_LOCK_NAMESPACE, project["node_id"]))
        conn.commit()

    return Enablement(
        project_ref=project["project_ref"],
        changed=was_enabled,
        memory_schema_version=project.get("maludb_memory_schema_version") or "",
        detail="disabled" if was_enabled else "already disabled",
    )


def _withdraw(tenant_conn: psycopg.Connection, names) -> None:
    """Reset only the setting `_expose` wrote, and tell PostgREST.

    `RESET pgrst.db_schemas` rather than setting it back to the default list: a
    reset leaves the rendered file's value in charge, which is what every project
    that never enabled this already runs on. Measured in slice 3: withdrawn in
    0.30 s, no restart. The notifications are delivered at commit.
    """
    tenant_conn.execute(
        sql.SQL("ALTER ROLE {role} IN DATABASE {database} RESET pgrst.db_schemas").format(
            role=sql.Identifier(names.authenticator),
            database=sql.Identifier(names.database),
        )
    )
    tenant_conn.execute("NOTIFY pgrst, 'reload config'")
    tenant_conn.execute("NOTIFY pgrst, 'reload schema'")


def refresh(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> CopyResult:
    """Refresh an enabled project's graph and replace its copy.

    An operator command in slice 3; slice 4 queues it behind a customer route
    and the per-plan limit. Takes the node lock shared, like enablement, so it
    never runs under an extension upgrade that is rebuilding the facades it
    calls.
    """
    project = _project(conn, project_id)
    if not project["maludb_datamodel_enabled"]:
        raise MaludbError("the data-model graph is not enabled for this project; enable it first")
    if project["status"] not in ENABLEABLE_STATUSES:
        raise MaludbError(
            f"project is {project['status']}; refresh it once that operation has finished"
        )

    locked = db.one(
        conn, "SELECT pg_try_advisory_lock_shared(%s, %s) AS ok",
        (NODE_LOCK_NAMESPACE, project["node_id"]),
    )["ok"]
    conn.commit()
    if not locked:
        raise MaludbError(
            "an extension upgrade is running on this project's node; refresh once that finishes"
        )
    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        tenant_conn = tenant_connect(project["database_name"])
        try:
            tenant_conn.autocommit = False
            # A refresh must not build into a schema the platform no longer
            # owns. Neither can change hands through a customer role, but the
            # check is cheap and the alternative is superuser code in a
            # customer's schema.
            for schema in (MEMORY_SCHEMA, COPY_SCHEMA):
                owner = schema_owner(tenant_conn, schema)
                if owner is None or not owner[0]:
                    raise MaludbError(
                        f"{schema} is missing or not platform-owned; re-run enablement"
                    )
            _ensure_copy_schema(tenant_conn)
            result = copy_graph(tenant_conn)
            _assert_reach(tenant_conn, names)
            tenant_conn.commit()
        except Exception:
            tenant_conn.rollback()
            raise
        finally:
            tenant_conn.close()
    finally:
        db.one(conn, "SELECT pg_advisory_unlock_shared(%s, %s) AS ok",
               (NODE_LOCK_NAMESPACE, project["node_id"]))
        conn.commit()
    return result


__all__ = [
    "AUDIT_DISABLED",
    "AUDIT_ENABLED",
    "COPY_SCHEMA",
    "COPY_TABLES",
    "CopyResult",
    "DATAMODEL_FACADES",
    "DATAMODEL_SINCE",
    "ENABLEABLE_STATUSES",
    "MEMORY_SCHEMA",
    "NODE_LOCK_NAMESPACE",
    "Enablement",
    "MaludbError",
    "customer_roles",
    "datamodel_facades_present",
    "enable",
    "memory_schema_owner",
    "copy_graph",
    "disable",
    "refresh",
    "schema_owner",
    "version_tuple",
]
