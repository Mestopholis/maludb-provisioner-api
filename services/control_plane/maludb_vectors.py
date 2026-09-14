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
    # Search goes by compartment id since the owner fence below: upstream's
    # name-based search takes the first compartment with a matching name,
    # whoever owns it.
    "exact_vector_search_sql",
)

# What the wrappers write directly rather than through an entry point. Deleting a
# chunk deletes its row: exact search does not honour tombstones (slice 0,
# finding 9), and the row cascades to its tombstone and delta rows. A chunk's
# metadata is set after `register_vector_chunk`, which takes none.
DIRECT_WRITES = {"malu$vector_chunk": {"DELETE", "UPDATE"}, "malu$vector_compartment": {"DELETE"}}

# The platform's own schema for what the wrappers read and customers must not:
# the plan's limits and the wrappers' helpers. Not `maludb`, which PostgREST
# serves -- every function there is an RPC, and the data-model graph grants
# service_role SELECT on every table there.
PRIVATE_SCHEMA = "maludb_private"
LIMITS_TABLE = "vector_limits"

# The customer contract (ADR-077 decision 3): name -> whether service_role may
# call it. Helpers live in PRIVATE_SCHEMA and are callable by nobody but the owner.
WRAPPERS = (
    "vector_compartment_create",
    "vector_compartment_delete",
    "vector_compartments",
    "vector_insert",
    "vector_insert_many",
    "vector_search",
    "vector_delete",
    "vector_explain",
)
MAX_BATCH = 1000
MAX_MATCH_COUNT = 1000

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


def derive_reach(
    tenant_conn: psycopg.Connection,
    *,
    entry_points: tuple[str, ...] | None = None,
    direct: dict[str, set[str]] | None = None,
) -> Reach:
    """Follow the entry points through every `maludb_core` body they name.

    Raises if a reachable table falls outside the vector tables or a reachable
    function runs as its definer -- both would widen the role past ADR-077.

    `entry_points` and `direct` (tables a wrapper touches itself, with the
    privileges it needs) default to the vector wrappers'. The memory search
    reader (ADR-079 memory slice 3) passes its own: one id-based search function
    and four tables it reads.
    """
    # Resolved at call time, not bound as defaults, so the module constants stay
    # the single source a test can replace.
    entry_points = ENTRY_POINTS if entry_points is None else entry_points
    direct = DIRECT_WRITES if direct is None else direct
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

    missing = [name for name in entry_points if name not in by_name]
    if missing:
        raise VectorsError(
            f"this tenant's maludb_core has no {', '.join(missing)}; vector compartments need "
            f"{'.'.join(map(str, VECTORS_SINCE))} or later"
        )

    reach = Reach()
    pending = list(entry_points)
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

    for table, privileges in direct.items():
        reach.tables.setdefault(table, set()).update(privileges | {"SELECT"})

    outside = sorted(t for t in reach.tables if not t.startswith(TABLE_PREFIXES))
    if outside:
        raise VectorsError(
            "the vector entry points reach table(s) outside the vector store: "
            + ", ".join(outside) + ". Granting them would widen the role past ADR-077; refusing"
        )

    # The wrappers take pgvector `vector` and convert it to `malu_vector` through
    # text (decision 7), which runs both types' input and output functions as the
    # owner -- and bootstrap 011 revoked EXECUTE on them too. Read from pg_type,
    # not named, so a type whose I/O functions change is followed.
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_type t "
            "JOIN pg_namespace tn ON tn.oid = t.typnamespace "
            "JOIN pg_proc p ON p.oid IN (t.typinput, t.typoutput, t.typmodin) "
            "WHERE (t.typname, tn.nspname) IN (('vector', 'public'), ('malu_vector', %s))",
            (EXTENSION_SCHEMA,),
        )
        type_io = {row[0] for row in cur.fetchall()}
    if len(type_io) < 5:
        raise VectorsError("the vector and malu_vector types were not both found with their I/O functions")
    reach.functions |= type_io

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
        # regprocedure text is already schema-qualified wherever the schema is not
        # on the connection's path, and resolves identically when it is.
        tenant_conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), role))


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
            if not (schema == EXTENSION_SCHEMA and privilege in reach.tables.get(table, set()))
            and not (schema == PRIVATE_SCHEMA and table == LIMITS_TABLE and privilege == "SELECT")
        )
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "CROSS JOIN LATERAL aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE r.rolname = %s AND n.nspname NOT IN ('maludb', 'maludb_private')", (names.vectors,),
        )
        extra += sorted(f"EXECUTE on {sig}" for (sig,) in cur.fetchall() if sig not in reach.functions)
        cur.execute(
            "SELECT n.nspname, a.privilege_type FROM pg_namespace n "
            "CROSS JOIN LATERAL aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE r.rolname = %s", (names.vectors,),
        )
        extra += sorted(
            f"{privilege} on schema {schema}" for schema, privilege in cur.fetchall()
            if not (privilege == "USAGE" and schema in (EXTENSION_SCHEMA, "public", PRIVATE_SCHEMA,
                                                        maludb.COPY_SCHEMA))
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
# The wrappers and the limits they enforce

# Every wrapper and helper runs as the owner with this path. `maludb_core` ahead
# of `public`, because upstream resolves its own functions unqualified (slice 0,
# finding 4) and a customer owns objects in `public`; `pg_temp` last, so a
# temporary object cannot shadow anything; `pg_catalog` is searched first
# implicitly. It also fixes `owner_schema` -- the first schema on the path the
# owner can use -- at `maludb_core` for every compartment (slice 0, finding 3).
PINNED_PATH = "maludb_core, public, pg_temp"

# **The fence.** `malu$vector_compartment` holds every compartment in the
# database, and not only the wrappers': MaluDB's memory schemas write their own
# embedded edges there under their own `owner_schema` (ADR-079 memory spaces;
# measured in `specs/maludb-memory-pipeline-model.md`). The wrappers' compartments
# are the ones owned by `maludb_core`, which the pinned path makes theirs. Every
# lookup, list and limit filters on it, and search resolves the compartment id
# itself -- upstream's `search_memory_exact` and `explain_vector_search` match by
# name with `LIMIT 1` and no owner, so a memory space with a colliding name would
# otherwise answer a customer's search.
WRAPPER_OWNER = "maludb_core"

_WRAPPER_SQL = r"""
CREATE OR REPLACE FUNCTION maludb_private.vector_limit(p_name text) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
    -- No row -- limits never written -- reads as 0 and every write is refused.
    SELECT coalesce((SELECT CASE p_name WHEN 'count' THEN max_count
                                        WHEN 'dimension' THEN max_dimension
                                        WHEN 'compartments' THEN max_compartments END
                       FROM maludb_private.vector_limits), 0)
$f$;

CREATE OR REPLACE FUNCTION maludb_private.vector_compartment_find(
    p_namespace text, p_subject text, p_verb text,
    OUT compartment_id bigint, OUT dimensions integer, OUT metric text, OUT vector_count bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
BEGIN
    SELECT c.compartment_id, c.embedding_dim, c.distance_metric, c.vector_count
      INTO compartment_id, dimensions, metric, vector_count
      FROM malu$vector_compartment c
      JOIN malu$vector_subject s ON s.subject_id = c.subject_id
      JOIN malu$vector_verb v ON v.verb_id = c.verb_id
     WHERE c.owner_schema = '{owner}'
       AND c.namespace = p_namespace AND s.subject_name = p_subject AND v.verb_name = p_verb;
END
$f$;

CREATE OR REPLACE FUNCTION maludb_private.vector_compartment_require(p_namespace text, p_subject text, p_verb text)
RETURNS bigint
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE v_id bigint;
BEGIN
    SELECT f.compartment_id INTO v_id FROM maludb_private.vector_compartment_find(p_namespace, p_subject, p_verb) f;
    IF v_id IS NULL THEN
        RAISE EXCEPTION 'no vector compartment %/%/%', p_namespace, p_subject, p_verb
            USING ERRCODE = 'PT404';
    END IF;
    RETURN v_id;
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_compartment_create(
    namespace text, subject text, verb text, dimensions integer, metric text DEFAULT 'cosine')
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE
    v_existing record;
    v_max_dimension integer := maludb_private.vector_limit('dimension');
    v_max_compartments integer := maludb_private.vector_limit('compartments');
BEGIN
    IF namespace IS NULL OR subject IS NULL OR verb IS NULL OR dimensions IS NULL THEN
        RAISE EXCEPTION 'namespace, subject, verb and dimensions are required' USING ERRCODE = 'PT400';
    END IF;
    IF metric IS NULL OR metric NOT IN ('cosine', 'l2', 'inner_product') THEN
        RAISE EXCEPTION 'metric must be cosine, l2 or inner_product' USING ERRCODE = 'PT400';
    END IF;
    IF dimensions < 1 OR dimensions > v_max_dimension THEN
        RAISE EXCEPTION 'vector limit: % dimensions is outside this plan''s 1 to %', dimensions, v_max_dimension
            USING ERRCODE = 'PT403', HINT = 'vector_max_dimension';
    END IF;
    -- One creation at a time per database, so two cannot both take the last slot.
    PERFORM pg_advisory_xact_lock(hashtext('maludb.vector_compartments'));
    SELECT * INTO v_existing FROM maludb_private.vector_compartment_find(namespace, subject, verb);
    IF v_existing.compartment_id IS NOT NULL THEN
        -- register_vector_compartment would return the existing id and keep its
        -- dimensions, so a different definition would be silently ignored.
        IF v_existing.dimensions <> dimensions OR v_existing.metric <> metric THEN
            RAISE EXCEPTION 'vector compartment %/%/% already exists with % dimensions and metric %',
                namespace, subject, verb, v_existing.dimensions, v_existing.metric USING ERRCODE = 'PT409';
        END IF;
        RETURN v_existing.compartment_id;
    END IF;
    IF (SELECT count(*) FROM malu$vector_compartment c WHERE c.owner_schema = '{owner}') >= v_max_compartments THEN
        RAISE EXCEPTION 'vector limit: this plan allows % compartment(s)', v_max_compartments
            USING ERRCODE = 'PT403', HINT = 'vector_max_compartments';
    END IF;
    RETURN register_vector_compartment(namespace, subject, verb, dimensions, 'customer', metric);
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_compartment_delete(namespace text, subject text, verb text)
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE v_id bigint := maludb_private.vector_compartment_require(namespace, subject, verb); v_count bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('maludb.vector_count'));
    SELECT c.vector_count INTO v_count FROM malu$vector_compartment c WHERE c.compartment_id = v_id;
    DELETE FROM malu$vector_compartment c WHERE c.compartment_id = v_id;
    RETURN v_count;
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_compartments()
RETURNS TABLE(namespace text, subject text, verb text, dimensions integer, metric text, vector_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
    SELECT c.namespace, s.subject_name, v.verb_name, c.embedding_dim, c.distance_metric, c.vector_count
      FROM malu$vector_compartment c
      JOIN malu$vector_subject s ON s.subject_id = c.subject_id
      JOIN malu$vector_verb v ON v.verb_id = c.verb_id
     WHERE c.owner_schema = '{owner}'
     ORDER BY 1, 2, 3
$f$;

CREATE OR REPLACE FUNCTION maludb_private.vector_reserve(p_count integer) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE v_max integer := maludb_private.vector_limit('count'); v_total bigint;
BEGIN
    -- Serialises writers per database, so concurrent inserts cannot both fit.
    PERFORM pg_advisory_xact_lock(hashtext('maludb.vector_count'));
    SELECT coalesce(sum(c.vector_count), 0) INTO v_total FROM malu$vector_compartment c
     WHERE c.owner_schema = '{owner}';
    IF v_total + p_count > v_max THEN
        RAISE EXCEPTION 'vector limit: this plan allows % vector(s); % stored, % requested', v_max, v_total, p_count
            USING ERRCODE = 'PT403', HINT = 'vector_max_count';
    END IF;
END
$f$;

CREATE OR REPLACE FUNCTION maludb_private.vector_insert_one(
    p_compartment bigint, p_content text, p_embedding vector, p_metadata jsonb)
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE v_id bigint;
BEGIN
    IF p_content IS NULL OR p_embedding IS NULL THEN
        RAISE EXCEPTION 'content and embedding are required' USING ERRCODE = 'PT400';
    END IF;
    v_id := register_vector_chunk(p_compartment, p_content, p_embedding::text::malu_vector, 'customer');
    IF p_metadata IS NOT NULL AND p_metadata <> '{{}}'::jsonb THEN
        UPDATE malu$vector_chunk c SET metadata = p_metadata WHERE c.chunk_id = v_id;
    END IF;
    RETURN v_id;
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_insert(
    namespace text, subject text, verb text, content text, embedding vector, metadata jsonb DEFAULT '{{}}')
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE v_id bigint := maludb_private.vector_compartment_require(namespace, subject, verb);
BEGIN
    PERFORM maludb_private.vector_reserve(1);
    RETURN maludb_private.vector_insert_one(v_id, content, embedding, metadata);
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_insert_many(namespace text, subject text, verb text, items jsonb)
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE
    v_id bigint := maludb_private.vector_compartment_require(namespace, subject, verb);
    v_item jsonb;
    v_n integer;
BEGIN
    IF items IS NULL OR jsonb_typeof(items) <> 'array' THEN
        RAISE EXCEPTION 'items must be an array of {{content, embedding, metadata}}' USING ERRCODE = 'PT400';
    END IF;
    v_n := jsonb_array_length(items);
    IF v_n > {max_batch} THEN
        RAISE EXCEPTION 'at most {max_batch} items per call' USING ERRCODE = 'PT400';
    END IF;
    PERFORM maludb_private.vector_reserve(v_n);
    FOR v_item IN SELECT value FROM jsonb_array_elements(items) LOOP
        PERFORM maludb_private.vector_insert_one(
            v_id, v_item->>'content', (v_item->>'embedding')::vector, coalesce(v_item->'metadata', '{{}}'));
    END LOOP;
    RETURN v_n;
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_search(
    namespace text, subject text, verb text, query vector,
    match_count integer DEFAULT 10, filter jsonb DEFAULT '{{}}')
RETURNS TABLE(id bigint, content text, metadata jsonb, similarity double precision, distance double precision)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
#variable_conflict use_column
DECLARE v_id bigint;
BEGIN
    v_id := maludb_private.vector_compartment_require(
        vector_search.namespace, vector_search.subject, vector_search.verb);
    IF query IS NULL THEN
        RAISE EXCEPTION 'query is required' USING ERRCODE = 'PT400';
    END IF;
    IF match_count IS NULL OR match_count < 1 OR match_count > {max_match} THEN
        RAISE EXCEPTION 'match_count must be between 1 and {max_match}' USING ERRCODE = 'PT400';
    END IF;
    -- search_memory_filter's own shape -- a fourfold overfetch, then metadata
    -- containment, ordered by distance then id -- over the compartment the fence
    -- resolved, rather than whichever one upstream finds first by name.
    RETURN QUERY
        SELECT h.chunk_id, h.source_text, c.metadata, h.similarity, h.distance
          FROM exact_vector_search_sql(v_id, query::text::malu_vector, match_count * 4, NULL) h
          JOIN malu$vector_chunk c ON c.chunk_id = h.chunk_id
         WHERE c.metadata @> coalesce(filter, '{{}}')
         ORDER BY h.distance ASC, h.chunk_id ASC
         LIMIT match_count;
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_delete(namespace text, subject text, verb text, ids bigint[])
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = {path} AS $f$
DECLARE v_id bigint := maludb_private.vector_compartment_require(namespace, subject, verb); v_n bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('maludb.vector_count'));
    -- Deleted, not tombstoned: exact search does not filter tombstones.
    DELETE FROM malu$vector_chunk c WHERE c.compartment_id = v_id AND c.chunk_id = ANY(ids);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    UPDATE malu$vector_compartment c SET vector_count = greatest(c.vector_count - v_n, 0), updated_at = now()
     WHERE c.compartment_id = v_id;
    RETURN v_n;
END
$f$;

CREATE OR REPLACE FUNCTION maludb.vector_explain(namespace text, subject text, verb text)
RETURNS TABLE(dimensions integer, metric text, vector_count bigint, search_mode text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
#variable_conflict use_column
DECLARE v_id bigint;
BEGIN
    v_id := maludb_private.vector_compartment_require(
        vector_explain.namespace, vector_explain.subject, vector_explain.verb);
    RETURN QUERY SELECT e.embedding_dim, e.distance_metric, e.vector_count, e.search_mode
                   FROM explain_vector_search(vector_explain.namespace, vector_explain.subject, vector_explain.verb) e
                  WHERE e.compartment_id = v_id;
END
$f$;
"""


def _ensure_private_schema(tenant_conn: psycopg.Connection) -> None:
    """The platform's own schema: superuser-owned, no customer role may use it."""
    maludb._refuse_squatted(tenant_conn, PRIVATE_SCHEMA)  # noqa: SLF001
    tenant_conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(PRIVATE_SCHEMA)))
    owner = maludb.schema_owner(tenant_conn, PRIVATE_SCHEMA)
    if owner is None or not owner[0]:
        raise VectorsError(f"{PRIVATE_SCHEMA} is not owned by the platform after creating it; refusing")
    tenant_conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(PRIVATE_SCHEMA)))
    tenant_conn.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {}.{} ("
        " only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),"
        " max_count integer NOT NULL, max_dimension integer NOT NULL, max_compartments integer NOT NULL,"
        " plan_code text, written_at timestamptz NOT NULL DEFAULT now())"
    ).format(sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(LIMITS_TABLE)))
    tenant_conn.execute(sql.SQL("REVOKE ALL ON {}.{} FROM PUBLIC").format(
        sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(LIMITS_TABLE)))


def write_limits(tenant_conn: psycopg.Connection, allowed: entitlements.Entitlements) -> None:
    """Record the plan's vector limits where the wrappers read them. Idempotent.

    A table in the tenant database rather than a setting: a custom setting can be
    changed by any session with `set_config`, including an RPC function a customer
    writes and then calls a wrapper from.
    """
    tenant_conn.execute(
        sql.SQL(
            "INSERT INTO {}.{} (only_row, max_count, max_dimension, max_compartments, plan_code, written_at) "
            "VALUES (true, %s, %s, %s, %s, now()) ON CONFLICT (only_row) DO UPDATE SET "
            "max_count = EXCLUDED.max_count, max_dimension = EXCLUDED.max_dimension, "
            "max_compartments = EXCLUDED.max_compartments, plan_code = EXCLUDED.plan_code, written_at = now()"
        ).format(sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(LIMITS_TABLE)),
        (allowed.vector_max_count, allowed.vector_max_dimension, allowed.vector_max_compartments,
         allowed.plan_code),
    )


def install_wrappers(tenant_conn: psycopg.Connection, names: provisioning.TenantNames) -> None:
    """Create or replace the wrappers, owned by the vectors role, callable by service_role only."""
    role = sql.Identifier(names.vectors)
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(PRIVATE_SCHEMA), role))
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(maludb.COPY_SCHEMA), role))
    tenant_conn.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
        sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(LIMITS_TABLE), role))
    tenant_conn.execute(_WRAPPER_SQL.format(
        path=PINNED_PATH, max_batch=MAX_BATCH, max_match=MAX_MATCH_COUNT, owner=WRAPPER_OWNER))
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text, n.nspname, p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = ANY(%s) AND p.proname LIKE 'vector\\_%%'",
            ([maludb.COPY_SCHEMA, PRIVATE_SCHEMA],),
        )
        functions = cur.fetchall()
    for signature, schema, name in functions:
        tenant_conn.execute(sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(sql.SQL(signature), role))
        tenant_conn.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(sql.SQL(signature)))
        if schema == maludb.COPY_SCHEMA and name in WRAPPERS:
            tenant_conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO service_role").format(sql.SQL(signature)))


def exercise_wrappers(tenant_conn: psycopg.Connection) -> None:
    """Call every wrapper as service_role, repeatedly, and roll it all back.

    The check that the wrappers work as a customer will call them, past the
    switch to generic plans. Inside a savepoint on the caller's transaction.
    """
    tenant_conn.execute("SAVEPOINT vectors_wrappers")
    try:
        # Room for the probe whatever the plan allows or the project has stored:
        # a tenant at its limit, or on a plan cut to zero, must not fail an
        # extension upgrade's verification. Undone with everything else below.
        tenant_conn.execute(sql.SQL(
            "UPDATE {}.{} SET max_count = (SELECT coalesce(sum(vector_count), 0) + 100 FROM "
            "maludb_core.\"malu$vector_compartment\"), max_dimension = greatest(max_dimension, 3), "
            "max_compartments = (SELECT count(*) + 1 FROM maludb_core.\"malu$vector_compartment\")"
        ).format(sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(LIMITS_TABLE)))
        tenant_conn.execute("SET LOCAL ROLE service_role")
        # A name no customer's compartment can already have.
        ns = (f"platform-probe-{uuid.uuid4().hex}", "probe", "probe")
        tenant_conn.execute("SELECT maludb.vector_compartment_create(%s, %s, %s, 3)", ns)
        for i in range(PROBE_CALLS):
            tenant_conn.execute("SELECT maludb.vector_insert(%s, %s, %s, 'probe', %s::vector, '{\"k\": 1}')",
                                (*ns, f"[{i + 1},2,3]"))
            tenant_conn.execute("SELECT count(*) FROM maludb.vector_search(%s, %s, %s, '[1,2,3]'::vector, 5)", ns)
            tenant_conn.execute(
                "SELECT count(*) FROM maludb.vector_search(%s, %s, %s, '[1,2,3]'::vector, 5, '{\"k\": 1}')", ns)
        tenant_conn.execute(
            "SELECT maludb.vector_insert_many(%s, %s, %s, '[{\"content\": \"a\", \"embedding\": [1,1,1]}]')", ns)
        tenant_conn.execute("SELECT * FROM maludb.vector_compartments()").fetchall()
        tenant_conn.execute("SELECT * FROM maludb.vector_explain(%s, %s, %s)", ns).fetchall()
        tenant_conn.execute("SELECT maludb.vector_delete(%s, %s, %s, ARRAY[0]::bigint[])", ns)
        tenant_conn.execute("SELECT maludb.vector_compartment_delete(%s, %s, %s)", ns)
    except psycopg.errors.InsufficientPrivilege as exc:
        tenant_conn.execute("ROLLBACK TO SAVEPOINT vectors_wrappers")
        raise VectorsError(
            f"the vector wrappers could not run as service_role: {str(exc).splitlines()[0]}"
        ) from None
    except Exception:
        tenant_conn.execute("ROLLBACK TO SAVEPOINT vectors_wrappers")
        raise
    tenant_conn.execute("ROLLBACK TO SAVEPOINT vectors_wrappers")


def revoke_outside(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, reach: Reach) -> list[str]:
    """Revoke what the owner holds in the extension that `reach` no longer names.

    For an extension upgrade, not an enablement: a new `maludb_core` that stops
    calling a function leaves the owner's grant on it behind, and
    `assert_definer` would then refuse -- rolling back an otherwise good upgrade
    for holding less than it did. Narrows only; returns what it revoked.
    """
    role = sql.Identifier(names.vectors)
    revoked: list[str] = []
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname, p.privilege_type FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) p JOIN pg_roles r ON r.oid = p.grantee "
            "WHERE r.rolname = %s AND n.nspname = %s AND c.relkind IN ('r', 'p', 'v')",
            (names.vectors, EXTENSION_SCHEMA),
        )
        tables = [(t, p) for t, p in cur.fetchall() if p not in reach.tables.get(t, set())]
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "CROSS JOIN LATERAL aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE r.rolname = %s AND n.nspname NOT IN ('maludb', 'maludb_private')",
            (names.vectors,),
        )
        functions = [f for (f,) in cur.fetchall() if f not in reach.functions]
    for table, privilege in tables:
        tenant_conn.execute(sql.SQL("REVOKE " + privilege + " ON {} FROM {}").format(
            sql.Identifier(EXTENSION_SCHEMA, table), role))
        revoked.append(f"{privilege} on {table}")
    for signature in functions:
        tenant_conn.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(sql.SQL(signature), role))
        revoked.append(f"EXECUTE on {signature}")
    return revoked


def reverify(tenant_conn: psycopg.Connection, names: provisioning.TenantNames) -> bool:
    """Bring a tenant's vector wrappers into line with its installed extension.

    Called by the extension upgrade run inside its per-tenant transaction
    (ADR-074 decision 5, compartments slice 3), after `ALTER EXTENSION`, so a
    release that breaks the wrappers rolls that tenant back rather than breaking
    its API. Keyed on what the tenant holds, not on the control-plane flag: a
    project that disabled vectors keeps its wrappers and data, and enabling again
    must find them working.

    Returns False, touching nothing, for a tenant that never had them.
    """
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    (f"{PRIVATE_SCHEMA}.{LIMITS_TABLE}", names.vectors))
        if not cur.fetchone()[0]:
            return False
    reach = derive_reach(tenant_conn)
    revoked = revoke_outside(tenant_conn, names, reach)
    if revoked:
        log.info("narrowed %s after an extension change: %s", names.vectors, "; ".join(revoked[:5]))
    grant_definer(tenant_conn, names, reach)
    install_wrappers(tenant_conn, names)
    assert_definer(tenant_conn, names, reach)
    exercise_definer(tenant_conn, names)
    exercise_wrappers(tenant_conn)
    return True


# --------------------------------------------------------------------------
# Enabling and disabling


def _project(conn: psycopg.Connection, project_id: uuid.UUID) -> dict:
    project = db.one(
        conn,
        "SELECT id, project_ref, node_id, database_name, status, maludb_datamodel_enabled, "
        "       maludb_vectors_enabled, maludb_memory_enabled FROM projects WHERE id = %s AND deleted_at IS NULL",
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


def _build(tenant_conn: psycopg.Connection, names: provisioning.TenantNames,
           allowed: entitlements.Entitlements) -> Reach:
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
    _ensure_private_schema(tenant_conn)
    write_limits(tenant_conn, allowed)
    install_wrappers(tenant_conn, names)
    assert_definer(tenant_conn, names, reach)
    exercise_definer(tenant_conn, names)
    exercise_wrappers(tenant_conn)
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
    allowed = entitlements.for_project(conn, project_id)
    if not allowed.maludb_vectors:
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
            reach = _build(tenant_conn, names, allowed)
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
        # ADR-079 memory slice 3: memory spaces' search is published there as well.
        if not (project["maludb_datamodel_enabled"] or project["maludb_memory_enabled"]):
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
    "exercise_wrappers",
    "grant_definer",
    "install_wrappers",
    "reverify",
    "revoke_outside",
    "write_limits",
]
