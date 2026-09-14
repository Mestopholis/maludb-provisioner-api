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
import pytest

from services.control_plane import api_keys, db, entitlements, memory_ingest, memory_worker, provisioner
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
    queued = _enqueue(project_id, memory_ingest.validate({"items": items}))

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
