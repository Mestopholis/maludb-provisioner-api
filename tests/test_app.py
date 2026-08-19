"""Application wiring, including the docs gate from ADR-024."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.control_plane import main as main_module
from services.control_plane.config import Config
from services.control_plane.main import create_app


def _config(**overrides) -> Config:
    base = {
        "environment": "development",
        "database_url": "postgresql://u:p@127.0.0.1/db",
        "gateway_domain": "maludb.local",
        "database_domain": "db.maludb.local",
        "docs_enabled": True,
        "kek": b"k" * 32,
        "token_pepper": b"p" * 32,
    }
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def dev_client():
    app = create_app(_config())
    # Bypass lifespan so the tests need no database.
    with TestClient(app) as _:
        pass
    return TestClient(app)


def test_healthz_is_available():
    app = create_app(_config())
    client = TestClient(app)
    app.router.on_startup = []
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_exposed_in_development():
    client = TestClient(create_app(_config(docs_enabled=True)))
    assert client.get("/openapi.json").status_code == 200


def test_docs_absent_when_disabled():
    """ADR-024: a gated deployment must not publish a map of the admin surface.

    Exercised through docs_enabled rather than environment="production", because
    the production guard now refuses to build the app at all while
    authentication is unenforced. That production implies docs_enabled=False is
    covered by tests/test_config.py::test_docs_disabled_by_default_in_production.
    """
    client = TestClient(create_app(_config(docs_enabled=False)))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} should not be served when docs are disabled"


def test_request_id_is_echoed():
    client = TestClient(create_app(_config()))
    response = client.get("/healthz", headers={"x-request-id": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_request_id_is_generated_when_absent():
    client = TestClient(create_app(_config()))
    assert client.get("/healthz").headers.get("x-request-id")


def test_unauthenticated_project_request_is_refused_without_touching_the_database():
    """Authentication is checked before the handler, so no pool is needed.

    Was a 404-on-malformed-ref test in slice 1. Since slice 2 the route requires
    a principal, so the deny-by-default 401 comes first. Malformed-ref handling
    is covered by tests/test_models.py and by the identity API tests.
    """
    client = TestClient(create_app(_config()))
    assert client.get("/v1/projects/DROP-TABLE").status_code == 401


def test_production_starts_now_that_authentication_is_enforced():
    """The slice-1 guard stands down once routes actually require a principal."""
    assert main_module.AUTHENTICATION_ENFORCED is True
    assert create_app(_config(environment="production", docs_enabled=False)) is not None


def test_guard_still_refuses_production_if_enforcement_is_switched_off(monkeypatch):
    """The guard remains live, not vestigial.

    If someone flips AUTHENTICATION_ENFORCED back without removing the route
    dependencies, production startup must fail loudly rather than silently
    serving unauthenticated traffic.
    """
    monkeypatch.setattr(main_module, "AUTHENTICATION_ENFORCED", False)
    with pytest.raises(main_module.InsecureConfiguration, match="no authentication"):
        create_app(_config(environment="production", docs_enabled=False))


def test_non_production_environments_still_start():
    for environment in ("development", "test", "staging"):
        assert create_app(_config(environment=environment)) is not None


def test_spec_declares_the_security_scheme_now_that_it_is_enforced():
    """Contract matches behaviour: FastAPI emits the scheme from real dependencies.

    Slice 1 asserted the opposite -- that no scheme was declared while none was
    enforced. Both assertions encode the same rule: the published contract must
    describe what the application actually does.
    """
    client = TestClient(create_app(_config(docs_enabled=True)))
    spec = client.get("/openapi.json").json()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert schemes, "authentication is enforced, so the spec must declare it"
    assert any(s.get("scheme") == "bearer" for s in schemes.values())
