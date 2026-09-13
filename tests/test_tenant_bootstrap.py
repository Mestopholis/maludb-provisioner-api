"""Tenant bootstrap, against a real database.

During the Phase 00 spike `anon` invoked `/rpc/gen_salt` on a provisioned
tenant. ADR-018 answered by revoking EXECUTE; ADR-076 replaced that with grants
to the customer roles and a PostgREST pre-request check. These assert the
properties as outcomes -- who can execute what -- rather than that statements ran.
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

    def build(ref: str, *, rpc_check_live: bool = True):
        # Live by default: a freshly provisioned tenant has no worker yet, which
        # is the provisioning pipeline's own reason for passing it (ADR-076).
        project_id = project_factory(ref)
        names, passwords = _provision_core(project_id, admin_conn, key_ring, ref)
        with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
            if MALUDB_CORE_AVAILABLE:
                tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
                tenant_conn.commit()
            with db.connection() as conn:
                tenant_bootstrap.bootstrap_project(
                    conn, tenant_conn, project_id=project_id, rpc_check_live=rpc_check_live
                )
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


# -- ADR-076: customer roles execute extension functions -------------------
#
# ADR-018 revoked EXECUTE from PUBLIC so anon could not call /rpc/gen_salt, and
# took every customer role with it. ADR-076 grants the six customer roles back
# and keeps the RPC surface closed with a PostgREST pre-request check instead;
# the refusal itself is asserted through a real PostgREST in test_workers.py.

_CUSTOMER_CALLS = (
    "SELECT gen_salt('bf')",
    "SELECT '[1,2]'::vector <-> '[2,3]'::vector",
    "SELECT similarity('abc', 'abd')",
    "SELECT digest('x', 'sha256')",
)


def _extension_functions_executable_by(conn, role: str, *, extension: str | None = None) -> tuple[int, int]:
    """(executable by role, total) over extension-owned functions, maludb_core excluded."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE has_function_privilege(%s, p.oid, 'EXECUTE')), count(*)
              FROM pg_proc p
              JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
              JOIN pg_extension e ON e.oid = d.refobjid
             WHERE e.extname <> 'maludb_core' AND (%s::text IS NULL OR e.extname = %s)
            """,
            (role, extension, extension),
        )
        return cur.fetchone()


@requires_maludb_core
def test_every_customer_role_executes_extension_functions(bootstrapped):
    """Pinning slice 0, finding 7: every one of these was `permission denied`,
    for service_role and the tenant's own admin as much as for anon."""
    _, names, _ = bootstrapped("tb000005")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as conn:
        for role in ("anon", "authenticated", "service_role", names.admin):
            executable, total = _extension_functions_executable_by(conn, role)
            assert executable == total, f"{role} executes {executable} of {total} extension functions"
            conn.execute(f'SET ROLE "{role}"')
            for statement in _CUSTOMER_CALLS:
                conn.execute(statement)
            conn.execute("RESET ROLE")


@requires_maludb_core
def test_a_uuid_default_and_a_crypt_trigger_work_for_a_signed_in_user(bootstrapped):
    """Checked against whoever runs the statement -- defaults and trigger bodies
    included -- which is why Phase 08's superuser-run migration test missed it."""
    _, names, passwords = bootstrapped("tb000006")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        tenant_conn.execute(
            "CREATE TABLE public.accounts (id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), "
            "secret text NOT NULL)"
        )
        tenant_conn.execute(
            "CREATE FUNCTION public.hash_secret() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN NEW.secret := crypt(NEW.secret, gen_salt('bf', 4)); RETURN NEW; END $$"
        )
        tenant_conn.execute(
            "CREATE TRIGGER hash_secret BEFORE INSERT ON public.accounts "
            "FOR EACH ROW EXECUTE FUNCTION public.hash_secret()"
        )
        tenant_conn.commit()

    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE authenticated")
        cur.execute("INSERT INTO public.accounts (secret) VALUES ('hunter2') RETURNING id, secret")
        row_id, stored = cur.fetchone()
    assert row_id is not None
    assert stored.startswith("$2a$04$"), f"trigger did not hash: {stored}"


@requires_maludb_core
def test_maludb_core_functions_stay_unexecutable_by_customer_roles(bootstrapped):
    """Excluded from ADR-076's grant: 94 are SECURITY DEFINER, owned by the node
    superuser, and mc2db -- reachable by PUBLIC -- writes MaluDB's MCP registry."""
    _, names, _ = bootstrapped("tb000007")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as conn, conn.cursor() as cur:
        for role in ("anon", "authenticated", "service_role", names.admin):
            cur.execute(
                """
                SELECT count(*) FROM pg_proc p
                  JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
                  JOIN pg_extension e ON e.oid = d.refobjid AND e.extname = 'maludb_core'
                 WHERE has_function_privilege(%s, p.oid, 'EXECUTE')
                """,
                (role,),
            )
            assert cur.fetchone()[0] == 0, f"{role} can execute maludb_core functions"
        cur.execute("SET ROLE anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT mc2db.create_server('x', 'x', 'x', ARRAY['x'], 'low')")


@requires_maludb_core
def test_bootstrap_holds_the_grants_until_the_rpc_check_is_live(bootstrapped):
    """ADR-076 decision 5. A serving tenant whose worker does not yet refuse
    extension functions as RPC must not receive the grants -- that is ADR-018's
    finding reopened. `apply` stops before 014 unless told the check is live."""
    project_id, names, passwords = bootstrapped("tb00001a", rpc_check_live=False)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        recorded = set(tenant_bootstrap.applied(tenant_conn))
        assert "013_extension_rpc_check" in recorded
        assert "014_extension_function_grants" not in recorded
        tenant_bootstrap.verify(tenant_conn)  # a legitimate state mid-rollout

    with db.connection() as conn:
        row = db.one(conn, "SELECT bootstrap_version FROM projects WHERE id = %s", (project_id,))
    assert row["bootstrap_version"] == tenant_bootstrap.RPC_CHECK_VERSION

    dsn = _tenant_dsn(names.database, names.authenticator, passwords["authenticator"])
    with psycopg.connect(dsn) as conn:
        conn.execute("SET ROLE anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT gen_salt('bf')")

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        assert tenant_bootstrap.apply(tenant_conn, rpc_check_live=True) == [
            "014_extension_function_grants"
        ]
        executable, total = _extension_functions_executable_by(tenant_conn, "anon")
        assert executable == total


@requires_maludb_core
def test_verify_rejects_extension_grants_widened_to_public(bootstrapped):
    """PUBLIC is every role on the node, not the six the platform names."""
    _, names, _ = bootstrapped("tb00001b")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("GRANT EXECUTE ON FUNCTION public.gen_salt(text) TO PUBLIC")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="PUBLIC"):
            tenant_bootstrap.verify(tenant_conn)


@requires_maludb_core
def test_verify_rejects_a_missing_customer_grant(bootstrapped):
    _, names, _ = bootstrapped("tb00001c")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_bootstrap.verify(tenant_conn)
        tenant_conn.execute("REVOKE EXECUTE ON FUNCTION public.gen_salt(text) FROM service_role")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="missing service_role"):
            tenant_bootstrap.verify(tenant_conn)


@requires_maludb_core
def test_verify_rejects_a_maludb_core_function_granted_to_a_customer_role(bootstrapped):
    _, names, _ = bootstrapped("tb00001d")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("GRANT EXECUTE ON FUNCTION mc2db.create_server(text, text, text, text[], text) TO anon")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="maludb_core"):
            tenant_bootstrap.verify(tenant_conn)


@requires_maludb_core
def test_verify_rejects_anon_execute_before_the_grants_are_due(bootstrapped):
    """Before 014 the old posture is the right one: the worker may not refuse
    extension functions as RPC, so anon executing one is ADR-018's finding."""
    _, names, _ = bootstrapped("tb00001e", rpc_check_live=False)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("GRANT EXECUTE ON FUNCTION public.gen_salt(text) TO anon")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="before bootstrap 014"):
            tenant_bootstrap.verify(tenant_conn)


@requires_maludb_core
def test_verify_rejects_an_in_database_pre_request_override(bootstrapped):
    """Grants slice 0, finding 7: PostgREST prefers in-database configuration to
    its file, and an empty value there switched the check off."""
    _, names, _ = bootstrapped("tb00001g")
    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as conn:
        tenant_bootstrap.verify(conn)
        for target, reset in (
            (f'ROLE "{names.authenticator}" IN DATABASE "{names.database}"',
             f'ROLE "{names.authenticator}" IN DATABASE "{names.database}"'),
            (f'DATABASE "{names.database}"', f'DATABASE "{names.database}"'),
        ):
            conn.execute(f"ALTER {target} SET pgrst.db_pre_request = ''")
            with pytest.raises(tenant_bootstrap.BootstrapError, match="in-database"):
                tenant_bootstrap.verify(conn)
            conn.execute(f"ALTER {reset} RESET pgrst.db_pre_request")
        tenant_bootstrap.verify(conn)


@requires_maludb_core
def test_the_rpc_check_schema_holds_only_the_check(bootstrapped):
    """anon, authenticated and service_role hold USAGE on maludb_guard, so
    anything else placed there is reachable by them."""
    _, names, _ = bootstrapped("tb00001h")
    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'maludb_guard'"
            )
            assert [r[0] for r in cur.fetchall()] == ["refuse_extension_rpc"]
            for role in ("anon", "authenticated", "service_role"):
                cur.execute("SELECT has_schema_privilege(%s, 'maludb_guard', 'USAGE'), "
                            "has_schema_privilege(%s, 'maludb_guard', 'CREATE')", (role, role))
                assert cur.fetchone() == (True, False)
            cur.execute("SELECT has_schema_privilege(%s, 'maludb_guard', 'CREATE')", (names.admin,))
            assert cur.fetchone()[0] is False

        conn.execute("CREATE FUNCTION maludb_guard.extra() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        with pytest.raises(tenant_bootstrap.BootstrapError, match="only refuse_extension_rpc"):
            tenant_bootstrap.verify(conn)


@requires_maludb_core
def test_a_customer_schema_named_maludb_guard_stops_bootstrap(admin_conn, key_ring, project_factory):
    """The tenant admin holds CREATE ON DATABASE, and the owner of a schema can
    replace what is in it. On a tenant provisioned before 013, a customer could
    already own the name; bootstrap refuses rather than adopting it."""
    ref = "tb00001i"
    project_id = project_factory(ref)
    names, _ = _provision_core(project_id, admin_conn, key_ring, ref)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
        tenant_conn.execute(f'CREATE SCHEMA maludb_guard AUTHORIZATION "{names.admin}"')
        tenant_conn.commit()
        with db.connection() as conn, pytest.raises(tenant_bootstrap.BootstrapError, match="reserved"):
            tenant_bootstrap.bootstrap_project(conn, tenant_conn, project_id=project_id,
                                               rpc_check_live=True)
        assert "013_extension_rpc_check" not in tenant_bootstrap.applied(tenant_conn)


@requires_maludb_core
def test_the_tenant_admin_cannot_replace_the_rpc_check(bootstrapped):
    """Grants slice 0 measured all of these refused; kept that way."""
    _, names, _ = bootstrapped("tb00001j")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as conn:
        for statement in (
            "CREATE OR REPLACE FUNCTION maludb_guard.refuse_extension_rpc() RETURNS void "
            "LANGUAGE sql AS 'SELECT'",
            "DROP FUNCTION maludb_guard.refuse_extension_rpc()",
            "REVOKE EXECUTE ON FUNCTION maludb_guard.refuse_extension_rpc() FROM anon",
            f'ALTER ROLE "{names.authenticator}" IN DATABASE "{names.database}" '
            "SET pgrst.db_pre_request = ''",
            f'ALTER DATABASE "{names.database}" SET pgrst.db_pre_request = \'\'',
        ):
            conn.execute(f'SET ROLE "{names.admin}"')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


# -- the posture must survive later extension changes ----------------------


def _spare_extension(conn) -> str:
    """An installable extension other than maludb_core, or skip."""
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


@requires_maludb_core
def test_installing_an_extension_after_bootstrap_grants_it_to_customer_roles(bootstrapped):
    """The event trigger now grants rather than revokes, so an extension a
    customer installs later gets the same posture by construction."""
    _, names, _ = bootstrapped("tb00000e")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        extension = _spare_extension(tenant_conn)
        tenant_conn.execute(f'CREATE EXTENSION "{extension}"')
        tenant_conn.commit()
        for role in ("anon", "service_role", names.admin):
            executable, total = _extension_functions_executable_by(tenant_conn, role, extension=extension)
            assert total and executable == total, f"{role}: {executable}/{total} of {extension}"
        tenant_bootstrap.verify(tenant_conn)


@requires_maludb_core
def test_installing_an_extension_before_the_grants_keeps_it_from_anon(bootstrapped):
    """A tenant the fleet run has not reached still has the revoking trigger."""
    _, names, _ = bootstrapped("tb00001f", rpc_check_live=False)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        extension = _spare_extension(tenant_conn)
        tenant_conn.execute(f'CREATE EXTENSION "{extension}"')
        tenant_conn.commit()
        executable, total = _extension_functions_executable_by(tenant_conn, "anon", extension=extension)
        assert total and executable == 0
        tenant_bootstrap.verify(tenant_conn)


def test_verify_rejects_a_tenant_whose_event_trigger_was_dropped(bootstrapped):
    """Only a superuser can drop it -- which is exactly how it would happen."""
    _, names, _ = bootstrapped("tb00000g")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("DROP EVENT TRIGGER maludb_harden_extensions")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="event trigger is missing"):
            tenant_bootstrap.verify(tenant_conn)


def test_verify_rejects_an_event_trigger_that_does_not_fire(bootstrapped):
    """Both ways a trigger can be present and inert.

    `DISABLE` is the obvious one. `ENABLE REPLICA` is the one that was accepted
    until the Phase 08 slice 6a security review: `evtenabled` has four values,
    and `'R'` means the trigger fires only when
    `session_replication_role = 'replica'` -- never, for ordinary customer DDL.
    A check written as `<> 'D'` therefore certified a tenant whose hardening was
    silently doing nothing.
    """
    _, names, _ = bootstrapped("tb00000h")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        for state in ("DISABLE", "ENABLE REPLICA"):
            tenant_conn.execute(f"ALTER EVENT TRIGGER maludb_harden_extensions {state}")
            tenant_conn.commit()
            with pytest.raises(tenant_bootstrap.BootstrapError, match="does not fire"):
                tenant_bootstrap.verify(tenant_conn)
            tenant_conn.execute("ALTER EVENT TRIGGER maludb_harden_extensions ENABLE")
            tenant_conn.commit()


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

        # Negative test B: the same session, a different `sub`. The policy must
        # follow the claim rather than the connection -- PostgREST reuses one
        # pooled connection across users, so a policy that latched onto the
        # first caller would serve their rows to everybody after them.
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            ('{"sub":"22222222-2222-2222-2222-222222222222","role":"authenticated"}',),
        )
        cur.execute("SELECT secret FROM public.items")
        assert [r[0] for r in cur.fetchall()] == ["theirs"]


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
