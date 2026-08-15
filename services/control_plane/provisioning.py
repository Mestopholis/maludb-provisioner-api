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


def role_exists(admin_conn: psycopg.Connection, role: str) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        return cur.fetchone() is not None


def create_roles(admin_conn: psycopg.Connection, names: TenantNames, *, passwords: dict[str, str],
                 connection_limits: dict[str, int]) -> None:
    """Create the three per-project roles and grant the shared names to the authenticator.

    Re-runnable, because a retry arrives here with the roles already created.
    An existing role has its password reset to the one being persisted in the
    same step rather than being left alone: if the previous attempt died between
    creating the roles and storing the credentials, the old password is gone and
    nothing can ever authenticate as that role again. Resetting recovers it;
    skipping strands the tenant.
    """
    for key, role in (("authenticator", names.authenticator), ("auth", names.auth)):
        verb = sql.SQL("ALTER ROLE") if role_exists(admin_conn, role) else sql.SQL("CREATE ROLE")
        admin_conn.execute(
            sql.SQL("{verb} {role} LOGIN PASSWORD {password} CONNECTION LIMIT {limit} NOINHERIT").format(
                verb=verb,
                role=sql.Identifier(role),
                password=sql.Literal(passwords[key]),
                limit=sql.Literal(int(connection_limits.get(key, 10))),
            )
        )
    if not role_exists(admin_conn, names.admin):
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


def database_exists(admin_conn: psycopg.Connection, database: str) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
        return cur.fetchone() is not None


def create_database(admin_conn: psycopg.Connection, names: TenantNames, *, owner: str) -> None:
    """Create the tenant database, owned by the platform (ADR-004).

    CREATE DATABASE has no IF NOT EXISTS, so a retry that got this far last time
    would otherwise fail here permanently. Existence is checked instead -- and
    the database is left alone if present, never recreated: by this point it may
    hold customer data.
    """
    if database_exists(admin_conn, names.database):
        return
    # CREATE DATABASE cannot run inside a transaction block, and psycopg refuses
    # to toggle autocommit while one is open -- which the check above just
    # started. Commit before flipping, not before checking.
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


def installed_extensions(tenant_conn: psycopg.Connection) -> dict[str, str]:
    """Read the recorded extension set without changing anything.

    A retry that skips the bootstrap step still has to record versions against
    the project, and re-running CREATE EXTENSION to find them out would be a
    write on a path that should not need one.
    """
    with tenant_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname = ANY(%s)",
            (list(REQUIRED_EXTENSIONS),),
        )
        return {row["extname"]: row["extversion"] for row in cur.fetchall()}


def install_extension(tenant_conn: psycopg.Connection) -> dict[str, str]:
    """ADR-015. Requires superuser; maludb_core is not a trusted extension."""
    tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
    tenant_conn.commit()
    return installed_extensions(tenant_conn)


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

        # Every tenant role, not just the admin one. The authenticator and auth
        # roles are the two that actually log in, so they are the ones a
        # customer's workers connect as -- checking only the admin role left
        # the most exposed roles unverified, and made the test suite stricter
        # than the production gate.
        for role in (names.authenticator, names.auth, names.admin):
            cur.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname = %s",
                (role,),
            )
            row = cur.fetchone()
            if row is None:
                raise ProvisioningError(f"{role}: tenant role missing")
            elevated = [
                attribute
                for attribute, held in row.items()
                if held and attribute in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls")
            ]
            if elevated:
                raise ProvisioningError(f"{role}: tenant role has elevated attributes {elevated}")

        # ADR-016: the shared names carry no privilege of their own, and their
        # safety rests on NOLOGIN. service_role's BYPASSRLS is the single
        # documented exception, asserted by name so the exemption is visible
        # rather than implied.
        for shared in SHARED_ROLES:
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (shared,),
            )
            row = cur.fetchone()
            if row is None:
                raise ProvisioningError(f"{shared}: shared role missing")
            if row["rolcanlogin"]:
                raise ProvisioningError(
                    f"{shared} can log in; its isolation depends on being reachable only via SET ROLE"
                )
            for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole"):
                if row[attribute]:
                    raise ProvisioningError(f"{shared} holds {attribute}; shared roles must be privilege-free")
            if row["rolbypassrls"] and shared != "service_role":
                raise ProvisioningError(f"{shared} holds BYPASSRLS; only service_role may")

            # No per-tenant role may be a member of a shared role.
            for tenant_role in (names.authenticator, names.auth, names.admin):
                cur.execute("SELECT pg_has_role(%s, %s, 'member') AS is_member", (shared, tenant_role))
                if cur.fetchone()["is_member"]:
                    raise ProvisioningError(f"{shared} is a member of {tenant_role}; grant direction is inverted")

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


def store_credential(
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
    # A retry resets the role's password, so the previously stored one is now
    # wrong. Supersede rather than overwrite -- the schema keeps one live
    # credential per type and retains revoked rows, and a stale row left live
    # would violate that index and, worse, be handed out by load_credential.
    db.execute(
        conn,
        """
        UPDATE project_credentials SET revoked_at = now(), rotated_at = now()
         WHERE project_id = %s AND credential_type = %s AND revoked_at IS NULL
        """,
        (project_id, credential_type),
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


def load_credential(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    credential_type: str,
    key_ring: crypto.KeyRing,
) -> str:
    """Recover a stored tenant credential.

    The return value is a live secret: never log it, never include it in an
    error, never return it over the API.
    """
    row = db.one(
        conn,
        """
        SELECT ciphertext, nonce, key_version FROM project_credentials
         WHERE project_id = %s AND credential_type = %s AND revoked_at IS NULL
        """,
        (project_id, credential_type),
    )
    if row is None:
        raise ProvisioningError(f"no {credential_type} credential recorded for this project")
    return key_ring.open(
        crypto.SealedValue(bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_version"]),
        aad=crypto.aad_for("project_credentials", "ciphertext", f"{project_id}:{credential_type}"),
    ).decode()
