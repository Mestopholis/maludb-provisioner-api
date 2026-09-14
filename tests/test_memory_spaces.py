"""Creating MaluDB memory spaces (ADR-079, memory slice 2a).

Three layers, as `tests/test_maludb_jobs.py` tests the data-model graph:

- **the queue** -- the name becomes an SQL identifier only through a fixed
  pattern, the plan's `memory_max_spaces` holds under concurrency, a failed build
  can be asked for again, and one job builds every pending space;
- **the routes** -- managers create, members list, outsiders learn nothing, and
  the platform's schema name is never returned;
- **the worker** -- on a real tenant, a space is a superuser-owned memory schema
  no customer role can use, a squatted schema is refused in the platform's own
  words, a `maludb_core` older than 0.105.0 is refused, and the project's vector
  wrappers are re-verified before any space exists.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, maludb, maludb_jobs, maludb_memory, maludb_vectors, provisioner
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_maludb_enable import (  # noqa: F401 - fixtures, resolved by name
    _tenant_conn,
    admin_node_conn,
    requires_node,
    tenants,
)
from tests.test_maludb_jobs import _headers, _job, _member, worker_node  # noqa: F401 - fixture

pytestmark = requires_db

SPACES = "/v1/projects/{ref}/maludb/memory/spaces"


def _request(project_id, name: str):
    with db.connection() as conn:
        try:
            return maludb_jobs.request_memory_space(conn, project_id=project_id, name=name, requested_by=None)
        finally:
            conn.commit()


def _spaces(project_id) -> list[dict]:
    with db.connection() as conn:
        return db.query(conn, "SELECT name, schema_name, state, detail FROM memory_spaces "
                              "WHERE project_id = %s ORDER BY name", (project_id,))


def _set_limits(project_id, **limits) -> None:
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET config_json = %s WHERE id = (SELECT plan_id FROM projects WHERE id = %s)",
                   (Jsonb({"limits": limits}), project_id))
        conn.commit()


# -- the queue ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "Support", "1bot", "a-b", "a" * 41, "x; DROP SCHEMA public", "ok\n"])
def test_a_name_that_is_not_the_pattern_is_refused_before_anything_is_written(placed_project, name):
    project_id = placed_project("msq00001")
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request(project_id, name)
    assert refused.value.status == 422
    assert _spaces(project_id) == []


def test_a_space_reserves_its_name_under_a_derived_schema_and_queues_one_job(placed_project):
    project_id = placed_project("msq00002")
    _set_limits(project_id, memory_max_spaces=3)
    space, job = _request(project_id, "support_bot")
    assert space["state"] == "pending" and space["schema_name"] == "mem_support_bot"
    assert job is not None and not job.coalesced

    # A second space joins the job already waiting: one job builds every pending space.
    _, second = _request(project_id, "sales")
    assert second.coalesced and second.job_id == job.job_id


def test_the_plans_space_limit_is_held_and_the_refusal_does_not_name_it(placed_project):
    project_id = placed_project("msq00003")  # an unknown plan code resolves to free: one space
    _request(project_id, "first")
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request(project_id, "second")
    assert refused.value.status == 409
    assert "1" not in str(refused.value)


def test_asking_again_for_an_active_space_queues_nothing(placed_project):
    project_id = placed_project("msq00004")
    _request(project_id, "bot")
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_spaces SET state = 'active', active_at = now(), "
                         "memory_schema_version = '0.105.0' WHERE project_id = %s", (project_id,))
        conn.commit()
    space, job = _request(project_id, "bot")
    assert job is None and space["state"] == "active"


def test_a_failed_space_can_be_asked_for_again_and_does_not_hold_a_slot(placed_project):
    project_id = placed_project("msq00005")
    _request(project_id, "bot")
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_spaces SET state = 'failed', detail = 'x' WHERE project_id = %s",
                   (project_id,))
        db.execute(conn, "UPDATE maludb_jobs SET state = 'failed', completed_at = now(), started_at = now() "
                         "WHERE project_id = %s", (project_id,))
        conn.commit()
    space, job = _request(project_id, "bot")
    assert space["state"] == "pending" and space["detail"] is None and job is not None
    assert [s["name"] for s in _spaces(project_id)] == ["bot"], "retried, not duplicated"


def test_a_plan_without_memory_is_refused(placed_project):
    project_id = placed_project("msq00006")
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET config_json = %s WHERE id = (SELECT plan_id FROM projects WHERE id = %s)",
                   (Jsonb({"maludb_memory": False}), project_id))
        conn.commit()
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request(project_id, "bot")
    assert refused.value.status == 403


# -- the routes --------------------------------------------------------------


def test_a_manager_creates_and_a_member_lists_without_the_schema_name(client, placed_project):
    placed_project("msr00001")
    manager = _headers(client, "msr00001")
    created = client.post(SPACES.format(ref="msr00001"), json={"name": "support_bot"}, headers=manager)
    assert created.status_code == 202, created.text
    assert created.json()["space"]["state"] == "pending"
    assert "schema_name" not in created.json()["space"]

    developer = _member(client, "msr00001", email="ms-dev@example.com", role="developer")
    assert client.post(SPACES.format(ref="msr00001"), json={"name": "other"}, headers=developer).status_code == 403
    listed = client.get(SPACES.format(ref="msr00001"), headers=developer)
    assert listed.status_code == 200
    body = listed.json()
    assert [s["name"] for s in body["spaces"]] == ["support_bot"]
    assert body["max_spaces"] >= 1 and "schema_name" not in body["spaces"][0]


def test_a_bad_name_answers_422_in_words(client, placed_project):
    placed_project("msr00002")
    response = client.post(SPACES.format(ref="msr00002"), json={"name": "Bad Name"},
                           headers=_headers(client, "msr00002"))
    assert response.status_code == 422
    assert "lower-case" in response.json()["detail"]


def test_a_non_member_cannot_tell_the_project_exists(client, placed_project):
    placed_project("msr00003")
    client.post("/v1/auth/signup", json={"email": "ms-out@example.com", "password": TEST_CREDENTIAL})
    token = client.post("/v1/auth/signin", json={"email": "ms-out@example.com", "password": TEST_CREDENTIAL}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert client.get(SPACES.format(ref="msr00003"), headers=headers).status_code == 404
    assert client.post(SPACES.format(ref="msr00003"), json={"name": "x"}, headers=headers).status_code == 404


# -- the worker, on a real tenant ---------------------------------------------


def _customer_reach(database: str, names, schema: str) -> list[str]:
    with _tenant_conn(database, autocommit=True) as t:
        present = [r[0] for r in t.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                                           (list(maludb.customer_roles(names)),)).fetchall()]
        return [role for role in present if t.execute(
            "SELECT has_schema_privilege(%s, %s, 'USAGE') OR EXISTS (SELECT 1 FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s "
            "AND has_function_privilege(%s, p.oid, 'EXECUTE'))", (role, schema, schema, role)).fetchone()[0]]


@requires_node
def test_the_provisioner_builds_a_closed_superuser_owned_space(tenants, worker_node, key_ring, monkeypatch):  # noqa: F811
    project_id, names, _ = tenants("mswrk001", plan_config={"limits": {"memory_max_spaces": 2}})
    worker_node()
    reverified: list[str] = []
    real_reverify = maludb_vectors.reverify
    monkeypatch.setattr(maludb_memory.maludb_vectors, "reverify",
                        lambda conn, n: reverified.append(n.database) or real_reverify(conn, n))

    _request(project_id, "support_bot")
    _, queued = _request(project_id, "sales")
    assert provisioner.run_maludb_once(key_ring=key_ring)
    job = _job(queued.job_id)
    assert job["state"] == "succeeded", job["detail"]
    assert sorted(job["result_json"]["created"]) == ["sales", "support_bot"]
    assert {s["state"] for s in _spaces(project_id)} == {"active"}
    assert reverified == [names.database, names.database], "vector wrappers re-verified before each space"

    with _tenant_conn(names.database, autocommit=True) as t:
        owner = maludb.schema_owner(t, "mem_support_bot")
        facades = t.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                            "WHERE n.nspname = 'mem_support_bot'").fetchone()[0]
    assert owner is not None and owner[0], "the space's schema must be superuser-owned"
    assert facades > 0, "enable_memory_schema built nothing"
    assert _customer_reach(names.database, names, "mem_support_bot") == []
    assert provisioner.run_maludb_once(key_ring=key_ring) is False


@requires_node
def test_a_squatted_schema_is_refused_in_the_platforms_words_and_the_rest_are_built(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names, _ = tenants("mswrk002", plan_config={"limits": {"memory_max_spaces": 2}})
    worker_node()
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute('CREATE SCHEMA "mem_taken"')

    _request(project_id, "taken")
    _, queued = _request(project_id, "fine")
    provisioner.run_maludb_once(key_ring=key_ring)
    job = _job(queued.job_id)
    assert job["state"] == "succeeded"
    spaces = {s["name"]: s for s in _spaces(project_id)}
    assert spaces["fine"]["state"] == "active"
    assert spaces["taken"]["state"] == "failed"
    assert "Rename or drop" in spaces["taken"]["detail"]
    with _tenant_conn(names.database, autocommit=True) as t:
        built_into = t.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                               "WHERE n.nspname = 'mem_taken'").fetchone()[0]
    assert built_into == 0, "nothing superuser-owned may be built into a customer's schema"


@requires_node
def test_a_tenant_before_0_105_0_is_refused(tenants, worker_node, key_ring, admin_node_conn):  # noqa: F811
    available = admin_node_conn.execute(
        "SELECT 1 FROM pg_available_extension_versions WHERE name = 'maludb_core' AND version = '0.104.0'"
    ).fetchone()
    if not available:
        pytest.skip("maludb_core 0.104.0 is not installable on this node")
    project_id, names, _ = tenants("mswrk003", version="0.104.0")
    worker_node()
    _request(project_id, "bot")
    provisioner.run_maludb_once(key_ring=key_ring)
    (space,) = _spaces(project_id)
    assert space["state"] == "failed"
    assert "0.105.0" in space["detail"] and "ADR-078" in space["detail"]
    with _tenant_conn(names.database, autocommit=True) as t:
        assert maludb.schema_owner(t, "mem_bot") is None, "the refusal came before anything was created"
