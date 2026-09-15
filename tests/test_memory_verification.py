"""The memory pipeline's verification checklist, where the slices left gaps (ADR-079).

`plans/active/memory-spaces.md` lists what must hold before memory ships. Most of it
is already asserted where each slice built it; these are the claims no single slice
owned, stated across the whole feature:

- **no superuser-owned function is reachable from any request role** in any schema
  memory adds -- the platform wrappers, the private registry, every space;
- **an ingest into one space never touches another**, although the writer holds
  `CREATE` on both: the worker's choice of schema is the only thing keeping them
  apart, so it is measured by what each space holds before and after;
- **a provider key appears nowhere in the control plane in clear** -- in no row of
  any table, in no log line, in no result -- after being set, used and refused;
- **the worker will not start in production without the egress proxy on loopback**.
"""

from __future__ import annotations

import logging

import httpx
import psycopg
import psycopg.sql
import pytest

from services.control_plane import (
    db,
    entitlements,
    maludb,
    maludb_memory,
    memory_ingest,
    memory_worker,
    model_providers,
    provider_keys,
    provisioning,
)
from tests.conftest import NODE_ADMIN_DSN, requires_db
from tests.test_maludb_enable import (  # noqa: F401 - fixtures, resolved by name
    _tenant_conn,
    admin_node_conn,
    requires_node,
    tenants,
)
from tests.test_maludb_jobs import _headers, worker_node  # noqa: F401 - fixture
from tests.test_memory_ingest import _active_space, _item, _models_for
from tests.test_memory_spaces import _stored_writer_password, _two_spaces_with_memories, _writer_dsn

pytestmark = requires_db

KEY = "sk-test-" + "V3r1fy" * 5 + "zzzz"  # noqa: S105 - test fixture, not a real key
MEMORY_SCHEMAS_SQL = ("SELECT nspname FROM pg_namespace "
                      "WHERE nspname IN ('maludb', 'maludb_private') OR nspname LIKE 'mem\\_%'")


# -- 1. no superuser-owned function reachable from a request role ----------------------


@requires_node
def test_no_request_role_can_execute_a_superuser_owned_definer_in_any_memory_schema(tenants, worker_node, key_ring):  # noqa: F811
    _, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "mver0001")
    with _tenant_conn(names.database, autocommit=True) as t:
        schemas = [r[0] for r in t.execute(MEMORY_SCHEMAS_SQL).fetchall()]
        roles = [r[0] for r in t.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                                         (list(maludb.customer_roles(names)),)).fetchall()]
        reachable = t.execute(
            "SELECT n.nspname || '.' || p.proname, r.rolname FROM pg_proc p "
            "  JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles o ON o.oid = p.proowner "
            "  CROSS JOIN unnest(%s::text[]) AS r(rolname) "
            " WHERE n.nspname = ANY(%s) AND p.prosecdef AND o.rolsuper "
            "   AND has_function_privilege(r.rolname, p.oid, 'EXECUTE')",
            (roles, schemas)).fetchall()
        definers = t.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_roles o ON o.oid = p.proowner WHERE n.nspname = ANY(%s) AND p.prosecdef AND o.rolsuper",
            (schemas,)).fetchone()[0]
    assert {"maludb", "mem_alpha", "mem_beta"} <= set(schemas), schemas
    assert len(roles) == len(maludb.customer_roles(names)), "a customer role was not provisioned; the check is empty"
    assert definers > 0, "the spaces hold no superuser definers at all; the query is not looking at them"
    assert reachable == [], f"request roles can execute superuser-owned definers: {reachable[:10]}"


# -- 2. an ingest into one space never touches another --------------------------------------


@requires_node
def test_an_ingest_into_one_space_changes_nothing_in_the_other(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "mver0002")
    with _tenant_conn(names.database, autocommit=True) as t:
        alpha_before = maludb_memory.residue(t, "mem_alpha")
        beta_before = maludb_memory.residue(t, "mem_beta")

    items = [_item(subject=f"isolated-{i}", embedding=(0.1 * i, 0.2, 0.3)) for i in range(1, 6)]
    with db.connection() as conn:
        queued = memory_ingest.enqueue(conn, project_id=project_id, space="alpha", items=items,
                                       allowed=entitlements.for_project(conn, project_id))
        conn.commit()
    password = _stored_writer_password(project_id, key_ring)

    def connect(ingest, pw):
        return psycopg.connect(_writer_dsn(NODE_ADMIN_DSN, names.database, names.memwriter, pw), autocommit=True)

    assert memory_worker.run_once(key_ring=key_ring, writer_connect=connect)
    with db.connection() as conn:
        assert db.one(conn, "SELECT written FROM memory_ingests WHERE id = %s", (queued.ingest_id,))["written"] == 5
    with _tenant_conn(names.database, autocommit=True) as t:
        alpha_after = maludb_memory.residue(t, "mem_alpha")
        beta_after = maludb_memory.residue(t, "mem_beta")
    assert password and alpha_after != alpha_before, "the ingest wrote nothing to alpha"
    assert beta_after == beta_before, f"an ingest into alpha changed beta: {beta_before} -> {beta_after}"


# -- 3. a provider key nowhere in clear ---------------------------------------------------------


class _NoWriter:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _rows_containing(conn, needles: list[str]) -> dict[str, int]:
    found = {}
    where = psycopg.sql.SQL(" OR ").join([psycopg.sql.SQL("strpos(x::text, %s) > 0")] * len(needles))
    for row in db.query(conn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"):
        table = row["tablename"]
        n = conn.execute(
            psycopg.sql.SQL("SELECT count(*) AS n FROM {} x WHERE {}").format(
                psycopg.sql.Identifier("public", table), where),
            needles).fetchone()["n"]
        if n:
            found[table] = n
    return found


def test_a_provider_key_set_used_and_refused_is_nowhere_in_the_control_plane_in_clear(
        client, placed_project, key_ring, caplog):
    project_id = placed_project("mver0003")
    _active_space(project_id)
    _models_for(project_id, extraction="openai", embedding="openai")
    manager = _headers(client, "mver0003")
    with caplog.at_level(logging.DEBUG):
        put = client.put("/v1/projects/mver0003/maludb/memory/provider-keys/openai", json={"api_key": KEY},
                         headers=manager)
        assert put.status_code == 200, put.text
        with db.connection() as conn:
            db.execute(conn, "UPDATE projects SET status = 'ACTIVE' WHERE id = %s", (project_id,))
            provisioning.store_credential(conn, project_id=project_id, credential_type="db_memwriter",
                                          role_name="mldb_mver0003_memwriter", secret="w-" + "x" * 20,
                                          key_ring=key_ring)
            kind, items = memory_ingest.validate({"items": [{"text": "carol owns the parser"}, {"text": "and more"}]})
            memory_ingest.enqueue(conn, project_id=project_id, space="bot", items=items,
                                  allowed=entitlements.resolve("free", {}), kind=kind)
            conn.commit()

        def refuse(_request):
            # A provider that echoes the key it was given, as OpenAI's 401 does.
            return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})

        models = model_providers.Models(transport=httpx.MockTransport(refuse), sleep=lambda _s: None)
        assert memory_worker.run_once(key_ring=key_ring, writer_connect=lambda i, p: _NoWriter(), models=models)
        listed = client.get("/v1/projects/mver0003/maludb/memory/provider-keys", headers=manager)
        audit = client.get("/v1/projects/mver0003/audit-events", headers=manager)

    with db.connection() as conn:
        results = db.one(conn, "SELECT state, results_json FROM memory_ingests WHERE project_id = %s",
                         (project_id,))
        clear = _rows_containing(conn, [KEY, KEY.encode().hex()])
        assert provider_keys.load_key(conn, project_id=project_id, provider="openai", key_ring=key_ring) == KEY
    assert results["state"] == "failed" and "[key]" in str(results["results_json"]), results
    assert clear == {}, f"the provider key is stored in clear in: {clear}"
    # A scan that has never found anything proves little: plant the key once, in a
    # column this test did not otherwise touch, and see it found.
    with db.connection() as conn:
        db.execute(conn, "UPDATE memory_spaces SET extraction_model = %s WHERE project_id = %s",
                   (KEY[:60], project_id))
        assert "memory_spaces" in _rows_containing(conn, [KEY[:60]]), "the clear-text scan cannot see a planted key"
        conn.rollback()
    for response in (put, listed, audit):
        assert KEY not in response.text
    assert KEY not in caplog.text, "a provider key reached a log line"


# -- 4. the worker refuses to start without the proxy ---------------------------------------------


@pytest.mark.parametrize("proxy", [None, "", "http://10.0.0.5:3128", "http://proxy.example.com:3128", "not a url"])
def test_the_worker_refuses_production_without_the_egress_proxy_on_loopback(proxy):
    with pytest.raises(SystemExit):
        memory_worker.require_egress_proxy(environment="production", proxy=proxy)


@pytest.mark.parametrize("proxy", ["http://127.0.0.1:3128", "http://[::1]:3128", "http://localhost:3128"])
def test_a_loopback_proxy_is_accepted(proxy):
    memory_worker.require_egress_proxy(environment="production", proxy=proxy)


def test_outside_production_no_proxy_is_required():
    memory_worker.require_egress_proxy(environment="development", proxy=None)
