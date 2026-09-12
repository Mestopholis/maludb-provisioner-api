"""MaluDB-native features for a project: the data-model graph (ADR-074).

Phase 12 slice 2 turns the surface on. What a customer can *read* arrives in
slice 3, and the route they call to ask for it in slice 4.

## What "enabled" means, and what it does not

`maludb_core` is installed in every tenant database whether or not anything here
runs (ADR-015). Enabling builds the extension's memory-schema facades into one
fixed, **platform-owned** schema, `maludb_memory`, and records that the project
has them. ADR-074 amended ADR-015 to allow exactly this flag: the extension is
platform, the surface on top of it is opt-in.

**Nothing is exposed by enabling.** The facades are `SECURITY DEFINER`, owned by
the node superuser, and Phase 12 slice 0 found them closed to every customer
role by the extension's own ACLs. That is checked again on every enablement
rather than trusted, because an upstream release could change it.

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
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements, provisioning

log = logging.getLogger("maludb.maludb")

# ADR-074 decision 2's fixed, platform-owned schema.
MEMORY_SCHEMA = "maludb_memory"

# The facades that make up the data-model graph. They arrived in 0.104.0; a
# schema enabled at an older version has none to find.
DATAMODEL_FACADES = ("maludb_datamodel_describe", "maludb_datamodel_refresh")
DATAMODEL_SINCE = (0, 104, 0)

# States in which a project's database exists and nothing else is changing it.
ENABLEABLE_STATUSES = ("PROVISIONED", "ACTIVE")

# One advisory-lock key per node, shared with `extension_upgrade`. An upgrade
# takes it exclusively; an enablement takes it shared. So enablements on
# different projects do not wait on each other, and neither runs while an
# upgrade is re-enabling schemas on the same node -- which could otherwise read
# the memory schema as absent a moment before this commits it, and strand it on
# the old facades.
NODE_LOCK_NAMESPACE = 0x4D455855  # "MEXU"

AUDIT_ENABLED = "maludb.datamodel.enabled"


class MaludbError(RuntimeError):
    """The data-model graph could not be enabled, and nothing was left half-built."""


@dataclass
class Enablement:
    project_ref: str
    changed: bool
    memory_schema_version: str
    detail: str


def version_tuple(text: str) -> tuple[int, ...]:
    """`0.104.0` as a comparable tuple. Unparseable parts compare as zero."""
    return tuple(int(part) if part.isdigit() else 0 for part in text.split("."))


def memory_schema_owner(tenant_conn: psycopg.Connection) -> tuple[bool, str] | None:
    """Whether the memory schema exists, and whether a superuser owns it.

    None when there is no such schema; otherwise (owned_by_superuser, owner).
    Customer roles are never superusers (docs/MALUDB.md), which is what makes
    `rolsuper` the right test for "the platform made this".
    """
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolsuper, r.rolname FROM pg_namespace n "
            "JOIN pg_roles r ON r.oid = n.nspowner WHERE n.nspname = %s",
            (MEMORY_SCHEMA,),
        )
        row = cur.fetchone()
    return None if row is None else (bool(row[0]), row[1])


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
               maludb_datamodel_enabled, maludb_datamodel_enabled_at
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
            version = _build(tenant_conn, names)
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
    )


def _build(tenant_conn: psycopg.Connection, names) -> str:
    """Build or refresh the memory schema inside the caller's transaction."""
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

    owner = memory_schema_owner(tenant_conn)
    if owner is not None and not owner[0]:
        raise MaludbError(
            f"a schema named {MEMORY_SCHEMA} already exists in this project and belongs to "
            f"{owner[1]}, not the platform. The data-model graph is built into that name, and "
            "building it into a schema the project owns would put platform code where the "
            f"project can change it. Rename or drop the project's {MEMORY_SCHEMA} schema, "
            "then enable again."
        )
    if owner is None:
        # Created here, over the superuser connection, so the platform owns it --
        # which is the property the check above exists to protect.
        tenant_conn.execute(f'CREATE SCHEMA "{MEMORY_SCHEMA}"')

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

    # Slice 0 found the facades closed to every customer role. Asserted on every
    # enablement, inside the transaction, because an upstream release that
    # granted them would otherwise be discovered by a customer.
    with tenant_conn.cursor() as cur:
        for role in customer_roles(names):
            cur.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, MEMORY_SCHEMA)
            )
            if cur.fetchone()[0]:
                raise MaludbError(
                    f"{role} can use {MEMORY_SCHEMA} after enabling. The facades there run as "
                    "the node superuser and must be reachable by no customer role; refusing"
                )
    return version


__all__ = [
    "AUDIT_ENABLED",
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
    "version_tuple",
]
