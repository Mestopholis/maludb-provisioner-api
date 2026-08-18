"""Installing an allowlisted extension, and refusing everything else (ADR-045).

Phase 08 slice 6. The customer-facing goal is one line: a migrated Supabase
schema opens with `create extension if not exists "uuid-ossp"`, and that line
has to work as written. An installer function the customer would call instead
does not achieve it, because then a migration cannot apply the customer's own
SQL unaltered -- which is why the mechanism here is not the one ADR-045
described. See `bootstrap/010_extension_allowlist.sql`.

The negatives are the reason this slice needed a node. Every one of them was
measured before it was coded, and two of the measurements changed the design:

- `current_user` inside a `SECURITY DEFINER` function is the *owner*, so a
  superuser-exemption written against it is unconditionally true and the trigger
  refuses nothing. It looked like it was working.
- `object_identity` from `pg_event_trigger_ddl_commands()` is quoted where the
  name needs it, so `uuid-ossp` arrives as `"uuid-ossp"` and never matches the
  allowlist -- refusing precisely the extension the ADR exists for.
"""

from __future__ import annotations

import psycopg
import pytest

from services.control_plane import db, provisioning, tenant_bootstrap
from tests.conftest import node_host_and_port, requires_db
from tests.test_provisioning import ADMIN_DSN, _tenant_admin_dsn, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

# Trusted by PostgreSQL and on the allowlist: the one a migrated schema opens
# with, and the one whose quoting broke the first implementation.
ALLOWED = "uuid-ossp"
# Trusted by PostgreSQL and deliberately *not* on the allowlist. This is the
# case the event trigger exists for: without it, PostgreSQL alone would permit
# every extension its packager marked trusted.
TRUSTED_BUT_NOT_ALLOWED = "isn"
# Not trusted, so PostgreSQL refuses it before the trigger is consulted --
# defence in depth, and negative test H's original subject.
UNTRUSTED = "postgres_fdw"


def test_the_spec_is_what_gets_synced():
    """The list a tenant is checked against is `specs/extension-allowlist.yaml`
    read at sync time, not a copy in code -- ADR-045 makes adding one a review
    and a merge, which is only true if nothing else has an opinion."""
    names = tenant_bootstrap.allowlisted_extensions()
    assert ALLOWED in names
    assert TRUSTED_BUT_NOT_ALLOWED not in names
    assert UNTRUSTED not in names


@pytest.fixture
def tenant_admin(tenant, key_ring):
    """A provisioned tenant, plus a connection acting as its admin role.

    The connection logs in as the executor and `SET ROLE`s to the admin, which
    is exactly what the Phase 08 console does -- so `session_user` is the
    executor here, and the superuser exemption in the trigger must not match it.
    """
    made = {}

    def make(ref: str):
        project_id, names, _ = tenant(ref)
        with db.connection() as conn:
            password = provisioning.load_credential(
                conn, project_id=project_id, credential_type="db_executor", key_ring=key_ring
            )
        host, port = node_host_and_port()
        conn = psycopg.connect(
            f"postgresql://{names.executor}:{password}@{host}:{port}/{names.database}",
            autocommit=True,
        )
        conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(names.admin)))
        made[ref] = conn
        return names, conn

    yield make
    for conn in made.values():
        conn.close()


@requires_node
@requires_maludb_core
def test_the_line_every_migrated_schema_opens_with_works_verbatim(tenant_admin):
    """ADR-045's motivating case, run as written rather than paraphrased."""
    names, conn = tenant_admin("ext00001")

    conn.execute('create extension if not exists "uuid-ossp"')

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = %s", (ALLOWED,))
        assert cur.fetchone()[0] == 1

    # And again, because a migration re-run must not fail on what it already did.
    conn.execute('create extension if not exists "uuid-ossp"')


@requires_node
@requires_maludb_core
def test_Q_a_trusted_extension_off_the_allowlist_is_still_refused(tenant_admin):
    """Negative test Q, and negative test H generalised rather than replaced.

    `trusted` is set by whoever packaged the extension, so leaving PostgreSQL's
    own check as the only gate would let a node's package set decide what
    tenants may install. `isn` is trusted and not allowlisted; it must fail, and
    it must leave nothing behind -- aborting at `ddl_command_end` rolls the
    install back, which is the property bootstrap 005 already depends on.
    """
    names, conn = tenant_admin("ext00002")

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not on this platform"):
        conn.execute(f"CREATE EXTENSION {TRUSTED_BUT_NOT_ALLOWED}")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = %s",
                    (TRUSTED_BUT_NOT_ALLOWED,))
        assert cur.fetchone()[0] == 0, "the refused install was not rolled back"


@requires_node
@requires_maludb_core
def test_an_untrusted_extension_is_refused_by_postgresql_before_the_trigger(tenant_admin):
    """Defence in depth, and the reason the allowlist is not the only control:
    ADR-045's criterion 1 leans on `trusted` precisely because it is maintained
    by people who know the extension."""
    names, conn = tenant_admin("ext00003")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"CREATE EXTENSION {UNTRUSTED}")


@requires_node
@requires_maludb_core
def test_the_customer_cannot_edit_the_list_they_are_checked_against(tenant_admin):
    """Otherwise the allowlist is a suggestion. `maludb_platform` is revoked
    from PUBLIC and the table is revoked again on its own account, so a later
    grant on the schema cannot quietly hand it over."""
    names, conn = tenant_admin("ext00004")

    for statement in (
        "INSERT INTO maludb_platform.allowed_extensions (name) VALUES ('isn')",
        "DELETE FROM maludb_platform.allowed_extensions",
        "SELECT * FROM maludb_platform.allowed_extensions",
        "DROP EVENT TRIGGER maludb_allowlist_extensions",
        "ALTER EVENT TRIGGER maludb_allowlist_extensions DISABLE",
    ):
        with pytest.raises(psycopg.Error):
            conn.execute(statement)


@requires_node
@requires_maludb_core
def test_the_platform_still_installs_what_a_tenant_needs(tenant_admin, admin_conn):
    """Provisioning installs `maludb_core` and `vector`, neither of which is
    trusted, as a superuser. The trigger exempts a superuser `session_user`, or
    provisioning would have started refusing its own extensions."""
    names, _ = tenant_admin("ext00005")

    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as platform:
        with platform.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
            assert cur.fetchone()[0] == 1, "provisioning's own install did not survive"
        # And it can still install something no customer may.
        platform.execute("CREATE EXTENSION IF NOT EXISTS isn")


@requires_node
@requires_maludb_core
def test_a_sync_that_removes_an_entry_takes_the_permission_away(tenant_admin, tmp_path):
    """The half a sync that only ever added would miss.

    Taking an extension off the spec is how a security decision gets reversed.
    A tenant that never hears about it keeps the old permission, and the fleet
    then differs by the month each project was provisioned.
    """
    names, conn = tenant_admin("ext00006")
    conn.execute('create extension if not exists "uuid-ossp"')

    narrowed = tmp_path / "extension-allowlist.yaml"
    narrowed.write_text("version: 1\nallowed:\n  - name: citext\n    why: 'only this'\n")

    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as platform:
        added, removed = tenant_bootstrap.sync_extension_allowlist(platform, narrowed)
    assert removed >= 1

    # Already installed, so still there: this governs the next install, and
    # dropping a customer's extension from under their schema is a different
    # decision.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = %s", (ALLOWED,))
        assert cur.fetchone()[0] == 1

    # But a fresh install of it is now refused.
    conn.execute('DROP EXTENSION "uuid-ossp"')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not on this platform"):
        conn.execute('create extension "uuid-ossp"')


@requires_node
@requires_maludb_core
def test_bootstrap_refuses_a_tenant_whose_allowlist_trigger_is_missing(tenant_admin):
    """Checked as an outcome, like the ADR-018 hardening trigger beside it.

    Without this trigger the admin role still holds `CREATE ON DATABASE` -- the
    grant that makes a migrated schema's own line work -- and PostgreSQL alone
    would permit every extension the node marks trusted.
    """
    names, _ = tenant_admin("ext00007")

    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as platform:
        tenant_bootstrap.verify(platform)  # the happy path, first

        platform.execute("ALTER EVENT TRIGGER maludb_allowlist_extensions DISABLE")
        with pytest.raises(tenant_bootstrap.BootstrapError, match="allowlist"):
            tenant_bootstrap.verify(platform)

        platform.execute("DROP EVENT TRIGGER maludb_allowlist_extensions")
        with pytest.raises(tenant_bootstrap.BootstrapError, match="allowlist"):
            tenant_bootstrap.verify(platform)


@requires_node
@requires_maludb_core
def test_a_migrated_schema_can_create_its_own_schemas(tenant_admin):
    """The other half of `GRANT CREATE ON DATABASE`, and slice 6 needs it: a
    migrated Supabase project brings schemas of its own."""
    names, conn = tenant_admin("ext00008")

    conn.execute("CREATE SCHEMA app")
    conn.execute("CREATE TABLE app.things (id int)")


# -- what the slice 6a security review found --------------------------------


@requires_node
@requires_maludb_core
def test_the_allowlist_is_closed_under_what_its_entries_require(admin_conn):
    """Criterion 5, checked against the node rather than by reading the file.

    `CREATE EXTENSION x CASCADE` installs x's dependencies and reports only *x*
    to an event trigger, so an entry whose dependency is unlisted would admit
    that dependency by the back door -- with its install script running as the
    bootstrap superuser. The trigger walks `pg_depend` to catch it at install
    time; this is the half that stops the two from ever disagreeing, by keeping
    the file itself closed.

    Measured against `pg_available_extensions`, so it fails on the node where a
    dependency is actually missing rather than on a list someone typed here.
    """
    allowed = set(tenant_bootstrap.allowlisted_extensions())
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            SELECT ae.name, v.requires
              FROM pg_available_extensions ae
              JOIN pg_available_extension_versions v
                ON v.name = ae.name AND v.version = ae.default_version
             WHERE ae.name = ANY(%s) AND v.requires IS NOT NULL
            """,
            (sorted(allowed),),
        )
        rows = cur.fetchall()

    for name, requires in rows:
        missing = set(requires) - allowed
        assert not missing, (
            f"{name} requires {sorted(missing)}, which the allowlist does not carry -- "
            "`CREATE EXTENSION ... CASCADE` would install it past the trigger"
        )


@requires_node
@requires_maludb_core
def test_an_extension_in_a_customers_own_schema_is_still_revoked_from_anon(tenant_admin):
    """ADR-018, in the schema the customer just created.

    Bootstrap 005 revoked only inside `public`, which was sufficient while no
    tenant role could install anything and everything was installed there by the
    platform. Bootstrap 010 grants `CREATE ON DATABASE`, so a customer can put an
    allowlisted extension in a schema of their own and grant `anon` USAGE on it
    -- and the security review measured `anon` holding EXECUTE on every function
    of it. `specs/tenant-role-model.md` lists that as a thing the admin role must
    never be able to do; bootstrap 011 makes it true by construction rather than
    by where the extension happened to land.
    """
    names, conn = tenant_admin("ext00010")

    conn.execute("CREATE SCHEMA mine")
    conn.execute("CREATE EXTENSION citext SCHEMA mine")
    conn.execute("GRANT USAGE ON SCHEMA mine TO anon")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
             WHERE n.nspname = 'mine'
               AND (has_function_privilege('anon', p.oid, 'EXECUTE')
                 OR has_function_privilege('authenticated', p.oid, 'EXECUTE'))
            """
        )
        reachable = cur.fetchone()[0]
    assert reachable == 0, f"{reachable} functions in a customer schema are callable by anon"


@requires_node
@requires_maludb_core
def test_a_trigger_that_only_fires_for_replicas_is_not_accepted(tenant_admin):
    """`evtenabled` has four values and `ENABLE REPLICA` is the invisible one.

    It fires only when `session_replication_role = 'replica'` -- never, for
    anything a customer does. A check written as `<> 'D'` certifies a tenant
    whose hardening is inert, which the security review demonstrated by
    installing a non-allowlisted extension while `verify` reported health.
    """
    names, _ = tenant_admin("ext00011")

    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as platform:
        tenant_bootstrap.verify(platform)

        for trigger in ("maludb_allowlist_extensions", "maludb_harden_extensions"):
            platform.execute(f"ALTER EVENT TRIGGER {trigger} ENABLE REPLICA")
            with pytest.raises(tenant_bootstrap.BootstrapError, match="does not fire"):
                tenant_bootstrap.verify(platform)
            platform.execute(f"ALTER EVENT TRIGGER {trigger} ENABLE")

        # The PostgREST reload pair shares the same three-state trap.
        platform.execute("ALTER EVENT TRIGGER maludb_pgrst_reload_ddl ENABLE REPLICA")
        with pytest.raises(tenant_bootstrap.BootstrapError):
            tenant_bootstrap.verify(platform)


# -- the fleet path, which is what makes the list changeable at all ---------


@requires_node
@requires_maludb_core
def test_the_fleet_sync_reaches_a_provisioned_tenant(
    tenant_admin, key_ring, capsys, monkeypatch, tmp_path
):
    """`cp-manage extensions sync`, against a real project.

    ADR-045 says adding an extension is "a review and a merge rather than a
    release", which is only true if something carries the merged file out to
    tenants that already exist. Provisioning covers new ones; this covers the
    rest, and without it the allowlist would be frozen per project at whatever
    it was provisioned with.
    """
    import argparse

    from services.control_plane import manage, nodes
    from tests.conftest import TEST_KEK, TEST_PEPPER

    names, conn = tenant_admin("ext00009")

    # `cp-manage` builds its own configuration from the environment, as a real
    # operator invocation does, while the suite seals with `TEST_KEK` and never
    # calls `config.load()`. So this test has to supply the whole of what
    # `config.load()` requires -- **both** key-material refs, not just the KEK.
    #
    # Supplying them rather than inheriting them is the point. The first version
    # set only `MALUDB_KEK_REF` and passed locally on a developer shell that had
    # `MALUDB_TOKEN_PEPPER_REF` exported from the bring-up instructions, then
    # failed in CI, which exports neither. That is the green-local-red-CI trap
    # `AGENTS.md` warns about for `python -m pytest`, arriving by a different
    # door: a test that reads the environment tests the environment.
    for variable, material in (
        ("MALUDB_KEK_REF", TEST_KEK),
        ("MALUDB_TOKEN_PEPPER_REF", TEST_PEPPER),
    ):
        path = tmp_path / variable.lower()
        path.write_bytes(material)
        path.chmod(0o600)  # the loader refuses group/world-readable key material
        monkeypatch.setenv(variable, str(path))

    # The command reaches tenants through the node's stored admin DSN, which the
    # test fixture has no reason to set. Setting it here is what makes this a
    # test of the real path rather than of a helper.
    with db.connection() as control:
        nodes.set_admin_dsn(control, name="is-node", dsn=ADMIN_DSN, key_ring=key_ring)
        control.commit()

    # Narrow the tenant's list first, so the sync has something to put back and
    # a no-op cannot pass for success.
    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as platform:
        platform.execute("DELETE FROM maludb_platform.allowed_extensions")

    assert manage._cmd_extensions_sync(argparse.Namespace(spec=None)) == 0
    assert "ext00009" in capsys.readouterr().out

    with psycopg.connect(_tenant_admin_dsn(names.database)) as platform, platform.cursor() as cur:
        cur.execute("SELECT name FROM maludb_platform.allowed_extensions")
        synced = {row[0] for row in cur.fetchall()}
    # Compared as a set: PostgreSQL's default collation and Python's sort
    # disagree about where `_` falls, so an ordered comparison would fail on
    # `pg_trgm` vs `pgcrypto` while the contents were identical.
    assert synced == set(tenant_bootstrap.allowlisted_extensions())

    # And the customer can install again, which is the point of the round trip.
    conn.execute('create extension if not exists "uuid-ossp"')
