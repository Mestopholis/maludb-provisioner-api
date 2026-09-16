"""The operator console's application (ADR-082 slice 1).

What this listener must be, asserted rather than reviewed:

- **it serves exactly its routes**, and the public and internal applications serve none
  of them -- the ADR-037 test's discipline, applied to a third surface;
- **it holds no KEK and no platform pepper**: its configuration has no field for either,
  its loader reads neither, and its import graph reaches no node credential, no
  provisioning work and no key ring;
- **staff sign in with password and code together and get an HttpOnly, SameSite=Strict,
  Secure cookie scoped to /admin**, never a token in the response body;
- **state changes need the X-MaluDB-Staff header**, which a cross-site form cannot send;
- **every refusal is the same 401**, sign-in is rate-limited per client, and a customer's
  credential is never a staff session.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from services.control_plane import config as config_module
from services.control_plane import db, identity, staff
from services.control_plane.admin_main import ADMIN_ROUTERS, create_admin_app
from services.control_plane.api import admin_session
from services.control_plane.main import INTERNAL_ROUTERS, PUBLIC_ROUTERS, create_app, create_public_app
from tests.conftest import TEST_PEPPER, requires_db
from tests.test_control_plane_surfaces import FORBIDDEN_MODULES, _import_closure, _module_file, _paths

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAFF_KEY = b"test-staff-key-material-not-the-kek" * 2
PASSWORD = "a-long-staff-password-for-tests"  # noqa: S105 - test fixture
WRONG_PASSWORD = "wrong-wrong-wrong-wrong"  # noqa: S105 - test fixture
HEADERS = {"X-MaluDB-Staff": "1"}
ADMIN_PATHS = {
    "/healthz", "/readyz", "/admin/v1/session",
    # Slice 3a.
    "/admin/v1/overview", "/admin/v1/sales", "/admin/v1/billing-events", "/admin/v1/customers",
    "/admin/v1/customers/{org_id}",
    # Slice 3b.
    "/admin/v1/usage", "/admin/v1/abuse",
    # Slice 3c.
    "/admin/v1/nodes", "/admin/v1/provisioning",
    # Slice 4: the pages.
    "/admin", "/admin/", "/admin/admin.js", "/admin/theme.js", "/admin/assets/admin.css", "/admin/assets/styles.css",
}


def _admin_config(database_url: str, **overrides) -> config_module.AdminConfig:
    base = config_module.AdminConfig(environment="test", database_url=database_url, staff_key=STAFF_KEY)
    return dataclasses.replace(base, **overrides)


@pytest.fixture
def admin_client(migrated_database, db_pool):
    app = create_admin_app(_admin_config(migrated_database))
    # https, so the Secure cookie is sent back as a browser would send it.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def seed(db_pool):
    """A staff account whose factor was confirmed a minute ago, and its seed."""
    import base64

    key = staff.StaffKey(STAFF_KEY)
    with db.connection() as conn:
        account = staff.create(conn, email="ops@example.com", password=PASSWORD, display_name="Ops", actor="t")
        enrolment = staff.enrol(conn, staff=account, staff_key=key, actor="t")
        seed = base64.b32decode(enrolment.secret + "=" * (-len(enrolment.secret) % 8))
        earlier = datetime.now(UTC) - timedelta(seconds=90)
        assert staff.confirm_enrolment(conn, staff=account, code=staff.totp(seed, staff.step_at(earlier)),
                                       staff_key=key, actor="t", now=earlier)
    return seed


def _now_code(seed: bytes) -> str:
    return staff.totp(seed, staff.step_at(datetime.now(UTC)))


def _sign_in(client, seed, **overrides):
    body = {"email": "ops@example.com", "password": PASSWORD, "code": _now_code(seed)} | overrides
    return client.post("/admin/v1/session", json=body, headers=HEADERS)


# -- signing in and out ------------------------------------------------------------


@requires_db
def test_sign_in_sets_a_strict_http_only_secure_cookie_and_returns_no_token(admin_client, seed):
    response = _sign_in(admin_client, seed)
    assert response.status_code == 200, response.text
    assert "mldb_staff_" not in response.text
    assert response.json()["email"] == "ops@example.com"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{admin_session.COOKIE}=mldb_staff_")
    for flag in ("HttpOnly", "Secure", "Path=/admin", "SameSite=strict"):
        assert flag.lower() in cookie.lower(), (flag, cookie)
    me = admin_client.get("/admin/v1/session")
    assert me.status_code == 200 and me.json()["display_name"] == "Ops"


@requires_db
def test_every_refusal_is_the_same_401(admin_client, seed):
    wrong_code = f"{(int(_now_code(seed)) + 1) % 1000000:06d}"
    refusals = [
        _sign_in(admin_client, seed, password=WRONG_PASSWORD),
        _sign_in(admin_client, seed, code=wrong_code),
        _sign_in(admin_client, seed, email="nobody@example.com"),
    ]
    assert {r.status_code for r in refusals} == {401}
    assert len({r.text for r in refusals}) == 1
    assert all("set-cookie" not in r.headers for r in refusals)
    with db.connection() as conn:
        reasons = [r["detail_json"].get("reason") for r in db.query(
            conn, "SELECT detail_json FROM audit_events WHERE event_type = 'staff.signin_failed' ORDER BY id")]
    assert reasons == ["credentials", "credentials", "unknown"], "the reason is in the audit trail, not the response"


@requires_db
def test_state_changes_need_the_staff_header(admin_client, seed):
    body = {"email": "ops@example.com", "password": PASSWORD, "code": _now_code(seed)}
    assert admin_client.post("/admin/v1/session", json=body).status_code == 403
    assert admin_client.post("/admin/v1/session", json=body, headers={"X-MaluDB-Staff": "yes"}).status_code == 403
    assert _sign_in(admin_client, seed).status_code == 200
    assert admin_client.delete("/admin/v1/session").status_code == 403
    assert admin_client.get("/admin/v1/session").status_code == 200, "still signed in"


@requires_db
def test_sign_out_ends_the_session_and_clears_the_cookie(admin_client, seed):
    assert _sign_in(admin_client, seed).status_code == 200
    token = admin_client.cookies.get(admin_session.COOKIE)
    out = admin_client.delete("/admin/v1/session", headers=HEADERS)
    assert out.status_code == 204
    assert 'max-age=0' in out.headers["set-cookie"].lower() or "expires=" in out.headers["set-cookie"].lower()
    admin_client.cookies.set(admin_session.COOKIE, token, domain="testserver", path="/admin")
    assert admin_client.get("/admin/v1/session").status_code == 401, "the old token no longer resolves"


@requires_db
def test_sign_in_is_rate_limited_per_client(migrated_database, db_pool, seed):
    app = create_admin_app(_admin_config(migrated_database, signin_attempts=3, signin_window_seconds=300))
    with TestClient(app, base_url="https://testserver") as client:
        statuses = [_sign_in(client, seed, password=WRONG_PASSWORD).status_code for _ in range(4)]
    assert statuses == [401, 401, 401, 429]


@requires_db
def test_a_customer_credential_is_never_a_staff_session(admin_client, seed):
    with db.connection() as conn:
        user, _ = identity.create_user_with_personal_org(conn, email="ops@example.com", password=PASSWORD)
        customer = identity.create_session(conn, user_id=user.id, pepper=TEST_PEPPER)
        conn.commit()
    assert admin_client.get("/admin/v1/session", headers={"Authorization": f"Bearer {customer}"}).status_code == 401
    admin_client.cookies.set(admin_session.COOKIE, customer, domain="testserver", path="/admin")
    assert admin_client.get("/admin/v1/session").status_code == 401


@requires_db
def test_staff_sessions_are_peppered_by_the_staff_key_not_the_platform_pepper(admin_client, seed):
    assert _sign_in(admin_client, seed).status_code == 200
    token = admin_client.cookies.get(admin_session.COOKIE)
    with db.connection() as conn:
        assert staff.resolve(conn, presented=token, pepper=TEST_PEPPER) is None
        assert staff.resolve(conn, presented=token, pepper=staff.StaffKey(STAFF_KEY).session_pepper)


@requires_db
def test_responses_are_not_cached_or_framed(admin_client):
    response = admin_client.get("/healthz")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


# -- the surface ------------------------------------------------------------------


def test_the_console_serves_exactly_its_routes_and_the_other_applications_none(migrated_database):
    admin = create_admin_app(_admin_config(migrated_database, docs_enabled=False))
    assert _paths(admin) == ADMIN_PATHS
    cfg = config_module.Config(environment="test", database_url=migrated_database, gateway_domain="x",
                               database_domain="db.x", docs_enabled=False, kek=b"k" * 32, token_pepper=b"p" * 32)
    for other in (create_app(cfg), create_public_app(cfg)):
        leaked = {p for p in _paths(other) if p.startswith("/admin")}
        assert leaked == set(), leaked
    assert admin_session.router not in PUBLIC_ROUTERS and admin_session.router not in INTERNAL_ROUTERS
    assert admin_session.router in ADMIN_ROUTERS


def test_the_console_configuration_has_no_kek_and_no_platform_pepper():
    fields = {f.name for f in dataclasses.fields(config_module.AdminConfig)}
    assert not fields & {"kek", "token_pepper", "malumail_api_key", "stripe_secret_key"}, fields
    import inspect

    loader = inspect.getsource(config_module.load_admin)
    for forbidden in ("MALUDB_KEK_REF", "MALUDB_TOKEN_PEPPER_REF", "MALUDB_CONTROL_PLANE_DATABASE_URL", "load()"):
        assert forbidden not in loader, forbidden


def test_the_console_cannot_reach_a_node_credential_provisioning_or_the_key_ring():
    modules, calls = _import_closure(["services.control_plane.admin_main"])
    assert "services.control_plane.api.admin_session" in modules, "the walk found nothing; it would pass anything"
    assert not modules & FORBIDDEN_MODULES, modules & FORBIDDEN_MODULES
    # Reports have their own queries so the console never imports what billing and
    # subscriptions do: the payment provider's client, plan changes and provisioning.
    beyond_reports = {"services.control_plane.billing", "services.control_plane.subscriptions",
                      "services.control_plane.stripe_api", "services.control_plane.plan_change",
                      "services.control_plane.provisioning", "services.control_plane.nodes"}
    assert not modules & beyond_reports, modules & beyond_reports
    assert calls == {}, calls
    for module in modules:
        path = _module_file(module)
        if path is None or module == "services.control_plane.crypto":
            continue
        source = path.read_text()
        assert not re.search(r"\bKeyRing\(", source), f"{module} builds a key ring"
        assert "token_pepper" not in source or module == "services.control_plane.config", module


def test_load_admin_reads_its_own_environment(monkeypatch, tmp_path):
    key = tmp_path / "staff-key"
    key.write_bytes(STAFF_KEY)
    key.chmod(0o600)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("MALUDB_ENV", "production")
    monkeypatch.setenv("MALUDB_STAFF_KEY_REF", str(key))
    monkeypatch.delenv("MALUDB_ADMIN_DATABASE_URL", raising=False)
    with pytest.raises(config_module.ConfigError):
        config_module.load_admin()
    monkeypatch.setenv("MALUDB_ADMIN_DATABASE_URL", "postgresql://cp_admin:secret@10.0.0.5/cp")
    cfg = config_module.load_admin()
    assert cfg.cookie_secure and not cfg.docs_enabled and cfg.staff_key == STAFF_KEY
    assert "secret" not in repr(cfg) and STAFF_KEY.decode() not in repr(cfg)
