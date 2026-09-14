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


# -- the writer (memory slice 2b) ----------------------------------------------


def _writer_dsn(admin_dsn: str, database: str, role: str, password: str) -> str:
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    info.update(dbname=database, user=role, password=password)
    return psycopg.conninfo.make_conninfo(**info)


def writer_ingests(dsn: str, space: str, subject: str) -> int:
    """What the memory worker will do: upload a document and ingest an embedded edge, as the writer."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as w:
        doc = w.execute(sql.SQL("SELECT {}.maludb_upload_document(p_title => 'src', p_content_text => %s, "
                                "p_source_type => 'note')").format(sql.Identifier(space)),
                        (f"{subject} owns the parser",)).fetchone()[0]
        return w.execute(sql.SQL(
            "SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', p_source_id => %s, "
            "p_subject_text => %s, p_verb_text => 'owns', "
            "p_embedding => '[0.1,0.2,0.3]'::maludb_core.malu_vector, p_embedding_model => 'stub-3')"
        ).format(sql.Identifier(space)), (doc, subject)).fetchone()[0]


def _stored_writer_password(project_id, key_ring) -> str:
    from services.control_plane import provisioning

    with db.connection() as conn:
        return provisioning.load_credential(conn, project_id=project_id, credential_type="db_memwriter",
                                            key_ring=key_ring)


@requires_node
def test_the_writer_holds_exactly_what_slice_1_measured_and_can_write(tenants, worker_node, key_ring):  # noqa: F811
    from tests.conftest import NODE_ADMIN_DSN

    project_id, names, _ = tenants("mswrt001", plan_config={"limits": {"memory_max_spaces": 2}})
    worker_node()
    _request(project_id, "bot")
    _, queued = _request(project_id, "other")
    provisioner.run_maludb_once(key_ring=key_ring)
    assert _job(queued.job_id)["state"] == "succeeded"

    with _tenant_conn(names.database, autocommit=True) as t:
        role = t.execute("SELECT rolcanlogin, rolsuper, rolbypassrls, rolinherit, rolcreaterole, rolcreatedb, "
                         "rolreplication FROM pg_roles WHERE rolname = %s", (names.memwriter,)).fetchone()
        member_of = t.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                              "WHERE r.rolname = %s", (names.memwriter,)).fetchone()[0]
        granted = {
            space: t.execute(
                "SELECT has_schema_privilege(%s, %s, 'CREATE'), "
                "       (SELECT array_agg(p.proname ORDER BY p.proname) FROM pg_proc p "
                "          JOIN pg_namespace n ON n.oid = p.pronamespace "
                "         WHERE n.nspname = %s AND has_function_privilege(%s, p.oid, 'EXECUTE'))",
                (names.memwriter, space, space, names.memwriter)).fetchone()
            for space in ("mem_bot", "mem_other")
        }
        direct_tables = t.execute(
            "SELECT count(*) FROM pg_class c WHERE c.relnamespace = 'maludb_core'::regnamespace AND c.relkind = 'r' "
            "AND has_table_privilege(%s, c.oid, 'SELECT')", (names.memwriter,)).fetchone()[0]
    assert role == (True, False, False, False, False, False, False)
    assert member_of == 0, "the writer is a member of nothing, maludb_memory_executor included"
    for space, (create, functions) in granted.items():
        assert create, space
        assert sorted(functions) == sorted(maludb_memory.WRITER_FACADES), (space, functions)
    assert direct_tables == 0, "the writer touches no extension table directly"

    password = _stored_writer_password(project_id, key_ring)
    assert writer_ingests(_writer_dsn(NODE_ADMIN_DSN, names.database, names.memwriter, password), "mem_bot", "alpha")


@requires_node
def test_a_writer_password_that_was_never_stored_is_replaced_not_stranded(tenants, worker_node, key_ring):  # noqa: F811
    from tests.conftest import NODE_ADMIN_DSN

    project_id, names, _ = tenants("mswrt002", plan_config={"limits": {"memory_max_spaces": 2}})
    worker_node()
    _request(project_id, "first")
    provisioner.run_maludb_once(key_ring=key_ring)
    # The run that created the role died before storing its password.
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM project_credentials WHERE project_id = %s AND credential_type = 'db_memwriter'",
                   (project_id,))
        conn.commit()

    _request(project_id, "second")
    provisioner.run_maludb_once(key_ring=key_ring)
    password = _stored_writer_password(project_id, key_ring)
    assert writer_ingests(_writer_dsn(NODE_ADMIN_DSN, names.database, names.memwriter, password), "mem_second", "beta")


@requires_node
def test_an_unreviewed_space_first_definer_refuses_the_space(tenants, worker_node, key_ring, monkeypatch):  # noqa: F811
    """The writer holds CREATE on the space; a definer searching the space could
    resolve an object the writer made. One is reviewed at 0.105.0; unreview it."""
    project_id, _, _ = tenants("mswrt003")
    worker_node()
    monkeypatch.setattr(maludb_memory, "REVIEWED_SPACE_FIRST_DEFINERS", {})
    _request(project_id, "bot")
    provisioner.run_maludb_once(key_ring=key_ring)
    (space,) = _spaces(project_id)
    assert space["state"] == "failed"
    assert "have not been reviewed" in space["detail"] and "maludb_document_graph_backfill" in space["detail"]


@requires_node
def test_an_upgrade_re_verifies_every_space_and_re_grants_the_writer(tenants, worker_node, key_ring):  # noqa: F811
    project_id, names, _ = tenants("mswrt004", plan_config={"limits": {"memory_max_spaces": 2}})
    worker_node()
    _request(project_id, "bot")
    provisioner.run_maludb_once(key_ring=key_ring)
    with _tenant_conn(names.database) as t:
        # A release that rebuilt the facades would drop the writer's grants; stand in for it.
        t.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA mem_bot FROM " + f'"{names.memwriter}"')
        # A customer-created `mem_` schema is not the platform's and is left alone.
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute("CREATE SCHEMA mem_mine")
        t.execute("RESET ROLE")
        assert maludb_memory.reverify_spaces(t, names) == ["mem_bot"]
        executable = t.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'mem_bot' AND has_function_privilege(%s, p.oid, 'EXECUTE')",
            (names.memwriter,)).fetchone()[0]
        t.rollback()
    assert executable == len(maludb_memory.WRITER_FACADES)


# -- search (memory slice 3) ----------------------------------------------------


def _ingest(database: str, schema: str, subject: str, embedding: str) -> int:
    """An embedded edge written the way the platform will, over its own connection."""
    from psycopg import sql

    with _tenant_conn(database, autocommit=True) as t:
        doc = t.execute(sql.SQL("SELECT {}.maludb_upload_document(p_title => 'src', p_content_text => %s, "
                                "p_source_type => 'note')").format(sql.Identifier(schema)),
                        (f"{subject} {embedding}",)).fetchone()[0]
        return t.execute(sql.SQL(
            "SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', p_source_id => %s, "
            "p_subject_text => %s, p_verb_text => 'owns', p_embedding => %s::maludb_core.malu_vector, "
            "p_embedding_model => 'stub-3', p_source_span => %s)"
        ).format(sql.Identifier(schema)), (doc, subject, embedding, f"{subject} span {embedding}")).fetchone()[0]


def _two_spaces_with_memories(tenants, worker_node, key_ring, ref):  # noqa: F811
    import random

    project_id, names, _ = tenants(ref, plan_config={"limits": {"memory_max_spaces": 2}})
    worker_node()
    _request(project_id, "alpha")
    _request(project_id, "beta")
    provisioner.run_maludb_once(key_ring=key_ring)
    assert {s["state"] for s in _spaces(project_id)} == {"active"}
    rng = random.Random(7)  # noqa: S311 - reproducible test embeddings, not a secret
    for schema in ("mem_alpha", "mem_beta"):
        for i in range(12):
            subject = ("carol", "dave")[i % 2]
            embedding = "[" + ",".join(f"{rng.uniform(-1, 1):.6f}" for _ in range(3)) + "]"
            _ingest(names.database, schema, subject, embedding)
    return project_id, names


@requires_node
def test_search_returns_what_the_facade_returns(tenants, worker_node, key_ring):  # noqa: F811
    """The wrapper re-implements upstream's query, so parity is proven on the pinned version."""
    _, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "mssrc001")
    queries = [("[0.1,0.2,0.3]", "carol", None), ("[-0.5,0.4,0.9]", "dave", None),
               ("[0.9,-0.1,0.0]", None, "owns"), ("[0.3,0.3,-0.3]", "carol", "owns")]
    with _tenant_conn(names.database, autocommit=True) as t:
        for query, subject, verb in queries:
            facade = t.execute(
                "SELECT chunk_id, statement_id, document_id, source_text, round(distance::numeric, 6), rank_no, "
                "       subject_name, verb_name "
                "  FROM mem_alpha.maludb_memory_search(%s::maludb_core.malu_vector, %s, %s, 'default', 10)",
                (query, subject, verb)).fetchall()
            t.execute("SET ROLE service_role")
            wrapper = t.execute(
                "SELECT chunk_id, statement_id, document_id, content, round(distance::numeric, 6), rank, "
                "       subject_name, verb_name FROM maludb.memory_search('alpha', %s::vector, %s, %s, 'default', 10)",
                (query, subject, verb)).fetchall()
            t.execute("RESET ROLE")
            assert facade, (query, subject, verb)
            assert wrapper == facade, (query, subject, verb)


@requires_node
def test_search_is_fenced_to_its_space_and_refuses_everyone_but_service_role(tenants, worker_node, key_ring):  # noqa: F811
    import psycopg

    _, names = _two_spaces_with_memories(tenants, worker_node, key_ring, "mssrc002")
    with _tenant_conn(names.database, autocommit=True) as t:
        alpha_chunks = {r[0] for r in t.execute(
            "SELECT ch.chunk_id FROM maludb_core.\"malu$vector_chunk\" ch "
            "JOIN maludb_core.\"malu$vector_compartment\" c USING (compartment_id) "
            "WHERE c.owner_schema = 'mem_alpha'").fetchall()}
        t.execute("SET ROLE service_role")
        beta = {r[0] for r in t.execute(
            "SELECT chunk_id FROM maludb.memory_search('beta', '[0.1,0.2,0.3]'::vector, 'carol', NULL, 'default', 1000)"
        ).fetchall()}
        assert beta and not beta & alpha_chunks, "a search of beta returned alpha's memories"
        with pytest.raises(psycopg.Error) as unknown:
            t.execute("SELECT * FROM maludb.memory_search('gamma', '[0.1,0.2,0.3]'::vector, 'carol')")
        assert unknown.value.sqlstate == "PT404"
        t.execute("RESET ROLE")
        with pytest.raises(psycopg.Error) as neither:
            t.execute("SET ROLE service_role")
            t.execute("SELECT * FROM maludb.memory_search('alpha', '[0.1,0.2,0.3]'::vector)")
        assert neither.value.sqlstate == "PT400"
        t.execute("RESET ROLE")
        for role in ("anon", "authenticated", names.admin, names.authenticator):
            assert not t.execute(
                "SELECT has_function_privilege(%s, 'maludb.memory_search(text,vector,text,text,text,integer,text)', "
                "'EXECUTE')", (role,)).fetchone()[0], role


@requires_node
def test_the_reader_holds_select_only_and_the_schema_is_published(tenants, worker_node, key_ring):  # noqa: F811
    from services.control_plane import maludb_vectors

    project_id, names, _ = tenants("mssrc003")
    worker_node()
    _request(project_id, "alpha")
    provisioner.run_maludb_once(key_ring=key_ring)
    with _tenant_conn(names.database, autocommit=True) as t:
        writes = t.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE r.rolname = %s AND a.privilege_type <> 'SELECT'", (names.memreader,)).fetchone()[0]
        role = t.execute("SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                         (names.memreader,)).fetchone()
        published = t.execute(
            "SELECT setconfig FROM pg_db_role_setting WHERE setrole = %s::regrole", (names.authenticator,)
        ).fetchone()
        reach = maludb_vectors.derive_reach(t, entry_points=maludb_memory.READER_ENTRY_POINTS,
                                            direct=maludb_memory.READER_READS)
    assert writes == 0 and role == (False, False, False)
    assert set(reach.tables) >= set(maludb_memory.READER_READS)
    assert published and any("maludb" in item for item in published[0])
    with db.connection() as conn:
        assert db.one(conn, "SELECT maludb_memory_enabled FROM projects WHERE id = %s", (project_id,))[
            "maludb_memory_enabled"]
