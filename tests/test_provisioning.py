"""Tenant provisioning against a real MaluDB cluster.

These run against the actual node, not a mock, because every property under
test is a PostgreSQL behaviour: CONNECT defaults, role attributes, cluster-wide
role membership. A mock would assert that the code issued statements, not that
isolation holds.

Test IDs map to the required negative tests in `specs/tenant-role-model.md`.

Requires MALUDB_NODE_ADMIN_DSN pointing at a superuser on a disposable
PostgreSQL/MaluDB cluster. Every object created is prefixed `mldb_tp` and
dropped afterwards. Never point this at a node carrying customer data.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest

from services.control_plane import crypto, db, jobs, models, provisioning
from tests.conftest import requires_db

ADMIN_DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()

pytestmark = [
    requires_db,
    pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset"),
]

TEST_CREDENTIAL = "correct-horse-battery-staple"  # noqa: S105 - test fixture, not a real secret
PLATFORM_OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")


def _maludb_core_available() -> bool:
    """Whether this cluster can install maludb_core.

    CI runs a plain postgres:17 service container, which cannot. The
    isolation properties under test here are PostgreSQL behaviours, so they
    still run there; only the extension assertions are skipped.
    """
    if not ADMIN_DSN:
        return False
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'maludb_core'")
                return cur.fetchone() is not None
    except psycopg.Error:
        return False


MALUDB_CORE_AVAILABLE = _maludb_core_available()
requires_maludb_core = pytest.mark.skipif(
    not MALUDB_CORE_AVAILABLE, reason="cluster has no maludb_core (ADR-015 assertions need a MaluDB node)"
)


def _drop_tenant(ref: str) -> None:
    names = provisioning.TenantNames.for_ref(ref)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
        for role in (names.authenticator, names.auth, names.admin):
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def _provision_core(project_id: uuid.UUID, admin_conn, key_ring, ref: str) -> tuple:
    """Roles, database and lockdown, without the extension.

    provisioning installs maludb_core unconditionally (ADR-015), which a
    plain PostgreSQL cluster cannot do. The isolation properties this slice
    exists to establish -- CONNECT lockdown, role attributes, cluster-wide
    membership -- are plain PostgreSQL behaviours, so they are exercised
    through the stages here and run everywhere, including CI.

    The orchestration itself is covered separately by the end-to-end test,
    which does require a MaluDB node. Stage-level testing alone is what let a
    broken orchestration path through review once already.
    """
    names = provisioning.TenantNames.for_ref(ref)
    passwords = {
        key: provisioning.generate_password() for key in ("authenticator", "auth", "admin")
    }
    with db.connection() as conn:
        provisioning.ensure_shared_roles(admin_conn)
        admin_conn.commit()
        provisioning.create_roles(admin_conn, names, passwords=passwords, connection_limits={})
        admin_conn.commit()
        provisioning.create_database(admin_conn, names, owner=PLATFORM_OWNER)
        db.execute(conn, "UPDATE projects SET database_name = %s WHERE id = %s", (names.database, project_id))
        conn.commit()
        provisioning.lock_down_database(admin_conn, names)
        provisioning.apply_plan_settings(admin_conn, names, settings={"statement_timeout": "8s"})
        admin_conn.commit()
    return names, passwords


def _provision(project_id: uuid.UUID, admin_conn, key_ring, ref: str) -> tuple:
    """Run provisioning through its real entry point.

    Returns (names, passwords) where the passwords are read back out of
    project_credentials -- the same route a worker-configuration step would
    take. Nothing here reaches inside the individual stages.
    """
    names = provisioning.TenantNames.for_ref(ref)
    settings = {"statement_timeout": "8s", "idle_in_transaction_session_timeout": "30s"}

    def tenant_connect(database: str):
        return psycopg.connect(_tenant_admin_dsn(database), autocommit=True)

    with db.connection() as conn:
        jobs.provision(
            conn,
            admin_conn,
            project_id=project_id,
            key_ring=key_ring,
            platform_owner=PLATFORM_OWNER,
            tenant_connect=tenant_connect,
            plan_settings=settings,
        )
        passwords = {
            key: provisioning.load_credential(
                conn, project_id=project_id, credential_type=f"db_{key}", key_ring=key_ring
            )
            # `client` since ADR-047: it is the role a paid customer connects
            # as, and the admin role's password is no longer issued to anyone.
            for key in ("authenticator", "auth", "admin", "client")
        }
    return names, passwords


def _tenant_admin_dsn(database: str) -> str:
    """The admin DSN, repointed at a tenant database."""
    parsed = urlsplit(ADMIN_DSN)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, ""))


def _tenant_dsn(database: str, user: str, password: str) -> str:
    """A DSN for a tenant role.

    Built from parts rather than by substituting into the admin DSN: string
    replacement left the admin password in place, which surfaced as an
    authentication failure that looked like a lockdown working.
    """
    parsed = urlsplit(ADMIN_DSN)
    host = parsed.hostname or "127.0.0.1"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parsed.scheme, netloc, f"/{database}", parsed.query, ""))


# -- happy path ------------------------------------------------------------


@requires_maludb_core
def test_provisioning_creates_database_roles_and_extension(admin_conn, key_ring, project_factory):
    project_id = project_factory("tp000001")
    names, _ = _provision(project_id, admin_conn, key_ring, "tp000001")

    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (names.database,))
        assert cur.fetchone(), "tenant database was not created"
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                    ([names.authenticator, names.auth, names.admin],))
        assert len(cur.fetchall()) == 3

    with db.connection() as conn:
        row = db.one(conn, "SELECT status, extension_versions FROM projects WHERE id = %s", (project_id,))
    assert row["status"] == "PROVISIONED"
    assert "maludb_core" in row["extension_versions"], "ADR-015: extension version must be recorded"


@requires_maludb_core
def test_credentials_are_stored_encrypted_and_recoverable(admin_conn, key_ring, project_factory):
    project_id = project_factory("tp000002")
    _, passwords = _provision(project_id, admin_conn, key_ring, "tp000002")

    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT credential_type, ciphertext, nonce, key_version FROM project_credentials "
            "WHERE project_id = %s ORDER BY credential_type",
            (project_id,),
        )
    # Five since ADR-047, which added the client role the customer connects as.
    # Asserted as an exact set rather than a count, so a credential type
    # appearing or vanishing is named in the failure.
    assert {row["credential_type"] for row in rows} == {
        "db_authenticator", "db_auth", "db_admin", "db_executor", "db_client",
    }
    for row in rows:
        raw = bytes(row["ciphertext"])
        # no plaintext password anywhere in the stored bytes
        for secret in passwords.values():
            assert secret.encode() not in raw
        recovered = key_ring.open(
            crypto.SealedValue(raw, bytes(row["nonce"]), row["key_version"]),
            aad=crypto.aad_for("project_credentials", "ciphertext", f"{project_id}:{row['credential_type']}"),
        ).decode()
        # The executor's password is generated inside the provisioning step and
        # deliberately never returned to a caller, so there is nothing to
        # compare it against here. What is asserted for it is the property that
        # matters: it decrypts, under its own AAD, to a real secret -- which the
        # `open` above already proves, since a wrong AAD fails rather than
        # returning the wrong plaintext.
        if row["credential_type"] != "db_executor":
            assert recovered in passwords.values()
        else:
            assert len(recovered) >= 32


# -- required negative tests (specs/tenant-role-model.md) ------------------


def test_C_tenant_b_cannot_connect_to_tenant_a_database(admin_conn, key_ring, project_factory):
    """ADR-014. Fails by default until CONNECT is revoked from PUBLIC."""
    a = project_factory("tp0000a1")
    b = project_factory("tp0000b1")
    names_a, _ = _provision_core(a, admin_conn, key_ring, "tp0000a1")
    _, passwords_b = _provision_core(b, admin_conn, key_ring, "tp0000b1")

    # tenant B's own credential, pointed at tenant A's database
    hostile = _tenant_dsn(names_a.database, "mldb_tp0000b1_authenticator", passwords_b["authenticator"])
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        psycopg.connect(hostile, connect_timeout=5).close()


def test_G_shared_roles_cannot_log_in(admin_conn, key_ring, project_factory):
    project_factory("tp0000g1")
    provisioning.ensure_shared_roles(admin_conn)
    admin_conn.commit()
    with admin_conn.cursor() as cur:
        cur.execute("SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
                    (list(provisioning.SHARED_ROLES),))
        for row in cur.fetchall():
            assert row["rolcanlogin"] is False, f"{row['rolname']} must be NOLOGIN"


def test_F_no_shared_role_is_a_member_of_a_tenant_role(admin_conn, key_ring, project_factory):
    """ADR-016: grants involving shared roles are one-directional."""
    project_id = project_factory("tp0000f1")
    names, _ = _provision_core(project_id, admin_conn, key_ring, "tp0000f1")
    with admin_conn.cursor() as cur:
        for shared in provisioning.SHARED_ROLES:
            for tenant_role in (names.authenticator, names.auth, names.admin):
                cur.execute("SELECT pg_has_role(%s, %s, 'member') AS m", (shared, tenant_role))
                assert cur.fetchone()["m"] is False, f"{shared} must not be a member of {tenant_role}"


def test_I_tenant_roles_cannot_reach_privileged_roles(admin_conn, key_ring, project_factory):
    """No customer-reachable role may be superuser or reach one."""
    project_id = project_factory("tp0000i1")
    names, _ = _provision_core(project_id, admin_conn, key_ring, "tp0000i1")
    with admin_conn.cursor() as cur:
        for role in (names.authenticator, names.auth, names.admin):
            cur.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = %s",
                (role,),
            )
            row = cur.fetchone()
            assert not row["rolsuper"], f"{role} is superuser"
            assert not row["rolcreatedb"], f"{role} can create databases"
            assert not row["rolcreaterole"], f"{role} can create roles"
            # the authenticator legitimately inherits service_role's BYPASSRLS
            # by membership; the attribute itself must not be set on it
            assert not row["rolbypassrls"], f"{role} has BYPASSRLS as an attribute"

            # pg_has_role raises UndefinedObject for a role that does not
            # exist, and 'maludb' is absent on a plain PostgreSQL cluster. Match
            # on pg_roles first so the check degrades to false rather than
            # erroring -- an earlier guard tested fetchone(), which execute()
            # never reached.
            cur.execute(
                """
                SELECT coalesce(bool_or(pg_has_role(%s, r.oid, 'member')), false) AS is_member
                  FROM pg_roles r WHERE r.rolname = 'maludb'
                """,
                (role,),
            )
            assert cur.fetchone()["is_member"] is False, f"{role} can reach the maludb superuser role"


def test_H_tenant_role_cannot_create_extensions(admin_conn, key_ring, project_factory):
    project_id = project_factory("tp0000h1")
    names, passwords = _provision_core(project_id, admin_conn, key_ring, "tp0000h1")
    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def test_D_tenant_b_roles_hold_no_privilege_on_tenant_a_objects(admin_conn, key_ring, project_factory):
    """Negative test D.

    `authenticated` is a shared name (ADR-016), so tenant A's session and tenant
    B's session enter *the same* role. What keeps them apart is that every grant
    to it attaches to a per-database object, plus the ADR-014 lockdown that stops
    B connecting to A at all. This asserts the first half directly: inside tenant
    A's database, none of tenant B's per-tenant roles hold any privilege.
    """
    a = project_factory("tp0000d1")
    b = project_factory("tp0000d2")
    names_a, passwords_a = _provision_core(a, admin_conn, key_ring, "tp0000d1")
    names_b, _ = _provision_core(b, admin_conn, key_ring, "tp0000d2")

    with psycopg.connect(_tenant_admin_dsn(names_a.database)) as conn:
        conn.execute("CREATE TABLE public.a_data (id int)")
        conn.commit()
        with conn.cursor() as cur:
            for role in (names_b.authenticator, names_b.auth, names_b.admin):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    cur.execute(
                        "SELECT has_table_privilege(%s, 'public.a_data', %s)", (role, privilege)
                    )
                    assert cur.fetchone()[0] is False, f"{role} holds {privilege} on tenant A's table"

    # And tenant A's own authenticator, which does reach the table, cannot
    # create one -- schema CREATE is the tenant admin's, not the API role's.
    dsn = _tenant_dsn(names_a.database, names_a.authenticator, passwords_a["authenticator"])
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE public.sneaky (id int)")


def test_ADR017_plan_settings_survive_set_role(admin_conn, key_ring, project_factory):
    """Acceptance criterion, and the reason ADR-017 exists.

    Settings are attached to the *login* role scoped IN DATABASE. Attaching them
    to `authenticated` would silently do nothing, because that role is entered
    through SET ROLE rather than login -- so the check that matters is whether
    the value is still in force after the SET ROLE that PostgREST performs.
    """
    project_id = project_factory("tp0000s1")
    names, passwords = _provision_core(project_id, admin_conn, key_ring, "tp0000s1")

    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        at_login = cur.fetchone()[0]
        cur.execute("SET ROLE authenticated")
        cur.execute("SHOW statement_timeout")
        after_set_role = cur.fetchone()[0]

    assert at_login == "8s", f"plan setting did not apply at login: {at_login}"
    assert after_set_role == "8s", "the setting was lost on SET ROLE, which is how PostgREST connects"


def test_lockdown_is_verified_not_assumed(admin_conn, key_ring, project_factory):
    """verify_isolation must reject a database whose lockdown silently failed."""
    project_id = project_factory("tp0000v1")
    names, _ = _provision_core(project_id, admin_conn, key_ring, "tp0000v1")

    # re-grant CONNECT to PUBLIC, simulating a lockdown that did not take
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{names.database}" TO PUBLIC')
    admin_conn.commit()
    with pytest.raises(provisioning.ProvisioningError, match="PUBLIC still has CONNECT"):
        provisioning.verify_isolation(admin_conn, names)


# -- identifier safety -----------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ['ab"; DROP DATABASE postgres; --', "ab12cd3'", "ab 12cd3", "AB12CD34", "", "x" * 64],
)
def test_hostile_project_refs_never_reach_sql(hostile):
    """A generated identifier must never be built from an unvalidated ref."""
    with pytest.raises(ValueError, match="invalid project_ref"):
        provisioning.TenantNames.for_ref(hostile)
    assert not models.is_valid_project_ref(hostile)


@requires_maludb_core
def test_provisioning_persists_credentials_before_anything_else_can_fail(admin_conn, key_ring, project_factory):
    """Security review finding: roles were created with passwords nobody held.

    The old provision_tenant generated the passwords locally and returned only the
    names, so no caller could persist them. Roles and a database existed on the
    node, the project was stuck in DATABASE_CREATING, and retry was refused --
    permanently unrecoverable without manual intervention.
    """
    project_id = project_factory("tp0000p1")
    names, passwords = _provision(project_id, admin_conn, key_ring, "tp0000p1")

    with db.connection() as conn:
        stored = db.query(
            conn,
            "SELECT credential_type, role_name FROM project_credentials WHERE project_id = %s",
            (project_id,),
        )
    assert {row["credential_type"] for row in stored} == {
        "db_authenticator", "db_auth", "db_admin", "db_executor", "db_client",
    }
    assert {row["role_name"] for row in stored} == {
        names.authenticator, names.auth, names.admin, names.executor, names.client,
    }

    # the recovered credential is the real one: it authenticates
    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == names.authenticator


def test_verification_covers_every_tenant_role(admin_conn, key_ring, project_factory):
    """The gate must not be weaker than the tests. Previously only the admin
    role's attributes were checked, leaving the two login roles unverified."""
    project_id = project_factory("tp0000q1")
    names, _ = _provision_core(project_id, admin_conn, key_ring, "tp0000q1")

    for role in (names.authenticator, names.auth, names.admin):
        admin_conn.execute(f'ALTER ROLE "{role}" CREATEDB')
        admin_conn.commit()
        with pytest.raises(provisioning.ProvisioningError, match="elevated attributes"):
            provisioning.verify_isolation(admin_conn, names)
        admin_conn.execute(f'ALTER ROLE "{role}" NOCREATEDB')
        admin_conn.commit()

    # and it holds again once reverted
    provisioning.verify_isolation(admin_conn, names)


def test_verification_rejects_a_shared_role_that_can_log_in(admin_conn, key_ring, project_factory):
    """ADR-016: shared-role safety rests on NOLOGIN, so verification asserts it."""
    project_id = project_factory("tp0000r1")
    names, _ = _provision_core(project_id, admin_conn, key_ring, "tp0000r1")
    admin_conn.execute('ALTER ROLE "authenticated" LOGIN')
    admin_conn.commit()
    try:
        with pytest.raises(provisioning.ProvisioningError, match="can log in"):
            provisioning.verify_isolation(admin_conn, names)
    finally:
        admin_conn.execute('ALTER ROLE "authenticated" NOLOGIN')
        admin_conn.commit()
