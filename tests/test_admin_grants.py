"""The operator console's database role (ADR-082 slice 2).

ADR-082 decision 4 says a bug in the console cannot select a ciphertext. These tests ask
the database, not the code:

- **the console runs end to end as `cp_admin_console`** -- sign in, who am I, a refused
  sign-in recorded, sign out -- and starts in production as that role, which is what
  keeps the allowlist honest;
- as that role it **cannot read** any sealed column or customer verifier, **cannot
  create or remake** a staff credential, and **cannot write** an audit event that is not
  a staff event;
- **it refuses to start in production** as a wider role, and a role that is also a
  gateway is refused by the grant command and preflight.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.control_plane import admin_grants, db, staff
from services.control_plane import config as config_module
from services.control_plane import manage as manage_mod
from services.control_plane import preflight as preflight_mod
from services.control_plane.admin_main import assert_narrowed, create_admin_app
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_gateway_grants import gateway_role  # noqa: F401 - fixture

pytestmark = requires_db

GROUP = admin_grants.GROUP_ROLE
LOGIN = "cp_admin_console_test"
STAFF_KEY = b"test-staff-key-material-not-the-kek" * 2
PASSWORD = "a-long-staff-password-for-tests"  # noqa: S105 - test fixture
HEADERS = {"X-MaluDB-Staff": "1"}


def _dsn_as(database_url: str, user: str, password: str) -> str:
    parts = urlsplit(database_url)
    host = parts.hostname or "127.0.0.1"
    netloc = f"{user}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture
def console_dsn(db_pool, migrated_database):  # noqa: ARG001 - the pool must exist before db.connection()
    with db.connection() as conn:
        try:
            conn.execute(f'DROP ROLE IF EXISTS "{LOGIN}"')
            if db.one(conn, "SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (GROUP,)) is None:
                conn.execute(f'CREATE ROLE "{GROUP}" NOLOGIN')
            conn.execute(f"CREATE ROLE \"{LOGIN}\" LOGIN PASSWORD '{TEST_CREDENTIAL}' IN ROLE \"{GROUP}\"")
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip("the control-plane role cannot CREATE ROLE, so the console's model cannot be applied here")
        for statement in admin_grants.statements(GROUP):
            conn.execute(statement)
        conn.commit()
    try:
        yield _dsn_as(migrated_database, LOGIN, TEST_CREDENTIAL)
    finally:
        with db.connection() as conn:
            for statement in admin_grants.revocations(GROUP):
                conn.execute(statement)
            conn.execute(f'DROP ROLE IF EXISTS "{LOGIN}"')
            conn.execute(f'DROP ROLE IF EXISTS "{GROUP}"')
            conn.commit()


@pytest.fixture
def seed(db_pool):
    key = staff.StaffKey(STAFF_KEY)
    with db.connection() as conn:
        account = staff.create(conn, email="ops@example.com", password=PASSWORD, display_name="Ops", actor="t")
        enrolment = staff.enrol(conn, staff=account, staff_key=key, actor="t")
        seed = base64.b32decode(enrolment.secret + "=" * (-len(enrolment.secret) % 8))
        earlier = datetime.now(UTC) - timedelta(seconds=90)
        assert staff.confirm_enrolment(conn, staff=account, code=staff.totp(seed, staff.step_at(earlier)),
                                       staff_key=key, actor="t", now=earlier)
    return seed


def _code(seed: bytes) -> str:
    return staff.totp(seed, staff.step_at(datetime.now(UTC)))


# -- end to end, as the role ---------------------------------------------------------


def test_the_console_signs_staff_in_and_out_as_its_own_role_in_production(console_dsn, seed, migrated_database):
    cfg = config_module.AdminConfig(environment="production", database_url=console_dsn, staff_key=STAFF_KEY)
    app = create_admin_app(cfg)
    # The suite's pool is the control plane's role; the console must open its own.
    db.close_pool()
    try:
        _drive_console(app, seed)
    finally:
        db.close_pool()
        db.init_pool(migrated_database)
    with db.connection() as conn:
        events = [r["event_type"] for r in db.query(
            conn, "SELECT event_type FROM audit_events WHERE event_type LIKE 'staff.signin%%' ORDER BY id")]
        failures = db.one(conn, "SELECT failed_signins FROM staff_users")["failed_signins"]
        actor_roles = db.one(conn, "SELECT count(*) AS n FROM staff_sessions")["n"]
    assert events == ["staff.signin_failed", "staff.signin"], "both were written by the console's role"
    assert failures == 0, "the failure was counted, then cleared by the successful sign-in"
    assert actor_roles == 1


def _drive_console(app, seed):
    with TestClient(app, base_url="https://testserver") as client:  # startup runs assert_narrowed
        refused = client.post("/admin/v1/session", headers=HEADERS,
                              json={"email": "ops@example.com", "password": "wrong-wrong-wrong-wrong",
                                    "code": _code(seed)})
        assert refused.status_code == 401
        signed_in = client.post("/admin/v1/session", headers=HEADERS,
                                json={"email": "ops@example.com", "password": PASSWORD, "code": _code(seed)})
        assert signed_in.status_code == 200, signed_in.text
        assert client.get("/admin/v1/session").status_code == 200
        assert client.delete("/admin/v1/session", headers=HEADERS).status_code == 204
        assert client.get("/admin/v1/session").status_code == 401
        with db.connection() as conn:
            assert db.one(conn, "SELECT current_user AS u")["u"] == LOGIN, "the requests ran as the console role"


# -- what the role cannot do ----------------------------------------------------------


@contextlib.contextmanager
def _as_console(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT password_hash FROM users",
        "SELECT admin_ciphertext FROM nodes",
        "SELECT storage_secret_ciphertext FROM nodes",
        "SELECT ciphertext FROM project_credentials",
        "SELECT verification_data FROM api_keys",
        "SELECT token_hash FROM user_sessions",
        "SELECT wrapped_dek FROM encryption_keys",
        "SELECT * FROM audit_events",
        "INSERT INTO staff_users (id, email, password_hash, created_by) "
        "VALUES (gen_random_uuid(), 'x@example.com', 'x', 'x')",
        "UPDATE staff_users SET password_hash = 'x'",
        "UPDATE staff_users SET status = 'active'",
        "UPDATE staff_mfa_factors SET seed_ciphertext = 'x', confirmed_at = now()",
        "DELETE FROM staff_sessions",
        "DELETE FROM audit_events",
    ],
)
def test_the_role_cannot_read_secrets_or_remake_staff_credentials(console_dsn, statement):
    with _as_console(console_dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


@pytest.mark.parametrize(
    "values",
    [
        "('user', 'x', 'customer.signin', '{}')",
        "('system', 'x', 'anything', '{}')",
    ],
)
def test_the_role_writes_only_staff_audit_events(console_dsn, values):
    with _as_console(console_dsn) as conn:
        conn.execute("INSERT INTO audit_events (actor_type, actor_id, event_type, detail_json) "
                     "VALUES ('staff', 'x', 'staff.test', '{}')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"INSERT INTO audit_events (actor_type, actor_id, event_type, detail_json) VALUES {values}")  # noqa: S608, E501 - literals


def test_the_granted_model_has_no_violations_and_preflight_agrees(console_dsn):  # noqa: ARG001
    with db.connection() as conn:
        assert admin_grants.violations(conn, GROUP) == []
        check = next(c for c in preflight_mod.run(conn, _cfg()).checks if c.name == "operator console role")
    assert check.ok, check.detail


def test_preflight_fails_a_role_that_can_read_a_customer_password_hash(console_dsn):  # noqa: ARG001
    with db.connection() as conn:
        conn.execute(f'GRANT SELECT (password_hash) ON users TO "{GROUP}"')
        conn.commit()
        check = next(c for c in preflight_mod.run(conn, _cfg()).checks if c.name == "operator console role")
    assert not check.ok and "users.password_hash" in check.detail


def test_the_console_refuses_production_as_a_wider_role(db_pool):
    with db.connection() as conn:
        with pytest.raises(RuntimeError, match="not a member of cp_admin_console"):
            assert_narrowed(conn, environment="production")
        assert_narrowed(conn, environment="development")  # warns, does not raise


def test_a_gateway_role_in_the_console_group_is_refused(console_dsn, gateway_role, capsys, monkeypatch):  # noqa: F811, ARG001
    with db.connection() as conn:
        conn.execute("INSERT INTO nodes (name, hostname, internal_host, node_pool, status, gateway_role) "
                     "VALUES ('n-admin', 'n.example.com', '10.0.0.9', 'shared', 'active', %s)", (gateway_role,))
        conn.execute(f'GRANT "{GROUP}" TO "{gateway_role}"')
        conn.commit()
        assert admin_grants.overlaps(conn) == [gateway_role]
    monkeypatch.setattr(manage_mod.db, "init_pool", lambda url: None)
    monkeypatch.setattr(manage_mod.db, "close_pool", lambda: None)
    assert manage_mod.main(["admin-console", "grant"]) == 2
    assert gateway_role in capsys.readouterr().out
    with db.connection() as conn:
        conn.execute(f'REVOKE "{GROUP}" FROM "{gateway_role}"')
        conn.commit()
    assert manage_mod.main(["admin-console", "grant"]) == 0
    assert "granted the operator console model" in capsys.readouterr().out


def _cfg() -> config_module.Config:
    base = config_module.Config(environment="production", database_url="postgresql://x/y", gateway_domain="e.com",
                                database_domain="db.e.com", docs_enabled=False, kek=b"k" * 32, token_pepper=b"p" * 32)
    return dataclasses.replace(base)


def test_a_superuser_is_not_reported_as_a_console_role_and_a_memory_worker(console_dsn, admin_conn):  # noqa: ARG001
    """`pg_has_role` is true for a superuser and every role. With `cp_memory_worker` present,
    `postgres` read as both, and preflight failed "operator console role" on the rehearsal."""
    admin_conn.autocommit = True
    created = admin_conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'cp_memory_worker'").fetchone() is None
    if created:
        admin_conn.execute("CREATE ROLE cp_memory_worker NOLOGIN")
    try:
        supers = {r["rolname"] for r in admin_conn.execute("SELECT rolname FROM pg_roles WHERE rolsuper")}
        assert supers, "the test cluster has no superuser to be misreported"
        with db.connection() as conn:
            assert not supers & set(admin_grants.overlaps(conn))
    finally:
        if created:
            admin_conn.execute("DROP ROLE cp_memory_worker")
