"""Tenant bootstrap, against a real database.

The ADR-018 revoke is the point of this slice: during the Phase 00 spike,
`anon` invoked `/rpc/gen_salt` on a provisioned tenant. These assert the
property directly -- that no extension function in the exposed schema is
executable by `anon` or `authenticated` -- rather than that the revoke
statement ran.
"""

from __future__ import annotations

import psycopg
import pytest

from services.control_plane import db, tenant_bootstrap
from tests.conftest import requires_db
from tests.test_provisioning import (
    ADMIN_DSN,
    MALUDB_CORE_AVAILABLE,
    _provision_core,
    _tenant_admin_dsn,
    _tenant_dsn,
    requires_maludb_core,
)

pytestmark = [
    requires_db,
    pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset"),
]


@pytest.fixture
def bootstrapped(admin_conn, key_ring, project_factory):
    """A provisioned tenant with bootstrap applied, and its credentials."""

    def build(ref: str):
        project_id = project_factory(ref)
        names, passwords = _provision_core(project_id, admin_conn, key_ring, ref)
        with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
            if MALUDB_CORE_AVAILABLE:
                tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
                tenant_conn.commit()
            with db.connection() as conn:
                tenant_bootstrap.bootstrap_project(conn, tenant_conn, project_id=project_id)
        return project_id, names, passwords

    return build


# -- versioning ------------------------------------------------------------


def test_bootstrap_applies_and_is_recorded(bootstrapped):
    project_id, names, _ = bootstrapped("tb000001")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM maludb_platform.bootstrap_migrations")
            assert cur.fetchone()[0] == len(tenant_bootstrap.discover())

    with db.connection() as conn:
        row = db.one(conn, "SELECT bootstrap_version FROM projects WHERE id = %s", (project_id,))
    assert row["bootstrap_version"] == tenant_bootstrap.latest_version()


def test_reapplying_bootstrap_is_a_no_op(bootstrapped):
    _, names, _ = bootstrapped("tb000002")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        assert tenant_bootstrap.apply(tenant_conn) == []


def test_a_changed_bootstrap_file_is_refused(bootstrapped):
    """Immutable once applied, same rule as the control-plane migrations."""
    _, names, _ = bootstrapped("tb000003")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        version = tenant_bootstrap.discover()[0][0]
        tenant_conn.execute(
            "UPDATE maludb_platform.bootstrap_migrations SET checksum = 'tampered' WHERE version = %s",
            (version,),
        )
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="different checksum"):
            tenant_bootstrap.apply(tenant_conn)


def test_platform_schema_is_not_reachable_by_api_roles(bootstrapped):
    """Bookkeeping is not customer API surface."""
    _, names, _ = bootstrapped("tb000004")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn, tenant_conn.cursor() as cur:
        for role in ("anon", "authenticated"):
            cur.execute("SELECT has_schema_privilege(%s, 'maludb_platform', 'USAGE')", (role,))
            assert cur.fetchone()[0] is False, f"{role} can reach maludb_platform"


# -- ADR-018: the point of this slice --------------------------------------


@requires_maludb_core
def test_no_extension_function_is_executable_by_api_roles(bootstrapped):
    """The Phase 00 finding: anon invoked /rpc/gen_salt on a provisioned tenant."""
    _, names, _ = bootstrapped("tb000005")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn, tenant_conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.oid::regprocedure::text FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
             WHERE n.nspname = 'public'
               AND (has_function_privilege('anon', p.oid, 'EXECUTE')
                 OR has_function_privilege('authenticated', p.oid, 'EXECUTE'))
             LIMIT 5
            """
        )
        reachable = [row[0] for row in cur.fetchall()]
    assert reachable == [], f"extension functions still reachable: {reachable}"


@requires_maludb_core
def test_anon_cannot_call_gen_salt_specifically(bootstrapped):
    """The exact function reached during the Phase 00 spike."""
    _, names, passwords = bootstrapped("tb000006")
    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT gen_salt('bf')")


@requires_maludb_core
def test_verify_rejects_a_tenant_whose_hardening_was_undone(bootstrapped):
    _, names, _ = bootstrapped("tb000007")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("GRANT EXECUTE ON FUNCTION public.gen_salt(text) TO anon")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="still executable"):
            tenant_bootstrap.verify(tenant_conn)


# -- ADR-018 over time: the revoke must survive later extension changes ----


def _spare_extension(conn) -> str:
    """An installable extension other than maludb_core, or skip.

    Any extension will do; the property under test is about what happens to
    `public` after an extension changes, not about which one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM pg_available_extensions "
            "WHERE name IN ('uuid-ossp','pgcrypto','tablefunc','citext') "
            "  AND installed_version IS NULL LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no spare contrib extension available to install")
    return row[0]


def _api_reachable(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.oid::regprocedure::text FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
             WHERE n.nspname = 'public'
               AND (has_function_privilege('anon', p.oid, 'EXECUTE')
                 OR has_function_privilege('authenticated', p.oid, 'EXECUTE'))
            """
        )
        return [row[0] for row in cur.fetchall()]


def test_installing_an_extension_after_bootstrap_does_not_re_expose_it(bootstrapped):
    """The revoke in 003 is point-in-time. Without the event trigger, this
    installs ten anon-callable functions and anon can invoke them."""
    _, names, _ = bootstrapped("tb00000e")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        extension = _spare_extension(tenant_conn)
        assert _api_reachable(tenant_conn) == []

        tenant_conn.execute(f'CREATE EXTENSION "{extension}"')
        tenant_conn.commit()

        reachable = _api_reachable(tenant_conn)
    assert reachable == [], f"{extension} re-exposed extension functions: {reachable}"


def test_verify_still_passes_after_an_extension_is_installed(bootstrapped):
    """The fleet-upgrade gate: verify() is what a per-tenant upgrade checks."""
    _, names, _ = bootstrapped("tb00000f")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute(f'CREATE EXTENSION "{_spare_extension(tenant_conn)}"')
        tenant_conn.commit()
        tenant_bootstrap.verify(tenant_conn)  # must not raise


def test_verify_rejects_a_tenant_whose_event_trigger_was_dropped(bootstrapped):
    """Only a superuser can drop it -- which is exactly how it would happen."""
    _, names, _ = bootstrapped("tb00000g")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("DROP EVENT TRIGGER maludb_harden_extensions")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="event trigger is missing"):
            tenant_bootstrap.verify(tenant_conn)


def test_verify_rejects_a_disabled_event_trigger(bootstrapped):
    _, names, _ = bootstrapped("tb00000h")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("ALTER EVENT TRIGGER maludb_harden_extensions DISABLE")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="is disabled"):
            tenant_bootstrap.verify(tenant_conn)


def test_the_tenant_admin_cannot_remove_the_hardening(bootstrapped):
    """A customer with a paid direct-SQL connection is the database owner, not
    a superuser, and must not be able to opt out of ADR-018."""
    _, names, _ = bootstrapped("tb00000i")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as conn:
        for statement in (
            "DROP EVENT TRIGGER maludb_harden_extensions",
            "ALTER EVENT TRIGGER maludb_harden_extensions DISABLE",
            "SELECT maludb_platform.harden_extension_functions()",
        ):
            conn.execute(f'SET ROLE "{names.admin}"')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_a_tenants_own_functions_stay_callable(bootstrapped):
    """The revoke is scoped to extension-owned functions, not everything."""
    _, names, passwords = bootstrapped("tb000008")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute(
            "CREATE FUNCTION public.own_rpc() RETURNS int LANGUAGE sql STABLE AS $$ SELECT 42 $$"
        )
        tenant_conn.execute("GRANT EXECUTE ON FUNCTION public.own_rpc() TO anon")
        tenant_conn.commit()
        # re-running bootstrap must not strip the tenant's own grant
        tenant_bootstrap.apply(tenant_conn)
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT has_function_privilege('anon', 'public.own_rpc()', 'EXECUTE')")
            assert cur.fetchone()[0] is True


# -- auth helpers ----------------------------------------------------------


def test_auth_helpers_read_the_modern_claim_key(bootstrapped):
    """GoTrue's initial migration ships a version reading request.jwt.claim.sub,
    which returns NULL against PostgREST 14 and fails every policy closed."""
    _, names, _ = bootstrapped("tb000009")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn, tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            ('{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated","email":"a@b.test"}',),
        )
        cur.execute("SELECT auth.uid()::text, auth.role(), auth.email()")
        uid, role, email = cur.fetchone()
    assert uid == "11111111-1111-1111-1111-111111111111"
    assert role == "authenticated"
    assert email == "a@b.test"


def test_auth_helpers_return_null_without_claims(bootstrapped):
    """No claims must mean no identity, not an error -- policies fail closed."""
    _, names, _ = bootstrapped("tb00000a")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn, tenant_conn.cursor() as cur:
        cur.execute("SELECT auth.uid() IS NULL, auth.role() IS NULL")
        assert cur.fetchone() == (True, True)


def test_rls_policy_filters_by_auth_uid(bootstrapped):
    """The end-to-end property migrated policies depend on."""
    _, names, passwords = bootstrapped("tb00000b")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute(
            "CREATE TABLE public.items (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
            "owner_id uuid NOT NULL, secret text)"
        )
        tenant_conn.execute("ALTER TABLE public.items ENABLE ROW LEVEL SECURITY")
        tenant_conn.execute(
            "CREATE POLICY own ON public.items FOR SELECT TO authenticated USING (owner_id = auth.uid())"
        )
        tenant_conn.execute(
            "INSERT INTO public.items (owner_id, secret) VALUES "
            "('11111111-1111-1111-1111-111111111111','mine'), "
            "('22222222-2222-2222-2222-222222222222','theirs')"
        )
        tenant_conn.commit()

    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE authenticated")
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            ('{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}',),
        )
        cur.execute("SELECT secret FROM public.items")
        assert [r[0] for r in cur.fetchall()] == ["mine"]


# -- grant posture ---------------------------------------------------------


def test_anon_gets_a_grant_so_rls_returns_empty_not_denied(bootstrapped):
    """Phase 00 finding 7: no grant surfaces as 42501, not an empty set, and
    migrated applications depend on the difference."""
    _, names, passwords = bootstrapped("tb00000c")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE public.notes (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY)")
        tenant_conn.execute("ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY")
        tenant_conn.commit()

    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE anon")
        cur.execute("SELECT * FROM public.notes")  # empty, not denied
        assert cur.fetchall() == []


def test_tables_without_rls_are_surfaced(bootstrapped):
    """Diagnostic only: enabling RLS automatically would change the behaviour
    of a migrated application, which ADR-001 forbids."""
    _, names, _ = bootstrapped("tb00000d")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE public.unguarded (id int)")
        tenant_conn.commit()
        with tenant_conn.cursor() as cur:
            cur.execute("SELECT table_name FROM maludb_platform.tables_without_rls")
            assert "unguarded" in [r[0] for r in cur.fetchall()]
