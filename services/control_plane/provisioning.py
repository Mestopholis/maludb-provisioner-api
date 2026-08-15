"""Tenant provisioning: roles, database, lockdown.

Implements `specs/tenant-role-model.md` against a real MaluDB node.

This module builds SQL identifiers from project metadata, which `AGENTS.md`
names a primary review concern. Two rules, applied without exception:

- `project_ref` is validated against a strict alphabet before it is used for
  anything, and `database_name_for` raises rather than returning a name built
  from an invalid ref;
- every generated identifier goes through `psycopg.sql.Identifier` and every
  generated password through `psycopg.sql.Literal`. There is no string
  formatting of SQL anywhere in this file. DDL cannot take bind parameters, so
  composition is the only safe route.

The ADRs this enforces, each verified empirically during the Phase 00 spike:

- ADR-014: PostgreSQL grants CONNECT to PUBLIC on every new database, so every
  tenant database is reachable by every role on the node until revoked.
- ADR-015: maludb_core goes into every tenant database.
- ADR-016: the three Supabase role names are shared and privilege-free; grants
  involving them are one-directional, never the reverse.
- ADR-017: plan settings apply to the *login* role, scoped IN DATABASE.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from services.control_plane import crypto, db, models

log = logging.getLogger(__name__)

SHARED_ROLES = ("anon", "authenticated", "service_role")

# Password length for generated tenant credentials. AGENTS.md requires
# cryptographic generation; 32 bytes of urlsafe base64 is ~43 characters.
PASSWORD_BYTES = 32

REQUIRED_EXTENSIONS = ("maludb_core", "vector", "btree_gist", "pg_trgm", "pgcrypto")


class ProvisioningError(RuntimeError):
    """Provisioning could not complete. Never carries credential material."""


@dataclass(frozen=True)
class TenantNames:
    """Every identifier derived from one project ref, validated at construction."""

    project_ref: str
    database: str
    authenticator: str
    auth: str
    admin: str

    @classmethod
    def for_ref(cls, project_ref: str) -> TenantNames:
        # Raises on anything outside the strict alphabet.
        database = models.database_name_for(project_ref)
        return cls(
            project_ref=project_ref,
            database=database,
            authenticator=f"{database}_authenticator",
            auth=f"{database}_auth",
            admin=f"{database}_admin",
        )


def generate_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


# --------------------------------------------------------------------------
# Node-level, once per node
# --------------------------------------------------------------------------


def ensure_shared_roles(admin_conn: psycopg.Connection) -> None:
    """Create the three privilege-free Supabase role names (ADR-016).

    Shared because migrated RLS policies name them literally and PostgreSQL
    roles are cluster-scoped; safe because they hold no privilege of their own
    and every grant to them attaches to a per-database object.

    service_role carries BYPASSRLS, matching Supabase. That is safe *only*
    because it is NOLOGIN and reachable solely via SET ROLE from a tenant's own
    authenticator, inside a session already bound to that tenant's database by
    the ADR-014 lockdown. Remove either control and this becomes a cross-tenant
    RLS bypass.
    """
    for role in SHARED_ROLES:
        attributes = sql.SQL("NOLOGIN BYPASSRLS") if role == "service_role" else sql.SQL("NOLOGIN")
        admin_conn.execute(
            sql.SQL(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name}) "
                "THEN EXECUTE {stmt}; END IF; END $$"
            ).format(
                name=sql.Literal(role),
                stmt=sql.Literal(
                    sql.SQL("CREATE ROLE {} {}").format(sql.Identifier(role), attributes).as_string(admin_conn)
                ),
            )
        )


# --------------------------------------------------------------------------
# Per project
# --------------------------------------------------------------------------


def create_roles(admin_conn: psycopg.Connection, names: TenantNames, *, passwords: dict[str, str],
                 connection_limits: dict[str, int]) -> None:
    """Create the three per-project roles and grant the shared names to the authenticator."""
    for key, role in (("authenticator", names.authenticator), ("auth", names.auth)):
        admin_conn.execute(
            sql.SQL("CREATE ROLE {role} LOGIN PASSWORD {password} CONNECTION LIMIT {limit} NOINHERIT").format(
                role=sql.Identifier(role),
                password=sql.Literal(passwords[key]),
                limit=sql.Literal(int(connection_limits.get(key, 10))),
            )
        )
    admin_conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(names.admin)))

    # ADR-016: one-directional only. Granting a per-tenant role TO a shared role
    # would make every tenant's `authenticated` a member of it.
    admin_conn.execute(
        sql.SQL("GRANT {anon}, {authenticated}, {service} TO {authenticator}").format(
            anon=sql.Identifier("anon"),
            authenticated=sql.Identifier("authenticated"),
            service=sql.Identifier("service_role"),
            authenticator=sql.Identifier(names.authenticator),
        )
    )


def create_database(admin_conn: psycopg.Connection, names: TenantNames, *, owner: str) -> None:
    """Create the tenant database, owned by the platform (ADR-004)."""
    # CREATE DATABASE cannot run inside a transaction block.
    admin_conn.commit()
    previous = admin_conn.autocommit
    admin_conn.autocommit = True
    try:
        admin_conn.execute(
            sql.SQL("CREATE DATABASE {db} OWNER {owner}").format(
                db=sql.Identifier(names.database), owner=sql.Identifier(owner)
            )
        )
    finally:
        admin_conn.autocommit = previous


def lock_down_database(admin_conn: psycopg.Connection, names: TenantNames) -> None:
    """ADR-014. Without this every role on the node can reach this database.

    Verified during the Phase 00 spike: a role with no grants of any kind
    connected to an unrelated tenant database and read its catalog.
    """
    admin_conn.execute(
        sql.SQL("REVOKE CONNECT ON DATABASE {db} FROM PUBLIC").format(db=sql.Identifier(names.database))
    )
    admin_conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {authenticator}, {auth}").format(
            db=sql.Identifier(names.database),
            authenticator=sql.Identifier(names.authenticator),
            auth=sql.Identifier(names.auth),
        )
    )


def apply_plan_settings(admin_conn: psycopg.Connection, names: TenantNames, *, settings: dict[str, Any]) -> None:
    """ADR-017: settings apply at login, to the login role, scoped IN DATABASE.

    Applying them to `authenticated` would silently do nothing, because that
    role is entered through SET ROLE rather than login. These are defaults, not
    enforcement -- most are session-settable by any client holding direct SQL.
    """
    for setting, value in settings.items():
        if value is None:
            continue
        for role in (names.authenticator, names.auth):
            admin_conn.execute(
                sql.SQL("ALTER ROLE {role} IN DATABASE {db} SET {setting} = {value}").format(
                    role=sql.Identifier(role),
                    db=sql.Identifier(names.database),
                    setting=sql.Identifier(setting),
                    value=sql.Literal(str(value)),
                )
            )


def install_extension(tenant_conn: psycopg.Connection) -> dict[str, str]:
    """ADR-015. Requires superuser; maludb_core is not a trusted extension."""
    tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
    tenant_conn.commit()
    with tenant_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname = ANY(%s)",
            (list(REQUIRED_EXTENSIONS),),
        )
        return {row["extname"]: row["extversion"] for row in cur.fetchall()}


def verify_isolation(admin_conn: psycopg.Connection, names: TenantNames) -> None:
    """Refuse to call a project provisioned unless isolation actually holds.

    Checks the properties, not the statements that were meant to establish
    them: a lockdown that silently failed must not reach a customer.
    """
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT has_database_privilege('public', %s, 'CONNECT') AS public_can_connect",
            (names.database,),
        )
        if cur.fetchone()["public_can_connect"]:
            raise ProvisioningError(f"{names.database}: PUBLIC still has CONNECT after lockdown")

        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = %s",
            (names.admin,),
        )
        row = cur.fetchone()
        if row is None:
            raise ProvisioningError(f"{names.admin}: tenant admin role missing")
        elevated = [attribute for attribute, held in row.items() if held]
        if elevated:
            raise ProvisioningError(f"{names.admin}: tenant role has elevated attributes {elevated}")

        # ADR-016: no per-tenant role may be a member of a shared role.
        for shared in SHARED_ROLES:
            cur.execute("SELECT pg_has_role(%s, %s, 'member') AS is_member", (shared, names.admin))
            if cur.fetchone()["is_member"]:
                raise ProvisioningError(f"{shared} is a member of {names.admin}; grant direction is inverted")

        # The customer must never reach a superuser or BYPASSRLS role.
        for role in (names.authenticator, names.auth, names.admin):
            cur.execute(
                """
                SELECT r.rolname FROM pg_roles r
                 WHERE (r.rolsuper OR r.rolbypassrls)
                   AND r.rolname <> 'service_role'
                   AND pg_has_role(%s, r.oid, 'member')
                """,
                (role,),
            )
            escalations = [r["rolname"] for r in cur.fetchall()]
            if escalations:
                raise ProvisioningError(f"{role} can reach privileged roles: {escalations}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _store_credential(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    credential_type: str,
    role_name: str | None,
    secret: str,
    key_ring: crypto.KeyRing,
) -> None:
    sealed = key_ring.seal(
        secret.encode(),
        aad=crypto.aad_for("project_credentials", "ciphertext", f"{project_id}:{credential_type}"),
    )
    db.execute(
        conn,
        """
        INSERT INTO project_credentials
            (id, project_id, credential_type, role_name, ciphertext, nonce, key_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (uuid.uuid4(), project_id, credential_type, role_name,
         sealed.ciphertext, sealed.nonce, sealed.key_version),
    )


def provision_tenant(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    platform_owner: str,
    plan_settings: dict[str, Any] | None = None,
    connection_limits: dict[str, int] | None = None,
) -> TenantNames:
    """Provision one tenant. Returns the generated names, never the secrets.

    Credentials are written encrypted to project_credentials and are not
    returned, logged, or included in any error. A caller that needs them reads
    them back through the key ring deliberately.
    """
    project = db.one(conn, "SELECT project_ref, status, database_name FROM projects WHERE id = %s", (project_id,))
    if project is None:
        raise ProvisioningError("project does not exist")
    if project["database_name"] is not None:
        raise ProvisioningError(f"project already has database {project['database_name']}")

    names = TenantNames.for_ref(project["project_ref"])
    passwords = {"authenticator": generate_password(), "auth": generate_password(), "admin": generate_password()}

    try:
        ensure_shared_roles(admin_conn)
        admin_conn.commit()

        db.execute(conn, "UPDATE projects SET status = 'ROLES_CREATING' WHERE id = %s", (project_id,))
        conn.commit()
        create_roles(admin_conn, names, passwords=passwords, connection_limits=connection_limits or {})
        admin_conn.commit()

        db.execute(conn, "UPDATE projects SET status = 'DATABASE_CREATING' WHERE id = %s", (project_id,))
        conn.commit()
        create_database(admin_conn, names, owner=platform_owner)
        # Record the database as soon as it exists: from here on, cleanup must
        # drop it rather than forget it (slice 1 finding).
        db.execute(conn, "UPDATE projects SET database_name = %s WHERE id = %s", (names.database, project_id))
        conn.commit()

        lock_down_database(admin_conn, names)
        apply_plan_settings(admin_conn, names, settings=plan_settings or {})
        admin_conn.commit()
    except psycopg.Error:
        # Never let driver text reach the caller or the job record: it can carry
        # the statement, and CREATE ROLE statements embed the password literal.
        log.error("provisioning failed for project %s at role/database stage", project_id)
        raise ProvisioningError(f"provisioning failed for {names.database} while creating roles or database") from None

    return names


def finalise_tenant(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    tenant_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    names: TenantNames,
    passwords: dict[str, str],
    key_ring: crypto.KeyRing,
) -> dict[str, str]:
    """Install the extension, verify isolation, persist credentials, mark provisioned."""
    db.execute(conn, "UPDATE projects SET status = 'BOOTSTRAPPING' WHERE id = %s", (project_id,))
    conn.commit()
    versions = install_extension(tenant_conn)

    db.execute(conn, "UPDATE projects SET status = 'KEYS_CONFIGURING' WHERE id = %s", (project_id,))
    conn.commit()
    for credential_type, role in (
        ("db_authenticator", names.authenticator),
        ("db_auth", names.auth),
        ("db_admin", names.admin),
    ):
        key = credential_type.removeprefix("db_")
        _store_credential(
            conn, project_id=project_id, credential_type=credential_type,
            role_name=role, secret=passwords[key], key_ring=key_ring,
        )
    conn.commit()

    db.execute(conn, "UPDATE projects SET status = 'VALIDATING' WHERE id = %s", (project_id,))
    conn.commit()
    verify_isolation(admin_conn, names)

    db.execute(
        conn,
        """
        UPDATE projects
           SET status = 'PROVISIONED', extension_versions = %s, provisioned_at = now()
         WHERE id = %s
        """,
        (psycopg.types.json.Jsonb(versions), project_id),
    )
    conn.commit()
    return versions
