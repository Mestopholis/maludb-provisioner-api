"""Shared fixtures.

Integration tests run against a real PostgreSQL database, because the
invariants being tested -- the last-owner rule, immediate revocation,
cross-organization isolation -- are enforced with SQL and transactions and
would be meaningless against a mock.

Set MALUDB_CONTROL_PLANE_DATABASE_URL to point at a scratch database. CI
provides one; locally the development database works. Tests truncate identity
tables between cases, so never point this at anything you care about.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = os.environ.get("MALUDB_CONTROL_PLANE_DATABASE_URL", "").strip()

requires_db = pytest.mark.skipif(not DATABASE_URL, reason="MALUDB_CONTROL_PLANE_DATABASE_URL is unset")

# Every table a test may write. Nodes and plans belong here as much as the
# identity tables: leaving them behind let one test's node satisfy another
# test's placement, which made assertions pass or fail on execution order.
# encryption_keys is deliberately excluded -- the key ring is loaded once per
# session and truncating it mid-run would orphan every ciphertext.
_MUTABLE_TABLES = (
    "org_invitations",
    "user_sessions",
    "personal_access_tokens",
    "user_mfa_factors",
    "org_members",
    "api_keys",
    "project_credentials",
    "project_email_settings",
    # Left out originally and it showed: two suites suppressing the same address
    # collided on the primary key, because a suppression from one test survived
    # into the next.
    "email_events",
    "email_suppressions",
    "provisioning_jobs",
    "audit_events",
    "projects",
    "organizations",
    "users",
    "nodes",
    "plans",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    if not DATABASE_URL:
        pytest.skip("no database configured")
    from services.control_plane import crypto, db, migrate

    migrate.run(DATABASE_URL)

    # encryption_keys is deliberately never truncated, so a database that has
    # been used with a real KEK keeps a DEK the suite's TEST_KEK cannot unwrap.
    # Left alone that surfaces once per test as a CryptoError four frames deep
    # in crypto.py, which reads like a crypto bug rather than a pointed-at-the-
    # wrong-database mistake. Fail once, early, and say which it is.
    db.close_pool()
    db.init_pool(DATABASE_URL)
    try:
        with db.connection() as conn:
            crypto.KeyRing(TEST_KEK).load(conn)
    except crypto.CryptoError:
        # pytest.exit rather than raise: this is a misconfigured run, not a
        # failure, and every database test would otherwise report the same
        # thing. Stop once, say it once.
        pytest.exit(
            f"'{DATABASE_URL.rsplit('/', 1)[-1]}' holds a data encryption key that the test "
            "KEK cannot unwrap, so it has been used with real key material. Point "
            "MALUDB_CONTROL_PLANE_DATABASE_URL at a scratch database -- the suite truncates "
            "tables, so never aim it at one you care about. See AGENTS.md.",
            returncode=1,
        )
    finally:
        db.close_pool()

    return DATABASE_URL


@pytest.fixture
def db_pool(migrated_database: str):
    from services.control_plane import db

    db.close_pool()
    db.init_pool(migrated_database)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_MUTABLE_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()
    yield
    db.close_pool()


# One KEK for the whole suite. Using a different one per module means the
# stored DEK cannot be unwrapped, which surfaces as a confusing CryptoError
# rather than the test failure you were looking for.
TEST_CREDENTIAL = "correct-horse-battery-staple"  # noqa: S105 - test fixture
TEST_KEK = b"test-kek-material-not-for-production" * 2
TEST_PEPPER = b"test-pepper-material-not-for-production" * 2


@pytest.fixture
def key_ring(db_pool):
    from services.control_plane import crypto, db

    ring = crypto.KeyRing(TEST_KEK)
    with db.connection() as conn:
        ring.load(conn)
    return ring


@pytest.fixture
def app_config(migrated_database: str):
    from services.control_plane.config import Config

    return Config(
        environment="test",
        database_url=migrated_database,
        gateway_domain="maludb.local",
        database_domain="db.maludb.local",
        docs_enabled=True,
        kek=TEST_KEK,
        token_pepper=TEST_PEPPER,
    )


@pytest.fixture
def client(app_config, db_pool):
    from fastapi.testclient import TestClient

    from services.control_plane.main import create_app

    app = create_app(app_config)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# Node fixtures, shared by the provisioning and bootstrap suites.
# --------------------------------------------------------------------------

NODE_ADMIN_DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
PLATFORM_OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")


# --------------------------------------------------------------------------
# Make the skips that matter impossible to miss.
#
# Without MALUDB_NODE_ADMIN_DSN the suite reports "124 passed, 36 skipped" in
# green, having verified none of Phase 02's security properties. CI does set
# it, so this is not a coverage hole -- but it is how someone confirms a change
# locally, sees green, and pushes a regression. Say plainly what did not run.
# --------------------------------------------------------------------------


def _maludb_core_available() -> bool:
    try:
        from tests.test_provisioning import MALUDB_CORE_AVAILABLE
    except Exception:  # noqa: BLE001 - reporting must never break the run
        return False
    return bool(MALUDB_CORE_AVAILABLE)


# Set where the extension is supposed to be present -- CI, once slice 0 gave it
# a cluster carrying maludb_core. A banner is the right response to a developer
# who has not installed it; it is the wrong response to CI, where an absent
# extension means the environment regressed and the ADR-018 assertions quietly
# stopped running. Somewhere has to insist, or "verified in CI" decays back into
# "skipped in CI" without anyone noticing.
REQUIRE_MALUDB_CORE = os.environ.get("MALUDB_REQUIRE_MALUDB_CORE", "").strip() not in ("", "0", "false")

# The same insistence, for the Phase 06 node assertions. `wal_level = logical`
# and the ADR-031 pg_hba reject cannot be had from the ordinary test node, so
# tests/test_realtime_node.py skips without its own cluster -- and the test the
# spec calls the one most likely to be dropped for being awkward is exactly the
# one that skipping loses. CI builds the cluster and sets this.
REQUIRE_REALTIME_NODE = os.environ.get("MALUDB_REQUIRE_REALTIME_NODE", "").strip() not in ("", "0", "false")
REALTIME_NODE_DSN = os.environ.get("MALUDB_REALTIME_NODE_DSN", "").strip()

# And again for the Realtime *server*, which is a different thing from the node
# it reads: a container runtime and the pinned image. What skips without it is
# whether Postgres Changes are delivered at all, and whether the container can
# reach the node's loopback -- the containment the whole arrangement rests on.
REQUIRE_REALTIME_SERVER = os.environ.get("MALUDB_REQUIRE_REALTIME_SERVER", "").strip() not in ("", "0", "false")
REALTIME_DATA_HOST = os.environ.get("MALUDB_REALTIME_DB_HOST", "").strip()


def pytest_configure(config) -> None:
    if REQUIRE_REALTIME_NODE and not REALTIME_NODE_DSN:
        raise pytest.UsageError(
            "MALUDB_REQUIRE_REALTIME_NODE is set but MALUDB_REALTIME_NODE_DSN is not. "
            "This environment claims to verify that a replicator cannot take a base backup "
            "of every tenant on the node (ADR-031) and cannot. "
            "Build one with scripts/realtime-test-cluster.sh."
        )
    if REQUIRE_REALTIME_SERVER:
        import shutil
        import subprocess

        image = os.environ.get(
            "MALUDB_REALTIME_IMAGE", "docker.io/supabase/realtime:v2.110.0"
        )
        if not REALTIME_DATA_HOST:
            raise pytest.UsageError(
                "MALUDB_REQUIRE_REALTIME_SERVER is set but MALUDB_REALTIME_DB_HOST is not. "
                "A Realtime container has no route to the node's loopback by design, so it "
                "cannot reach PostgreSQL without a data address."
            )
        present = shutil.which("podman") is not None and subprocess.run(  # noqa: S603
            ["podman", "image", "exists", image], check=False  # noqa: S607
        ).returncode == 0
        if not present:
            raise pytest.UsageError(
                f"MALUDB_REQUIRE_REALTIME_SERVER is set but {image} is not available to podman. "
                "This environment claims to verify that Postgres Changes reach a client and "
                "that the container cannot reach the node's loopback, and can do neither."
            )

    if not REQUIRE_MALUDB_CORE:
        return
    missing = []
    if not DATABASE_URL:
        missing.append("MALUDB_CONTROL_PLANE_DATABASE_URL")
    if not NODE_ADMIN_DSN:
        missing.append("MALUDB_NODE_ADMIN_DSN")
    if missing:
        raise pytest.UsageError(
            f"MALUDB_REQUIRE_MALUDB_CORE is set but {' and '.join(missing)} is not. "
            "This environment claims to verify the ADR-018 assertions and cannot."
        )
    if not _maludb_core_available():
        raise pytest.UsageError(
            "MALUDB_REQUIRE_MALUDB_CORE is set but the node has no installable maludb_core, "
            "so the ADR-015 and ADR-018 assertions would skip. Refusing to report a pass that "
            "does not cover them."
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    ungated: list[tuple[str, str]] = []

    if not DATABASE_URL:
        ungated.append(
            (
                "MALUDB_CONTROL_PLANE_DATABASE_URL is unset",
                "nothing that touches the database ran: identity, org isolation, "
                "credential encryption, provisioning, bootstrap",
            )
        )
    elif not NODE_ADMIN_DSN:
        ungated.append(
            (
                "MALUDB_NODE_ADMIN_DSN is unset",
                "cross-tenant isolation, CONNECT lockdown, per-tenant role privilege "
                "limits and the ADR-018 extension-function revoke were NOT verified",
            )
        )
    elif not _maludb_core_available():
        ungated.append(
            (
                "maludb_core is not installable on this cluster",
                "the end-to-end ADR-018 checks did not run, including whether anon can "
                "reach gen_salt -- the finding that ADR-018 exists for",
            )
        )

    # Independent of the chain above: a node prepared for Realtime is a separate
    # cluster, so having one says nothing about the others and lacking one says
    # nothing about them either.
    if not REALTIME_NODE_DSN:
        ungated.append(
            (
                "MALUDB_REALTIME_NODE_DSN is unset",
                "the Phase 06 assertions did NOT run: that pg_hba.conf rejects a base backup "
                "by a role holding REPLICATION (ADR-031), that a stalled consumer loses its "
                "slot rather than the node losing its disk (ADR-032), that no "
                "customer-reachable tenant role holds REPLICATION, and that a project's "
                "replicator cannot reach another tenant's database",
            )
        )

    if not REALTIME_DATA_HOST:
        ungated.append(
            (
                "MALUDB_REALTIME_DB_HOST is unset",
                "no real Realtime server ran: that Postgres Changes are delivered at all, and "
                "that the container cannot reach the node's loopback -- where a tenant's "
                "PostgREST answers anonymous reads to anything that can open its port -- were "
                "NOT verified",
            )
        )

    if not ungated:
        return

    terminalreporter.write_sep("=", "security properties not verified", red=True, bold=True)
    for gate, consequence in ungated:
        terminalreporter.write_line(f"  {gate}")
        terminalreporter.write_line(f"    -> {consequence}")
    terminalreporter.write_line("")
    terminalreporter.write_line("  A pass here does not mean tenant isolation holds. See AGENTS.md.")



@pytest.fixture
def placed_project(db_pool):
    """A project placed on a node, without provisioning a real tenant.

    Enough state for anything that allocates a port or reads a worker's row,
    and nothing more: no database, no roles, no node connection.
    """
    import uuid

    from services.control_plane import db, identity

    def make(ref: str) -> uuid.UUID:
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            node = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, node_pool, status) "
                "VALUES (%s,%s,%s,'shared','active') ON CONFLICT (name) DO UPDATE "
                "SET status='active' RETURNING id",
                ("wk-node", "wk.example", "wk.internal"),
            )["id"]
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES (%s,'Test') "
                "ON CONFLICT (code) DO UPDATE SET name='Test' RETURNING id",
                (f"plan-{ref}",),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                "node_id, database_name) VALUES (%s,%s,%s,%s,%s,'PROVISIONED',%s,%s)",
                (project_id, org, ref, ref, plan, node, f"mldb_{ref}"),
            )
            conn.commit()
        return project_id

    return make

@pytest.fixture
def admin_conn():
    import psycopg

    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    conn = psycopg.connect(NODE_ADMIN_DSN, row_factory=psycopg.rows.dict_row)
    yield conn
    conn.close()


@pytest.fixture
def project_factory(db_pool):
    """Create control-plane projects, dropping any tenant objects afterwards."""
    import uuid

    import psycopg

    from services.control_plane import db, identity, provisioning

    created: list[str] = []

    def drop(ref: str) -> None:
        names = provisioning.TenantNames.for_ref(ref)
        with psycopg.connect(NODE_ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
            # The executor joined this list in Phase 08 slice 2. It was created
            # by slice 1 and never dropped, so every run left another
            # `mldb_*_executor` on the cluster.
            # The client role joined this list in Phase 09 slice 2, for the
            # reason the executor did in Phase 08: a role created by a new step
            # and never dropped leaves one behind on the cluster per run.
            for role in (names.authenticator, names.auth, names.admin,
                         names.executor, names.client):
                conn.execute(f'DROP ROLE IF EXISTS "{role}"')

    def make(ref: str):
        created.append(ref)
        drop(ref)
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES (%s,'Test') "
                "ON CONFLICT (code) DO UPDATE SET name='Test' RETURNING id",
                (f"plan-{ref}",),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
                "VALUES (%s,%s,%s,%s,%s,'PLACEMENT_RESERVED')",
                (project_id, org, ref, ref, plan),
            )
            conn.commit()
        return project_id

    yield make
    for ref in created:
        drop(ref)


def node_host_and_port() -> tuple[str, int]:
    """Where a tenant connection goes, taken from the node admin DSN.

    The tests run every tenant on the same cluster the admin DSN names, so this
    is the one place that has to agree with `MALUDB_NODE_ADMIN_DSN`.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(NODE_ADMIN_DSN or "")
    return parts.hostname or "127.0.0.1", parts.port or 5432


@pytest.fixture
def tenant(admin_conn, key_ring, project_factory):
    """A provisioned project, its executor credential, and a node row for it.

    Lives here rather than in one test module because Phase 08 slices 2 and 3
    both need a project a *route* can reach: the node row carries the host and
    port a tenant connection is built from, and `jobs.provision` is what stores
    the credentials a request unwraps.

    Deliberately not `test_sql_console.console_project`, which re-creates the
    executor role with a fresh password and therefore leaves the stored
    credential stale. Reading the stored one back is what a request does.
    """
    import psycopg  # noqa: F401 - imported by the provisioning helper below

    from services.control_plane import db, provisioning, sql_console
    from tests.test_provisioning import _provision

    host, port = node_host_and_port()

    def make(ref: str):
        project_id = project_factory(ref)
        with db.connection() as conn:
            node = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, db_port, node_pool, status) "
                "VALUES ('is-node','is.example',%s,%s,'shared','active') "
                "ON CONFLICT (name) DO UPDATE SET internal_host = EXCLUDED.internal_host, "
                "  db_port = EXCLUDED.db_port, status='active' RETURNING id",
                (host, port),
            )["id"]
            db.execute(conn, "UPDATE projects SET node_id = %s WHERE id = %s", (node, project_id))
            conn.commit()
        names, _ = _provision(project_id, admin_conn, key_ring, ref)
        with db.connection() as conn:
            password = provisioning.load_credential(
                conn, project_id=project_id, credential_type="db_executor", key_ring=key_ring
            )
        dsn = sql_console.executor_dsn(
            host=host, port=port, database=names.database,
            role=names.executor, password=password,
        )
        return project_id, names, dsn

    return make
