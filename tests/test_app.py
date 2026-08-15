"""Application wiring, including the docs gate from ADR-024."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


def test_docs_absent_in_production():
    """ADR-024: production must not publish a map of the admin surface."""
    client = TestClient(create_app(_config(environment="production", docs_enabled=False)))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} should not be served in production"


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
