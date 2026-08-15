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
