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
    "project_credentials",
    "project_email_settings",
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
    from services.control_plane import migrate

    migrate.run(DATABASE_URL)
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
            for role in (names.authenticator, names.auth, names.admin):
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
