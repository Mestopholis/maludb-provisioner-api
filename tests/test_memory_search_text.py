"""Searching a memory space by text (ADR-079, memory slice 6a).

Two hops, each tested where it can fail:

- **the embedder** verifies the customer's own secret key against the named project
  -- not the gateway's word -- before it spends the project's provider key. It answers a
  space with no model, a missing key and a provider's refusal in words, never logs the
  query or the key, and runs narrowed as `cp_memory_embedder`: no memory writer
  credential, no ingest, no publishable key's ciphertext;
- **the gateway** refuses what it can before anything is paid for, sends the embedder
  the caller's key and nothing else of the platform's, and runs the search as the
  existing wrapper through PostgREST as `service_role`;
- **configuration** will not send keys to anything but a private or loopback address.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import psycopg
import psycopg.sql
import psycopg.types.json
import pytest
from fastapi.testclient import TestClient

from services.control_plane import (
    api_keys,
    config,
    db,
    memory_embedder,
    memory_worker_grants,
    model_providers,
    provider_keys,
)
from services.gateway.app import Gateway, create_app
from tests.conftest import TEST_PEPPER, requires_db
from tests.test_gateway import GATEWAY_DOMAIN, _issue, _Recorder, gateway_project, upstream  # noqa: F401
from tests.test_memory_ingest import FakeModels, _active_space, _models_for

KEY = "sk-test-" + "Q6a0" * 6 + "text"  # noqa: S105 - test fixture, not a real key
QUERY = "who owns the parser"


# -- configuration -------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://api.example.com:8114", "http://8.8.8.8:8114", "ftp://10.0.0.2:8114",
                                 "http://10.0.0.2:8114/other", "http://embedder.internal:8114",
                                 "http://0.0.0.0:8114"])
def test_a_key_is_never_sent_to_anything_but_a_private_address(url):
    with pytest.raises(config.ConfigError):
        config._embedder_url(url)  # noqa: SLF001


@pytest.mark.parametrize("url", ["", "http://10.0.0.2:8114", "http://127.0.0.1:8114/", "https://[::1]:8114"])
def test_private_and_loopback_embedders_are_accepted(url):
    config._embedder_url(url)  # noqa: SLF001


@pytest.mark.parametrize("value", ["0.0.0.0:8114", "[::]:8114", "8.8.8.8:8114", "8114", "embedder:8114"])
def test_the_embedder_never_listens_on_a_public_address(value):
    with pytest.raises(SystemExit):
        memory_embedder.bind_address(value)


# -- the embedder --------------------------------------------------------------------------------


class _Refusing(FakeModels):
    def embed(self, provider, model, key, texts):
        raise model_providers.ProviderError("auth", "voyage refused this project's API key: [key] is invalid")


def _project_with_space(placed_project, key_ring, ref: str, *, models: bool = True, provider_key: bool = True):
    project_id = placed_project(ref)
    _active_space(project_id)
    if models:
        _models_for(project_id, extraction="anthropic", embedding="voyage")
    if provider_key:
        with db.connection() as conn:
            provider_keys.set_key(conn, project_id=project_id, provider="voyage", api_key=KEY, key_ring=key_ring,
                                  actor_user_id=None)
            conn.commit()
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status = 'ACTIVE' WHERE id = %s", (project_id,))
        conn.commit()
    return project_id


def _embedder(key_ring, models_client):
    return TestClient(memory_embedder.create_app(key_ring=key_ring, pepper=TEST_PEPPER, models_client=models_client))


def _embed(client, ref, key, *, space="bot", text=QUERY):
    return client.post(memory_embedder.ROUTE, json={"project_ref": ref, "space": space, "text": text},
                       headers={"apikey": key} if key is not None else {})


@requires_db
def test_the_embedder_embeds_for_the_projects_secret_key_only(placed_project, key_ring, caplog):
    project_id = _project_with_space(placed_project, key_ring, "mste0001")
    other = _project_with_space(placed_project, key_ring, "mste0002")
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    publishable = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    other_secret = _issue(other, api_keys.SECRET, key_ring)
    models = FakeModels({})
    client = _embedder(key_ring, models)

    with caplog.at_level(logging.DEBUG):
        assert _embed(client, "mste0001", None).status_code == 401
        assert _embed(client, "mste0001", other_secret).status_code == 401, "another project's key was accepted"
        assert _embed(client, "nosuchpr", secret).status_code == 401
        assert _embed(client, "mste0001", publishable).status_code == 403
        assert _embed(client, "mste0001", secret, space="nope").status_code == 404
        assert _embed(client, "mste0001", secret, text="x" * 2001).status_code == 422
        done = _embed(client, "mste0001", secret)
    assert done.status_code == 200, done.text
    assert done.json() == {"embedding": [0.1, 0.2, 0.3], "provider": "voyage", "model": "voyage-3.5"}
    assert models.keys == {KEY}
    for secret_text in (QUERY, KEY, secret):
        assert secret_text not in caplog.text


@requires_db
def test_the_embedder_says_what_is_missing_and_passes_a_providers_refusal_on(placed_project, key_ring):
    no_models = _project_with_space(placed_project, key_ring, "mste0003", models=False, provider_key=False)
    no_key = _project_with_space(placed_project, key_ring, "mste0004", provider_key=False)
    refused = _project_with_space(placed_project, key_ring, "mste0005")

    response = _embed(_embedder(key_ring, FakeModels({})), "mste0003", _issue(no_models, api_keys.SECRET, key_ring))
    assert response.status_code == 409 and "embedding model" in response.json()["message"]
    response = _embed(_embedder(key_ring, FakeModels({})), "mste0004", _issue(no_key, api_keys.SECRET, key_ring))
    assert response.status_code == 409 and "no voyage API key" in response.json()["message"]
    response = _embed(_embedder(key_ring, _Refusing({})), "mste0005", _issue(refused, api_keys.SECRET, key_ring))
    assert response.status_code == 424 and "[key]" in response.json()["message"]


@contextlib.contextmanager
def _pool_as(monkeypatch, role: str):
    original = db.connection

    @contextlib.contextmanager
    def connection():
        with original() as conn:
            conn.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
            conn.commit()
            try:
                yield conn
            finally:
                conn.rollback()
                conn.execute("RESET ROLE")
                conn.commit()

    monkeypatch.setattr(db, "connection", connection)
    try:
        yield
    finally:
        monkeypatch.setattr(db, "connection", original)


@pytest.fixture
def embedder_role(db_pool):  # noqa: ARG001
    group = memory_worker_grants.EMBEDDER_GROUP_ROLE
    with db.connection() as conn:
        try:
            if db.one(conn, "SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (group,)) is None:
                conn.execute(psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(psycopg.sql.Identifier(group)))
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip("the control-plane role cannot CREATE ROLE here")
        for statement in memory_worker_grants.statements(group, reads=memory_worker_grants.EMBEDDER_READS,
                                                         writes=memory_worker_grants.EMBEDDER_WRITES):
            conn.execute(statement)
        conn.commit()
    try:
        yield group
    finally:
        with db.connection() as conn:
            for statement in memory_worker_grants.revocations(group):
                conn.execute(statement)
            conn.execute(psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(group)))
            conn.commit()


@requires_db
def test_the_embedder_runs_end_to_end_as_its_own_narrow_role(placed_project, key_ring, embedder_role, monkeypatch):
    project_id = _project_with_space(placed_project, key_ring, "mste0006")
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    client = _embedder(key_ring, FakeModels({}))
    with db.connection() as conn:
        assert memory_worker_grants.embedder_violations(conn, embedder_role) == []
    with _pool_as(monkeypatch, embedder_role):
        with db.connection() as conn:
            memory_embedder.assert_narrowed(conn, environment="production")
            for statement in ("SELECT ciphertext FROM project_credentials", "SELECT ciphertext FROM api_keys",
                              "SELECT * FROM memory_ingests", "SELECT admin_ciphertext FROM nodes"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
                conn.rollback()
        response = _embed(client, "mste0006", secret)
    assert response.status_code == 200, response.text


@requires_db
def test_the_embedder_holds_the_projects_rate_itself(placed_project, key_ring):
    """The route is reachable without the gateway, and every call spends the customer's quota."""
    project_id = _project_with_space(placed_project, key_ring, "mste0007")
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET config_json = %s WHERE id = (SELECT plan_id FROM projects WHERE id = %s)",
                   (psycopg.types.json.Jsonb({"limits": {"api_requests_per_window": 2, "api_window_seconds": 60}}),
                    project_id))
        conn.commit()
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    models = FakeModels({})
    client = _embedder(key_ring, models)
    assert [_embed(client, "mste0007", secret).status_code for _ in range(3)] == [200, 200, 429]


# -- the gateway ------------------------------------------------------------------------------------


class _FakeEmbedder(BaseHTTPRequestHandler):
    received: list[dict] = []
    answer: tuple[int, dict] = (200, {"embedding": [0.25, -0.5, 1.0], "provider": "voyage", "model": "voyage-3.5"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        type(self).received.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                    "body": json.loads(self.rfile.read(length) or b"null")})
        status, payload = type(self).answer
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def embedder_server():
    _FakeEmbedder.received = []
    _FakeEmbedder.answer = (200, {"embedding": [0.25, -0.5, 1.0], "provider": "voyage", "model": "voyage-3.5"})
    server = HTTPServer(("127.0.0.1", 0), _FakeEmbedder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def search_client(app_config, key_ring, embedder_server):
    cfg = dataclasses.replace(app_config, memory_embedder_url=f"http://127.0.0.1:{embedder_server.server_port}")
    gateway = Gateway(config=cfg, key_ring=key_ring, wake_sleeping=False, client=httpx.AsyncClient(timeout=10))
    with TestClient(create_app(gateway)) as test_client:
        yield test_client


def _search(client, ref, key, body):
    return client.post("/memory/v1/spaces/bot/search", json=body,
                       headers={"host": f"{ref}.{GATEWAY_DOMAIN}", "apikey": key})


@requires_db
def test_the_gateway_embeds_with_the_callers_key_then_searches_through_the_wrapper(
        search_client, gateway_project, key_ring):  # noqa: F811
    project_id = gateway_project("gwsrch01")
    _active_space(project_id)
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    _Recorder.received = []

    response = _search(search_client, "gwsrch01", secret, {"text": QUERY, "subject": "carol", "limit": 5})
    assert response.status_code == 200, response.text

    [asked] = _FakeEmbedder.received
    assert asked["path"] == memory_embedder.ROUTE
    assert asked["body"] == {"project_ref": "gwsrch01", "space": "bot", "text": QUERY}
    assert asked["headers"]["apikey"] == secret, "the embedder must verify the caller's own key"
    assert "authorization" not in asked["headers"], "no platform credential goes to the embedder"

    [rpc] = [r for r in _Recorder.received if r["path"] == "/rpc/memory_search"]
    assert rpc["method"] == "POST"
    assert rpc["headers"]["content-profile"] == "maludb" and rpc["headers"]["authorization"].startswith("Bearer ")
    assert secret not in json.dumps(rpc), "the platform key reached PostgREST"
    assert json.loads(rpc["body"]) == {"space": "bot", "query": "[0.25,-0.5,1.0]", "subject": "carol", "verb": None,
                                       "namespace": "default", "match_count": 5}


@requires_db
@pytest.mark.parametrize("body", [{"subject": "carol"}, {"text": "q"}, {"text": "q", "subject": "c", "limit": 0},
                                  {"text": "x" * 2001, "verb": "owns"}, ["not", "an", "object"]])
def test_what_the_gateway_refuses_costs_nothing(search_client, gateway_project, key_ring, body):  # noqa: F811
    project_id = gateway_project("gwsrch02")
    _active_space(project_id)
    response = _search(search_client, "gwsrch02", _issue(project_id, api_keys.SECRET, key_ring), body)
    assert response.status_code == 422, response.text
    assert _FakeEmbedder.received == [], "a refused search reached the embedder, which spends the customer's quota"


@requires_db
def test_a_publishable_key_never_reaches_the_embedder(search_client, gateway_project, key_ring):  # noqa: F811
    project_id = gateway_project("gwsrch03")
    _active_space(project_id)
    response = _search(search_client, "gwsrch03", _issue(project_id, api_keys.PUBLISHABLE, key_ring),
                       {"text": QUERY, "verb": "owns"})
    assert response.status_code == 403 and _FakeEmbedder.received == []


@requires_db
def test_the_embedders_refusal_is_passed_on_in_its_words(search_client, gateway_project, key_ring):  # noqa: F811
    project_id = gateway_project("gwsrch04")
    _active_space(project_id)
    _FakeEmbedder.answer = (409, {"message": "memory space 'bot' has no embedding model"})
    _Recorder.received = []
    response = _search(search_client, "gwsrch04", _issue(project_id, api_keys.SECRET, key_ring),
                       {"text": QUERY, "verb": "owns"})
    assert response.status_code == 409 and "no embedding model" in response.json()["message"]
    assert not [r for r in _Recorder.received if r["path"] == "/rpc/memory_search"]


@requires_db
def test_without_an_embedder_search_by_text_is_503_saying_how_to_search(app_config, key_ring, gateway_project):  # noqa: F811
    gateway = Gateway(config=app_config, key_ring=key_ring, wake_sleeping=False, client=httpx.AsyncClient(timeout=10))
    project_id = gateway_project("gwsrch05")
    _active_space(project_id)
    with TestClient(create_app(gateway)) as client:
        response = _search(client, "gwsrch05", _issue(project_id, api_keys.SECRET, key_ring),
                           {"text": QUERY, "verb": "owns"})
    assert response.status_code == 503 and "rpc/memory_search" in response.json()["message"]
