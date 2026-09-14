"""Build a project's pending memory spaces on its node (ADR-079, memory slice 2a).

A space is a MaluDB memory schema: `CREATE SCHEMA mem_<name>` and
`maludb_core.enable_memory_schema` over the node superuser connection, which is
the one connection whose `session_user` passes the pipeline's guard (ADR-079
decision 3). Called by the provisioner, never by the public application
(ADR-038).

**What is refused before anything is built, and why:**

- **A `maludb_core` older than 0.105.0.** The pipeline's rows reach `pg_dump` only
  from 0.105.0 (ADR-078); a space built earlier would lose its memories on the
  first move or restore.
- **A schema that already exists and is not the platform's.** The same squat
  refusal the data-model graph uses: a customer-created `mem_x` would otherwise
  have superuser-owned facades built into a schema a customer role owns.

**What is done first, in the same transaction:** the project's vector wrappers
are re-verified (`maludb_vectors.reverify`), which installs the owner fence
(#146). A space writes embedded edges into `malu$vector_compartment`; a tenant
still holding pre-fence wrappers would let the customer vector API list, search
and delete them (memory slice 0, finding 1e). Re-verifying is a no-op for a
project that never had vectors.

**What is asserted after, before commit:** no customer role can use the space's
schema or execute anything in it. `enable_memory_schema` grants MaluDB's own,
cluster-wide roles, and no customer role reaches those (docs/MALUDB.md); an
upstream release that changed that would roll the build back rather than publish
superuser-owned facades.

**The writer** (slice 2b). Each space grants the project's memory writer --
`mldb_<ref>_memwriter`, created with the first space -- exactly what memory
slice 1 measured the pipeline needs: `CONNECT` on its own database, `USAGE` on
`maludb_core`, `USAGE` and `CREATE` on the space, and `EXECUTE` on the space's
seven write facades, per object. Its password is stored sealed under the KEK
(`db_memwriter`) after the tenant transaction commits; a run that dies between
the two finds no stored password next time and resets the role's, so the
credential is never stranded.

**Search** (slice 3). `maludb.memory_search(space, query, ...)` is the customer's
way in, through PostgREST, as `service_role` only. It is **not** the pipeline's
search facade, which runs as the node superuser behind the guard: it is a
re-implementation of that facade's query (memory slice 0, finding 1b; parity
measured 0 differences in 400 rows), `SECURITY DEFINER`, owned by
`mldb_<ref>_memreader`, a `NOLOGIN` role holding `SELECT` on exactly what the
installed extension's id-based search reaches -- derived by reading the bodies,
the way ADR-077 derives the vector definer's grants, and refused if the
derivation ever needs more than `SELECT`. The space is resolved through a
platform-owned registry, so an unregistered name answers 404 and a registered
one is fenced to its own `owner_schema`. Because the wrapper re-implements
upstream's query, parity is re-proven on the pinned version by a test, and the
reader's grants are re-derived on every extension upgrade.

Ingest (slice 5) is still not reachable.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from services.control_plane import crypto, db, entitlements, maludb, maludb_vectors, provisioning

log = logging.getLogger("maludb.maludb_memory")

# ADR-078: the first version whose pipeline data survives pg_dump.
MEMORY_SINCE = (0, 105, 0)

AUDIT_SPACE_CREATED = "maludb.memory.space_created"

CREDENTIAL_TYPE = "db_memwriter"

# The facades the writer may execute in each space (memory slice 1, finding 1b):
# upload, ingest, extraction request and harvest, and the model configuration the
# extraction path reads. Not search, which is the reader wrapper's (slice 3).
WRITER_FACADES = (
    "maludb_upload_document",
    "maludb_memory_ingest_edge",
    "maludb_memory_request_extraction",
    "maludb_memory_harvest_extractions",
    "maludb_memory_set_model_config",
    "maludb_register_model_provider",
    "maludb_register_model_alias",
)


class MemoryError_(maludb.MaludbError):  # noqa: N801 - `MemoryError` is a builtin
    """A space could not be built, and nothing was left half-built. Shown to the customer."""


@dataclass
class Built:
    project_ref: str
    created: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def _assert_closed(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, schema: str) -> None:
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(maludb.customer_roles(names)),))
        present = [row[0] for row in cur.fetchall()]
        for role in present:
            cur.execute("SELECT has_schema_privilege(%s, %s, 'USAGE') OR has_schema_privilege(%s, %s, 'CREATE')",
                        (role, schema, role, schema))
            if cur.fetchone()[0]:
                raise MemoryError_(
                    f"{role} can use {schema} after building it. Its facades run as the node superuser "
                    "and must be reachable by no customer role; refusing"
                )
            cur.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = %s AND has_function_privilege(%s, p.oid, 'EXECUTE')",
                (schema, role),
            )
            if cur.fetchone()[0]:
                raise MemoryError_(f"{role} can execute functions in {schema} after building it; refusing")


# -- search (slice 3) ---------------------------------------------------------

PRIVATE_SCHEMA = maludb_vectors.PRIVATE_SCHEMA
REGISTRY_TABLE = "memory_space_registry"
API_SCHEMA = maludb.COPY_SCHEMA
MAX_MATCH_COUNT = maludb_vectors.MAX_MATCH_COUNT
# What the search wrapper calls, and the tables it reads itself; everything else
# the reader is granted is found by following these through the installed bodies.
READER_ENTRY_POINTS = ("exact_vector_search_sql",)
READER_READS = {"malu$vector_chunk": set(), "malu$vector_compartment": set(),
                "malu$vector_subject": set(), "malu$vector_verb": set()}
READER_PATH = "maludb_core, public, pg_temp"

_SEARCH_SQL = r"""
CREATE OR REPLACE FUNCTION maludb.memory_search(
    space text, query vector, subject text DEFAULT NULL, verb text DEFAULT NULL,
    namespace text DEFAULT 'default', match_count integer DEFAULT 20, metric text DEFAULT NULL)
RETURNS TABLE(chunk_id bigint, statement_id bigint, document_id bigint, content text,
              distance double precision, similarity double precision, rank integer,
              subject_name text, verb_name text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = {path} AS $f$
#variable_conflict use_column
DECLARE v_schema name;
BEGIN
    SELECT r.schema_name INTO v_schema FROM maludb_private.{registry} r WHERE r.name = memory_search.space;
    IF v_schema IS NULL THEN
        RAISE EXCEPTION 'no memory space %', memory_search.space USING ERRCODE = 'PT404';
    END IF;
    IF query IS NULL THEN
        RAISE EXCEPTION 'query is required' USING ERRCODE = 'PT400';
    END IF;
    IF memory_search.subject IS NULL AND memory_search.verb IS NULL THEN
        RAISE EXCEPTION 'a subject or a verb is required' USING ERRCODE = 'PT400';
    END IF;
    IF match_count IS NULL OR match_count < 1 OR match_count > {max_match} THEN
        RAISE EXCEPTION 'match_count must be between 1 and {max_match}' USING ERRCODE = 'PT400';
    END IF;
    IF memory_search.metric IS NOT NULL AND memory_search.metric NOT IN ('cosine', 'l2', 'inner_product') THEN
        RAISE EXCEPTION 'metric must be cosine, l2 or inner_product' USING ERRCODE = 'PT400';
    END IF;
    RETURN QUERY
    WITH matching AS (
        SELECT c.compartment_id, s.subject_name AS s_name, v.verb_name AS v_name
          FROM malu$vector_compartment c
          JOIN malu$vector_subject s ON s.owner_schema = c.owner_schema AND s.namespace = c.namespace
                                    AND s.subject_id = c.subject_id
          JOIN malu$vector_verb v ON v.owner_schema = c.owner_schema AND v.namespace = c.namespace
                                 AND v.verb_id = c.verb_id
         WHERE c.owner_schema = v_schema
           AND c.namespace = coalesce(memory_search.namespace, 'default')
           AND (memory_search.subject IS NULL OR s.subject_name = memory_search.subject)
           AND (memory_search.verb IS NULL OR v.verb_name = memory_search.verb)
    ), hits AS (
        SELECT h.chunk_id AS h_chunk, h.source_text AS h_text, h.distance AS h_distance,
               h.similarity AS h_similarity, m.compartment_id AS h_compartment, m.s_name, m.v_name
          FROM matching m
          CROSS JOIN LATERAL exact_vector_search_sql(m.compartment_id, query::text::malu_vector,
                                                     match_count, memory_search.metric) h
    ), ranked AS (
        SELECT hits.*, row_number() OVER (ORDER BY h_distance, h_compartment, h_chunk)::integer AS h_rank
          FROM hits
    )
    SELECT r.h_chunk, vc.statement_id, vc.document_id, r.h_text, r.h_distance, r.h_similarity, r.h_rank,
           r.s_name, r.v_name
      FROM ranked r JOIN malu$vector_chunk vc ON vc.chunk_id = r.h_chunk
     WHERE r.h_rank <= match_count
     ORDER BY r.h_rank;
END
$f$;
"""


def _ensure_platform_schema(tenant_conn: psycopg.Connection, schema: str) -> None:
    """A platform-owned schema no customer role may create in; refuses one a customer made first."""
    maludb._refuse_squatted(tenant_conn, schema)  # noqa: SLF001
    tenant_conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    owner = maludb.schema_owner(tenant_conn, schema)
    if owner is None or not owner[0]:
        raise MemoryError_(f"{schema} is not owned by the platform after creating it; refusing")
    tenant_conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))


def register_space(tenant_conn: psycopg.Connection, name: str, schema: str) -> None:
    """Record a space where the search wrapper resolves it. The platform's table, no customer's."""
    _ensure_platform_schema(tenant_conn, PRIVATE_SCHEMA)
    tenant_conn.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {}.{} (name text PRIMARY KEY, schema_name name NOT NULL UNIQUE)"
    ).format(sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(REGISTRY_TABLE)))
    tenant_conn.execute(sql.SQL("REVOKE ALL ON {}.{} FROM PUBLIC").format(
        sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(REGISTRY_TABLE)))
    tenant_conn.execute(sql.SQL(
        "INSERT INTO {}.{} (name, schema_name) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE "
        "SET schema_name = EXCLUDED.schema_name"
    ).format(sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(REGISTRY_TABLE)), (name, schema))


def install_reader(tenant_conn: psycopg.Connection, names: provisioning.TenantNames) -> maludb_vectors.Reach:
    """(Re)build the reader's grants from the installed extension, and the wrapper it owns.

    Grants are revoked and re-granted inside the caller's transaction, so an
    extension upgrade that stops needing a function leaves no grant behind and no
    moment exists at which the reader holds either set.
    """
    provisioning.create_memreader_role(tenant_conn, names)
    reach = maludb_vectors.derive_reach(tenant_conn, entry_points=READER_ENTRY_POINTS, direct=READER_READS)
    # **SELECT only, whatever the walk finds.** `exact_vector_search_sql` has a
    # branch that builds an ANN index for a compartment in approximate mode, and
    # reading its body cannot tell that branch from the exact one: the walk reports
    # INSERT/UPDATE/DELETE on the ANN tables and an UPDATE on compartments that exact
    # search never performs (memory slice 0 ran the reader with SELECT alone). So
    # the reader is granted reading and nothing else -- a search that did take the
    # ANN branch fails on a permission, it never writes -- and `exercise_reader`
    # proves the exact path works with exactly these grants.
    unused = sorted(f"{p} on {t}" for t, privs in reach.tables.items() for p in privs if p != "SELECT")
    if unused:
        log.debug("memory reader: not granting %s (ANN build path only)", ", ".join(unused))
    reach = maludb_vectors.Reach(functions=reach.functions,
                                 tables={table: {"SELECT"} for table in reach.tables})
    role = sql.Identifier(names.memreader)
    for schema in ("maludb_core", "public"):
        for kind in ("TABLES", "FUNCTIONS"):
            tenant_conn.execute(sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(
                sql.SQL(kind), sql.Identifier(schema), role))
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA maludb_core, public, {} TO {}").format(
        sql.Identifier(PRIVATE_SCHEMA), role))
    for table in sorted(reach.tables):
        tenant_conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier("maludb_core", table), role))
    for signature in sorted(reach.functions):
        tenant_conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), role))
    tenant_conn.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
        sql.Identifier(PRIVATE_SCHEMA), sql.Identifier(REGISTRY_TABLE), role))

    _ensure_platform_schema(tenant_conn, API_SCHEMA)
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO service_role").format(sql.Identifier(API_SCHEMA)))
    tenant_conn.execute(_SEARCH_SQL.format(path=READER_PATH, registry=REGISTRY_TABLE, max_match=MAX_MATCH_COUNT))
    signature = "maludb.memory_search(text,vector,text,text,text,integer,text)"
    tenant_conn.execute(sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(sql.SQL(signature), role))
    tenant_conn.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(sql.SQL(signature)))
    tenant_conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO service_role").format(sql.SQL(signature)))
    assert_reader(tenant_conn, names, reach)
    return reach


def assert_reader(tenant_conn: psycopg.Connection, names: provisioning.TenantNames,
                  reach: maludb_vectors.Reach) -> None:
    """Refuse unless the reader and its wrapper are exactly what ADR-079 decision 3 describes."""
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s", (names.memreader,))
        row = cur.fetchone()
        if row is None or any(row):
            raise MemoryError_(f"{names.memreader} is missing or holds a login or role attribute it must not")
        cur.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                    "WHERE r.rolname = %s", (names.memreader,))
        if cur.fetchone()[0]:
            raise MemoryError_(f"{names.memreader} is a member of another role; it must be of none")
        cur.execute(
            "SELECT n.nspname, c.relname, p.privilege_type FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) p JOIN pg_roles r ON r.oid = p.grantee "
            "WHERE r.rolname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')", (names.memreader,))
        extra = sorted(
            f"{privilege} on {schema}.{table}" for schema, table, privilege in cur.fetchall()
            if not (privilege == "SELECT" and ((schema == "maludb_core" and table in reach.tables)
                                               or (schema == PRIVATE_SCHEMA and table == REGISTRY_TABLE)))
        )
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a "
            "JOIN pg_roles r ON r.oid = a.grantee JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE r.rolname = %s AND n.nspname <> %s", (names.memreader, API_SCHEMA))
        extra += sorted(f"EXECUTE on {sig}" for (sig,) in cur.fetchall() if sig not in reach.functions)
        if extra:
            raise MemoryError_(f"{names.memreader} holds more than the search reaches: {', '.join(extra)}; refusing")
        cur.execute(
            "SELECT r FROM unnest(%s::text[]) r WHERE r <> 'service_role' "
            "AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) "
            "AND has_function_privilege(r, 'maludb.memory_search(text,vector,text,text,text,integer,text)', "
            "'EXECUTE')",
            (list(maludb.customer_roles(names)),))
        callers = [row[0] for row in cur.fetchall()]
        if callers:
            raise MemoryError_(f"{', '.join(callers)} can call maludb.memory_search; only service_role may")


def exercise_reader(tenant_conn: psycopg.Connection, name: str, schema: str) -> None:
    """Search a probe memory as service_role, and roll it all back.

    What reading the bodies cannot prove: that the grants are enough for a real
    hit through `exact_vector_search_sql`. The probe is written into the space's
    own `owner_schema`, so the fence is on the path too.
    """
    tenant_conn.execute("SAVEPOINT memory_reader")
    try:
        tenant_conn.execute(sql.SQL("SET LOCAL search_path = {}, maludb_core, public").format(sql.Identifier(schema)))
        ns = f"platform-probe-{uuid.uuid4().hex}"
        cid = tenant_conn.execute(
            "SELECT maludb_core.register_vector_compartment(%s, 'probe', 'probe', 3, 'probe', 'cosine')", (ns,)
        ).fetchone()[0]
        tenant_conn.execute(
            "SELECT maludb_core.register_vector_chunk(%s, 'probe', '[1,2,3]'::maludb_core.malu_vector, 'probe')",
            (cid,),
        )
        tenant_conn.execute("SET LOCAL ROLE service_role")
        found = tenant_conn.execute(
            "SELECT count(*) FROM maludb.memory_search(%s, '[1,2,3]'::vector, 'probe', NULL, %s, 5)", (name, ns)
        ).fetchone()[0]
        if found != 1:
            raise MemoryError_(f"the memory search wrapper found {found} probe row(s) in {name}, not 1")
    except psycopg.errors.InsufficientPrivilege as exc:
        tenant_conn.execute("ROLLBACK TO SAVEPOINT memory_reader")
        raise MemoryError_(f"memory search could not run as service_role: {str(exc).splitlines()[0]}") from None
    except Exception:
        tenant_conn.execute("ROLLBACK TO SAVEPOINT memory_reader")
        raise
    tenant_conn.execute("ROLLBACK TO SAVEPOINT memory_reader")


def publish(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, name: str, schema: str) -> None:
    """Register the space, (re)build the reader and its wrapper, prove a search, and serve `maludb`."""
    register_space(tenant_conn, name, schema)
    install_reader(tenant_conn, names)
    exercise_reader(tenant_conn, name, schema)
    maludb._expose(tenant_conn, names)  # noqa: SLF001


# Definers `enable_memory_schema` builds with the space itself first on their
# `search_path`. The writer holds `CREATE` on the space, so an unqualified name in
# such a body could resolve to an object the writer created -- superuser
# execution. Each entry was read and found fully qualified at the version noted
# (memory slice 1, finding 3); any other is refused until someone has read it.
REVIEWED_SPACE_FIRST_DEFINERS = {
    "maludb_document_graph_backfill": "0.105.0",
}


def assert_definer_paths(tenant_conn: psycopg.Connection, schema: str) -> None:
    """Refuse a space whose definers could be steered by what the writer creates in it.

    Two ways: a definer in the space with no pinned `search_path` at all, which
    runs with whatever the caller set; or one that puts the space on its path and
    has not been reviewed. Checked on every build and every extension upgrade, so
    a release that adds either cannot turn the writer's `CREATE` into superuser
    execution without anyone noticing.
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname, coalesce((SELECT c FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%%' "
            "LIMIT 1), '') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s AND p.prosecdef",
            (schema,),
        )
        definers = cur.fetchall()
    unpinned = sorted(name for name, path in definers if not path)
    if unpinned:
        raise MemoryError_(f"{schema} has SECURITY DEFINER function(s) with no pinned search_path: "
                           f"{', '.join(unpinned)}; refusing")
    space_first = sorted(
        name for name, path in definers
        if schema in [part.strip().strip('"') for part in path.removeprefix("search_path=").split(",")]
        and name not in REVIEWED_SPACE_FIRST_DEFINERS
    )
    if space_first:
        raise MemoryError_(
            f"{schema} has SECURITY DEFINER function(s) that search the space itself, which the memory "
            f"writer can create objects in, and have not been reviewed: {', '.join(space_first)}. Read each "
            "body for unqualified references, then add it to REVIEWED_SPACE_FIRST_DEFINERS"
        )


def reverify_spaces(tenant_conn: psycopg.Connection, names: provisioning.TenantNames) -> list[str]:
    """Re-enable every platform-built space after an extension upgrade. Returns the schemas.

    Found from the tenant's own record of enabled memory schemas rather than the
    control plane, which an upgrade run does not read. A `mem_` schema a customer
    owns is left alone, as the data-model schema is: re-enabling would build
    superuser-owned definers into a customer's schema.
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT e.schema_name FROM maludb_core.\"malu$enabled_schema\" e "
            "JOIN pg_namespace n ON n.nspname = e.schema_name JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE e.schema_name LIKE 'mem\\_%%' AND r.rolsuper ORDER BY 1"
        )
        spaces = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (names.memwriter,))
        writer = cur.fetchone()[0]
    for schema in spaces:
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT enabled_version FROM maludb_core.enable_memory_schema(%s)", (schema,))
        assert_definer_paths(tenant_conn, schema)
        _assert_closed(tenant_conn, names, schema)
        if writer:
            grant_writer(tenant_conn, names, schema)
        # The search wrapper re-implements upstream's query, so every upgrade
        # re-derives what it reads and proves it still finds a memory.
        publish(tenant_conn, names, schema.removeprefix("mem_"), schema)
    return spaces


def grant_writer(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, schema: str) -> int:
    """The writer's grants on one space, and nothing more. Returns how many facades.

    Refuses if the space lacks any facade the writer needs: a release that renamed
    one would otherwise leave a space the worker cannot write, found only when a
    customer's ingest fails.
    """
    role = sql.Identifier(names.memwriter)
    tenant_conn.execute(sql.SQL("GRANT USAGE ON SCHEMA maludb_core TO {}").format(role))
    tenant_conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(sql.Identifier(schema), role))
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text, p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s AND p.proname = ANY(%s)",
            (schema, list(WRITER_FACADES)),
        )
        functions = cur.fetchall()
    missing = sorted(set(WRITER_FACADES) - {name for _, name in functions})
    if missing:
        raise MemoryError_(f"{schema} lacks the facade(s) the memory writer needs: {', '.join(missing)}")
    for signature, _ in functions:
        tenant_conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), role))
    return len(functions)


def build_space(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, schema: str) -> str:
    """Build one space inside the caller's transaction. Returns the memory schema version.

    The writer role must already exist in the cluster; `build_pending` creates it.
    """
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'maludb_core'")
        row = cur.fetchone()
    if row is None:
        raise MemoryError_("maludb_core is not installed in this tenant database (ADR-015)")
    if maludb.version_tuple(row[0]) < MEMORY_SINCE:
        raise MemoryError_(
            f"this tenant has maludb_core {row[0]}; memory spaces need "
            f"{'.'.join(map(str, MEMORY_SINCE))} or later, so their data survives a move or restore "
            "(ADR-078). Run `cp-manage extension upgrade` for its node first"
        )

    maludb_vectors.reverify(tenant_conn, names)
    maludb._refuse_squatted(tenant_conn, schema)  # noqa: SLF001
    tenant_conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    owner = maludb.schema_owner(tenant_conn, schema)
    if owner is None or not owner[0]:
        raise MemoryError_(f"{schema} is not owned by the platform after creating it; refusing")
    tenant_conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT enabled_version FROM maludb_core.enable_memory_schema(%s)", (schema,))
        version = cur.fetchone()[0]
    assert_definer_paths(tenant_conn, schema)
    _assert_closed(tenant_conn, names, schema)
    grant_writer(tenant_conn, names, schema)
    publish(tenant_conn, names, schema.removeprefix("mem_"), schema)
    return version


def _writer_password(conn: psycopg.Connection, project_id: uuid.UUID, key_ring: crypto.KeyRing) -> tuple[str, bool]:
    """The stored writer password, or a new one to store. (password, is_new)."""
    try:
        return provisioning.load_credential(conn, project_id=project_id, credential_type=CREDENTIAL_TYPE,
                                            key_ring=key_ring), False
    except provisioning.ProvisioningError:
        return provisioning.generate_password(), True


def build_pending(conn: psycopg.Connection, *, project_id: uuid.UUID, tenant_connect,
                  key_ring: crypto.KeyRing) -> Built:
    """Build every pending space of a project, each in its own tenant transaction.

    One space's refusal does not stop the others: each is recorded `failed` with
    the platform's own sentence, and the rest are built. The control-plane row is
    marked active only after its tenant transaction commits, so a failure leaves
    a pending row a re-run finishes -- `enable_memory_schema` is idempotent.
    """
    project = maludb._project(conn, project_id)  # noqa: SLF001
    if project["status"] not in maludb.ENABLEABLE_STATUSES:
        raise MemoryError_(f"project is {project['status']}; ask again once that operation has finished")
    allowed = entitlements.for_project(conn, project_id)
    if not allowed.maludb_memory:
        raise MemoryError_("this project's plan does not include MaluDB memory spaces")
    pending = db.query(
        conn, "SELECT id, name, schema_name FROM memory_spaces WHERE project_id = %s AND state = 'pending' "
              "ORDER BY requested_at", (project_id,),
    )
    built = Built(project_ref=project["project_ref"])
    if not pending:
        return built

    locked = db.one(conn, "SELECT pg_try_advisory_lock_shared(%s, %s) AS ok",
                    (maludb.NODE_LOCK_NAMESPACE, project["node_id"]))["ok"]
    conn.commit()
    if not locked:
        raise MemoryError_("an extension upgrade is running on this project's node; ask again once it finishes")
    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        for space in pending:
            password, is_new = _writer_password(conn, project_id, key_ring)
            tenant_conn = tenant_connect(project["database_name"])
            try:
                tenant_conn.autocommit = False
                # Re-stated on every build: idempotent, and a role that drifted is put back.
                provisioning.create_memwriter_role(tenant_conn, names, password=password)
                provisioning.grant_memwriter_connect(tenant_conn, names)
                version = build_space(tenant_conn, names, space["schema_name"])
                tenant_conn.commit()
            except maludb.MaludbError as exc:
                tenant_conn.rollback()
                db.execute(conn, "UPDATE memory_spaces SET state = 'failed', detail = %s WHERE id = %s",
                           (str(exc), space["id"]))
                conn.commit()
                built.failed[space["name"]] = str(exc)
                continue
            except Exception:
                tenant_conn.rollback()
                db.execute(conn, "UPDATE memory_spaces SET state = 'failed', detail = %s WHERE id = %s",
                           ("the platform could not build this space; it has been logged and can be "
                            "asked for again", space["id"]))
                conn.commit()
                raise
            finally:
                tenant_conn.close()
            db.execute(
                conn,
                "UPDATE projects SET maludb_memory_enabled = TRUE, "
                "maludb_memory_enabled_at = coalesce(maludb_memory_enabled_at, now()) WHERE id = %s",
                (project_id,),
            )
            if is_new:
                provisioning.store_credential(conn, project_id=project_id, credential_type=CREDENTIAL_TYPE,
                                              role_name=names.memwriter, secret=password, key_ring=key_ring)
            db.execute(
                conn,
                "UPDATE memory_spaces SET state = 'active', active_at = now(), memory_schema_version = %s, "
                "       detail = NULL WHERE id = %s",
                (version, space["id"]),
            )
            db.execute(
                conn,
                "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
                "VALUES (%s, 'system', %s, %s)",
                (project_id, AUDIT_SPACE_CREATED, Jsonb({"space": space["name"], "memory_schema_version": version})),
            )
            conn.commit()
            built.created.append(space["name"])
    finally:
        db.one(conn, "SELECT pg_advisory_unlock_shared(%s, %s) AS ok",
               (maludb.NODE_LOCK_NAMESPACE, project["node_id"]))
        conn.commit()
    return built


__all__ = [
    "AUDIT_SPACE_CREATED",
    "CREDENTIAL_TYPE",
    "MEMORY_SINCE",
    "WRITER_FACADES",
    "Built",
    "MemoryError_",
    "build_pending",
    "build_space",
    "REVIEWED_SPACE_FIRST_DEFINERS",
    "READER_ENTRY_POINTS",
    "assert_reader",
    "exercise_reader",
    "install_reader",
    "publish",
    "register_space",
    "assert_definer_paths",
    "grant_writer",
    "reverify_spaces",
]
