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

_IDENTITY_TABLES = (
    "org_invitations",
    "user_sessions",
    "personal_access_tokens",
    "user_mfa_factors",
    "org_members",
    "projects",
    "organizations",
    "users",
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
            cur.execute(f"TRUNCATE {', '.join(_IDENTITY_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()
    yield
    db.close_pool()


@pytest.fixture
def app_config(migrated_database: str):
    from services.control_plane.config import Config

    return Config(
        environment="test",
        database_url=migrated_database,
        gateway_domain="maludb.local",
        docs_enabled=True,
        kek=b"test-kek-material-not-for-production" * 2,
        token_pepper=b"test-pepper-material-not-for-production" * 2,
    )


@pytest.fixture
def client(app_config, db_pool):
    from fastapi.testclient import TestClient

    from services.control_plane.main import create_app

    app = create_app(app_config)
    with TestClient(app) as test_client:
        yield test_client
