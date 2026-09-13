"""MaluDB vector compartments for a project: the owner role and enablement (ADR-077).

Compartments slice 1. The wrappers customers call arrive in slice 2; this slice
builds what they stand on and turns the surface on and off.

## The definer role, and why its grants are derived rather than listed

A wrapper is `SECURITY DEFINER`, so it runs as its owner. ADR-077 decision 3 makes
that owner `mldb_<ref>_vectors` -- a `NOLOGIN` role holding grants on the vector
tables and nothing else -- rather than the node superuser, so a wrapper bug reaches
that and no further.

What the role needs was measured in slice 0 (`specs/vector-compartments-model.md`)
and two findings shape how it is granted:

- **Function `EXECUTE` as well as table grants.** Bootstrap 011 revokes `EXECUTE`
  on extension functions in every schema, and ADR-076 gives it back to customer
  roles by name -- not to a platform definer.
- **Calling each entry point once does not find them all.** A cached PL/pgSQL
  plan reached `vector_l2_squared` on a later call than the first.

So the grants are **read from the installed extension's function bodies**,
starting at the entry points the wrappers will call and following every
`maludb_core` function they name. That keeps them right across an extension
upgrade without a list to update, and it is fenced two ways so a body that names
something unexpected cannot widen the role:

- table privileges only on tables whose names begin `malu$vector_` or `malu$ann_`;
  a reachable table outside that fails enablement rather than being granted;
- `EXECUTE` only on functions that are **not** `SECURITY DEFINER`. An invoker
  function runs with the role's own narrow rights, so executing one cannot reach
  anything the table grants do not; a definer function would, and reaching one
  fails enablement.

Then the role is **exercised**: inside a savepoint that is rolled back, it creates
a compartment, inserts, and searches repeatedly on one connection -- past
PostgreSQL's switch to generic plans -- so a grant the reading missed fails the
enablement rather than a customer's twelfth call.

## Exposure

`maludb` is served while **any** MaluDB feature is enabled (ADR-077 decision 6).
Enabling vectors sets the same in-database `pgrst.db_schemas` the data-model graph
does; disabling vectors withdraws it only when the data-model graph is off too,
and `maludb.disable` does the same in reverse.

Operator-run first (`cp-manage project maludb enable --feature vectors`), as the
data-model graph was, because it runs as the node superuser (ADR-038).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements, maludb, provisioning

log = logging.getLogger("maludb.maludb_vectors")

EXTENSION_SCHEMA = "maludb_core"

# What the wrappers will call (compartments slice 2). Everything else the role is
# granted is found by following these through the installed bodies.
ENTRY_POINTS = (
    "register_vector_compartment",
    "register_vector_chunk",
    "search_memory_exact",
    "search_memory_filter",
    "explain_vector_search",
)

# Deleting a chunk deletes its row: exact search does not honour tombstones
# (slice 0, finding 9). The row cascades to its tombstone and delta rows.
DIRECT_WRITES = {"malu$vector_chunk": {"DELETE"}, "malu$vector_compartment": {"DELETE"}}

TABLE_PREFIXES = ("malu$vector_", "malu$ann_")

VECTORS_SINCE = (0, 104, 0)

# Past PostgreSQL's five custom plans before a generic one (plancache.c), with room.
PROBE_CALLS = 12

AUDIT_ENABLED = "maludb.vectors.enabled"
AUDIT_DISABLED = "maludb.vectors.disabled"

_TABLE_REF = re.compile(r"malu\$[a-z0-9_]+")
_WRITE = {
    "INSERT": re.compile(r"\bINSERT\s+INTO\s+(?:maludb_core\.)?\"?(malu\$[a-z0-9_]+)", re.I),
    "UPDATE": re.compile(r"\bUPDATE\s+(?:maludb_core\.)?\"?(malu\$[a-z0-9_]+)", re.I),
    "DELETE": re.compile(r"\bDELETE\s+FROM\s+(?:maludb_core\.)?\"?(malu\$[a-z0-9_]+)", re.I),
}


class VectorsError(maludb.MaludbError):
    """Vector compartments could not be enabled, and nothing was left half-built."""


@dataclass
class Reach:
    """What the wrappers' entry points reach in the installed extension."""

    functions: set[str] = field(default_factory=set)  # regprocedure text
    tables: dict[str, set[str]] = field(default_factory=dict)  # name -> privileges
    sequences: set[str] = field(default_factory=set)  # regclass text


@dataclass
class VectorsEnablement:
    project_ref: str
    changed: bool
    detail: str
    reach: Reach | None = None


# --------------------------------------------------------------------------
# Reading the extension


def derive_reach(tenant_conn: psycopg.Connection) -> Reach:
    """Follow the entry points through every `maludb_core` body they name.

    Raises if a reachable table falls outside the vector tables or a reachable
    function runs as its definer -- both would widen the role past ADR-077.
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid, p.proname, p.oid::regprocedure::text, p.prosecdef, l.lanname, "
            "       CASE WHEN l.lanname IN ('sql', 'plpgsql') THEN pg_get_functiondef(p.oid) END "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "  JOIN pg_language l ON l.oid = p.prolang "
            " WHERE n.nspname = %s AND p.prokind = 'f'",
            (EXTENSION_SCHEMA,),
        )
        catalogue = cur.fetchall()
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v')",
            (EXTENSION_SCHEMA,),
        )
        relations = {row[0] for row in cur.fetchall()}

    by_name: dict[str, list[tuple]] = {}
    for row in catalogue:
        by_name.setdefault(row[1], []).append(row)
    names_pattern = re.compile(
        r"(?<![\w$.])(?:maludb_core\.)?(" + "|".join(sorted(map(re.escape, by_name), key=len, reverse=True))
        + r")\s*\("
    )

    missing = [name for name in ENTRY_POINTS if name not in by_name]
    if missing:
        raise VectorsError(
            f"this tenant's maludb_core has no {', '.join(missing)}; vector compartments need "
            f"{'.'.join(map(str, VECTORS_SINCE))} or later"
        )

    reach = Reach()
    pending = list(ENTRY_POINTS)
    seen: set[str] = set()
    walked_tables: set[str] = set()

    def scan(text: str) -> None:
        for called in names_pattern.findall(text):
            if called not in seen:
                pending.append(called)
        for table in set(_TABLE_REF.findall(text)) & relations:
            reach.tables.setdefault(table, set()).add("SELECT")
        for privilege, pattern in _WRITE.items():
            for match in pattern.finditer(text):
                table = match.group(1)
                if table not in relations:
                    continue
                reach.tables.setdefault(table, set()).add(privilege)
                # An upsert needs UPDATE too, and says so only later in the statement.
                if privilege == "INSERT" and re.search(r"\bDO\s+UPDATE\b", text[match.end():].split(";", 1)[0], re.I):
                    reach.tables[table].add("UPDATE")

    while pending or set(reach.tables) - walked_tables:
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            for _oid, _proname, signature, secdef, _lang, body in by_name.get(name, []):
                if secdef:
                    raise VectorsError(
                        f"{signature} is reachable from the vector entry points "
                        "and runs as its definer; granting it would reach past the vector tables"
                    )
                reach.functions.add(signature)
                if body:
                    scan(body.split("AS $function$", 1)[-1])
        # A table's CHECK constraints, column defaults and triggers run functions
        # too, with the writer's privileges: slice 1 found `octet_length` in the
        # chunk table's CHECK only by running it.
        for table in sorted(set(reach.tables) - walked_tables):
            walked_tables.add(table)
            with tenant_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k "
                    "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s AND k.contype = 'c' "
                    "UNION ALL SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
                    "JOIN pg_class c ON c.oid = d.adrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s "
                    "UNION ALL SELECT p.proname || '()' FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                    "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s AND NOT t.tgisinternal",
                    (EXTENSION_SCHEMA, table) * 3,
                )
                for (expression,) in cur.fetchall():
                    scan(expression)

    for table, privileges in DIRECT_WRITES.items():
        reach.tables.setdefault(table, set()).update(privileges | {"SELECT"})

    outside = sorted(t for t in reach.tables if not t.startswith(TABLE_PREFIXES))
    if outside:
        raise VectorsError(
            "the vector entry points reach table(s) outside the vector store: "
            + ", ".join(outside) + ". Granting them would widen the role past ADR-077; refusing"
        )

    inserted = [t for t, p in reach.tables.items() if "INSERT" in p]
    if inserted:
        with tenant_conn.cursor() as cur:
            cur.execute(
                "SELECT s.oid::regclass::text FROM pg_class s "
                "JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') "
                "JOIN pg_class t ON t.oid = d.refobjid JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE s.relkind = 'S' AND n.nspname = %s AND t.relname = ANY(%s)",
                (EXTENSION_SCHEMA, inserted),
            )
            reach.sequences = {row[0] for row in cur.fetchall()}
    return reach


def grant_definer(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, reach: Reach) -> None:
    """Grant the definer exactly `reach`, in this database. Idempotent."""
    role = sql.Identifier(names.vectors)
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(EXTENSION_SCHEMA), role))
    # `vector`'s own type and operators live in public (ADR-018); the wrappers take
    # pgvector values, so the definer needs to resolve them.
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    for table, privileges in sorted(reach.tables.items()):
        tenant_conn.execute(sql.SQL("GRANT {} ON {} TO {}").format(
            sql.SQL(", ").join(sql.SQL(p) for p in sorted(privileges)),
            sql.Identifier(EXTENSION_SCHEMA, table), role,
        ))
    for sequence in sorted(reach.sequences):
        tenant_conn.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(sql.SQL(sequence), role))
    for signature in sorted(reach.functions):
        tenant_conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
            sql.SQL(f"{EXTENSION_SCHEMA}.") + sql.SQL(signature.removeprefix(f"{EXTENSION_SCHEMA}.")), role,
        ))


def assert_definer(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, reach: Reach) -> None:
    """Refuse unless the definer is exactly what ADR-077 decision 3 describes.

    No login, no attribute, no membership, and in this database no table, sequence
    or function privilege beyond `reach`. Checked on every enablement, so a role
    widened by hand is found rather than trusted.
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = %s", (names.vectors,),
        )
        row = cur.fetchone()
        if row is None:
            raise VectorsError(f"{names.vectors} does not exist")
        if any(row):
            raise VectorsError(f"{names.vectors} holds a login or role attribute it must not")
        cur.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = %s", (names.vectors,),
        )
        if cur.fetchone()[0]:
            raise VectorsError(f"{names.vectors} is a member of another role; it must be of none")

        cur.execute(
            "SELECT n.nspname, c.relname, p.privilege_type FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) p JOIN pg_roles r ON r.oid = p.grantee "
            "WHERE r.rolname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')", (names.vectors,),
        )
        extra = sorted(
            f"{privilege} on {schema}.{table}" for schema, table, privilege in cur.fetchall()
            if schema != EXTENSION_SCHEMA or privilege not in reach.tables.get(table, set())
        )
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "CROSS JOIN LATERAL aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE r.rolname = %s AND n.nspname <> 'maludb'", (names.vectors,),
        )
        extra += sorted(f"EXECUTE on {sig}" for (sig,) in cur.fetchall() if sig not in reach.functions)
        cur.execute(
            "SELECT n.nspname, a.privilege_type FROM pg_namespace n "
            "CROSS JOIN LATERAL aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE r.rolname = %s", (names.vectors,),
        )
        extra += sorted(
            f"{privilege} on schema {schema}" for schema, privilege in cur.fetchall()
            if not (privilege == "USAGE" and schema in (EXTENSION_SCHEMA, "public"))
        )
    if extra:
        raise VectorsError(f"{names.vectors} holds more than the vector store needs: " + "; ".join(extra[:8]))


def exercise_definer(tenant_conn: psycopg.Connection, names: provisioning.TenantNames) -> None:
    """Run the entry points as the definer, repeatedly, and roll every row back.

    The check a reading of the bodies cannot give: that the grants are enough.
    Inside a savepoint, on the caller's transaction, so nothing survives it.
    """
    probe_dim = 3
    tenant_conn.execute("SAVEPOINT vectors_probe")
    try:
        tenant_conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(names.vectors)))
        tenant_conn.execute("SET LOCAL search_path = maludb_core, public")
        cid = tenant_conn.execute(
            "SELECT register_vector_compartment('platform-probe', 'probe', 'probe', %s, 'probe', 'cosine')",
            (probe_dim,),
        ).fetchone()[0]
        for i in range(PROBE_CALLS):
            vector = f"[{i + 1},2,3]"
            tenant_conn.execute("SELECT register_vector_chunk(%s, 'probe', %s::malu_vector, 'probe')",
                                (cid, vector))
            tenant_conn.execute(
                "SELECT count(*) FROM search_memory_exact('platform-probe', 'probe', 'probe', "
                "%s::malu_vector, 5, NULL)", (vector,),
            ).fetchone()
            tenant_conn.execute(
                "SELECT count(*) FROM search_memory_filter('platform-probe', 'probe', 'probe', "
                "%s::malu_vector, '{}'::jsonb, 5, NULL)", (vector,),
            ).fetchone()
        tenant_conn.execute("SELECT * FROM explain_vector_search('platform-probe', 'probe', 'probe')").fetchall()
        tenant_conn.execute('DELETE FROM "malu$vector_chunk" WHERE compartment_id = %s', (cid,))
        tenant_conn.execute('DELETE FROM "malu$vector_compartment" WHERE compartment_id = %s', (cid,))
    except psycopg.errors.InsufficientPrivilege as exc:
        tenant_conn.execute("ROLLBACK TO SAVEPOINT vectors_probe")
        raise VectorsError(
            f"{names.vectors} was granted what the installed extension's bodies name and still "
            f"could not run the vector store: {str(exc).splitlines()[0]}"
        ) from None
    except Exception:
        tenant_conn.execute("ROLLBACK TO SAVEPOINT vectors_probe")
        raise
    tenant_conn.execute("ROLLBACK TO SAVEPOINT vectors_probe")


# --------------------------------------------------------------------------
# Enabling and disabling


def _project(conn: psycopg.Connection, project_id: uuid.UUID) -> dict:
    project = db.one(
        conn,
        "SELECT id, project_ref, node_id, database_name, status, maludb_datamodel_enabled, "
        "       maludb_vectors_enabled FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None:
        raise VectorsError("project does not exist")
    if project["database_name"] is None or project["node_id"] is None:
        raise VectorsError("project has no database yet; provision it before enabling this")
    return project


def _with_node_lock(conn: psycopg.Connection, node_id: int, verb: str):
    locked = db.one(conn, "SELECT pg_try_advisory_lock_shared(%s, %s) AS ok",
                    (maludb.NODE_LOCK_NAMESPACE, node_id))["ok"]
    conn.commit()
    if not locked:
        raise VectorsError(f"an extension upgrade is running on this project's node; {verb} once that finishes")


def _release_node_lock(conn: psycopg.Connection, node_id: int) -> None:
    db.one(conn, "SELECT pg_advisory_unlock_shared(%s, %s) AS ok", (maludb.NODE_LOCK_NAMESPACE, node_id))
    conn.commit()


def _build(tenant_conn: psycopg.Connection, names: provisioning.TenantNames) -> Reach:
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'maludb_core'")
        row = cur.fetchone()
    if row is None:
        raise VectorsError("maludb_core is not installed in this tenant database (ADR-015)")
    if maludb.version_tuple(row[0]) < VECTORS_SINCE:
        raise VectorsError(
            f"this tenant has maludb_core {row[0]}; vector compartments need "
            f"{'.'.join(map(str, VECTORS_SINCE))} or later. Run `cp-manage extension upgrade` first"
        )
    # The schema the wrappers will live in, platform-owned -- the same squat
    # refusal and re-check the data-model graph's copy gets.
    maludb._refuse_squatted(tenant_conn, maludb.COPY_SCHEMA)  # noqa: SLF001
    tenant_conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(maludb.COPY_SCHEMA)))
    owner = maludb.schema_owner(tenant_conn, maludb.COPY_SCHEMA)
    if owner is None or not owner[0]:
        raise VectorsError(f"{maludb.COPY_SCHEMA} is not owned by the platform after creating it; refusing")
    tenant_conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(maludb.COPY_SCHEMA)))
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO service_role").format(
        sql.Identifier(maludb.COPY_SCHEMA)))

    provisioning.create_vectors_role(tenant_conn, names)
    reach = derive_reach(tenant_conn)
    grant_definer(tenant_conn, names, reach)
    assert_definer(tenant_conn, names, reach)
    exercise_definer(tenant_conn, names)
    maludb._expose(tenant_conn, names)  # noqa: SLF001
    return reach


def enable(conn: psycopg.Connection, *, project_id: uuid.UUID, tenant_connect) -> VectorsEnablement:
    """Turn vector compartments on for one project. Safe to re-run.

    The tenant work is one transaction; the control-plane record is written only
    after it commits, so a failure leaves nothing built or a built store with no
    record, and a re-run finishes the second.
    """
    project = _project(conn, project_id)
    if project["status"] not in maludb.ENABLEABLE_STATUSES:
        raise VectorsError(f"project is {project['status']}; enable it once that operation has finished")
    if not entitlements.for_project(conn, project_id).maludb_vectors:
        raise VectorsError(
            "this project's plan does not include vector compartments (maludb_vectors is false). "
            "Change the plan rather than enabling it here."
        )

    _with_node_lock(conn, project["node_id"], "enable it")
    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        tenant_conn = tenant_connect(project["database_name"])
        try:
            tenant_conn.autocommit = False
            reach = _build(tenant_conn, names)
            tenant_conn.commit()
        except Exception:
            tenant_conn.rollback()
            raise
        finally:
            tenant_conn.close()

        was_enabled = bool(project["maludb_vectors_enabled"])
        db.execute(
            conn,
            "UPDATE projects SET maludb_vectors_enabled = TRUE, "
            "maludb_vectors_enabled_at = coalesce(maludb_vectors_enabled_at, now()) WHERE id = %s",
            (project_id,),
        )
        if not was_enabled:
            db.execute(
                conn,
                "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
                "VALUES (%s, 'system', %s, %s)",
                (project_id, AUDIT_ENABLED, Jsonb({"functions": len(reach.functions),
                                                   "tables": sorted(reach.tables)})),
            )
        conn.commit()
    finally:
        _release_node_lock(conn, project["node_id"])

    return VectorsEnablement(project_ref=project["project_ref"], changed=not was_enabled,
                             detail="enabled" if not was_enabled else "already enabled", reach=reach)


def disable(conn: psycopg.Connection, *, project_id: uuid.UUID, tenant_connect) -> VectorsEnablement:
    """Turn vector compartments off. Nothing is dropped, and nobody's vectors are lost.

    `maludb` comes off the Data API only if the data-model graph is off too. The
    compartments, the role and its grants stay, so enabling again finds them.
    No entitlement check, for `maludb.disable`'s reason.
    """
    project = _project(conn, project_id)
    if project["status"] not in maludb.DISABLEABLE_STATUSES:
        raise VectorsError(f"project is {project['status']}; disable it once that operation has finished")

    _with_node_lock(conn, project["node_id"], "disable it")
    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        if not project["maludb_datamodel_enabled"]:
            tenant_conn = tenant_connect(project["database_name"])
            try:
                tenant_conn.autocommit = False
                maludb._withdraw(tenant_conn, names)  # noqa: SLF001
                tenant_conn.commit()
            except Exception:
                tenant_conn.rollback()
                raise
            finally:
                tenant_conn.close()

        was_enabled = bool(project["maludb_vectors_enabled"])
        db.execute(conn, "UPDATE projects SET maludb_vectors_enabled = FALSE WHERE id = %s", (project_id,))
        if was_enabled:
            db.execute(
                conn,
                "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
                "VALUES (%s, 'system', %s, %s)",
                (project_id, AUDIT_DISABLED, Jsonb({})),
            )
        conn.commit()
    finally:
        _release_node_lock(conn, project["node_id"])

    return VectorsEnablement(project_ref=project["project_ref"], changed=was_enabled,
                             detail="disabled" if was_enabled else "already disabled")


__all__ = [
    "AUDIT_DISABLED",
    "AUDIT_ENABLED",
    "ENTRY_POINTS",
    "Reach",
    "VectorsEnablement",
    "VectorsError",
    "assert_definer",
    "derive_reach",
    "disable",
    "enable",
    "exercise_definer",
    "grant_definer",
]
