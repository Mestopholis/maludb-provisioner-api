"""Enabling MaluDB vector compartments for a project (ADR-077, compartments slice 1).

Against real tenants. What matters is what a stub cannot show: that the owner
role the wrappers will run as holds exactly what the installed extension's vector
store needs and nothing more, that the grants were exercised rather than assumed,
that no customer role gained anything, and that `maludb` stays served while any
MaluDB feature is on.
"""

# Fixtures are imported from test_maludb_enable, which ruff reads as redefinition;
# the dump test runs pg_dump and pg_restore with fixed arguments.
# ruff: noqa: F811, S603, S607

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
