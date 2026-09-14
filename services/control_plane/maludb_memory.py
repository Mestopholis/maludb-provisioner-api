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

Nothing here makes a space reachable. Search (slice 3) and ingest (slice 5) are
what customers call; until then a space is built, recorded, and closed.
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
    "assert_definer_paths",
    "grant_writer",
    "reverify_spaces",
]
