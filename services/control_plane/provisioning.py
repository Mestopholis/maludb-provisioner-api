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
    # What the platform connects as to run a customer's SQL on their behalf
    # (ADR-039). Member of `admin` and nothing else; owns nothing.
    executor: str
    # What the *customer* connects as for paid direct SQL (ADR-047). The
    # executor's shape with a different caller: member of `admin` and nothing
    # else, owning nothing, and arriving in the admin role on login. `LOGIN`
    # only where the plan grants it.
    client: str
    # Named here with the others, but created only when a project enables
    # Realtime -- see `create_replicator_role`. It is the one tenant role that
    # holds `REPLICATION`, and ADR-031 is about not handing that out by default.
    replicator: str
    # What upstream `storage-api` connects as, and the owner of the tenant's
    # `storage` schema (Phase 10 slice 1). A platform-internal service
    # credential in the same class as `auth`, never issued to a customer.
    # Created for every project, because ADR-056 puts Storage on every tier.
    storage: str

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
            executor=f"{database}_executor",
            client=f"{database}_client",
            replicator=f"{database}_replicator",
            storage=f"{database}_storage",
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

    # The admin role gets its password here too, and is NOLOGIN until a plan
    # entitles the project to direct SQL (ADR-005, `set_direct_sql_access`).
    #
    # It previously got no password at all, while provisioning stored a
    # `db_admin` credential regardless -- so the stored secret corresponded to
    # nothing, and enabling LOGIN would have produced a role nobody could
    # authenticate as. Setting it now is what makes upgrading a single attribute
    # change rather than a credential rotation the customer has to be told about.
    admin_verb = sql.SQL("ALTER ROLE") if role_exists(admin_conn, names.admin) else sql.SQL("CREATE ROLE")
    admin_conn.execute(
        sql.SQL("{verb} {role} NOLOGIN PASSWORD {password} NOINHERIT").format(
            verb=admin_verb,
            role=sql.Identifier(names.admin),
            password=sql.Literal(passwords["admin"]),
        )
    )

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


EXECUTOR_CONNECTION_LIMIT = 5


def create_executor_role(
    admin_conn: psycopg.Connection, names: TenantNames, *, password: str,
    connection_limit: int = EXECUTOR_CONNECTION_LIMIT,
) -> None:
    """Create the role the platform runs a customer's SQL as (ADR-039).

    Separate from `create_roles` on purpose. Folding it in would make
    `_roles_done` report every project provisioned before this existed as
    unfinished, and the repair for that predicate is to reset all three role
    passwords -- which would invalidate the PostgREST and GoTrue configurations
    of every running project on the node. A new capability must not be able to
    do that, so it gets its own step and its own predicate.

    `NOINHERIT`, like the authenticator: privileges are reached by an explicit
    `SET ROLE` rather than held ambiently, so a session that has not asked for
    the admin role does not have it.

    The role is a member of `names.admin` and of nothing else. That single
    membership is the whole of its privilege, and `RESET ROLE` in customer SQL
    returns here -- which is not an escape, because the admin role is the
    customer's intended ceiling inside their own database and they reach it
    either way. What this role buys is that the stored credential is not the
    admin role's password, that console connections are capped separately, and
    that a free project never receives a role it can log in as.

    ADR-016 in the permitted direction only: shared names may be granted *to*
    this role, never the reverse.
    """
    verb = sql.SQL("ALTER ROLE") if role_exists(admin_conn, names.executor) else sql.SQL("CREATE ROLE")
    admin_conn.execute(
        sql.SQL(
            "{verb} {role} LOGIN PASSWORD {password} CONNECTION LIMIT {limit} "
            "NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        ).format(
            verb=verb,
            role=sql.Identifier(names.executor),
            password=sql.Literal(password),
            limit=sql.Literal(int(connection_limit)),
        )
    )
    admin_conn.execute(
        sql.SQL("GRANT {admin} TO {executor}").format(
            admin=sql.Identifier(names.admin),
            executor=sql.Identifier(names.executor),
        )
    )


def grant_executor_connect(admin_conn: psycopg.Connection, names: TenantNames) -> None:
    """CONNECT on its own database, and nothing else (ADR-014).

    Split from `create_executor_role` because roles are created before the
    database exists, and a backfill runs against a database that already does.
    """
    admin_conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {executor}").format(
            db=sql.Identifier(names.database),
            executor=sql.Identifier(names.executor),
        )
    )


def has_executor_role(admin_conn: psycopg.Connection, names: TenantNames) -> bool:
    return role_exists(admin_conn, names.executor)


CLIENT_CONNECTION_LIMIT_FALLBACK = 5


def create_client_role(
    admin_conn: psycopg.Connection, names: TenantNames, *, password: str,
    connection_limit: int = CLIENT_CONNECTION_LIMIT_FALLBACK,
) -> None:
    """Create the role a paid customer connects as (ADR-047).

    Created `NOLOGIN` regardless of plan. `set_direct_sql_access` is what turns
    it on, so a project that upgrades does not receive a new credential and a
    project that downgrades does not lose the one it had — the same reasoning
    that kept the admin role's password stored from the first provisioning run.

    Its own step and predicate, on `create_executor_role`'s precedent: folding
    it into `create_roles` would make every project provisioned before this
    report as unfinished, and the repair for that predicate resets the
    authenticator and auth passwords — which would stop every PostgREST and
    GoTrue worker on the node.

    `NOINHERIT`, member of `mldb_<ref>_admin` and of nothing else. ADR-016 in
    the permitted direction only: shared names may be granted *to* this role,
    never the reverse.
    """
    verb = sql.SQL("ALTER ROLE") if role_exists(admin_conn, names.client) else sql.SQL("CREATE ROLE")
    admin_conn.execute(
        sql.SQL(
            "{verb} {role} NOLOGIN PASSWORD {password} CONNECTION LIMIT {limit} "
            "NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        ).format(
            verb=verb,
            role=sql.Identifier(names.client),
            password=sql.Literal(password),
            limit=sql.Literal(int(connection_limit)),
        )
    )
    admin_conn.execute(
        sql.SQL("GRANT {admin} TO {client}").format(
            admin=sql.Identifier(names.admin),
            client=sql.Identifier(names.client),
        )
    )


def grant_client_connect(admin_conn: psycopg.Connection, names: TenantNames) -> None:
    """CONNECT on its own database, and the role it arrives in.

    The `SET role` default is not a convenience. Without it a table the
    customer creates over their direct connection is owned by the client role,
    and `ALTER DEFAULT PRIVILEGES` only affects objects created by the role it
    names — which is how Phase 08 produced a table that the customer's own data
    API could not read. Measured: with it, `session_user` is the client role,
    `current_user` is the admin role, and objects are owned by the admin role,
    so a direct connection and the SQL console produce indistinguishable
    results.

    Split from `create_client_role` for `grant_executor_connect`'s reason:
    roles are created before the database exists, and a backfill runs against
    one that already does.
    """
    admin_conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {client}").format(
            db=sql.Identifier(names.database),
            client=sql.Identifier(names.client),
        )
    )
    admin_conn.execute(
        sql.SQL("ALTER ROLE {client} IN DATABASE {db} SET role = {admin}").format(
            client=sql.Identifier(names.client),
            db=sql.Identifier(names.database),
            admin=sql.Literal(names.admin),
        )
    )


def has_client_role(admin_conn: psycopg.Connection, names: TenantNames) -> bool:
    return role_exists(admin_conn, names.client)


STORAGE_CONNECTION_LIMIT = 10


def create_storage_role(
    admin_conn: psycopg.Connection, names: TenantNames, *, password: str,
    connection_limit: int = STORAGE_CONNECTION_LIMIT,
) -> None:
    """Create the role upstream `storage-api` connects as (Phase 10 slice 1).

    A platform-internal service credential in the same class as
    `mldb_<ref>_auth`, and it exists for the same reason: ADR-004 gives
    customers no superuser, and the service that migrates a schema owns it.
    Upstream would otherwise create `supabase_storage_admin` itself, along with
    `anon`, `authenticated` and `service_role` -- which on a shared node would
    be one tenant's container reaching for names ADR-016 shares with every
    other tenant. `DB_INSTALL_ROLES=false` turns that off, and this is the half
    the platform then owes it.

    Its own step and predicate, on `create_executor_role`'s precedent: folding
    it into `create_roles` would make every project provisioned before this
    report as unfinished, and the repair for that predicate resets the
    authenticator and auth passwords -- stopping every PostgREST and GoTrue
    worker on the node.

    Created for **every** project regardless of plan, because ADR-056 puts
    Storage on every tier including free. Unlike `create_replicator_role`, this
    role holds no attribute worth withholding: it owns one schema in one
    database and can log in to nothing else.

    The three shared names are granted **to** it -- ADR-016's permitted
    direction only, never the reverse -- because `storage-api` switches role per
    request rather than querying as the owner. `set_config('role', <role from
    the JWT>, true)` in its `internal/database/postgres/scope.js` is a
    `SET LOCAL ROLE`, and without the membership every customer-scoped query
    fails. It is also what makes row-level security apply at all: a query that
    stayed as the owner would bypass every storage policy, since upstream
    enables RLS without forcing it.

    `NOINHERIT`, like the authenticator and for the same reason: the privileges
    of those three names are reached by an explicit role switch rather than held
    ambiently.
    """
    verb = sql.SQL("ALTER ROLE") if role_exists(admin_conn, names.storage) else sql.SQL("CREATE ROLE")
    admin_conn.execute(
        sql.SQL(
            "{verb} {role} LOGIN PASSWORD {password} CONNECTION LIMIT {limit} "
            "NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        ).format(
            verb=verb,
            role=sql.Identifier(names.storage),
            password=sql.Literal(password),
            limit=sql.Literal(int(connection_limit)),
        )
    )
    admin_conn.execute(
        sql.SQL("GRANT {anon}, {authenticated}, {service} TO {storage}").format(
            anon=sql.Identifier("anon"),
            authenticated=sql.Identifier("authenticated"),
            service=sql.Identifier("service_role"),
            storage=sql.Identifier(names.storage),
        )
    )


def grant_storage_connect(admin_conn: psycopg.Connection, names: TenantNames) -> None:
    """CONNECT on its own database, and nothing else (ADR-014).

    Split from `create_storage_role` for `grant_executor_connect`'s reason:
    roles are created before the database exists, and a backfill runs against
    one that already does.

    No grant on `public` accompanies this, and the omission is measured rather
    than assumed: all 63 of upstream's tenant migrations complete without one,
    so bootstrap 007's `GRANT USAGE ON SCHEMA public` for the auth role has no
    counterpart here.

    That is a statement about this role's own privileges and not about what
    `storage-api` can reach. It can `SET ROLE` into the three shared names --
    it must, or nothing it queries is governed by RLS -- and bootstrap 004
    grants those `ALL ON ALL TABLES IN SCHEMA public`. Its reach through a role
    switch is PostgREST's authenticator's reach, unchanged by this slice.
    """
    admin_conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {storage}").format(
            db=sql.Identifier(names.database),
            storage=sql.Identifier(names.storage),
        )
    )


def has_storage_role(admin_conn: psycopg.Connection, names: TenantNames) -> bool:
    return role_exists(admin_conn, names.storage)


def create_replicator_role(
    admin_conn: psycopg.Connection, names: TenantNames, *, password: str,
    connection_limit: int = 5,
) -> None:
    """Create the one tenant role that holds `REPLICATION`.

    Separate from `create_roles`, and called only when a project enables
    Realtime, because ADR-031 turns on this attribute being rare. Logical
    decoding cannot be had without it -- PostgreSQL refuses both the SQL and the
    protocol path -- so the platform must issue it to something, and the whole
    design is that the something is a dedicated role with `CONNECT` on one
    database and a node that rejects physical replication underneath it.

    Never the admin or the authenticator. Both are customer-reachable on paid
    plans, and `REPLICATION` on either hands that customer a byte-level copy of
    every tenant on the node.

    What this role can do inside its own database is *everything*: decoding
    reads WAL, which is written before any grant or policy is consulted. So its
    credential is Class B of the highest value in the system, and it is granted
    no table privileges at all -- not because that constrains it, but because
    granting any would imply the grants mean something here.
    """
    verb = sql.SQL("ALTER ROLE") if role_exists(admin_conn, names.replicator) else sql.SQL("CREATE ROLE")
    admin_conn.execute(
        sql.SQL(
            "{verb} {role} LOGIN REPLICATION PASSWORD {password} "
            "CONNECTION LIMIT {limit} NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE"
        ).format(
            verb=verb,
            role=sql.Identifier(names.replicator),
            password=sql.Literal(password),
            limit=sql.Literal(int(connection_limit)),
        )
    )
    # CONNECT on its own database only. This is what bounds the *logical* path:
    # the spike confirmed a logical replication connection to another tenant is
    # refused by exactly this privilege. The physical path is not bounded by it
    # and is closed at the node instead (ADR-031).
    admin_conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {database} TO {role}").format(
            database=sql.Identifier(names.database),
            role=sql.Identifier(names.replicator),
        )
    )
    admin_conn.commit()


def drop_replicator_role(
    admin_conn: psycopg.Connection, names: TenantNames, *, tenant_conn: psycopg.Connection | None = None
) -> None:
    """Remove the replicator role entirely when Realtime is turned off.

    Dropped rather than left NOLOGIN, which is how direct SQL is disabled. The
    difference is what the attribute is worth: a dormant admin role holds
    nothing until it is enabled, while a dormant role holding `REPLICATION` is
    one `pg_hba.conf` regression away from reading the cluster. Turning a
    capability off should reduce what exists, not just what is reachable.

    Everything the role depends on has to go first, and PostgreSQL is
    unforgiving about the order. The `SET ON PARAMETER` grant is a catalogue
    entry that blocks the drop with `role ... cannot be dropped because some
    objects depend on it: privileges for parameter log_min_messages`, and the
    `realtime` schema is *owned* by this role, so it blocks it too. Both were
    found by the enablement tests failing on teardown, which is what those tests
    are for.
    """
    if not role_exists(admin_conn, names.replicator):
        return

    if tenant_conn is not None:
        # `DROP OWNED BY` is doing more work than its name suggests, and the
        # extra work is load-bearing. Upstream's migration ends with
        # `GRANT supabase_realtime_admin TO postgres` executed *by* the
        # replicator, which makes it the grantor -- and PostgreSQL refuses to
        # drop a grantor while its grants stand. DROP OWNED BY removes those
        # memberships too, so no explicit REVOKE is needed. Dropping the role
        # without it fails with `privileges for membership of role postgres in
        # role supabase_realtime_admin`.
        #
        # Realtime's own schema, which the role owns. Dropped rather than
        # reassigned: the server is being turned off, its bookkeeping has no
        # meaning without it, and re-enabling re-runs the migrations that build
        # it. Customer tables are in `public` and are untouched.
        tenant_conn.execute("DROP SCHEMA IF EXISTS realtime CASCADE")
        tenant_conn.execute(
            sql.SQL("DROP OWNED BY {role}").format(role=sql.Identifier(names.replicator))
        )

    admin_conn.execute(
        sql.SQL("REVOKE SET ON PARAMETER log_min_messages FROM {role}").format(
            role=sql.Identifier(names.replicator)
        )
    )
    admin_conn.execute(
        sql.SQL("REVOKE {admin} FROM {role}").format(
            admin=sql.Identifier(REALTIME_ADMIN_ROLE), role=sql.Identifier(names.replicator)
        )
    )
    admin_conn.execute(
        sql.SQL("REVOKE ALL ON DATABASE {database} FROM {role}").format(
            database=sql.Identifier(names.database),
            role=sql.Identifier(names.replicator),
        )
    )
    admin_conn.execute(sql.SQL("DROP ROLE IF EXISTS {role}").format(
        role=sql.Identifier(names.replicator)
    ))
    admin_conn.commit()


def has_replicator_role(admin_conn: psycopg.Connection, names: TenantNames) -> bool:
    return role_exists(admin_conn, names.replicator)


# Upstream Realtime's own bookkeeping role. Cluster-wide, like ADR-016's shared
# names and safe for the same reason: it is NOLOGIN and carries no privilege of
# its own, and every privilege it holds attaches to per-database objects.
REALTIME_ADMIN_ROLE = "supabase_realtime_admin"


def grant_realtime_migration_rights(
    admin_conn: psycopg.Connection, tenant_conn: psycopg.Connection, names: TenantNames
) -> None:
    """Let the replicator run upstream's tenant migrations without superuser.

    Realtime applies 36 migrations *inside the tenant database*. Supabase runs
    them as `supabase_admin`, a superuser; MaluDB cannot, because a role with
    `LOGIN` and superuser turns any compromise of the Realtime server into a
    node compromise -- `COPY FROM PROGRAM` is arbitrary code execution as the
    PostgreSQL operating-system user -- and erases the containment ADR-031
    exists to establish.

    Each grant below was found by running the migrations and reading the
    failure; `specs/realtime-server-model.md` records which error produced
    which. Re-runnable, because enablement is.
    """
    # Pre-created rather than left to upstream's migration, which needs
    # CREATEROLE to do it. The migration is guarded by an IF EXISTS check, so
    # creating it here turns that statement into a no-op instead of an error.
    admin_conn.execute(
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {name}) "
            "THEN CREATE ROLE {role} WITH NOINHERIT NOLOGIN NOREPLICATION; END IF; END $$"
        ).format(name=sql.Literal(REALTIME_ADMIN_ROLE), role=sql.Identifier(REALTIME_ADMIN_ROLE))
    )
    # INHERIT before the grant, and INHERIT TRUE on the grant. PostgreSQL 16
    # records the inherit option **per grant**, defaulting to the member's
    # rolinherit at the time it was made -- so granting first and setting
    # INHERIT afterwards produces membership that does not inherit, and the
    # ownership checks fail with nothing pointing at inheritance as the cause.
    admin_conn.execute(
        sql.SQL("ALTER ROLE {role} INHERIT").format(role=sql.Identifier(names.replicator))
    )
    admin_conn.execute(
        sql.SQL("GRANT {admin} TO {role} WITH INHERIT TRUE, ADMIN OPTION").format(
            admin=sql.Identifier(REALTIME_ADMIN_ROLE), role=sql.Identifier(names.replicator)
        )
    )
    # Realtime creates its own publications.
    admin_conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {database} TO {role}").format(
            database=sql.Identifier(names.database), role=sql.Identifier(names.replicator)
        )
    )
    admin_conn.commit()

    # Owned by the replicator, the same arrangement bootstrap 007 makes for
    # GoTrue and the `auth` schema: the service that migrates a schema owns it.
    tenant_conn.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS realtime AUTHORIZATION {role}").format(
            role=sql.Identifier(names.replicator)
        )
    )
    # A migration creates a function with `SET log_min_messages`, which is a
    # superuser-only GUC. PostgreSQL 15 added per-parameter grants for exactly
    # this, which is what makes the whole non-superuser arrangement possible.
    tenant_conn.execute(
        sql.SQL("GRANT SET ON PARAMETER log_min_messages TO {role}").format(
            role=sql.Identifier(names.replicator)
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
        # The admin role is granted CONNECT here but is NOLOGIN until a plan
        # entitles the project to direct SQL -- see `set_direct_sql_access`.
        # Granting CONNECT to a role that cannot log in changes nothing today
        # and means enabling direct SQL is one attribute change rather than two
        # operations that could be half-applied.
        sql.SQL("GRANT CONNECT ON DATABASE {db} TO {authenticator}, {auth}, {admin}").format(
            db=sql.Identifier(names.database),
            authenticator=sql.Identifier(names.authenticator),
            auth=sql.Identifier(names.auth),
            admin=sql.Identifier(names.admin),
        )
    )


def existing_roles(admin_conn: psycopg.Connection, wanted: tuple[str, ...]) -> tuple[str, ...]:
    """Which of `wanted` the cluster actually has, in the order given."""
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(wanted),))
        found = {row["rolname"] for row in cur.fetchall()}
    return tuple(role for role in wanted if role in found)


def settings_roles(names: TenantNames) -> tuple[str, ...]:
    """The roles a plan's GUCs are written to: every role that can log in.

    ADR-017: settings apply at login, to the login role. Applying them to
    `authenticated` would silently do nothing, because that role is entered
    through `SET ROLE` rather than by logging in.

    **`admin` and `executor` were missing until Phase 09 slice 0, and they are
    the two a customer's own session logs in as.** Measured 2026-08-19 on a
    provisioned paid tenant: a direct connection as `mldb_<ref>_admin` reported
    `temp_file_limit = -1`, `work_mem = 4MB` and
    `max_parallel_workers_per_gather = 2` -- the cluster's defaults, not the
    plan's, on the tier that pays for a connection. `authenticator` and `auth`
    had the settings and are the roles *the platform* logs in as, for PostgREST
    and GoTrue.

    That mattered more than a wrong `work_mem`. ADR-017 found only two of these
    six actually bind against a client that does not want them, and
    `temp_file_limit` is one -- so the single per-session control a tenant
    cannot switch off was applied to nobody who could switch it off, and both
    paid direct SQL and the every-tier SQL console could fill a shared node's
    disk with temp files.

    `storage` joined the list in Phase 10 slice 1 on the same reasoning that put
    `auth` here: it is a role the platform logs in as, and `temp_file_limit` is
    a per-session control that binds on the session it was written to. A
    listing over a bucket with a million objects is exactly the shape that
    spills to disk.
    """
    return (
        names.authenticator, names.auth, names.admin,
        names.executor, names.client, names.storage,
    )


def apply_plan_settings(admin_conn: psycopg.Connection, names: TenantNames, *, settings: dict[str, Any]) -> None:
    """Write a plan's GUCs to every login role, scoped IN DATABASE.

    Idempotent: `ALTER ROLE ... SET` is a write of the value, not an increment,
    so re-applying an unchanged plan is a no-op that costs a few statements.
    Phase 09 slice 0 depends on that -- reconciliation runs this against
    projects whose plan has not changed.

    Applies to the roles that exist, which is not defensive coding: provisioning
    calls this while creating the database, and `mldb_<ref>_executor` is created
    a stage later because it needs `CONNECT` on a database that does not exist
    yet. The executor stage calls this again once its role is there. A role that
    is absent for any other reason shows up as drift in `plan_apply.inspect`
    rather than being skipped in silence.
    """
    present = existing_roles(admin_conn, settings_roles(names))
    for setting, value in settings.items():
        if value is None:
            continue
        for role in present:
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


def set_direct_sql_access(
    admin_conn: psycopg.Connection, names: TenantNames, *, enabled: bool,
    connection_limit: int = 5,
) -> None:
    """Turn a project's direct SQL on or off.

    ADR-005 makes direct PostgreSQL access a paid capability. **Since ADR-047
    the mechanism is `mldb_<ref>_client`'s LOGIN attribute, not the admin
    role's**, so that a customer's credential can be rotated without touching
    the identity the platform acts under, and revoking direct access is not the
    same operation as breaking the SQL console.

    The role is created NOLOGIN with its password stored from the first
    provisioning run, so enabling access is a single attribute change and never
    mints a new credential -- a customer who already has the password does not
    get a different one on upgrade, and one who downgrades and upgrades again
    does not have to reconfigure their application.

    `mldb_<ref>_admin` is forced NOLOGIN on every call, on every tier. It was
    the login role until ADR-047 and a project provisioned before that has it
    enabled; leaving it alone here would leave a second door open with a
    password the platform also uses.

    Disabling is immediate for new connections. Existing sessions survive until
    they end, which is PostgreSQL's behaviour and worth knowing: revoking access
    is not the same as terminating a session, and a downgrade that must take
    effect now needs `pg_terminate_backend` as well.
    """
    verb = sql.SQL("LOGIN") if enabled else sql.SQL("NOLOGIN")
    if role_exists(admin_conn, names.client):
        admin_conn.execute(
            sql.SQL("ALTER ROLE {role} {verb} CONNECTION LIMIT {limit}").format(
                role=sql.Identifier(names.client),
                verb=verb,
                limit=sql.Literal(int(connection_limit) if enabled else 0),
            )
        )
    admin_conn.execute(
        sql.SQL("ALTER ROLE {role} NOLOGIN CONNECTION LIMIT 0").format(
            role=sql.Identifier(names.admin),
        )
    )
    admin_conn.commit()


def has_direct_sql_access(admin_conn: psycopg.Connection, names: TenantNames) -> bool:
    """Whether the *client* role can log in (ADR-047).

    Reading the admin role here would answer a question about the platform's
    own identity rather than about the customer's access, and since ADR-047 the
    honest answer to that one is always `False`.
    """
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (names.client,))
        row = cur.fetchone()
    return bool(row and row["rolcanlogin"])


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
