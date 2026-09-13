"""Carrying maludb_core's vector data beside a dump (ADR-077 decision 8, compartments slice 0).

`pg_dump` carries no row of any table `maludb_core` owns, so a move or a restore
without `extension_data.carry` leaves a tenant's vector compartments behind with
no error. Each property is asserted against real tenants provisioned the
platform's way, with its control beside it: the same target, without the carry,
has nothing to search.
"""

from __future__ import annotations

import subprocess

import psycopg
import pytest
from psycopg import sql

from services.control_plane import extension_data, provisioning, tenant_bootstrap
from tests.test_provisioning import ADMIN_DSN, _tenant_admin_dsn, requires_maludb_core

pytestmark = [requires_maludb_core]

SOURCE, TARGET = "vcd00001", "vcd00002"
QUERY = "[1,2,2]"


def _drop(admin, ref: str) -> None:
    names = provisioning.TenantNames.for_ref(ref)
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(names.database)))
    for (role,) in admin.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                                 (f"mldb\\_{ref}\\_%",)).fetchall():
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _provision(admin, ref: str) -> str:
    _drop(admin, ref)
    names = provisioning.TenantNames.for_ref(ref)
    passwords = {k: provisioning.generate_password()
                 for k in ("authenticator", "auth", "admin", "executor", "client", "storage")}
    with psycopg.connect(ADMIN_DSN) as conn:
        provisioning.ensure_shared_roles(conn)
        provisioning.create_roles(conn, names, passwords=passwords, connection_limits={})
        provisioning.create_storage_role(conn, names, password=passwords["storage"])
        conn.commit()
        provisioning.create_database(conn, names, owner=admin.info.user)
        provisioning.lock_down_database(conn, names)
        provisioning.grant_storage_connect(conn, names)
        conn.commit()
    with psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True) as t:
        provisioning.install_extension(t, pins=dict(t.execute(
            "SELECT name, default_version FROM pg_available_extensions "
            "WHERE name IN ('vector', 'maludb_core')").fetchall()))
        tenant_bootstrap.apply(t)
    return names.database


def _tenant(database: str, **kw) -> psycopg.Connection:
    conn = psycopg.connect(_tenant_admin_dsn(database), **kw)
    # Upstream calls its own functions unqualified (slice 0, finding 4).
    conn.execute("SET search_path = maludb_core, public")
    return conn


def _search(database: str) -> list[tuple]:
    with _tenant(database, autocommit=True) as t:
        return t.execute(
            "SELECT chunk_id, source_text FROM maludb_core.search_memory_exact("
            "'ns', 'doc', 'about', %s::maludb_core.malu_vector, 5, NULL)", (QUERY,)
        ).fetchall()


@pytest.fixture
def tenants():
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        source, target = _provision(admin, SOURCE), _provision(admin, TARGET)
        with _tenant(source, autocommit=True) as t:
            cid = t.execute("SELECT register_vector_compartment('ns', 'doc', 'about', 3, 'm', 'cosine')").fetchone()[0]
            for body, emb in (("hello", "[1,2,3]"), ("world", "[3,2,1]"), ("gone", "[1,1,1]")):
                t.execute("SELECT register_vector_chunk(%s, %s, %s::malu_vector, 'm')", (cid, body, emb))
            # A tombstone, so a table with a foreign key into chunks is carried too.
            # (Exact search does not filter tombstones -- only the ANN path does --
            # so "gone" still answers searches on both sides; slice 0, finding 9.)
            t.execute('INSERT INTO "malu$vector_tombstone" (chunk_id) '
                      'SELECT chunk_id FROM "malu$vector_chunk" WHERE source_text = %s', ("gone",))
        try:
            yield source, target
        finally:
            _drop(admin, SOURCE)
            _drop(admin, TARGET)


def test_pg_dump_leaves_the_vector_store_behind(tenants):
    """The finding the carry exists for, kept as a test: if an upstream release
    registers these tables for dumping, this fails and the carry can retire."""
    source, _ = tenants
    dump = subprocess.run(["pg_dump", "--data-only", _tenant_admin_dsn(source)],  # noqa: S603, S607
                          capture_output=True, text=True, check=True).stdout
    assert "hello" not in dump
    assert 'COPY maludb_core."malu$vector_chunk"' not in dump


def test_every_vector_table_is_carried_or_named_as_left_behind(tenants):
    """An upstream release adding a vector table must not be left out silently."""
    source, _ = tenants
    with _tenant(source) as t:
        found = {r[0] for r in t.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'maludb_core' AND c.relkind = 'r' "
            "AND (c.relname LIKE 'malu$vector\\_%' OR c.relname LIKE 'malu$ann\\_%')").fetchall()}
    assert found == set(extension_data.CARRIED_TABLES) | set(extension_data.NOT_CARRIED)


def test_a_carried_tenant_searches_exactly_as_its_source(tenants):
    source, target = tenants
    # The control: loaded without the carry, the target has no compartment at all.
    with pytest.raises(psycopg.errors.NoDataFound):
        _search(target)

    with _tenant(source) as s, _tenant(target) as d:
        report = extension_data.carry(extension_data.ConnectionSource(s), d)

    assert report.rows["malu$vector_chunk"] == 3
    assert report.rows["malu$vector_tombstone"] == 1
    assert _search(target) == _search(source)
    # The sequences moved past what was carried: a new chunk takes a fresh id.
    with _tenant(target, autocommit=True) as t:
        new = t.execute("SELECT register_vector_chunk(1, 'after', '[2,2,2]'::malu_vector, 'm')").fetchone()[0]
        highest = t.execute('SELECT max(chunk_id) FROM "malu$vector_chunk" WHERE source_text <> %s',
                            ("after",)).fetchone()[0]
    assert new > highest


def test_a_second_carry_into_the_same_target_is_refused(tenants):
    source, target = tenants
    with _tenant(source) as s, _tenant(target) as d:
        extension_data.carry(extension_data.ConnectionSource(s), d)
    with _tenant(source) as s, _tenant(target) as d, \
            pytest.raises(extension_data.CarryError, match="already holds"):
        extension_data.carry(extension_data.ConnectionSource(s), d)


def test_a_source_column_the_target_lacks_is_refused(tenants):
    """A restore can read a tenant from before an extension upgrade. Dropping a
    column's data to make the copy fit is the silent loss this module prevents."""
    source, target = tenants
    with _tenant(source, autocommit=True) as t:
        t.execute('ALTER TABLE "malu$vector_chunk" ADD COLUMN spike_only text')
    with _tenant(source) as s, _tenant(target) as d, \
            pytest.raises(extension_data.CarryError, match="spike_only"):
        extension_data.carry(extension_data.ConnectionSource(s), d)
    with _tenant(target) as d:
        assert d.execute('SELECT count(*) FROM "malu$vector_chunk"').fetchone()[0] == 0


def test_links_outside_the_carried_tables_are_found_from_the_catalogue(tenants):
    _, target = tenants
    with _tenant(target) as d:
        links = extension_data._links_outside(d, list(extension_data.CARRIED_TABLES))  # noqa: SLF001
    assert {(table, column) for table, column, _ in links} >= {
        ("malu$vector_chunk", "statement_id"),
        ("malu$vector_subject", "svpor_subject_id"),
        ("malu$vector_verb", "svpor_verb_id"),
    }


def test_a_row_linked_outside_the_carried_tables_is_refused(tenants):
    source, target = tenants

    class Linked(extension_data.ConnectionSource):
        def count(self, table, where=None):
            if where and "statement_id" in where:
                return 1
            return super().count(table, where)

    with _tenant(source) as s, _tenant(target) as d, \
            pytest.raises(extension_data.CarryError, match="statement_id"):
        extension_data.carry(Linked(s), d)


def test_a_count_that_disagrees_rolls_the_whole_carry_back(tenants):
    source, target = tenants

    class Short(extension_data.ConnectionSource):
        def count(self, table, where=None):
            n = super().count(table, where)
            return n + 1 if table == "malu$vector_chunk" and not where else n

    with _tenant(source) as s, _tenant(target) as d, \
            pytest.raises(extension_data.CarryError, match="carried 3"):
        extension_data.carry(Short(s), d)
    # Subjects, verbs and the compartment were copied before the mismatch; none of
    # them may remain.
    with _tenant(target) as d:
        for table in extension_data.CARRIED_TABLES:
            assert d.execute(sql.SQL("SELECT count(*) FROM {}").format(
                sql.Identifier("maludb_core", table))).fetchone()[0] == 0, table
