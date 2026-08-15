"""Per-project GoTrue workers, and the tenant reconciliation they need.

The assertions that matter here are the three collisions bootstrap 007 exists
to resolve. All three were reproduced against stock GoTrue 2.195.0 before the
fix was written, and the first is not what the Phase 00 note predicted: on
PostgreSQL 15+ the migration does not quietly pollute `public`, it fails
outright, because `public` no longer grants CREATE to PUBLIC.

They run against the real binary rather than a stub, because what is under test
is GoTrue's own behaviour.
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid

import psycopg
import pytest

from services.control_plane import auth_workers, db, tenant_bootstrap, workers
from tests.conftest import requires_db
from tests.test_provisioning import (
    ADMIN_DSN,
    _provision,
    _provision_core,
    _tenant_admin_dsn,
    requires_maludb_core,
)

pytestmark = [requires_db]

GOTRUE_BIN = os.environ.get("MALUDB_GOTRUE_BIN", "gotrue")
requires_gotrue = pytest.mark.skipif(
    shutil.which(GOTRUE_BIN) is None and not os.path.exists(GOTRUE_BIN),
    reason="GoTrue binary not available",
)
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

TEST_JWT_SECRET = "test-jwt-secret-not-for-production-" + "0" * 24  # noqa: S105


def _place_on_node(project_id: uuid.UUID) -> None:
    """Give a provisioned project a node, which port allocation locks against.

    project_factory builds a real tenant but leaves it unplaced; the worker
    tests need both, because a port is allocated per node.
    """
    with db.connection() as conn:
        node = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status) "
            "VALUES ('aw-node','aw.example','aw.internal','shared','active') "
            "ON CONFLICT (name) DO UPDATE SET status='active' RETURNING id",
        )["id"]
        db.execute(conn, "UPDATE projects SET node_id = %s WHERE id = %s", (node, project_id))
        conn.commit()


def _settings(**overrides) -> auth_workers.AuthSettings:
    base = {
        "project_ref": "aw000001",
        "database": "mldb_aw000001",
        "auth_role": "mldb_aw000001_auth",
        "auth_password": "s3cr3t-auth-password",
        "jwt_secret": TEST_JWT_SECRET,
        "port": 21001,
        "site_url": "https://aw000001.maludb.local",
        "external_url": "https://aw000001.maludb.local/auth/v1",
    }
    return auth_workers.AuthSettings(**{**base, **overrides})


# -- configuration ---------------------------------------------------------


def test_the_env_file_reuses_the_projects_signing_secret():
    """The carried-forward note from Phase 03: minting a second secret gives a
    project whose own Auth tokens its own Data API rejects."""
    env = auth_workers.render_env(_settings())
    assert f'GOTRUE_JWT_SECRET="{TEST_JWT_SECRET}"' in env


def test_the_worker_connects_as_the_tenant_auth_role_not_a_superuser():
    env = auth_workers.render_env(_settings())
    assert "postgres://mldb_aw000001_auth:" in env
    assert "postgres://postgres" not in env


def test_bookkeeping_is_confined_to_the_auth_schema():
    """Phase 00 finding 4 and ADR-018: schema_migrations in `public` would be
    served on the customer's Data API."""
    assert 'GOTRUE_DB_NAMESPACE="auth"' in auth_workers.render_env(_settings())


def test_the_worker_binds_loopback_only():
    """docs/API-GATEWAY.md: internal worker endpoints must not be reachable
    from the internet. Binding the socket beats a firewall rule."""
    env = auth_workers.render_env(_settings())
    assert 'GOTRUE_API_HOST="127.0.0.1"' in env


def test_the_external_url_is_the_gateway_not_the_loopback_bind():
    """It ends up in emailed confirmation links, so a loopback value would
    send every user a URL that resolves to their own machine."""
    env = auth_workers.render_env(_settings())
    assert 'API_EXTERNAL_URL="https://aw000001.maludb.local/auth/v1"' in env


def test_the_audience_matches_what_postgrest_expects():
    """A token GoTrue signs with a different aud is refused by the Data API,
    which surfaces as a compatibility bug rather than a config one."""
    env = auth_workers.render_env(_settings())
    assert 'GOTRUE_JWT_AUD="authenticated"' in env
    assert 'GOTRUE_JWT_DEFAULT_GROUP_NAME="authenticated"' in env


def test_confirmation_is_on_unless_explicitly_disabled():
    """ADR-019: MAILER_AUTOCONFIRM=true accepts addresses without proving
    control of them and is not a production default."""
    assert 'GOTRUE_MAILER_AUTOCONFIRM="false"' in auth_workers.render_env(_settings())
    assert 'GOTRUE_MAILER_AUTOCONFIRM="true"' in auth_workers.render_env(
        _settings(autoconfirm=True)
    )


def test_a_password_with_whitespace_survives_the_env_file():
    """systemd parses this file itself; an unquoted value would truncate at the
    space and the worker would authenticate with half a password."""
    awkward = 'pw with "quotes" and spaces'  # noqa: S105 - test fixture, not a real secret
    env = auth_workers.render_env(_settings(auth_password=awkward))
    line = next(x for x in env.splitlines() if x.startswith("GOTRUE_DB_DATABASE_URL="))
    assert line.endswith('"')
    assert '\\"quotes\\"' in line


def test_the_env_file_is_written_private(tmp_path):
    path = auth_workers.write_env(_settings(), config_dir=tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "env file holds a live password"


def test_the_unit_name_matches_the_shipped_template():
    """If these drift, the control plane starts units that do not exist."""
    unit = auth_workers.supervisor().unit_for("abcd1234")
    assert unit == "maludb-gotrue@abcd1234.service"
    template = open("deploy/maludb-gotrue@.service").read()
    assert "/etc/maludb/gotrue/%i.env" in template


@pytest.mark.parametrize("hostile", ['a"; rm -rf /', "../../etc/passwd", "AB12CD34", ""])
def test_a_hostile_project_ref_never_reaches_systemctl(hostile):
    with pytest.raises(workers.WorkerError, match="invalid project ref"):
        auth_workers.supervisor().unit_for(hostile)


# -- port allocation -------------------------------------------------------


@requires_node
def test_api_and_auth_ports_never_collide(admin_conn, key_ring, project_factory):
    """Both come from one range. An Auth worker handed the API worker's port
    would make the gateway route Data API traffic into GoTrue."""
    project_id = project_factory("aw000010")
    _provision_core(project_id, admin_conn, key_ring, "aw000010")
    _place_on_node(project_id)
    with db.connection() as conn:
        api = workers.allocate_port(conn, project_id=project_id, column="api_port")
        auth = workers.allocate_port(conn, project_id=project_id, column="auth_port")
        conn.commit()
    assert api != auth


def test_an_unknown_port_column_is_refused(db_pool):
    """The name is composed with sql.Identifier so it cannot inject; the real
    risk AGENTS.md names is acting on the wrong column."""
    with db.connection() as conn, pytest.raises(workers.WorkerError, match="unknown port column"):
        workers.allocate_port(conn, project_id=uuid.uuid4(), column="database_name")


# -- the three collisions bootstrap 007 resolves ---------------------------


@requires_node
@requires_maludb_core
def test_bootstrap_007_hands_the_auth_schema_to_the_projects_auth_role(
    admin_conn, key_ring, project_factory
):
    project_id = project_factory("aw000011")
    names, _ = _provision_core(project_id, admin_conn, key_ring, "aw000011")
    _place_on_node(project_id)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
        tenant_conn.commit()
        with db.connection() as conn:
            tenant_bootstrap.bootstrap_project(conn, tenant_conn, project_id=project_id)

        with tenant_conn.cursor() as cur:
            cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='auth'")
            assert cur.fetchone()[0] == names.auth, "GoTrue cannot create tables it does not own"

            for fn in ("uid", "jwt", "role", "email"):
                cur.execute(
                    "SELECT pg_get_userbyid(proowner) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname='auth' AND proname=%s",
                    (fn,),
                )
                owner = cur.fetchone()[0]
                assert owner == names.auth, f"auth.{fn}() still owned by {owner}; GoTrue's "
                "`create or replace` raises 'must be owner of function'"

            cur.execute(
                "SELECT setconfig FROM pg_db_role_setting s "
                "JOIN pg_roles r ON r.oid = s.setrole "
                "WHERE r.rolname = %s",
                (names.auth,),
            )
            row = cur.fetchone()
            assert row is not None, "no search_path set; GoTrue reaches for public"
            assert any(c.startswith("search_path=auth") for c in row[0])


@requires_node
@requires_maludb_core
@requires_gotrue
def test_gotrue_migrates_cleanly_and_leaves_public_empty(admin_conn, key_ring, project_factory):
    """The end-to-end property. Before bootstrap 007 this failed with
    `permission denied for schema public` -- on PostgreSQL 15+ GoTrue cannot
    start at all, rather than quietly polluting the Data API as Phase 00
    predicted on an older server."""
    project_id = project_factory("aw000012")
    _place_on_node(project_id)
    # The real entry point: it persists the credentials settings_for reads back,
    # and applies the bootstrap including 007.
    names, _ = _provision(project_id, admin_conn, key_ring, "aw000012")

    with db.connection() as conn:
        settings = auth_workers.settings_for(
            conn, project_id=project_id, key_ring=key_ring, gateway_domain="maludb.local"
        )
    auth_workers.migrate(settings, binary=GOTRUE_BIN)

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn, tenant_conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        assert cur.fetchall() == [], "GoTrue bookkeeping reached the customer's Data API"

        cur.execute("SELECT schemaname FROM pg_tables WHERE tablename = 'schema_migrations'")
        assert cur.fetchone()[0] == "auth"

        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'auth'")
        assert cur.fetchone()[0] > 5, "GoTrue did not create its tables"


@requires_node
@requires_maludb_core
@requires_gotrue
def test_the_auth_helpers_still_read_the_modern_claim_key_after_gotrue_migrates(
    admin_conn, key_ring, project_factory
):
    """GoTrue's first migration defines uid() reading only the legacy
    `request.jwt.claim.sub`, which returns NULL against PostgREST 14 and fails
    every policy closed. Three later migrations coalesce both. So a fully
    migrated tenant is correct and a half-migrated one is not -- which is why
    verify() probes behaviour rather than trusting that migrations ran."""
    project_id = project_factory("aw000013")
    _place_on_node(project_id)
    # The real entry point: it persists the credentials settings_for reads back,
    # and applies the bootstrap including 007.
    names, _ = _provision(project_id, admin_conn, key_ring, "aw000013")

    with db.connection() as conn:
        settings = auth_workers.settings_for(
            conn, project_id=project_id, key_ring=key_ring, gateway_domain="maludb.local"
        )
    auth_workers.migrate(settings, binary=GOTRUE_BIN)

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        # The exact gate provisioning uses, re-run after GoTrue has replaced
        # the helper definitions.
        tenant_bootstrap.verify(tenant_conn)

        with tenant_conn.cursor() as cur:
            cur.execute("SELECT has_function_privilege('anon', 'auth.uid()', 'EXECUTE')")
            assert cur.fetchone()[0] is True, "CREATE OR REPLACE dropped anon's grant"


# -- demand-driven start ---------------------------------------------------


@requires_node
def test_auth_is_off_until_something_asks_for_it(admin_conn, key_ring, project_factory):
    """ADR-022: the Auth worker is 17.6 MB of the 31.8 MB a warm project costs,
    and must not be started for projects that do not use Auth."""
    project_id = project_factory("aw000014")
    with db.connection() as conn:
        row = db.one(conn, "SELECT auth_enabled FROM projects WHERE id = %s", (project_id,))
        assert row["auth_enabled"] is False

        with pytest.raises(auth_workers.AuthWorkerError, match="on demand"):
            auth_workers.start_worker(
                conn,
                project_id=project_id,
                key_ring=key_ring,
                gateway_domain="maludb.local",
                supervisor=auth_workers.supervisor(),
            )

        auth_workers.enable_auth(conn, project_id=project_id)
        row = db.one(conn, "SELECT auth_enabled FROM projects WHERE id = %s", (project_id,))
        assert row["auth_enabled"] is True
