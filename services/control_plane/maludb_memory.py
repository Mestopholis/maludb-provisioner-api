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

from services.control_plane import db, entitlements, maludb, maludb_vectors, provisioning

log = logging.getLogger("maludb.maludb_memory")

# ADR-078: the first version whose pipeline data survives pg_dump.
MEMORY_SINCE = (0, 105, 0)

AUDIT_SPACE_CREATED = "maludb.memory.space_created"


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


def build_space(tenant_conn: psycopg.Connection, names: provisioning.TenantNames, schema: str) -> str:
    """Build one space inside the caller's transaction. Returns the memory schema version."""
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
    _assert_closed(tenant_conn, names, schema)
    return version


def build_pending(conn: psycopg.Connection, *, project_id: uuid.UUID, tenant_connect) -> Built:
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
            tenant_conn = tenant_connect(project["database_name"])
            try:
                tenant_conn.autocommit = False
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


__all__ = ["AUDIT_SPACE_CREATED", "MEMORY_SINCE", "Built", "MemoryError_", "build_pending", "build_space"]
