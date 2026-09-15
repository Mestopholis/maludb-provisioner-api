"""Memory ingest: the gateway queues it, the worker writes it (ADR-079, memory slice 5a).

Tested where each part can fail:

- **the request** -- what an item may carry, validated before anything is stored;
- **admission** -- the plan's ingests per hour and stored memories, held before the
  request is queued, and a request's status visible to its own project only;
- **the gateway** -- the secret key only, a project with memory on, 202 with a
  status URL;
- **the worker** -- on a real tenant, each item written as the project's memory
  writer, one bad item reported and rolled back while the rest are written, the
  items cleared from the control plane, and what was written found by search.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import psycopg.types.json
import pytest

from services.control_plane import (
    api_keys,
    db,
    entitlements,
    maludb_jobs,
    memory_ingest,
    memory_worker,
    model_providers,
    provider_keys,
    provisioner,
)
from tests.conftest import NODE_ADMIN_DSN, requires_db
from tests.test_gateway import GATEWAY_DOMAIN, _issue, client, gateway_project, upstream  # noqa: F401
from tests.test_maludb_enable import _tenant_conn, admin_node_conn, requires_node, tenants  # noqa: F401
from tests.test_maludb_jobs import worker_node  # noqa: F401
from tests.test_memory_spaces import _request, _writer_dsn

pytestmark = requires_db


def _item(subject="carol", verb="owns", text="carol owns the parser", embedding=(0.1, 0.2, 0.3)):
    return {"subject": subject, "verb": verb, "text": text, "embedding": list(embedding)}


def _active_space(project_id, name="bot") -> None:
    with db.connection() as conn:
        db.execute(conn, "INSERT INTO memory_spaces (project_id, name, schema_name, state, active_at, "
                         "memory_schema_version) VALUES (%s, %s, %s, 'active', now(), '0.105.0')",
                   (project_id, name, f"mem_{name}"))
        db.execute(conn, "UPDATE projects SET maludb_memory_enabled = TRUE, maludb_memory_enabled_at = now() "
                         "WHERE id = %s", (project_id,))
        conn.commit()


def _enqueue(project_id, items, *, limits=None, space="bot", now=None):
    allowed = entitlements.resolve("free", {"limits": limits or {}})
    with db.connection() as conn:
        try:
            return memory_ingest.enqueue(conn, project_id=project_id, space=space, items=items,
                                         allowed=allowed, now=now)
        finally:
            conn.commit()


# -- the request -----------------------------------------------------------------


@pytest.mark.parametrize(("payload", "fragment"), [
    (None, "body must be"),
    ({"items": []}, "1 to 100"),
    ({"items": [_item()] * 101}, "1 to 100"),
    ({"items": [dict(_item(), subject="")]}, "items[0].subject"),
    ({"items": [dict(_item(), text="x" * 8001)]}, "items[0].text"),
    ({"items": [dict(_item(), embedding=[])]}, "items[0].embedding"),
    ({"items": [dict(_item(), embedding=[1.0, float("nan")])]}, "finite"),
    ({"items": [dict(_item(), embedding=[True, 1.0])]}, "finite"),
    ({"items": [_item(), "not an object"]}, "items[1]"),
])
def test_what_an_item_may_carry(payload, fragment):
    with pytest.raises(memory_ingest.IngestRefused) as refused:
        memory_ingest.validate(payload)
    assert refused.value.status == 422 and fragment in str(refused.value)


# -- admission ---------------------------------------------------------------------


def test_an_unknown_or_inactive_space_is_404(placed_project):
    project_id = placed_project("mig00001")
    with pytest.raises(memory_ingest.IngestRefused) as refused:
        _enqueue(project_id, [_item()])
    assert refused.value.status == 404


def test_the_hourly_limit_refuses_with_when_the_next_is_allowed(placed_project):
    project_id = placed_project("mig00002")
    _active_space(project_id)
    now = datetime.now(UTC)
    _enqueue(project_id, [_item()], limits={"memory_ingests_per_hour": 2}, now=now - timedelta(minutes=50))
    _enqueue(project_id, [_item()], limits={"memory_ingests_per_hour": 2}, now=now)
    with pytest.raises(memory_ingest.IngestRefused) as refused:
        _enqueue(project_id, [_item()], limits={"memory_ingests_per_hour": 2}, now=now)
    assert refused.value.status == 429
    assert 0 < refused.value.retry_after <= 11 * 60


def test_stored_and_queued_items_count_against_the_ceiling_which_is_not_named(placed_project):
    project_id = placed_project("mig00003")
    _active_space(project_id)
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_spaces SET item_count = 7 WHERE project_id = %s", (project_id,))
        conn.commit()
    _enqueue(project_id, [_item(), _item()], limits={"memory_max_items": 10})
    with pytest.raises(memory_ingest.IngestRefused) as refused:
        _enqueue(project_id, [_item(), _item()], limits={"memory_max_items": 10})
    assert refused.value.status == 409 and "10" not in str(refused.value)


def test_a_request_is_visible_to_its_own_project_only(placed_project):
    first, second = placed_project("mig00004"), placed_project("mig00005")
    _active_space(first)
    queued = _enqueue(first, [_item()])
    with db.connection() as conn:
        mine = memory_ingest.status(conn, project_id=first, ingest_id=str(queued.ingest_id))
        theirs = memory_ingest.status(conn, project_id=second, ingest_id=str(queued.ingest_id))
        nonsense = memory_ingest.status(conn, project_id=first, ingest_id="not-a-uuid")
    assert mine["state"] == "pending" and mine["items"] == 1
    assert theirs is None and nonsense is None


def test_the_worker_does_not_claim_an_ingest_naming_another_projects_space(placed_project):
    """The gateway role writes these rows, so the space-to-project pairing is checked
    where the worker reads it, not assumed from whoever wrote it."""
    mine, theirs = placed_project("mig00006"), placed_project("mig00007")
    _active_space(theirs)
    with db.connection() as conn:
        space_id = db.one(conn, "SELECT id FROM memory_spaces WHERE project_id = %s", (theirs,))["id"]
        db.execute(conn, "INSERT INTO memory_ingests (id, project_id, space_id, item_count, items_json) "
                         "VALUES (gen_random_uuid(), %s, %s, 1, '[]'::jsonb)", (mine, space_id))
        conn.commit()
        assert memory_worker.claim(conn) is None
        conn.rollback()


def test_the_worker_serves_the_least_recently_served_project_first(placed_project):
    """Security review, slice 5b: a text ingest costs seconds per item, so strict
    oldest-first would let one busy project starve the others."""
    busy, quiet = placed_project("mig00009"), placed_project("mig00010")
    _active_space(busy)
    _active_space(quiet)
    now = datetime.now(UTC)
    with db.connection() as conn:
        db.execute(conn, "INSERT INTO memory_ingests (id, project_id, space_id, state, item_count, results_json, "
                         "  written, failed, requested_at, started_at, completed_at) "
                         "SELECT gen_random_uuid(), %s, id, 'succeeded', 1, '[]', 1, 0, %s, %s, %s "
                         "  FROM memory_spaces WHERE project_id = %s",
                   (busy, now - timedelta(minutes=5), now - timedelta(minutes=5), now - timedelta(minutes=4), busy))
        conn.commit()
    _enqueue(busy, [_item()], now=now - timedelta(minutes=2))
    _enqueue(quiet, [_item()], now=now - timedelta(minutes=1))
    with db.connection() as conn:
        first = memory_worker.claim(conn)
        conn.rollback()
    assert first["project_id"] == quiet


# -- the gateway ---------------------------------------------------------------------


def _post(test_client, ref, key, path, body):
    return test_client.post(path, json=body, headers={"host": f"{ref}.{GATEWAY_DOMAIN}", "apikey": key})


def test_the_gateway_queues_an_ingest_for_the_secret_key_only(client, gateway_project, key_ring):  # noqa: F811
    test_client, _ = client
    project_id = gateway_project("gwmem001")
    _active_space(project_id)
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    publishable = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    path = "/memory/v1/spaces/bot/ingest"

    assert _post(test_client, "gwmem001", publishable, path, {"items": [_item()]}).status_code == 403
    queued = _post(test_client, "gwmem001", secret, path, {"items": [_item(), _item(subject="dave")]})
    assert queued.status_code == 202, queued.text
    body = queued.json()
    assert body["items"] == 2 and body["state"] == "pending"

    shown = test_client.get(body["status_url"], headers={"host": f"gwmem001.{GATEWAY_DOMAIN}", "apikey": secret})
    assert shown.status_code == 200 and shown.json()["space"] == "bot"
    assert _post(test_client, "gwmem001", secret, "/memory/v1/spaces/nope/ingest",
                 {"items": [_item()]}).status_code == 404
    assert _post(test_client, "gwmem001", secret, path, {"items": [{"subject": "x"}]}).status_code == 422


def test_a_project_without_memory_is_told_how_to_turn_it_on(client, gateway_project, key_ring):  # noqa: F811
    test_client, _ = client
    project_id = gateway_project("gwmem002")
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    response = _post(test_client, "gwmem002", secret, "/memory/v1/spaces/bot/ingest", {"items": [_item()]})
    assert response.status_code == 404 and "memory/spaces" in response.json()["message"]


def test_no_key_answers_401_whatever_the_path(client, gateway_project):  # noqa: F811
    test_client, _ = client
    gateway_project("gwmem003")
    response = test_client.post("/memory/v1/spaces/bot/ingest", json={"items": [_item()]},
                                headers={"host": f"gwmem003.{GATEWAY_DOMAIN}"})
    assert response.status_code == 401


# -- the worker, on a real tenant -------------------------------------------------------


@requires_node
def test_the_worker_writes_as_the_writer_reports_each_item_and_search_finds_it(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names, _ = tenants("mingw001")
    worker_node()
    _request(project_id, "bot")
    provisioner.run_maludb_once(key_ring=key_ring)

    items = [
        _item(subject="carol", embedding=(0.1, 0.2, 0.3)),
        # Same subject and verb at another dimension: the compartment already holds 3.
        _item(subject="carol", embedding=(0.1, 0.2, 0.3, 0.4)),
        _item(subject="dave", embedding=(0.3, 0.2, 0.1)),
    ]
    queued = _enqueue(project_id, memory_ingest.validate({"items": items})[1])

    sessions: list[str] = []

    def connect(ingest, password):
        conn = psycopg.connect(_writer_dsn(NODE_ADMIN_DSN, names.database, names.memwriter, password), autocommit=True)
        sessions.append(conn.execute("SELECT session_user").fetchone()[0])
        return conn

    assert memory_worker.run_once(key_ring=key_ring, writer_connect=connect)
    assert sessions == [names.memwriter], "the worker must write as the project's memory writer"

    with db.connection() as conn:
        row = db.one(conn, "SELECT state, written, failed, results_json, items_json FROM memory_ingests WHERE id = %s",
                     (queued.ingest_id,))
        stored = db.one(conn, "SELECT item_count FROM memory_spaces WHERE project_id = %s", (project_id,))["item_count"]
    assert row["state"] == "partial" and (row["written"], row["failed"]) == (2, 1)
    assert [r["written"] for r in row["results_json"]] == [True, False, True]
    assert row["results_json"][1]["reason"], "a failed item says why"
    assert row["items_json"] is None, "the customer's items do not outlive the request"
    assert stored == 2

    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("SET ROLE service_role")
        found = sorted({r[0] for r in t.execute(
            "SELECT subject_name FROM maludb.memory_search('bot', '[0.1,0.2,0.3]'::vector, NULL, 'owns')").fetchall()})
    assert found == ["carol", "dave"]
    assert memory_worker.run_once(key_ring=key_ring, writer_connect=connect) is False


@requires_node
def test_a_project_that_stopped_serving_fails_the_ingest_without_touching_it(tenants, worker_node, key_ring):  # noqa: F811
    project_id, _, _ = tenants("mingw002")
    worker_node()
    _request(project_id, "bot")
    provisioner.run_maludb_once(key_ring=key_ring)
    queued = _enqueue(project_id, [_item()])
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status = 'PAUSED' WHERE id = %s", (project_id,))
        conn.commit()

    def never(*_):
        raise AssertionError("a paused project's database must not be connected to")

    memory_worker.run_once(key_ring=key_ring, writer_connect=never)
    with db.connection() as conn:
        row = db.one(conn, "SELECT state, detail, items_json FROM memory_ingests WHERE id = %s", (queued.ingest_id,))
    assert row["state"] == "failed" and "not available" in row["detail"] and row["items_json"] is None


# -- text (slice 5b) -------------------------------------------------------------------

PROVIDER_KEY = "sk-test-" + "M5b0" * 6 + "text"  # noqa: S105 - test fixture, not a real key


def _models_for(project_id, name="bot", extraction="anthropic", embedding="voyage"):
    with db.connection() as conn:
        row = maludb_jobs.set_memory_models(conn, project_id=project_id, name=name, extraction_provider=extraction,
                                            extraction_model=None, embedding_provider=embedding,
                                            embedding_model=None, actor_user_id=None)
        conn.commit()
    return row


@pytest.mark.parametrize(("payload", "fragment"), [
    ({"items": [{"text": "a"}, _item()]}, "not both"),
    ({"items": [{"text": "a"}] * 21}, "1 to 20"),
    ({"items": [{"title": "no text"}]}, "items[0].text"),
    ({"items": [{"text": "a", "title": "t" * 201}]}, "items[0].title"),
])
def test_what_a_text_ingest_may_carry(payload, fragment):
    with pytest.raises(memory_ingest.IngestRefused) as refused:
        memory_ingest.validate(payload)
    assert refused.value.status == 422 and fragment in str(refused.value)


def test_a_text_ingest_needs_the_space_to_name_its_models(placed_project):
    project_id = placed_project("mig00008")
    _active_space(project_id)
    kind, items = memory_ingest.validate({"items": [{"text": "carol owns the parser"}]})
    assert kind == "text"
    allowed = entitlements.resolve("free", {})
    with db.connection() as conn:
        with pytest.raises(memory_ingest.IngestRefused) as refused:
            memory_ingest.enqueue(conn, project_id=project_id, space="bot", items=items, allowed=allowed, kind=kind)
        conn.rollback()
    assert refused.value.status == 409 and "/models" in str(refused.value)

    _models_for(project_id)
    with db.connection() as conn:
        queued = memory_ingest.enqueue(conn, project_id=project_id, space="bot", items=items, allowed=allowed,
                                       kind=kind)
        conn.commit()
        shown = memory_ingest.status(conn, project_id=project_id, ingest_id=str(queued.ingest_id))
    assert queued.kind == "text" and shown["kind"] == "text"


class FakeModels:
    """Stands in for the providers. `answers` maps a text to its edges, or to an
    exception to raise; embeddings are whatever `vectors` says for an edge's subject."""

    def __init__(self, answers, vectors=None):
        self.answers = answers
        self.vectors = vectors or {}
        self.extracted: list[str] = []
        self.keys: set[str] = set()

    def extract(self, provider, model, key, text):
        self.extracted.append(text)
        self.keys.add(key)
        answer = self.answers[text]
        if isinstance(answer, Exception):
            raise answer
        return model_providers.Extraction(edges=[
            {"subject_text": s, "verb_text": v, "source_span": f"{s} {v}", "confidence": 0.9} for s, v in answer])

    def embed(self, provider, model, key, texts):
        self.keys.add(key)
        return [self.vectors.get(text.split(" ", 1)[0], [0.1, 0.2, 0.3]) for text in texts]


def _text_tenant(tenants, worker_node, key_ring, ref):  # noqa: F811
    project_id, names, _ = tenants(ref)
    worker_node()
    _request(project_id, "bot")
    provisioner.run_maludb_once(key_ring=key_ring)
    _models_for(project_id)
    for provider in ("anthropic", "voyage"):
        with db.connection() as conn:
            provider_keys.set_key(conn, project_id=project_id, provider=provider, api_key=PROVIDER_KEY,
                                  key_ring=key_ring, actor_user_id=None)
            conn.commit()

    def connect(ingest, password):
        return psycopg.connect(_writer_dsn(NODE_ADMIN_DSN, names.database, names.memwriter, password), autocommit=True)

    return project_id, names, connect


def _row(ingest_id):
    with db.connection() as conn:
        return db.one(conn, "SELECT state, written, failed, results_json, items_json, detail, heartbeat_at "
                            "  FROM memory_ingests WHERE id = %s", (ingest_id,))


@requires_node
def test_the_worker_extracts_embeds_and_writes_text_reporting_each_edge(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names, connect = _text_tenant(tenants, worker_node, key_ring, "mingw003")
    texts = ["carol owns the parser", "nothing to see", "refused text", "dave owns the lexer"]
    models = FakeModels(
        {texts[0]: [("carol", "owns"), ("carol", "owns")], texts[1]: [],
         texts[2]: model_providers.ProviderError("refused", "anthropic declined to extract from this text"),
         texts[3]: [("dave", "owns")]},
    )
    # The second carol edge arrives at another dimension, which the compartment refuses.
    calls = {"n": 0}
    original = models.embed

    def embed(provider, model, key, batch):
        calls["n"] += 1
        vectors = original(provider, model, key, batch)
        return [vectors[0], [0.1, 0.2, 0.3, 0.4]] if len(batch) == 2 else vectors

    models.embed = embed
    queued = _enqueue_text(project_id, texts)
    assert memory_worker.run_once(key_ring=key_ring, writer_connect=connect, models=models)

    row = _row(queued.ingest_id)
    results = row["results_json"]
    assert row["state"] == "partial" and (row["written"], row["failed"]) == (2, 2)
    assert [r["written"] for r in results] == [True, False, False, True]
    assert results[0]["memories"] == 1 and results[0]["skipped"][0]["edge"] == 1
    assert "no memories" in results[1]["reason"] and "declined" in results[2]["reason"]
    assert row["items_json"] is None and row["heartbeat_at"] is not None
    assert models.keys == {PROVIDER_KEY}
    assert PROVIDER_KEY not in str(results), "a provider key reached the customer's results"
    with db.connection() as conn:
        assert db.one(conn, "SELECT item_count FROM memory_spaces WHERE project_id = %s",
                      (project_id,))["item_count"] == 2

    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("SET ROLE service_role")
        found = sorted({r[0] for r in t.execute(
            "SELECT subject_name FROM maludb.memory_search('bot', '[0.1,0.2,0.3]'::vector, NULL, 'owns')").fetchall()})
    assert found == ["carol", "dave"]


@requires_node
def test_a_refused_key_stops_the_ingest_instead_of_spending_quota_on_the_rest(tenants, worker_node, key_ring):  # noqa: F811
    project_id, _, connect = _text_tenant(tenants, worker_node, key_ring, "mingw004")
    auth = model_providers.ProviderError("auth", "anthropic refused this project's API key: invalid x-api-key")
    models = FakeModels({"one": auth, "two": [("x", "owns")], "three": [("y", "owns")]})
    queued = _enqueue_text(project_id, ["one", "two", "three"])
    memory_worker.run_once(key_ring=key_ring, writer_connect=connect, models=models)
    row = _row(queued.ingest_id)
    assert models.extracted == ["one"], "no provider call after a refused key"
    assert row["state"] == "failed" and all("refused this project's API key" in r["reason"]
                                            for r in row["results_json"])


@requires_node
def test_the_plans_memory_ceiling_is_held_after_extraction_too(tenants, worker_node, key_ring):  # noqa: F811
    project_id, _, connect = _text_tenant(tenants, worker_node, key_ring, "mingw005")
    with db.connection() as conn:
        plan = db.one(conn, "SELECT pl.code, pl.config_json FROM projects p LEFT JOIN plans pl ON pl.id = p.plan_id "
                            "WHERE p.id = %s", (project_id,))
        ceiling = entitlements.resolve(plan["code"], plan["config_json"]).memory_max_items
        db.execute(conn, "UPDATE memory_spaces SET item_count = %s WHERE project_id = %s", (ceiling - 1, project_id))
        conn.commit()
    # Admitted: one stored-memory's headroom covers one item. Extraction then finds two.
    models = FakeModels({"first": [("a", "owns"), ("b", "owns")], "second": [("c", "owns")]})
    queued = _enqueue_text(project_id, ["first"])
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_ingests SET items_json = %s, item_count = 2 WHERE id = %s",
                   (psycopg.types.json.Jsonb([{"text": "first", "title": None}, {"text": "second", "title": None}]),
                    queued.ingest_id))
        conn.commit()
    memory_worker.run_once(key_ring=key_ring, writer_connect=connect, models=models)
    results = _row(queued.ingest_id)["results_json"]
    assert results[0]["memories"] == 1 and "stored memory limit" in results[0]["skipped"][0]["reason"]
    assert not results[1]["written"] and "stored memory limit" in results[1]["reason"]
    assert models.extracted == ["first"]


@requires_node
def test_a_text_ingest_without_the_providers_key_fails_saying_which(tenants, worker_node, key_ring):  # noqa: F811
    project_id, _, _ = _text_tenant(tenants, worker_node, key_ring, "mingw006")
    with db.connection() as conn:
        provider_keys.remove_key(conn, project_id=project_id, provider="voyage", actor_user_id=None)
        conn.commit()
    queued = _enqueue_text(project_id, ["carol owns the parser"])

    def never(*_):
        raise AssertionError("an ingest that cannot call its models must not connect to the tenant")

    memory_worker.run_once(key_ring=key_ring, writer_connect=never, models=FakeModels({}))
    row = _row(queued.ingest_id)
    assert row["state"] == "failed" and "no voyage API key" in row["detail"] and row["items_json"] is None


def _enqueue_text(project_id, texts):
    kind, items = memory_ingest.validate({"items": [{"text": t} for t in texts]})
    with db.connection() as conn:
        queued = memory_ingest.enqueue(conn, project_id=project_id, space="bot", items=items,
                                       allowed=entitlements.resolve("free", {}), kind=kind)
        conn.commit()
    return queued


def test_the_gateway_queues_text_once_the_space_names_its_models(client, gateway_project, key_ring):  # noqa: F811
    test_client, _ = client
    project_id = gateway_project("gwmem004")
    _active_space(project_id)
    secret = _issue(project_id, api_keys.SECRET, key_ring)
    path = "/memory/v1/spaces/bot/ingest"
    body = {"items": [{"text": "carol owns the parser", "title": "notes"}]}
    unready = _post(test_client, "gwmem004", secret, path, body)
    assert unready.status_code == 409 and "models" in unready.json()["message"]
    _models_for(project_id)
    queued = _post(test_client, "gwmem004", secret, path, body)
    assert queued.status_code == 202, queued.text
    assert queued.json()["kind"] == "text"
