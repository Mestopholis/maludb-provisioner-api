"""Phase 10 slice 1: the tenant's `storage` schema, under platform ownership.

**Object** storage. `tests/test_storage.py` is about database storage
accounting -- `pg_database_size`, the quota state machine, ADR-040's write
restriction -- and shares nothing with this file but a word. The plan named the
collision in advance and split the modules for it.

Two things are being established here, and only one of them is a feature.

The first is that upstream `storage-api` can be made to run without the
superuser it asks for. `.env.sample` wants `DB_INSTALL_ROLES=true` and
`DB_SUPER_USER=postgres`, which on a shared node is a container reaching for
role names ADR-016 shares with every other tenant. Slice 0 measured
`DB_INSTALL_ROLES=false` with the migrating role still a superuser and recorded
the remaining question as the one "most likely to produce an unwelcome
surprise". These answer it.

The second is that the answer stays true. The schema is populated long after
bootstrap runs -- first when the storage worker serves the tenant, then again
on every `storage-api` upgrade -- so the interesting assertions are about what
happens to a tenant *after* provisioning has finished with it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest

from services.control_plane import db, provisioning, tenant_bootstrap
from tests.conftest import STORAGE_IMAGE, requires_db, storage_image_available
from tests.test_provisioning import (
    ADMIN_DSN,
    MALUDB_CORE_AVAILABLE,
    _provision_core,
    _tenant_admin_dsn,
    _tenant_dsn,
)

pytestmark = [
    requires_db,
    pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset"),
]

requires_storage_image = pytest.mark.skipif(
    not storage_image_available(),
    reason=f"{STORAGE_IMAGE} is not available to podman",
)

SHARED_ROLES = ("anon", "authenticated", "service_role")


@pytest.fixture
def bootstrapped(admin_conn, key_ring, project_factory):
    """A provisioned, bootstrapped tenant and the passwords for its roles."""

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


def _scalar(conn: psycopg.Connection, query: str, params=None):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()[0]


# -- ownership and reach ---------------------------------------------------


def test_the_storage_schema_is_owned_by_the_tenants_own_storage_role(bootstrapped):
    """Bootstrap 007's arrangement, for a second service.

    The service that migrates a schema owns it. What makes that safe is that
    the owner is a per-tenant name: the privilege is attached to a
    per-database object, so it confers nothing in any other tenant.
    """
    _, names, _ = bootstrapped("os000001")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        owner = _scalar(
            tenant_conn,
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'storage'",
        )
    assert owner == names.storage


def test_the_shared_roles_reach_the_schema_and_the_tenant_admin_does_not(bootstrapped):
    """Slice 0's one-line remedy, and the line it does not cross.

    Upstream's migrations grant table privileges to the three shared names but
    leave the schema itself owner-only, so without the schema grant every
    Storage request answers `403 AccessDenied`. The admin role is deliberately
    not on that list: `storage` is service-owned bookkeeping whose consistency
    with the object store is the platform's responsibility, and a customer
    `DELETE` there orphans bytes that nothing will ever collect.
    """
    _, names, _ = bootstrapped("os000002")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        for role in SHARED_ROLES:
            assert _scalar(
                tenant_conn, "SELECT has_schema_privilege(%s, 'storage', 'USAGE')", (role,)
            ), f"{role} cannot reach the storage schema"

        # The admin role, and the two roles a customer actually logs in as --
        # both members of it and holding nothing beyond it. Asserted rather
        # than reasoned about: ADR-047 added the client role after the
        # executor, and a third one is plausible. Skipped where the helper
        # that built this tenant did not create them.
        for role in (names.admin, names.executor, names.client):
            if not _scalar(
                tenant_conn, "SELECT to_regrole(%s) IS NOT NULL", (role,)
            ):
                continue
            assert not _scalar(
                tenant_conn, "SELECT has_schema_privilege(%s, 'storage', 'USAGE')", (role,)
            ), f"{role} can reach the storage schema"


def test_nothing_is_granted_to_the_storage_role_outside_its_own_schema(bootstrapped):
    """Measured, not assumed: all 63 migrations complete without a grant on
    `public`, so bootstrap 007's `GRANT USAGE ON SCHEMA public` for the auth
    role has no counterpart here.

    Deliberately *not* asserting that `storage-api` cannot read the tenant's
    tables, because it can: the role switches into `anon`, `authenticated` and
    `service_role` per request -- it must, or no query it makes is governed by
    RLS -- and bootstrap 004 grants those `ALL ON ALL TABLES IN SCHEMA public`.
    Its reach through a role switch is PostgREST's authenticator's reach.

    `USAGE` is not checked for the same reason it is not revoked: PostgreSQL
    grants it on `public` to `PUBLIC` by default, so it is true of every role in
    the database and says nothing about this one.
    """
    _, names, _ = bootstrapped("os000003")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        assert not _scalar(
            tenant_conn, "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (names.storage,)
        )
        tenant_conn.execute("CREATE TABLE public.customer_table (id int)")
        tenant_conn.commit()
        assert not _scalar(
            tenant_conn,
            "SELECT has_table_privilege(%s, 'public.customer_table', 'SELECT')",
            (names.storage,),
        ), "the storage role holds a privilege of its own on a customer table"


def test_the_storage_roles_search_path_is_pinned_to_its_own_schema(bootstrapped):
    """The finding this slice turned on.

    Upstream migration 0011 opens with an unqualified `CREATE OR REPLACE
    FUNCTION update_updated_at_column()`. It lands wherever `search_path` says,
    and upstream says `storage` because its own migration 0002 sets that on
    `supabase_storage_admin` -- which is inside the `DB_INSTALL_ROLES=true`
    branch the platform must turn off. Left at the default, the function lands
    in `public`, the one schema PostgREST exposes.

    Pinned `IN DATABASE`, because a bare `ALTER ROLE ... SET` is cluster-wide
    and this is a property of the role's work in one tenant.
    """
    _, names, _ = bootstrapped("os000004")
    with psycopg.connect(ADMIN_DSN) as conn:
        setting = _scalar(
            conn,
            "SELECT setconfig FROM pg_db_role_setting s "
            "  JOIN pg_roles r ON r.oid = s.setrole "
            "  JOIN pg_database d ON d.oid = s.setdatabase "
            " WHERE r.rolname = %s AND d.datname = %s",
            (names.storage, names.database),
        )
    assert "search_path=storage" in setting


# -- the role itself -------------------------------------------------------


def test_the_storage_role_holds_no_attribute_worth_stealing(bootstrapped):
    """A service credential that lives in a container is a credential that can
    leak, so what it is worth is what it can do.

    `REPLICATION` is the one that matters and is checked here as well as in
    `tests/test_realtime_enablement.py`: ADR-031 exists because a role holding
    it answers a base backup with a readable copy of every database on the node.
    """
    _, names, _ = bootstrapped("os000005")
    with psycopg.connect(ADMIN_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, "
                "       rolinherit, rolcanlogin "
                "  FROM pg_roles WHERE rolname = %s",
                (names.storage,),
            )
            row = cur.fetchone()
    (super_, bypassrls, createrole, createdb, replication, inherit, canlogin) = row
    assert not any((super_, bypassrls, createrole, createdb, replication))
    assert not inherit, "NOINHERIT: the shared roles are entered by an explicit switch"
    assert canlogin, "storage-api connects as this role"


def test_the_storage_role_can_switch_into_the_shared_roles(bootstrapped):
    """What makes row-level security apply at all.

    `storage-api` does not query as the owner. Its
    `internal/database/postgres/scope.js` issues
    `set_config('role', <role from the JWT>, true)` -- a `SET LOCAL ROLE` --
    per request, and without the membership every customer-scoped query fails.

    ADR-016's permitted direction only: the shared names are granted *to* this
    role. The reverse would make every tenant's `authenticated` a member of one
    tenant's storage role, which is the single most dangerous grant in the
    model.
    """
    _, names, passwords = bootstrapped("os000006")
    dsn = _tenant_dsn(names.database, names.storage, passwords["storage"])
    with psycopg.connect(dsn) as conn:
        for role in SHARED_ROLES:
            with conn.transaction():
                conn.execute("SELECT set_config('role', %s, true)", (role,))
                assert _scalar(conn, "SELECT current_user") == role

    with psycopg.connect(ADMIN_DSN) as conn:
        for role in SHARED_ROLES:
            assert not _scalar(
                conn, "SELECT pg_has_role(%s, %s, 'MEMBER')", (role, names.storage)
            ), f"{role} is a member of {names.storage}; ADR-016 forbids that direction"


# -- hardening, which runs long after bootstrap does -----------------------


def test_the_hardening_trigger_fires_for_ordinary_ddl(bootstrapped):
    """Bootstrap 003's mistake, not repeated.

    The `storage` schema is empty when bootstrap runs. A one-shot revoke would
    harden nothing and then be recorded as applied forever, and the tables that
    matter appear the first time the storage worker serves this tenant.
    """
    _, names, _ = bootstrapped("os000007")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        enabled = _scalar(
            tenant_conn,
            "SELECT evtenabled FROM pg_event_trigger WHERE evtname = 'maludb_harden_storage'",
        )
    # 'O' and 'A' fire for ordinary DDL. 'R' fires only when
    # session_replication_role is 'replica', which is to say never -- the
    # distinction `tenant_bootstrap._FIRING` exists for.
    assert enabled in ("O", "A")


def test_a_new_table_in_storage_is_governed_before_the_ddl_commits(bootstrapped):
    """The property the trigger buys, stated as an outcome.

    A future `storage-api` migration adding a table is the case this exists
    for. Bootstrap 004's grant posture (ADR-018) means a denial should look
    like an empty set rather than an error, so upstream grants broadly to
    `anon` and lets RLS decide -- and a table carrying that grant with RLS off
    is readable by anyone holding the project's publishable key.
    """
    _, names, _ = bootstrapped("os000008")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE storage.pretend_upstream_table (id int)")
        tenant_conn.commit()
        assert _scalar(
            tenant_conn,
            "SELECT relrowsecurity FROM pg_class WHERE oid = 'storage.pretend_upstream_table'::regclass",
        ), "the hardening did not run before the CREATE TABLE committed"


def test_surface_phase_10_does_not_expose_is_revoked(bootstrapped):
    """`buckets_vectors`, `vector_indexes` and the two `iceberg_*` tables.

    Upstream grants `SELECT` on the vector tables to all three shared names
    unconditionally. Phase 10 exposes neither feature, and an unexamined table
    reachable from a tenant's own API roles is the shape ADR-018 exists for.

    Simulated rather than waited for: the real ones arrive with slice 3, and
    the invariant is that the hardening covers them by name whenever they do.
    """
    _, names, _ = bootstrapped("os000009")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE storage.buckets_vectors (id int)")
        tenant_conn.execute(
            "GRANT SELECT ON storage.buckets_vectors TO anon, authenticated, service_role"
        )
        tenant_conn.commit()
        # The GRANT is not in the trigger's tag list, deliberately -- bootstrap
        # 012 records why a hardening function that issues GRANT and REVOKE
        # should not be reachable from a trigger on GRANT and REVOKE. So this
        # is the explicit call slice 3 makes after running migrations.
        tenant_conn.execute("SELECT maludb_platform.harden_storage_schema()")
        tenant_conn.commit()
        for role in SHARED_ROLES:
            assert not _scalar(
                tenant_conn,
                "SELECT has_table_privilege(%s, 'storage.buckets_vectors', 'SELECT')",
                (role,),
            ), f"{role} can still read storage.buckets_vectors"


def test_the_hardening_is_idempotent(bootstrapped):
    """It runs on every DDL touching the schema and on every worker
    registration, so a second call must be a no-op rather than a second
    opinion."""
    _, names, _ = bootstrapped("os000010")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE storage.objects_stand_in (id int)")
        tenant_conn.commit()
        first = _scalar(tenant_conn, "SELECT maludb_platform.harden_storage_schema()")
        second = _scalar(tenant_conn, "SELECT maludb_platform.harden_storage_schema()")
        tenant_conn.commit()
    assert first == 0, "the trigger should already have enabled RLS on the new table"
    assert second == 0


def test_the_tenant_admin_cannot_remove_the_hardening(bootstrapped):
    """The admin role holds `CREATE ON DATABASE` (bootstrap 010), which is a
    broader privilege than it reads as. It is still not the owner of anything
    in `storage`."""
    _, names, passwords = bootstrapped("os000011")
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'ALTER ROLE "{names.admin}" LOGIN')
    try:
        dsn = _tenant_dsn(names.database, names.admin, passwords["admin"])
        with psycopg.connect(dsn) as conn:
            for statement in (
                "DROP EVENT TRIGGER maludb_harden_storage",
                "ALTER EVENT TRIGGER maludb_harden_storage DISABLE",
                "DROP SCHEMA storage CASCADE",
                "ALTER SCHEMA storage OWNER TO CURRENT_USER",
                "GRANT USAGE ON SCHEMA storage TO CURRENT_USER",
            ):
                with pytest.raises(psycopg.errors.Error), conn.transaction():
                    conn.execute(statement)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'ALTER ROLE "{names.admin}" NOLOGIN')


# -- verify() refuses to certify a tenant that drifted ---------------------


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        (
            "ALTER SCHEMA storage OWNER TO CURRENT_USER",
            "owned by",
        ),
        (
            "REVOKE USAGE ON SCHEMA storage FROM anon",
            "USAGE on the storage schema",
        ),
        (
            "DROP EVENT TRIGGER maludb_harden_storage",
            "maludb_harden_storage event trigger is missing",
        ),
        (
            "ALTER EVENT TRIGGER maludb_harden_storage ENABLE REPLICA",
            "does not fire for ordinary DDL",
        ),
        (
            "DROP SCHEMA storage CASCADE",
            "storage schema is missing",
        ),
    ],
)
def test_verify_refuses_a_tenant_whose_storage_hardening_was_undone(
    bootstrapped, tamper, expected
):
    """Outcomes, not statements. A superuser-run repair script is how each of
    these actually goes missing, which is the case `verify` exists for -- and
    `ENABLE REPLICA` is in the list because it is the value that looks enabled
    and fires for nothing a customer does."""
    _, names, _ = bootstrapped("os000012")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute(tamper)
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match=expected):
            tenant_bootstrap.verify(tenant_conn)


def test_turning_row_level_security_off_in_storage_is_undone_before_it_commits(
    bootstrapped,
):
    """`ALTER TABLE ... DISABLE ROW LEVEL SECURITY` is itself an `ALTER TABLE`,
    so the trigger fires on it and puts it back.

    Worth asserting as its own property rather than as a side effect: the
    hardening is not only a catch-up pass over tables that appear later, it is
    also what makes the invariant survive a statement aimed at breaking it.
    """
    _, names, _ = bootstrapped("os000013")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE storage.ungoverned (id int)")
        tenant_conn.execute("ALTER TABLE storage.ungoverned DISABLE ROW LEVEL SECURITY")
        tenant_conn.commit()
        assert _scalar(
            tenant_conn,
            "SELECT relrowsecurity FROM pg_class WHERE oid = 'storage.ungoverned'::regclass",
        )


def test_verify_refuses_a_table_in_storage_without_row_level_security(bootstrapped):
    """The state a tenant can only reach past the trigger -- which is the state
    `verify` exists for. A superuser-run repair script that disables event
    triggers while it works is how it actually happens."""
    _, names, _ = bootstrapped("os000021")
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("ALTER EVENT TRIGGER maludb_harden_storage DISABLE")
        tenant_conn.execute("CREATE TABLE storage.ungoverned (id int)")
        tenant_conn.execute("ALTER EVENT TRIGGER maludb_harden_storage ENABLE")
        tenant_conn.commit()
        with pytest.raises(tenant_bootstrap.BootstrapError, match="row-level security"):
            tenant_bootstrap.verify(tenant_conn)


def test_bootstrap_refuses_a_tenant_with_no_storage_role(bootstrapped):
    """Bootstrap 012 raises rather than skipping, on bootstrap 007's reasoning.

    Continuing would leave the schema owned by the platform superuser and
    upstream's migrations unable to create a table in it -- diagnosed much
    later, as a confusing migration failure inside a container.
    """
    _, names, _ = bootstrapped("os000014")
    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as tenant_conn:
        tenant_conn.execute("DROP SCHEMA storage CASCADE")
        tenant_conn.execute(
            "DELETE FROM maludb_platform.bootstrap_migrations WHERE version LIKE '012%'"
        )
        # CONNECT and the search_path setting are both dependencies of the role.
        # Production never meets this: `jobs.cleanup` drops the database first,
        # which takes them with it.
        tenant_conn.execute(f'DROP OWNED BY "{names.storage}"')
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP ROLE "{names.storage}"')

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        with pytest.raises(psycopg.errors.RaiseException, match="no storage role"):
            tenant_bootstrap.apply(tenant_conn)


# -- upstream's migrations, under the constrained owner --------------------


def _tenant_migrations(destination: Path) -> list[Path]:
    """Take upstream's tenant migrations out of the pinned image.

    There is no release tarball -- `supabase/storage` publishes `api.json` and
    `api-admin.json` and nothing else (ADR-058) -- so the image is the only
    place these exist, and it is also the artefact the platform will run.
    Reading them from anywhere else would be testing a copy.
    """
    tar = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "podman", "run", "--rm", "--entrypoint", "sh", STORAGE_IMAGE,
            "-c", "cd /app/migrations/tenant && tar cf - .",
        ],
        check=True, capture_output=True,
    ).stdout
    subprocess.run(  # noqa: S603
        ["tar", "xf", "-", "-C", str(destination)], input=tar, check=True  # noqa: S607
    )
    return sorted(destination.glob("*.sql"), key=lambda p: p.name)


# Upstream's own defaults, minus the parts ADR-004 and ADR-016 forbid. These are
# the `set_config` calls `dist/internal/database/migrations/migrate.js` makes
# before it applies a file, so applying them here is running upstream's
# migrations the way upstream runs them.
_SETTINGS = (
    # The whole point. True would have this container create `anon`,
    # `authenticated`, `service_role` and a superuser, on a cluster it shares
    # with every other tenant.
    ("install_roles", "false"),
    # ADR-058's topology, and not only a label: migration 0038 returns early
    # when it is set, so a multi-tenant instance never creates the two
    # `iceberg_*` tables at all.
    ("multitenant", "true"),
    ("anon_role", "anon"),
    ("authenticated_role", "authenticated"),
    ("service_role", "service_role"),
)


def _apply_tenant_migrations(dsn: str, storage_role: str) -> int:
    """Run every tenant migration out of the pinned image, as `storage_role`.

    Session-scoped `set_config`, then one file at a time -- and the
    `-- postgres-migrations disable-transaction` marker is honoured rather than
    ignored. Seven files carry it, all of them `CREATE`/`DROP INDEX
    CONCURRENTLY`, which PostgreSQL refuses inside a transaction block. Sending
    a file as one multi-statement string puts it in an implicit one, so
    ignoring the marker fails the same way upstream's own runner would if it
    did.
    """
    applied = 0
    with tempfile.TemporaryDirectory() as tmp:
        files = _tenant_migrations(Path(tmp))
        with psycopg.connect(dsn, autocommit=True) as conn:
            for key, value in (*_SETTINGS, ("super_user", storage_role)):
                conn.execute(
                    "SELECT set_config(%s, %s, false)", (f"storage.{key}", value)
                )
            for path in files:
                body = path.read_text()
                if "postgres-migrations disable-transaction" in body:
                    conn.execute(body)
                else:
                    with conn.transaction():
                        conn.execute(body)
                applied += 1
    return applied


@requires_storage_image
def test_upstreams_migrations_complete_under_a_constrained_owner(bootstrapped):
    """Slice 0's open question, answered.

    `specs/storage-server-model.md` recorded that `DB_INSTALL_ROLES=false` was
    measured with the migrating role still a **superuser**, and named this as
    the unknown most likely to produce an unwelcome surprise. It did produce
    one, and bootstrap 012 records it: 62 of 63 files pass untouched, and
    `0011-add-trigger-to-auto-update-updated_at-column.sql` fails with
    `permission denied for schema public` unless the role's `search_path` is
    pinned.

    The tempting fix -- granting `CREATE ON SCHEMA public` -- makes the
    migration pass while dropping a platform function into the customer's Data
    API namespace, which is why the assertion below is not "the migrations
    succeeded" but "the migrations succeeded **and** `public` is empty".
    """
    _, names, passwords = bootstrapped("os000015")
    dsn = _tenant_dsn(names.database, names.storage, passwords["storage"])

    # A diff rather than a count of zero: `public` already holds maludb_core's
    # 373 functions on a node that has the extension (ADR-015), and the question
    # is what the migrations add, not what was there first.
    def public_objects() -> set[str]:
        with psycopg.connect(_tenant_admin_dsn(names.database)) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 'rel:' || c.relname FROM pg_class c "
                "  JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f') "
                "UNION ALL "
                "SELECT 'fn:' || p.oid::regprocedure::text FROM pg_proc p "
                "  JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
            )
            return {row[0] for row in cur.fetchall()}

    before = public_objects()
    applied = _apply_tenant_migrations(dsn, names.storage)
    assert applied >= 60, f"only {applied} migrations came out of the image"

    added = public_objects() - before
    assert added == set(), (
        f"upstream's migrations added {sorted(added)} to `public`, the one schema PostgREST "
        "exposes; migration 0011 creates a function unqualified and the search_path pin is "
        "what keeps it out"
    )

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        with tenant_conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, pg_get_userbyid(c.relowner), c.relrowsecurity "
                "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'storage' AND c.relkind IN ('r','p')"
            )
            tables = cur.fetchall()

    assert {name for name, _, _ in tables} >= {"buckets", "objects", "migrations"}
    for name, owner, rls in tables:
        assert owner == names.storage, f"storage.{name} is owned by {owner}"
        assert rls, f"storage.{name} has row-level security disabled"

    # And the tenant is still certifiable afterwards, which is the check a
    # fleet-wide `storage-api` upgrade would gate on.
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_bootstrap.verify(tenant_conn)


@requires_storage_image
def test_row_level_security_governs_a_switched_role_and_not_the_owner(bootstrapped):
    """The decision bootstrap 012 had to make deliberately, demonstrated.

    Upstream enables RLS without forcing it, so the owner bypasses it. Forcing
    it would deny `storage-api`'s own bookkeeping -- migrations, multipart
    reaping, deletion -- against a schema with no policies in it, and the
    service would not work. So the owner bypass stays and the control is that
    the owning role is not customer-reachable.

    `service_role` bypassing storage policies is ADR-041's finding in a new
    place: a role named in a request selects a credential, never a permission
    boundary. It is upstream's behaviour, it matches Supabase, and it is the
    customer's own project reached with the customer's own service key.
    """
    _, names, passwords = bootstrapped("os000016")
    dsn = _tenant_dsn(names.database, names.storage, passwords["storage"])
    _apply_tenant_migrations(dsn, names.storage)

    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO storage.buckets (id, name) VALUES ('b','b')")
        conn.execute(
            "INSERT INTO storage.objects (bucket_id, name, owner) VALUES ('b','secret.txt',%s)",
            ("11111111-1111-1111-1111-111111111111",),
        )
        conn.commit()

        assert _scalar(conn, "SELECT count(*) FROM storage.objects") == 1

        with conn.transaction():
            conn.execute("SELECT set_config('role', 'authenticated', true)")
            assert _scalar(conn, "SELECT count(*) FROM storage.objects") == 0, (
                "authenticated saw an object with no policy granting it; RLS is not applying"
            )

        with conn.transaction():
            conn.execute("SELECT set_config('role', 'anon', true)")
            assert _scalar(conn, "SELECT count(*) FROM storage.objects") == 0

        with conn.transaction():
            conn.execute("SELECT set_config('role', 'service_role', true)")
            assert _scalar(conn, "SELECT count(*) FROM storage.objects") == 1, (
                "service_role no longer bypasses RLS; every migrated Supabase application "
                "that uses a service key against Storage depends on it doing so"
            )


@requires_storage_image
def test_a_policy_on_storage_objects_gates_a_switched_role(bootstrapped):
    """Storage policies *are* RLS policies, which is why this phase cannot
    harden by turning RLS off. The full compatibility surface is slice 5's;
    this is the mechanism working at all.

    The policy is created by the platform rather than by the storage role, and
    the reason is a gap slice 1 found and did not close. Two things stop a
    customer writing this policy themselves today:

    * `CREATE POLICY` requires **ownership** of the table, and the owner is
      `mldb_<ref>_storage`, which no customer-reachable role is a member of;
    * the storage role itself has no `USAGE` on `auth`, so even it cannot
      compile a policy calling `auth.uid()` -- which is what essentially every
      Supabase storage policy calls.

    Supabase's answer is that its `postgres` role is a member of
    `supabase_storage_admin`, so the dashboard's policy editor can create them.
    The MaluDB analogue would hand `mldb_<ref>_admin` membership in the storage
    role, and that is owner-level bypass of every storage policy plus write
    access to metadata the object store is kept consistent with -- a decision
    with real weight, belonging to the slice that serves the Storage API rather
    than to the one that creates its schema. `plans/active/phase-10-storage.md`
    carries it forward to slice 4.
    """
    _, names, passwords = bootstrapped("os000017")
    dsn = _tenant_dsn(names.database, names.storage, passwords["storage"])
    owner = "11111111-1111-1111-1111-111111111111"
    _apply_tenant_migrations(dsn, names.storage)

    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as platform:
        platform.execute(
            "CREATE POLICY own_objects ON storage.objects FOR SELECT TO authenticated "
            "  USING (owner = auth.uid())"
        )

    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO storage.buckets (id, name) VALUES ('b','b')")
        conn.execute(
            "INSERT INTO storage.objects (bucket_id, name, owner) VALUES ('b','mine.txt',%s)",
            (owner,),
        )
        conn.commit()

        for subject, expected in ((owner, 1), ("22222222-2222-2222-2222-222222222222", 0)):
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": subject, "role": "authenticated"}),),
                )
                conn.execute("SELECT set_config('role', 'authenticated', true)")
                assert _scalar(conn, "SELECT count(*) FROM storage.objects") == expected


# -- provisioning ----------------------------------------------------------


def test_the_step_is_idempotent_and_keeps_the_stored_credential_usable(
    admin_conn, key_ring, project_factory
):
    """`_storage_role_done` asks for the role *and* a credential, on
    `_client_done`'s reasoning: a leftover role from an earlier run would make
    the step a no-op and leave a login nobody holds the password for.

    Re-running resets the password to the one being stored in the same step,
    which is `create_roles`' rule and is what recovers a run that died between
    creating the role and storing the secret.
    """
    ref = "os000018"
    project_id = project_factory(ref)
    names, passwords = _provision_core(project_id, admin_conn, key_ring, ref)

    second = provisioning.generate_password()
    provisioning.create_storage_role(admin_conn, names, password=second)
    provisioning.grant_storage_connect(admin_conn, names)
    admin_conn.commit()

    with psycopg.connect(_tenant_dsn(names.database, names.storage, second)) as conn:
        assert _scalar(conn, "SELECT current_user") == names.storage
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(names.database, names.storage, passwords["storage"]))


def test_the_storage_role_cannot_reach_another_tenants_database(
    admin_conn, key_ring, project_factory
):
    """ADR-014, for a role that did not exist when it was written.

    One shared `storage-api` process holds every registered tenant's DSN
    (ADR-058), so the question of what one tenant's credential can reach is not
    hypothetical -- it is the blast radius the shared topology was accepted on.
    """
    first, second = "os000019", "os000020"
    names_a, passwords_a = _provision_core(
        project_factory(first), admin_conn, key_ring, first
    )
    names_b, _ = _provision_core(project_factory(second), admin_conn, key_ring, second)

    with pytest.raises(psycopg.OperationalError, match="permission denied|not permitted"):
        psycopg.connect(_tenant_dsn(names_b.database, names_a.storage, passwords_a["storage"]))
