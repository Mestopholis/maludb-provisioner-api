"""Enabling MaluDB vector compartments for a project (ADR-077, compartments slice 1).

Against real tenants. What matters is what a stub cannot show: that the owner
role the wrappers will run as holds exactly what the installed extension's vector
store needs and nothing more, that the grants were exercised rather than assumed,
that no customer role gained anything, and that `maludb` stays served while any
MaluDB feature is on.
"""

# Fixtures are imported from test_maludb_enable, which ruff reads as redefinition;
# the dump test runs pg_dump and pg_restore with fixed arguments.
# ruff: noqa: F811, S603, S607, E501

from __future__ import annotations

import subprocess

import psycopg
import pytest
from psycopg import sql

from services.control_plane import db, maludb, maludb_vectors
from tests.conftest import NODE_ADMIN_DSN
from tests.test_maludb_enable import (  # noqa: F401 - fixtures
    _audit,
    _control_plane,
    _enable,
    _rows,
    _tenant_conn,
    _tenant_connect,
    admin_node_conn,
    requires_node,
    tenants,
)


def _vectors(project_id, *, on: bool = True):
    with db.connection() as conn:
        action = maludb_vectors.enable if on else maludb_vectors.disable
        return action(conn, project_id=project_id, tenant_connect=_tenant_connect)


def _published(names) -> bool:
    return _rows(names.database, "SELECT count(*) FROM pg_db_role_setting WHERE setrole = %s::regrole",
                 (names.authenticator,))[0][0] > 0


def _flag(project_id) -> bool:
    with db.connection() as conn:
        return db.one(conn, "SELECT maludb_vectors_enabled FROM projects WHERE id = %s", (project_id,))[
            "maludb_vectors_enabled"]


@requires_node
def test_enabling_creates_a_narrow_owner_and_publishes_maludb(tenants):
    project_id, names, _ = tenants("vcena001")

    result = _vectors(project_id)

    assert result.changed and result.detail == "enabled"
    assert _flag(project_id)
    assert _audit(project_id, maludb_vectors.AUDIT_ENABLED) == 1
    assert _published(names)
    with _tenant_conn(names.database) as t:
        role = t.execute("SELECT rolcanlogin, rolsuper, rolbypassrls, rolinherit FROM pg_roles "
                         "WHERE rolname = %s", (names.vectors,)).fetchone()
        assert role == (False, False, False, False)
        reach = maludb_vectors.derive_reach(t)
        maludb_vectors.assert_definer(t, names, reach)
        assert set(reach.tables) <= {t for t in reach.tables if t.startswith(maludb_vectors.TABLE_PREFIXES)}
        # The probe left nothing behind.
        assert t.execute('SELECT count(*) FROM maludb_core."malu$vector_compartment"').fetchone()[0] == 0


@requires_node
def test_enabling_twice_changes_nothing(tenants):
    project_id, _, _ = tenants("vcena002")
    _vectors(project_id)
    again = _vectors(project_id)
    assert not again.changed and again.detail == "already enabled"
    assert _audit(project_id, maludb_vectors.AUDIT_ENABLED) == 1


@requires_node
def test_the_owner_can_run_the_vector_store_on_one_connection_past_generic_plans(tenants):
    """Slice 0 found a grant a first call did not need and a later one did."""
    project_id, names, _ = tenants("vcena003")
    _vectors(project_id)
    with _tenant_conn(names.database) as t:
        t.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(names.vectors)))
        t.execute("SET search_path = maludb_core, public")
        cid = t.execute("SELECT register_vector_compartment('ns','doc','about',3,'m','cosine')").fetchone()[0]
        for i in range(20):
            t.execute("SELECT register_vector_chunk(%s, 'x', %s::malu_vector, 'm')", (cid, f"[{i},1,1]"))
            t.execute("SELECT * FROM search_memory_exact('ns','doc','about','[1,1,1]'::malu_vector,5,NULL)").fetchall()
            t.execute("SELECT * FROM search_memory_filter('ns','doc','about','[1,1,1]'::malu_vector,"
                      "'{}'::jsonb,5,NULL)").fetchall()
        t.rollback()


@requires_node
def test_no_customer_role_gains_any_reach_into_the_vector_store(tenants):
    project_id, names, _ = tenants("vcena004")
    _vectors(project_id)
    with _tenant_conn(names.database) as t:
        for role in maludb.customer_roles(names):
            if not t.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                continue
            assert not t.execute("SELECT has_schema_privilege(%s, 'maludb_core', 'USAGE')", (role,)).fetchone()[0], role
            assert not t.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (role, names.vectors)).fetchone()[0], role


@requires_node
def test_a_grant_added_to_the_owner_by_hand_is_refused_on_the_next_enable(tenants):
    """A role widened outside the platform is found, not trusted."""
    project_id, names, _ = tenants("vcena005")
    _vectors(project_id)
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(sql.SQL('GRANT SELECT ON maludb_core."malu$svpor_statement" TO {}').format(
            sql.Identifier(names.vectors)))
    with pytest.raises(maludb_vectors.VectorsError, match="more than the vector store needs"):
        _vectors(project_id)


@requires_node
def test_a_grant_the_reading_missed_fails_enablement_and_leaves_nothing(tenants, monkeypatch):
    """The exercise is the control on the derivation: drop one function grant and
    enabling must fail before anything is recorded or published."""
    project_id, names, _ = tenants("vcena006")
    real = maludb_vectors.grant_definer

    def short(tenant_conn, names_, reach):
        reach.functions = {f for f in reach.functions if not f.startswith("maludb_core.vector_normalize")}
        return real(tenant_conn, names_, reach)

    monkeypatch.setattr(maludb_vectors, "grant_definer", short)
    monkeypatch.setattr(maludb_vectors, "assert_definer", lambda *_: None)
    with pytest.raises(maludb_vectors.VectorsError, match="still could not run"):
        _vectors(project_id)
    assert not _flag(project_id)
    assert not _published(names)
    with _tenant_conn(names.database) as t:
        assert not t.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (names.vectors,)).fetchone()


@requires_node
def test_a_reachable_table_outside_the_vector_store_is_refused(tenants, monkeypatch):
    project_id, names, _ = tenants("vcena007")
    monkeypatch.setitem(maludb_vectors.DIRECT_WRITES, "malu$svpor_statement", {"DELETE"})
    with pytest.raises(maludb_vectors.VectorsError, match="outside the vector store"):
        _vectors(project_id)
    assert not _flag(project_id)


@requires_node
def test_a_reachable_definer_function_is_refused(tenants, monkeypatch):
    """An invoker function runs with the owner's narrow rights; a definer one would not."""
    project_id, _, _ = tenants("vcena008")
    monkeypatch.setattr(maludb_vectors, "ENTRY_POINTS", ("retrieve_with_envelope",))
    with pytest.raises(maludb_vectors.VectorsError, match="runs as its definer"):
        _vectors(project_id)


@requires_node
def test_a_plan_without_the_entitlement_is_refused(tenants):
    project_id, names, _ = tenants("vcena009", plan_config={"maludb_vectors": False})
    with pytest.raises(maludb_vectors.VectorsError, match="maludb_vectors is false"):
        _vectors(project_id)
    with _tenant_conn(names.database) as t:
        assert not t.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (names.vectors,)).fetchone()


@requires_node
def test_a_customer_owned_maludb_schema_is_refused(tenants):
    project_id, names, _ = tenants("vcena010")
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(sql.SQL("CREATE SCHEMA maludb AUTHORIZATION {}").format(sql.Identifier(names.admin)))
    with pytest.raises(maludb.MaludbError, match="already exists"):
        _vectors(project_id)
    assert not _flag(project_id)


# -- maludb is served while any feature is on (ADR-077 decision 6) ----------


@requires_node
def test_disabling_vectors_keeps_maludb_served_while_the_graph_is_on(tenants):
    project_id, names, _ = tenants("vcexp001")
    _enable(project_id)
    _vectors(project_id)
    _vectors(project_id, on=False)
    assert _published(names), "turning vectors off unpublished the data-model graph"
    assert not _flag(project_id)


@requires_node
def test_disabling_the_graph_keeps_maludb_served_while_vectors_are_on(tenants):
    project_id, names, _ = tenants("vcexp002")
    _enable(project_id)
    _vectors(project_id)
    with db.connection() as conn:
        maludb.disable(conn, project_id=project_id, tenant_connect=_tenant_connect)
    assert _published(names), "turning the graph off unpublished the vector wrappers"


@requires_node
def test_turning_off_the_last_feature_withdraws_maludb_and_keeps_the_data(tenants):
    project_id, names, _ = tenants("vcexp003")
    _vectors(project_id)
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("SET search_path = maludb_core, public")
        t.execute("SELECT register_vector_compartment('ns','doc','about',3,'m','cosine')")
    result = _vectors(project_id, on=False)
    assert result.changed
    assert not _published(names)
    assert _rows(names.database, 'SELECT count(*) FROM maludb_core."malu$vector_compartment"')[0][0] == 1
    assert _audit(project_id, maludb_vectors.AUDIT_DISABLED) == 1


# -- moves ------------------------------------------------------------------


@requires_node
def test_a_dump_restored_onto_a_cluster_with_the_owner_keeps_its_grants(tenants, admin_node_conn):
    """Why a move creates the role before `pg_restore`: the dump carries the grants
    on maludb_core's vector tables, and a grant to an absent role is dropped."""
    project_id, names, _ = tenants("vcmov001")
    _vectors(project_id)
    copy = f"{names.database}_copy"
    admin_node_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(copy)))
    admin_node_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(copy)))
    try:
        info = psycopg.conninfo.conninfo_to_dict(NODE_ADMIN_DSN)
        dump = subprocess.run(
            ["pg_dump", "-Fc", psycopg.conninfo.make_conninfo(**{**info, "dbname": names.database})],
            capture_output=True, check=True).stdout
        subprocess.run(
            ["pg_restore", "-d", psycopg.conninfo.make_conninfo(**{**info, "dbname": copy})],
            input=dump, capture_output=True, check=False)
        with _tenant_conn(copy) as t:
            maludb_vectors.assert_definer(t, names, maludb_vectors.derive_reach(t))
            maludb_vectors.exercise_definer(t, names)
            t.rollback()
    finally:
        admin_node_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(copy)))


# -- the wrappers (compartments slice 2) --------------------------------------


def _as_service_role(database: str):
    conn = _tenant_conn(database, autocommit=True)
    conn.execute("SET ROLE service_role")
    return conn


def _limits(project_id, **limits):
    """Put the project on a plan with these vector limits and apply it to the node."""
    from psycopg.types.json import Jsonb

    from services.control_plane import entitlements, plan_apply, provisioning
    with db.connection() as conn:
        plan_id = db.one(conn, "SELECT plan_id FROM projects WHERE id = %s", (project_id,))["plan_id"]
        db.execute(conn, "UPDATE plans SET config_json = %s WHERE id = %s", (Jsonb({"limits": limits}), plan_id))
        conn.commit()
        allowed = entitlements.for_project(conn, project_id)
        ref = db.one(conn, "SELECT project_ref FROM projects WHERE id = %s", (project_id,))["project_ref"]
    with psycopg.connect(NODE_ADMIN_DSN) as admin:
        plan_apply.apply(admin, provisioning.TenantNames.for_ref(ref), allowed)


def _sqlstate(callable_) -> tuple[str, str | None]:
    with pytest.raises(psycopg.Error) as caught:
        callable_()
    return caught.value.sqlstate, caught.value.diag.message_hint


NS = ("docs", "page", "about")


@requires_node
def test_service_role_creates_inserts_searches_and_deletes(tenants):
    project_id, names, _ = tenants("vcwrp001")
    _vectors(project_id)
    with _as_service_role(names.database) as c:
        c.execute("SELECT maludb.vector_compartment_create(%s, %s, %s, 3)", NS)
        a = c.execute("SELECT maludb.vector_insert(%s, %s, %s, 'alpha', '[1,0,0]', '{\"lang\": \"en\"}')", NS).fetchone()[0]
        c.execute("SELECT maludb.vector_insert(%s, %s, %s, 'beta', '[0,1,0]', '{\"lang\": \"fr\"}')", NS)
        added = c.execute("SELECT maludb.vector_insert_many(%s, %s, %s, %s)",
                          (*NS, '[{"content": "gamma", "embedding": [0,0,1]}]')).fetchone()[0]
        assert added == 1

        hits = c.execute("SELECT content, metadata FROM maludb.vector_search(%s, %s, %s, '[1,0.1,0]', 2)", NS).fetchall()
        assert [h[0] for h in hits] == ["alpha", "beta"]
        assert hits[0][1] == {"lang": "en"}
        filtered = c.execute("SELECT content FROM maludb.vector_search(%s, %s, %s, '[1,0.1,0]', 5, "
                             "'{\"lang\": \"fr\"}')", NS).fetchall()
        assert [f[0] for f in filtered] == ["beta"]

        assert c.execute("SELECT maludb.vector_delete(%s, %s, %s, %s)", (*NS, [a])).fetchone()[0] == 1
        after = c.execute("SELECT content FROM maludb.vector_search(%s, %s, %s, '[1,0,0]', 5)", NS).fetchall()
        assert "alpha" not in [r[0] for r in after], "a deleted chunk is still searchable"
        assert c.execute("SELECT vector_count FROM maludb.vector_compartments()").fetchone()[0] == 2
        assert c.execute("SELECT maludb.vector_compartment_delete(%s, %s, %s)", NS).fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM maludb.vector_compartments()").fetchone()[0] == 0


@requires_node
def test_anon_and_authenticated_cannot_call_any_wrapper(tenants):
    project_id, names, _ = tenants("vcwrp002")
    _vectors(project_id)
    with _tenant_conn(names.database) as t:
        for role in ("anon", "authenticated"):
            for name in maludb_vectors.WRAPPERS:
                assert not t.execute(
                    "SELECT bool_or(has_function_privilege(%s, p.oid, 'EXECUTE')) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'maludb' AND p.proname = %s",
                    (role, name)).fetchone()[0], f"{role} can call {name}"
        # Nothing in maludb_private is callable by any customer role, and the
        # limits are readable by the owner alone.
        for role in maludb.customer_roles(names):
            if t.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                assert not t.execute("SELECT has_schema_privilege(%s, 'maludb_private', 'USAGE')",
                                     (role,)).fetchone()[0], role


@requires_node
def test_each_limit_accepts_at_its_value_and_refuses_past_it(tenants):
    project_id, names, _ = tenants("vcwrp003")
    _vectors(project_id)
    _limits(project_id, vector_max_count=2, vector_max_dimension=4, vector_max_compartments=1)
    with _as_service_role(names.database) as c:
        state, hint = _sqlstate(lambda: c.execute("SELECT maludb.vector_compartment_create('a','b','c',5)"))
        assert (state, hint) == ("PT403", "vector_max_dimension")
        c.execute("SELECT maludb.vector_compartment_create('a','b','c',4)")
        state, hint = _sqlstate(lambda: c.execute("SELECT maludb.vector_compartment_create('a','b','d',4)"))
        assert (state, hint) == ("PT403", "vector_max_compartments")
        c.execute("SELECT maludb.vector_insert('a','b','c','one','[1,0,0,0]')")
        c.execute("SELECT maludb.vector_insert('a','b','c','two','[0,1,0,0]')")
        state, hint = _sqlstate(lambda: c.execute("SELECT maludb.vector_insert('a','b','c','three','[0,0,1,0]')"))
        assert (state, hint) == ("PT403", "vector_max_count")
        state, hint = _sqlstate(lambda: c.execute(
            "SELECT maludb.vector_insert_many('a','b','c', '[{\"content\":\"x\",\"embedding\":[1,1,1,1]}]')"))
        assert hint == "vector_max_count"
    # A plan change raises it, and the same insert now fits.
    _limits(project_id, vector_max_count=3, vector_max_dimension=4, vector_max_compartments=1)
    with _as_service_role(names.database) as c:
        c.execute("SELECT maludb.vector_insert('a','b','c','three','[0,0,1,0]')")


@requires_node
def test_refusals_carry_stable_codes(tenants):
    project_id, names, _ = tenants("vcwrp004")
    _vectors(project_id)
    with _as_service_role(names.database) as c:
        assert _sqlstate(lambda: c.execute(
            "SELECT * FROM maludb.vector_search('no','such','thing','[1,2,3]')"))[0] == "PT404"
        c.execute("SELECT maludb.vector_compartment_create('a','b','c',3)")
        assert _sqlstate(lambda: c.execute("SELECT maludb.vector_compartment_create('a','b','c',4)"))[0] == "PT409"
        assert _sqlstate(lambda: c.execute(
            "SELECT maludb.vector_compartment_create('a','b','d',3,'hamming')"))[0] == "PT400"
        assert _sqlstate(lambda: c.execute(
            "SELECT * FROM maludb.vector_search('a','b','c','[1,2,3]', 0)"))[0] == "PT400"
        # The same definition again is not a conflict: it returns the compartment.
        c.execute("SELECT maludb.vector_compartment_create('a','b','c',3)")


@requires_node
def test_a_customer_table_named_like_the_store_does_not_shadow_it(tenants):
    """The wrappers pin maludb_core ahead of public; a customer owns public."""
    project_id, names, _ = tenants("vcwrp005")
    _vectors(project_id)
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(names.admin)))
        t.execute('CREATE TABLE public."malu$vector_compartment" (compartment_id bigint, namespace text)')
        t.execute("INSERT INTO public.\"malu$vector_compartment\" VALUES (999, 'a')")
    with _as_service_role(names.database) as c:
        c.execute("SELECT maludb.vector_compartment_create('a','b','c',3)")
        assert c.execute("SELECT count(*) FROM maludb.vector_compartments()").fetchone()[0] == 1
    assert _rows(names.database, 'SELECT count(*) FROM maludb_core."malu$vector_compartment"')[0][0] == 1


@requires_node
def test_a_dump_restored_with_the_owner_keeps_working_wrappers(tenants, admin_node_conn):
    project_id, names, _ = tenants("vcwrp006")
    _vectors(project_id)
    with _as_service_role(names.database) as c:
        c.execute("SELECT maludb.vector_compartment_create('a','b','c',3)")
        c.execute("SELECT maludb.vector_insert('a','b','c','kept','[1,2,3]')")
    copy = f"{names.database}_copy"
    admin_node_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(copy)))
    admin_node_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(copy)))
    try:
        from services.control_plane import extension_data
        info = psycopg.conninfo.conninfo_to_dict(NODE_ADMIN_DSN)
        dump = subprocess.run(["pg_dump", "-Fc", psycopg.conninfo.make_conninfo(**{**info, "dbname": names.database})],
                              capture_output=True, check=True).stdout
        subprocess.run(["pg_restore", "-d", psycopg.conninfo.make_conninfo(**{**info, "dbname": copy})],
                       input=dump, capture_output=True, check=False)
        with _tenant_conn(names.database) as s, _tenant_conn(copy) as d:
            extension_data.carry(extension_data.ConnectionSource(s), d)
        with _tenant_conn(copy) as t:
            owners = {r[0] for r in t.execute(
                "SELECT pg_get_userbyid(p.proowner) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname IN ('maludb', 'maludb_private')").fetchall()}
            assert owners == {names.vectors}, owners
        with _as_service_role(copy) as c:
            assert c.execute("SELECT content FROM maludb.vector_search('a','b','c','[1,2,3]')").fetchall() == [("kept",)]
    finally:
        admin_node_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(copy)))


# -- re-verification after an extension change (compartments slice 3) --------


@requires_node
def test_reverify_leaves_a_tenant_without_vectors_untouched(tenants):
    _, names, _ = tenants("vcrev001")
    with _tenant_conn(names.database) as t:
        assert maludb_vectors.reverify(t, names) is False
        assert not t.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (names.vectors,)).fetchone()
        t.rollback()


@requires_node
def test_reverify_narrows_a_grant_the_extension_no_longer_needs(tenants):
    """A release that stops calling a function leaves the owner's grant on it
    behind; an upgrade must narrow that, not refuse the tenant for holding it."""
    project_id, names, _ = tenants("vcrev002")
    _vectors(project_id)
    with _tenant_conn(names.database) as t:
        t.execute(sql.SQL("GRANT EXECUTE ON FUNCTION maludb_core.text_search(text, text[], integer) TO {}").format(
            sql.Identifier(names.vectors)))
        # The control: without narrowing, the check refuses this owner.
        with pytest.raises(maludb_vectors.VectorsError, match="more than the vector store needs"):
            t.execute("SAVEPOINT s")
            try:
                maludb_vectors.assert_definer(t, names, maludb_vectors.derive_reach(t))
            finally:
                t.execute("ROLLBACK TO SAVEPOINT s")
        assert maludb_vectors.reverify(t, names) is True
        assert not t.execute("SELECT has_function_privilege(%s, 'maludb_core.text_search(text, text[], integer)', "
                             "'EXECUTE')", (names.vectors,)).fetchone()[0]
        t.commit()


@requires_node
def test_reverify_passes_for_a_tenant_at_its_limit_and_restores_the_limits(tenants):
    """The probe must not fail an upgrade because the project is full or its plan
    was cut to zero -- and must leave the real limits exactly as they were."""
    project_id, names, _ = tenants("vcrev003")
    _vectors(project_id)
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("UPDATE maludb_private.vector_limits SET max_count = 0, max_dimension = 0, max_compartments = 0")
    with _tenant_conn(names.database) as t:
        assert maludb_vectors.reverify(t, names) is True
        t.commit()
    assert _rows(names.database, "SELECT max_count, max_dimension, max_compartments "
                                 "FROM maludb_private.vector_limits")[0] == (0, 0, 0)
    assert _rows(names.database, 'SELECT count(*) FROM maludb_core."malu$vector_compartment"')[0][0] == 0


def _memory_space_compartment(database: str, space: str, names: tuple[str, str, str], contents: list[str]) -> None:
    """A compartment as a MaluDB memory schema writes one: its own owner_schema.

    Written the way the extension does it for a space (ADR-079) -- the owner is
    `current_schema()` -- over the platform's superuser connection, since nothing
    shipped writes one yet.
    """
    with _tenant_conn(database, autocommit=True) as t:
        t.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(space)))
        t.execute(sql.SQL("SET search_path = {}, maludb_core, public").format(sql.Identifier(space)))
        cid = t.execute("SELECT register_vector_compartment(%s, %s, %s, 3, 'space', 'cosine')", names).fetchone()[0]
        for body in contents:
            t.execute("SELECT register_vector_chunk(%s, %s, '[1,0,0]'::malu_vector, 'space')", (cid, body))


@requires_node
def test_the_wrappers_see_nothing_a_memory_space_stores(tenants):
    """The fence: `malu$vector_compartment` also holds memory spaces' compartments.

    Found by the memory pipeline spike: unfenced, the customer API listed a space's
    compartments, searched and deleted them, and counted their vectors against the
    plan. Upstream's name-based search takes the first same-named compartment
    whoever owns it, so the colliding name is the case that matters most.
    """
    project_id, names, _ = tenants("vcfen001")
    _vectors(project_id)
    _limits(project_id, vector_max_count=2, vector_max_dimension=4, vector_max_compartments=1)
    database = names.database
    # Before the customer's own compartment exists, so upstream's LIMIT 1 would
    # find the space's first.
    _memory_space_compartment(database, "space_a", NS, ["space-secret"] * 5)
    _memory_space_compartment(database, "space_a", ("mem", "only", "space"), ["space-only"] * 5)

    with _as_service_role(database) as c:
        assert c.execute("SELECT count(*) FROM maludb.vector_compartments()").fetchone()[0] == 0
        # Nothing of the space's is reachable by name.
        assert _sqlstate(lambda: c.execute("SELECT * FROM maludb.vector_search('mem','only','space','[1,0,0]')"))[0] == "PT404"
        assert _sqlstate(lambda: c.execute("SELECT maludb.vector_compartment_delete('mem','only','space')"))[0] == "PT404"
        assert _sqlstate(lambda: c.execute("SELECT * FROM maludb.vector_explain('mem','only','space')"))[0] == "PT404"

        # The customer's compartment of the same name: its own, within its own limits
        # (one compartment, two vectors) although the space holds ten.
        c.execute("SELECT maludb.vector_compartment_create(%s, %s, %s, 3)", NS)
        c.execute("SELECT maludb.vector_insert(%s, %s, %s, 'mine', '[1,0,0]')", NS)
        c.execute("SELECT maludb.vector_insert(%s, %s, %s, 'also-mine', '[0.9,0.1,0]')", NS)
        hits = [r[0] for r in c.execute("SELECT content FROM maludb.vector_search(%s, %s, %s, '[1,0,0]', 10)", NS)]
        assert hits == ["mine", "also-mine"]
        listed = c.execute("SELECT namespace, subject, verb, vector_count FROM maludb.vector_compartments()").fetchall()
        assert listed == [(*NS, 2)]
        assert c.execute("SELECT vector_count FROM maludb.vector_explain(%s, %s, %s)", NS).fetchone()[0] == 2
        assert c.execute("SELECT maludb.vector_compartment_delete(%s, %s, %s)", NS).fetchone()[0] == 2

    # And the space's rows are all still there.
    assert _rows(database, "SELECT count(*) FROM maludb_core.\"malu$vector_chunk\" ch "
                           "JOIN maludb_core.\"malu$vector_compartment\" co USING (compartment_id) "
                           "WHERE co.owner_schema = 'space_a'")[0][0] == 10
