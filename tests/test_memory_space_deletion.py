"""Deleting a MaluDB memory space (ADR-079, memory slice 2c).

Measured first (`specs/maludb-memory-pipeline-model.md`, "Memory slice 2c"), then
held here where it can fail:

- **the request** -- writes stop the moment deletion is asked for: the space is
  marked `deleting`, its pending ingests fail with a reason, nothing new is admitted
  or claimed, its models cannot change and its name cannot be taken again until it
  is gone; a space that never built is simply removed; the hourly budget applies,
  because a create-delete cycle is repeatable superuser work;
- **the job, on a real tenant** -- nothing in the database names the space
  afterwards, the space beside it still finds exactly its own memories, the
  control-plane row and its ingests are gone, the slot is free, and the same name
  can be built again empty;
- **the route** -- managers delete, members cannot.
"""

from __future__ import annotations

import psycopg
import psycopg.sql
import pytest

from services.control_plane import db, maludb, maludb_jobs, maludb_memory, memory_ingest, memory_worker, provisioner
from tests.conftest import requires_db
from tests.test_maludb_enable import (  # noqa: F401 - fixtures, resolved by name
    _tenant_conn,
    admin_node_conn,
    requires_node,
    tenants,
)
from tests.test_maludb_jobs import _headers, _job, _member, worker_node  # noqa: F401 - fixture
from tests.test_memory_ingest import _active_space, _enqueue, _item
from tests.test_memory_spaces import _request, _set_limits, _spaces, _two_spaces_with_memories

pytestmark = requires_db

SPACES = "/v1/projects/{ref}/maludb/memory/spaces"


def _delete(project_id, name: str):
    with db.connection() as conn:
        try:
            return maludb_jobs.request_memory_space_deletion(conn, project_id=project_id, name=name,
                                                             requested_by=None)
        finally:
            conn.commit()


# -- the request ---------------------------------------------------------------------


def test_asking_stops_writes_at_once_and_queues_one_job(placed_project):
    project_id = placed_project("msdel001")
    _active_space(project_id)
    queued = _enqueue(project_id, [_item()])

    space, job = _delete(project_id, "bot")
    again_space, again = _delete(project_id, "bot")

    assert space["state"] == "deleting" and job.kind == maludb_jobs.KIND_MEMORY_SPACES
    assert again.coalesced and again.job_id == job.job_id and again_space["state"] == "deleting"
    with db.connection() as conn:
        ingest = db.one(conn, "SELECT state, detail, items_json FROM memory_ingests WHERE id = %s",
                        (queued.ingest_id,))
        assert memory_worker.claim(conn) is None, "an ingest into a space being deleted was claimed"
        conn.rollback()
    assert ingest["state"] == "failed" and "deleted" in ingest["detail"] and ingest["items_json"] is None

    with pytest.raises(memory_ingest.IngestRefused) as refused:
        _enqueue(project_id, [_item()])
    assert refused.value.status == 404


def test_a_space_being_deleted_keeps_its_name_and_slot_and_its_models(placed_project):
    project_id = placed_project("msdel002")
    _active_space(project_id)
    _set_limits(project_id, memory_max_spaces=1)
    _delete(project_id, "bot")

    with pytest.raises(maludb_jobs.JobRefused) as same_name:
        _request(project_id, "bot")
    assert same_name.value.status == 409 and "being deleted" in str(same_name.value)
    with pytest.raises(maludb_jobs.JobRefused) as other_name:
        _request(project_id, "other")
    assert other_name.value.status == 409, "a space still being deleted must hold its slot"
    with db.connection() as conn:
        with pytest.raises(maludb_jobs.JobRefused) as models:
            maludb_jobs.set_memory_models(conn, project_id=project_id, name="bot", extraction_provider="openai",
                                          extraction_model=None, embedding_provider="openai", embedding_model=None,
                                          actor_user_id=None)
        conn.rollback()
    assert models.value.status == 409


def test_a_space_that_never_built_is_removed_without_a_job(placed_project):
    project_id = placed_project("msdel003")
    _active_space(project_id)
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_spaces SET state = 'failed', detail = 'x' WHERE project_id = %s",
                   (project_id,))
        conn.commit()
    assert _delete(project_id, "bot") == (None, None)
    assert _spaces(project_id) == []


def test_an_unknown_space_is_404(placed_project):
    project_id = placed_project("msdel004")
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _delete(project_id, "nope")
    assert refused.value.status == 404


def test_creation_is_rationed_and_deletion_never_is(placed_project):
    """Deletion made a create-delete cycle possible. Refusing creation bounds it; refusing
    deletion would keep a customer from removing their own data because they spent their hour."""
    project_id = placed_project("msdel005")
    _set_limits(project_id, memory_max_spaces=5, datamodel_refreshes_per_hour=1)
    _, job = _request(project_id, "one")
    with db.connection() as conn:
        db.execute(conn, "UPDATE maludb_jobs SET state = 'succeeded', started_at = now(), completed_at = now() "
                         "WHERE id = %s", (job.job_id,))
        db.execute(conn, "UPDATE memory_spaces SET state = 'active', active_at = now(), "
                         "memory_schema_version = '0.105.0' WHERE project_id = %s", (project_id,))
        conn.commit()
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request(project_id, "two")
    assert refused.value.status == 429 and "memory space changes" in str(refused.value)
    assert [s["name"] for s in _spaces(project_id)] == ["one"], "a refused creation must reserve nothing"

    space, deletion = _delete(project_id, "one")
    assert space["state"] == "deleting" and deletion is not None


# -- the route ------------------------------------------------------------------------


def test_a_manager_deletes_and_a_developer_cannot(client, placed_project):
    project_id = placed_project("msdel006")
    _active_space(project_id)
    developer = _member(client, "msdel006", email="msdel-dev@example.com", role="developer")
    assert client.delete(f"{SPACES.format(ref='msdel006')}/bot", headers=developer).status_code == 403
    response = client.delete(f"{SPACES.format(ref='msdel006')}/bot", headers=_headers(client, "msdel006"))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["space"]["state"] == "deleting" and body["job"]["kind"] == maludb_jobs.KIND_MEMORY_SPACES
    assert "schema_name" not in body["space"]


# -- the job, on a real tenant ---------------------------------------------------------------


def _search(database: str, space: str) -> set[int]:
    with _tenant_conn(database, autocommit=True) as t:
        t.execute("SET ROLE service_role")
        return {r[0] for r in t.execute(
            "SELECT chunk_id FROM maludb.memory_search(%s, '[0.1,0.2,0.3]'::vector, 'carol', NULL, 'default', 1000)",
            (space,)).fetchall()}


@requires_node
def test_deleting_a_space_leaves_nothing_and_its_neighbour_untouched(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "msdel101")
    beta_before = _search(names.database, "beta")
    with _tenant_conn(names.database, autocommit=True) as t:
        alpha_rows = sum(v for v in maludb_memory.residue(t, "mem_alpha").values())
    assert beta_before and alpha_rows > 20

    _, job = _delete(project_id, "alpha")
    provisioner.run_maludb_once(key_ring=key_ring)
    done = _job(job.job_id)
    assert done["state"] == "succeeded", done
    assert done["result_json"]["deleted"] == ["alpha"]

    with _tenant_conn(names.database, autocommit=True) as t:
        assert maludb_memory.residue(t, "mem_alpha") == {}, "something in the tenant still names the space"
        assert t.execute("SELECT count(*) FROM maludb_private.memory_space_registry WHERE name = 'alpha'"
                         ).fetchone()[0] == 0
        t.execute("SET ROLE service_role")
        with pytest.raises(psycopg.Error) as gone:
            t.execute("SELECT * FROM maludb.memory_search('alpha', '[0.1,0.2,0.3]'::vector, 'carol')")
        assert gone.value.sqlstate == "PT404"
    assert _search(names.database, "beta") == beta_before, "deleting alpha changed what beta finds"

    assert [s["name"] for s in _spaces(project_id)] == ["beta"]
    with db.connection() as conn:
        flags = db.one(conn, "SELECT maludb_memory_enabled FROM projects WHERE id = %s", (project_id,))
        events = db.query(conn, "SELECT detail_json FROM audit_events WHERE project_id = %s AND event_type = %s",
                          (project_id, maludb_memory.AUDIT_SPACE_DELETED))
    assert flags["maludb_memory_enabled"], "another space is still active"
    assert events and events[0]["detail_json"]["space"] == "alpha" and events[0]["detail_json"]["rows"] > 0

    # The slot is free and the name builds again, empty.
    _, rebuilt = _request(project_id, "alpha")
    provisioner.run_maludb_once(key_ring=key_ring)
    assert _job(rebuilt.job_id)["state"] == "succeeded"
    assert _search(names.database, "alpha") == set()


@requires_node
def test_deleting_the_last_space_turns_memory_off_and_a_rerun_is_harmless(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "msdel102")
    _delete(project_id, "alpha")
    _delete(project_id, "beta")
    provisioner.run_maludb_once(key_ring=key_ring)
    assert _spaces(project_id) == []
    with db.connection() as conn:
        assert not db.one(conn, "SELECT maludb_memory_enabled FROM projects WHERE id = %s",
                          (project_id,))["maludb_memory_enabled"]
    with _tenant_conn(names.database) as t:
        # Idempotent: a job that died after the tenant commit finishes on a re-run.
        assert maludb_memory.delete_space(t, names, "alpha", "mem_alpha") == 0
        t.rollback()


@requires_node
def test_a_schema_a_customer_owns_is_not_dropped(tenants, worker_node, key_ring, admin_node_conn):  # noqa: F811
    project_id, names, _ = tenants("msdel103")
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'CREATE SCHEMA mem_theirs AUTHORIZATION "{names.admin}"')
    with _tenant_conn(names.database) as t:
        with pytest.raises(maludb_memory.MemoryError_):
            maludb_memory.delete_space(t, names, "theirs", "mem_theirs")
        t.rollback()
        assert maludb.schema_owner(t, "mem_theirs") is not None


@requires_node
def test_a_space_something_outside_depends_on_is_not_dropped(tenants, worker_node, key_ring):  # noqa: F811
    """CASCADE would take the dependent with it; the deletion refuses instead."""
    project_id, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "msdel104")
    with _tenant_conn(names.database, autocommit=True) as t:
        view = t.execute("SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'mem_alpha'::regnamespace "
                         "AND c.relkind IN ('v', 'r') ORDER BY c.relkind DESC, 1 LIMIT 1").fetchone()[0]
        t.execute(psycopg.sql.SQL("CREATE VIEW public.depends_on_alpha AS SELECT 1 AS x FROM {} LIMIT 0")
                  .format(psycopg.sql.Identifier("mem_alpha", view)))
    with _tenant_conn(names.database, autocommit=True) as t:
        alpha_before = maludb_memory.residue(t, "mem_alpha")
    _delete(project_id, "alpha")
    provisioner.run_maludb_once(key_ring=key_ring)
    [alpha] = [s for s in _spaces(project_id) if s["name"] == "alpha"]
    assert alpha["state"] == "deleting" and "depend on it" in alpha["detail"]
    with _tenant_conn(names.database, autocommit=True) as t:
        assert maludb_memory.residue(t, "mem_alpha") == alpha_before, "a refused deletion removed rows first"
        assert t.execute("SELECT to_regclass('public.depends_on_alpha') IS NOT NULL").fetchone()[0]
        assert maludb.schema_owner(t, "mem_alpha") is not None, "nothing may have been dropped"


# -- batched deletion (measured at scale: one transaction is quadratic) -----------------


@requires_node
def test_a_large_space_is_deleted_in_committed_batches_that_report_progress(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "msdel201")
    beta_before = _search(names.database, "beta")
    reported: list[int] = []
    with _tenant_conn(names.database) as t:
        removed = maludb_memory.delete_in_batches(t, "alpha", "mem_alpha", progress=reported.append, batch_rows=4)
        # Committed, not held: a second connection already sees the rows gone and search refused.
        with _tenant_conn(names.database, autocommit=True) as other:
            chunks = other.execute('SELECT count(*) FROM maludb_core."malu$vector_chunk" ch JOIN '
                                   'maludb_core."malu$vector_compartment" c USING (compartment_id) '
                                   "WHERE c.owner_schema = 'mem_alpha'").fetchone()[0]
            other.execute("SET ROLE service_role")
            with pytest.raises(psycopg.Error) as gone:
                other.execute("SELECT * FROM maludb.memory_search('alpha', '[0.1,0.2,0.3]'::vector, 'carol')")
    assert removed > 0 and len(reported) > 3 and reported == sorted(reported)
    assert chunks == 0 and gone.value.sqlstate == "PT404"

    # The provisioner finishes what the batches left, and the residue check still holds.
    _delete(project_id, "alpha")
    provisioner.run_maludb_once(key_ring=key_ring)
    with _tenant_conn(names.database, autocommit=True) as t:
        assert maludb_memory.residue(t, "mem_alpha") == {}
    assert _search(names.database, "beta") == beta_before


@requires_node
def test_a_deletion_that_dies_between_batches_finishes_when_run_again(tenants, worker_node, key_ring, monkeypatch):  # noqa: F811
    project_id, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "msdel202")
    _delete(project_id, "alpha")
    real = maludb_memory.delete_in_batches

    def dies_after_two_batches(tenant_conn, name, schema, *, progress, batch_rows=4):
        calls = {"n": 0}

        def progress_then_die(count):
            progress(count)
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("the provisioner was killed")

        return real(tenant_conn, name, schema, progress=progress_then_die, batch_rows=batch_rows)

    monkeypatch.setattr(maludb_memory, "delete_in_batches", dies_after_two_batches)
    provisioner.run_maludb_once(key_ring=key_ring)
    [alpha] = [s for s in _spaces(project_id) if s["name"] == "alpha"]
    assert alpha["state"] == "deleting", "a deletion that died must stay deleting, not vanish or revert"
    with _tenant_conn(names.database, autocommit=True) as t:
        assert maludb_memory.residue(t, "mem_alpha"), "the first run deleted everything; the test proves nothing"

    monkeypatch.setattr(maludb_memory, "delete_in_batches", real)
    _delete(project_id, "alpha")  # asking again queues the job the failure left
    provisioner.run_maludb_once(key_ring=key_ring)
    assert [s["name"] for s in _spaces(project_id)] == ["beta"]
    with _tenant_conn(names.database, autocommit=True) as t:
        assert maludb_memory.residue(t, "mem_alpha") == {}


def test_a_long_job_with_a_recent_heartbeat_is_not_taken_for_abandoned(placed_project):
    project_id = placed_project("msdel203")
    with db.connection() as conn:
        job = db.one(conn, "INSERT INTO maludb_jobs (project_id, kind, state, started_at, heartbeat_at) "
                           "VALUES (%s, 'memory_spaces', 'running', now() - interval '2 hours', now()) "
                           "RETURNING id", (project_id,))["id"]
        stale = db.one(conn, "INSERT INTO maludb_jobs (project_id, kind, state, started_at) "
                             "VALUES (%s, 'enable', 'running', now() - interval '2 hours') RETURNING id",
                       (project_id,))["id"]
        conn.commit()
        maludb_jobs.claim(conn)
        conn.commit()
        states = {r["id"]: r["state"] for r in db.query(conn, "SELECT id, state FROM maludb_jobs WHERE id = ANY(%s)",
                                                        ([job, stale],))}
    assert states == {job: "running", stale: "failed"}

