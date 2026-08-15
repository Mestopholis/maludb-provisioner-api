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


def test_project_endpoint_rejects_malformed_ref_without_touching_the_database():
    """A malformed ref is refused before any query runs, so no pool is needed."""
    client = TestClient(create_app(_config()))
    assert client.get("/v1/projects/DROP-TABLE").status_code == 404


def test_refuses_to_start_in_production_without_authentication():
    """Fail closed: an unauthenticated control plane must not reach production.

    Regression for the security review finding that nothing prevented deploying
    routes carrying no authentication. Delete this test only when
    AUTHENTICATION_ENFORCED is true and real dependencies guard the routes.
    """
    with pytest.raises(main_module.InsecureConfiguration, match="no authentication"):
        create_app(_config(environment="production", docs_enabled=False))


def test_non_production_environments_still_start():
    for environment in ("development", "test", "staging"):
        assert create_app(_config(environment=environment)) is not None


def test_spec_declares_no_authentication_while_none_is_enforced():
    """The published contract must not assert a control the app does not implement."""
    client = TestClient(create_app(_config(docs_enabled=True)))
    spec = client.get("/openapi.json").json()
    assert "security" not in spec
    assert "securitySchemes" not in spec.get("components", {})
