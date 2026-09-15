"""A memory space's models (ADR-079, memory slice 5b).

What a manager can name -- an extraction and an embedding provider from a fixed
list, and a model name that is text and never an address -- and the one thing that
cannot change once a space holds memories: the embedding model, because search
compares only vectors from one model.
"""

from __future__ import annotations

import pytest

from services.control_plane import db, maludb_jobs, model_providers
from tests.conftest import requires_db
from tests.test_maludb_jobs import _headers, _member
from tests.test_memory_ingest import _active_space

pytestmark = requires_db

MODELS = "/v1/projects/{ref}/maludb/memory/spaces/bot/models"


def _set(project_id, **overrides):
    values = {"extraction_provider": "openai", "extraction_model": None, "embedding_provider": "openai",
              "embedding_model": None} | overrides
    with db.connection() as conn:
        try:
            return maludb_jobs.set_memory_models(conn, project_id=project_id, name="bot", actor_user_id=None, **values)
        finally:
            conn.commit()


def test_unnamed_models_take_the_providers_defaults(placed_project):
    project_id = placed_project("mmd00001")
    _active_space(project_id)
    row = _set(project_id)
    assert row["extraction_model"] == model_providers.DEFAULT_EXTRACTION_MODELS["openai"]
    assert row["embedding_model"] == model_providers.DEFAULT_EMBEDDING_MODELS["openai"]


@pytest.mark.parametrize(("overrides", "fragment"), [
    ({"extraction_provider": "voyage"}, "extraction_provider"),
    ({"embedding_provider": "anthropic"}, "embedding_provider"),
    ({"extraction_model": "https://attacker.example/v1"}, "extraction_model"),
    ({"embedding_model": "a b"}, "embedding_model"),
])
def test_a_provider_off_the_list_or_a_model_name_that_is_not_a_name_is_refused(placed_project, overrides, fragment):
    project_id = placed_project(f"mmd1{abs(hash(fragment + str(overrides))) % 10000:04d}")
    _active_space(project_id)
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _set(project_id, **overrides)
    assert refused.value.status == 422 and fragment in str(refused.value)


def test_the_embedding_model_is_fixed_once_the_space_holds_memories(placed_project):
    project_id = placed_project("mmd00002")
    _active_space(project_id)
    _set(project_id, embedding_provider="voyage")
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_spaces SET item_count = 3 WHERE project_id = %s", (project_id,))
        conn.commit()
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _set(project_id, embedding_provider="openai")
    assert refused.value.status == 409
    # The extraction model is not part of what search compares, so it may change.
    assert _set(project_id, extraction_provider="anthropic", embedding_provider="voyage")["extraction_provider"] \
        == "anthropic"


def test_the_embedding_model_is_fixed_while_an_ingest_is_queued(placed_project):
    project_id = placed_project("mmd00003")
    _active_space(project_id)
    _set(project_id)
    with db.connection() as conn:
        space_id = db.one(conn, "SELECT id FROM memory_spaces WHERE project_id = %s", (project_id,))["id"]
        db.execute(conn, "INSERT INTO memory_ingests (id, project_id, space_id, kind, item_count, items_json) "
                         "VALUES (gen_random_uuid(), %s, %s, 'text', 1, '[{\"text\": \"x\"}]'::jsonb)",
                   (project_id, space_id))
        conn.commit()
    with pytest.raises(maludb_jobs.JobRefused):
        _set(project_id, embedding_model="text-embedding-3-large")


def test_a_manager_sets_models_a_developer_cannot_and_the_listing_shows_them(client, placed_project):
    project_id = placed_project("mmd00004")
    _active_space(project_id)
    manager = _headers(client, "mmd00004")
    body = {"extraction_provider": "anthropic", "embedding_provider": "voyage", "embedding_model": "voyage-3.5-lite"}
    put = client.put(MODELS.format(ref="mmd00004"), json=body, headers=manager)
    assert put.status_code == 200, put.text
    assert put.json()["embedding_model"] == "voyage-3.5-lite"

    developer = _member(client, "mmd00004", email="mmd-dev@example.com", role="developer")
    assert client.put(MODELS.format(ref="mmd00004"), json=body, headers=developer).status_code == 403

    listed = client.get("/v1/projects/mmd00004/maludb/memory/spaces", headers=manager).json()["spaces"][0]
    assert (listed["extraction_provider"], listed["embedding_model"]) == ("anthropic", "voyage-3.5-lite")
    assert "schema_name" not in listed
    audit = client.get("/v1/projects/mmd00004/audit-events", headers=manager).json()
    assert any(e["event_type"] == maludb_jobs.AUDIT_SPACE_MODELS_SET for e in audit)
