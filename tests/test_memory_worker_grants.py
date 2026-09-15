"""The memory worker's control-plane role (ADR-079 decision 6, memory slice 5c).

The worker holds the KEK. Decision 6 says a compromised worker reaches memory and
not the fleet, and before this slice that held only because of the code it ran.
These tests ask the database instead:

- the role cannot read a node's admin credential, any credential but a memory
  writer's, a revoked provider key, or users, API keys, sessions and billing;
- it cannot queue work or rename a space, only move a request along;
- **the worker runs end to end as that role**, which is what keeps an allowlist
  honest -- a grant that is too narrow fails here, not in production;
- no role may be a gateway and a memory worker at once, because the two together
  read a node's tenant credentials that neither reaches alone;
- the worker refuses to start in production as anything wider.
"""

from __future__ import annotations

import contextlib

import psycopg
import psycopg.sql
import pytest

from services.control_plane import (
    db,
    entitlements,
    memory_ingest,
    memory_worker,
    memory_worker_grants,
    provider_keys,
    provisioning,
)
from services.control_plane import manage as manage_mod
from services.control_plane import preflight as preflight_mod
from tests.conftest import requires_db
from tests.test_gateway_grants import gateway_role, two_nodes_two_projects  # noqa: F401
from tests.test_memory_ingest import FakeModels, _active_space, _models_for

pytestmark = requires_db

GROUP = memory_worker_grants.GROUP_ROLE
PROVIDER_KEY = "sk-test-" + "C5c0" * 6 + "role"  # noqa: S105 - test fixture, not a real key
WRITER_SECRET = "writer-" + "secret"  # a test fixture, sealed and opened, never a real password


@pytest.fixture
def worker_role(db_pool):  # noqa: ARG001 - the pool must exist before db.connection()
    with db.connection() as conn:
        try:
            if db.one(conn, "SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (GROUP,)) is None:
                conn.execute(f'CREATE ROLE "{GROUP}" NOLOGIN')
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip("the control-plane role cannot CREATE ROLE, so the worker's model cannot be applied here")
        for statement in memory_worker_grants.statements(GROUP):
            conn.execute(statement)
        conn.commit()
    try:
        yield GROUP
    finally:
        with db.connection() as conn:
            for statement in memory_worker_grants.revocations(GROUP):
                conn.execute(statement)
            conn.execute(f'DROP ROLE IF EXISTS "{GROUP}"')
            conn.commit()


@contextlib.contextmanager
def _pool_as_worker(monkeypatch):
    """Every `db.connection()` inside the block runs as the worker's role."""
    original = db.connection

    @contextlib.contextmanager
    def connection():
        with original() as conn:
            conn.execute(f'SET ROLE "{GROUP}"')
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


def _worker_credential_rows(two_nodes_two_projects, key_ring):  # noqa: F811
    alpha = two_nodes_two_projects["alpha"]
    with db.connection() as conn:
        provisioning.store_credential(conn, project_id=alpha["project_id"], credential_type="db_memwriter",
                                      role_name=f"mldb_{alpha['ref']}_memwriter", secret=WRITER_SECRET,
                                      key_ring=key_ring)
        for api_key in (PROVIDER_KEY.replace("role", "old1"), PROVIDER_KEY):
            provider_keys.set_key(conn, project_id=alpha["project_id"], provider="openai", api_key=api_key,
                                  key_ring=key_ring, actor_user_id=None)
        conn.commit()


# -- the catalogue and the rows ---------------------------------------------------------


def test_the_role_reaches_nothing_a_memory_worker_must_not(worker_role):
    with db.connection() as conn:
        assert memory_worker_grants.violations(conn, worker_role) == []
        for table, column in (("nodes", "admin_ciphertext"), ("nodes", "admin_nonce"),
                              ("projects", "jwt_secret_ciphertext"), ("project_credentials", "role_name")):
            exists = db.one(conn, "SELECT count(*) AS n FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                                  "AND attname = %s", (table, column))["n"]
            if exists:
                assert not db.one(conn, "SELECT has_column_privilege(%s, %s, %s, 'SELECT') AS ok",
                                  (worker_role, table, column))["ok"], f"{table}.{column}"
        writable = db.query(conn, "SELECT table_name FROM information_schema.role_table_grants "
                                  " WHERE grantee = %s AND privilege_type IN ('INSERT', 'DELETE', 'TRUNCATE')",
                            (worker_role,))
    assert writable == [], "the worker may move requests along; it may never create or delete rows"


def test_the_worker_sees_writer_credentials_and_live_keys_only(worker_role, two_nodes_two_projects, key_ring):  # noqa: F811
    _worker_credential_rows(two_nodes_two_projects, key_ring)
    try:
        with db.connection() as conn:
            conn.execute(f'SET LOCAL ROLE "{worker_role}"')
            types = {r["credential_type"] for r in db.query(conn, "SELECT credential_type FROM project_credentials")}
            keys = db.query(conn, "SELECT revoked_at FROM project_provider_keys")
            refs = {r["project_ref"] for r in db.query(conn, "SELECT project_ref FROM projects")}
            conn.rollback()
    finally:
        # The shared fixture deletes its projects; these rows would hold them.
        with db.connection() as conn:
            alpha = two_nodes_two_projects["alpha"]["project_id"]
            for table in ("audit_events", "project_provider_keys"):
                conn.execute(psycopg.sql.SQL("DELETE FROM {} WHERE project_id = %s").format(
                    psycopg.sql.Identifier(table)), (alpha,))
            conn.commit()
    assert types == {"db_memwriter"}, "a tenant's database password reached the memory worker"
    assert keys and all(k["revoked_at"] is None for k in keys)
    # The whole fleet's projects, by design: one worker serves every node's spaces.
    assert {two_nodes_two_projects["alpha"]["ref"], two_nodes_two_projects["beta"]["ref"]} <= refs


@pytest.mark.parametrize("statement", [
    "SELECT admin_ciphertext FROM nodes",
    "SELECT role_name FROM project_credentials",
    "SELECT * FROM api_keys",
    "SELECT * FROM users",
    "INSERT INTO memory_ingests (id, project_id, space_id, item_count, items_json) "
    "VALUES (gen_random_uuid(), gen_random_uuid(), 1, 1, '[]')",
    "UPDATE memory_spaces SET schema_name = 'mem_other'",
])
def test_what_the_role_is_refused(worker_role, statement):
    with db.connection() as conn:
        conn.execute(f'SET LOCAL ROLE "{worker_role}"')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()


def test_a_gateway_is_not_admitted_by_the_workers_policies(worker_role, gateway_role, two_nodes_two_projects):  # noqa: F811
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s", (gateway_role, alpha["node_id"]))
        conn.execute(f'SET LOCAL ROLE "{gateway_role}"')
        assert db.one(conn, "SELECT public.is_memory_worker() AS yes")["yes"] is False
        refs = {r["project_ref"] for r in db.query(conn, "SELECT project_ref FROM projects")}
        conn.rollback()
    assert alpha["ref"] in refs and beta["ref"] not in refs


def test_a_role_cannot_be_both_a_gateway_and_the_memory_worker(worker_role, gateway_role, two_nodes_two_projects,  # noqa: F811
                                                               capsys):
    import argparse

    alpha = two_nodes_two_projects["alpha"]
    with db.connection() as conn:
        conn.execute(f'GRANT "{worker_role}" TO "{gateway_role}"')
        conn.commit()
    try:
        with db.connection() as conn:
            name = db.one(conn, "SELECT name FROM nodes WHERE id = %s", (alpha["node_id"],))["name"]
        assert manage_mod._cmd_gateway_grant(argparse.Namespace(role=gateway_role, node=name)) == 2
        assert "must not also be the memory worker" in capsys.readouterr().out

        with db.connection() as conn:
            db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s", (gateway_role, alpha["node_id"]))
            conn.commit()
            assert memory_worker_grants.gateway_members(conn) == [gateway_role]
        assert manage_mod._cmd_memory_worker_grant(argparse.Namespace()) == 2
        report = preflight_mod.Report()
        with db.connection() as conn:
            preflight_mod._check_memory_worker_role(conn, report)
        assert report.failures and "gateway roles" in report.failures[0].detail
    finally:
        with db.connection() as conn:
            conn.execute(f'REVOKE "{worker_role}" FROM "{gateway_role}"')
            db.execute(conn, "UPDATE nodes SET gateway_role = NULL WHERE id = %s", (alpha["node_id"],))
            conn.commit()


# -- the worker, as the role ----------------------------------------------------------------


class _NoWriter:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_the_worker_runs_end_to_end_as_its_own_role(worker_role, placed_project, key_ring, monkeypatch):
    """Claim, credential, provider keys, plan, heartbeat, finish -- every read and
    write the worker makes, through the grants and the policies rather than as owner.
    The tenant write itself is stubbed: it is the writer's connection, not this role's."""
    project_id = placed_project("mwg00001")
    _active_space(project_id)
    _models_for(project_id, extraction="openai", embedding="openai")
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status = 'ACTIVE' WHERE id = %s", (project_id,))
        provisioning.store_credential(conn, project_id=project_id, credential_type="db_memwriter",
                                      role_name="mldb_mwg00001_memwriter", secret=WRITER_SECRET, key_ring=key_ring)
        provider_keys.set_key(conn, project_id=project_id, provider="openai", api_key=PROVIDER_KEY,
                              key_ring=key_ring, actor_user_id=None)
        kind, items = memory_ingest.validate({"items": [{"text": "carol owns the parser"}]})
        text = memory_ingest.enqueue(conn, project_id=project_id, space="bot", items=items,
                                     allowed=entitlements.resolve("free", {}), kind=kind)
        _, edge_items = memory_ingest.validate({"items": [{"subject": "dave", "verb": "owns", "text": "dave owns it",
                                                           "embedding": [0.1, 0.2]}]})
        edges = memory_ingest.enqueue(conn, project_id=project_id, space="bot", items=edge_items,
                                      allowed=entitlements.resolve("free", {}))
        conn.commit()

    passwords: list[str] = []

    def connect(ingest, password):
        passwords.append(password)
        return _NoWriter()

    def text_items(writer, schema, items, *, extract, embed, embedding_model, remaining, beat):
        assert remaining > 0
        extract(items[0]["text"])
        beat()
        return [{"index": 0, "written": True, "memories": 2, "edges": []}]

    monkeypatch.setattr(memory_worker, "write_text_items", text_items)
    monkeypatch.setattr(memory_worker, "write_items",
                        lambda writer, schema, items: [{"index": 0, "written": True, "statement_id": 1}])
    models = FakeModels({"carol owns the parser": []})
    with _pool_as_worker(monkeypatch):
        with db.connection() as conn:
            assert db.one(conn, "SELECT current_user AS u")["u"] == worker_role
            memory_worker.assert_narrowed(conn, environment="production")
        assert memory_worker.run_once(key_ring=key_ring, writer_connect=connect, models=models)
        assert memory_worker.run_once(key_ring=key_ring, writer_connect=connect, models=models)

    assert passwords == [WRITER_SECRET, WRITER_SECRET]
    assert models.keys == {PROVIDER_KEY}
    with db.connection() as conn:
        rows = {r["id"]: r for r in db.query(conn, "SELECT id, state, heartbeat_at FROM memory_ingests "
                                                   " WHERE project_id = %s", (project_id,))}
        stored = db.one(conn, "SELECT item_count FROM memory_spaces WHERE project_id = %s", (project_id,))
    assert rows[text.ingest_id]["state"] == "succeeded" and rows[text.ingest_id]["heartbeat_at"] is not None
    assert rows[edges.ingest_id]["state"] == "succeeded"
    assert stored["item_count"] == 3


# -- refusing to start -------------------------------------------------------------------


def test_a_worker_connected_as_the_control_plane_role_refuses_to_start_in_production(db_pool):  # noqa: ARG001
    with db.connection() as conn:
        with pytest.raises(RuntimeError, match="cp_memory_worker"):
            memory_worker.assert_narrowed(conn, environment="production")


def test_outside_production_it_warns_rather_than_refuses(db_pool, caplog):  # noqa: ARG001
    with db.connection() as conn:
        memory_worker.assert_narrowed(conn, environment="development")
    assert "refused in production" in caplog.text


def test_preflight_passes_a_correctly_granted_role(worker_role):
    report = preflight_mod.Report()
    with db.connection() as conn:
        preflight_mod._check_memory_worker_role(conn, report)
    assert report.ok and report.checks[0].ok, report.checks


def test_preflight_names_a_column_the_grant_has_not_caught_up_with(worker_role):
    with db.connection() as conn:
        conn.execute(psycopg.sql.SQL("REVOKE SELECT (heartbeat_at) ON memory_ingests FROM {}").format(
            psycopg.sql.Identifier(worker_role)))
        conn.commit()
        report = preflight_mod.Report()
        preflight_mod._check_memory_worker_role(conn, report)
    assert not report.ok and "memory_ingests.heartbeat_at" in report.failures[0].detail
