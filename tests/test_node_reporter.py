"""A node reports its own health as a role that can do nothing else (ADR-080).

Found by the deployment rehearsal: placement needs a fresh health report and
nothing in the runbook could send one from a node. What is held here:

- **the reporter's role reads nothing and writes nothing** but the one function;
- **it reports for its own node only**, decided by the login role in the database,
  and an unmapped role is refused rather than reporting for nobody silently;
- **a report merges**: what realtime-check and backup-check recorded survives it
  (the old `node health` erased both);
- **the time is the database's**, and a reported node becomes placeable;
- **the grant refuses roles that must not report**: a gateway role, a role that
  cannot log in, a role already reporting for another node;
- **the reporter stays silent while local PostgreSQL does not answer**.
"""

from __future__ import annotations

import argparse
import json

import psycopg
import pytest

from services.control_plane import db, gateway_grants, manage, node_reporter, nodes
from tests.conftest import agree_with_pins, requires_db

REPORTER = "mldb_test_node_reporter"
OTHER = "mldb_test_node_reporter_unmapped"
PASSWORD = "reporter-test-only-4c1d"  # noqa: S105 - a throwaway role in a scratch database
GATEWAY = "mldb_test_reporter_gateway"


def _drop(conn, role: str) -> None:
    exists = db.one(conn, "SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (role,))
    if exists:
        conn.execute(f'REVOKE ALL ON FUNCTION public.report_node_health(bigint) FROM "{role}"')
        conn.execute(f'REVOKE ALL ON SCHEMA public FROM "{role}"')
        conn.execute(f'DROP ROLE "{role}"')


@pytest.fixture
def roles(db_pool, migrated_database):  # noqa: ARG001
    with db.connection() as conn:
        try:
            for role in (REPORTER, OTHER):
                _drop(conn, role)
                conn.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip("the control-plane role cannot CREATE ROLE here")
        for name in ("rep-node", "rep-other"):
            nodes.register_node(conn, name=name, hostname=f"{name}.test", internal_host="10.0.9.9",
                                node_pool="shared", capacity={})
            nodes.set_status(conn, name=name, status="active")
        conn.commit()

    def login(role: str) -> psycopg.Connection:
        parts = psycopg.conninfo.conninfo_to_dict(migrated_database)
        parts.update(user=role, password=PASSWORD)
        return psycopg.connect(psycopg.conninfo.make_conninfo(**parts), autocommit=True)

    try:
        yield login
    finally:
        with db.connection() as conn:
            conn.execute("UPDATE nodes SET health_reporter_role = NULL")
            for role in (REPORTER, OTHER):
                _drop(conn, role)
            conn.commit()


def _grant(role: str = REPORTER, node: str = "rep-node") -> int:
    return manage._cmd_node_reporter_grant(argparse.Namespace(role=role, node=node))


def _node(name: str) -> dict:
    with db.connection() as conn:
        return db.one(conn, "SELECT metrics_json, last_health_at FROM nodes WHERE name = %s", (name,))


pytestmark = requires_db


def test_the_grant_maps_the_role_and_proves_it_holds_no_table_privilege(roles, capsys):
    assert _grant() == 0
    out = capsys.readouterr().out
    assert "table privileges: none" in out
    with db.connection() as conn:
        assert node_reporter.wider_than_the_model(conn, REPORTER) == []
        assert node_reporter.can_report(conn, REPORTER)
        assert not node_reporter.can_report(conn, OTHER), "EXECUTE is revoked from PUBLIC"


def test_a_report_is_for_its_own_node_merges_and_makes_the_node_placeable(roles):
    with db.connection() as conn:
        conn.execute("UPDATE nodes SET metrics_json = %s WHERE name = 'rep-node'",
                     (json.dumps({"backup_checked_at": "2026-09-15T00:00:00Z", "realtime_failures": []}),))
        conn.commit()
    assert _grant() == 0
    with roles(REPORTER) as conn:
        assert conn.execute("SELECT public.report_node_health(%s)", (123 * 2**30,)).fetchone()[0] == "rep-node"
    reported = _node("rep-node")
    assert reported["last_health_at"] is not None
    assert reported["metrics_json"]["free_disk_bytes"] == 123 * 2**30
    assert reported["metrics_json"]["health_reported_by"] == REPORTER
    assert reported["metrics_json"]["backup_checked_at"] == "2026-09-15T00:00:00Z", "a report erased backup-check"
    assert reported["metrics_json"]["realtime_failures"] == []
    assert _node("rep-other")["last_health_at"] is None
    with db.connection() as conn:
        node_id = db.one(conn, "SELECT id FROM nodes WHERE name = 'rep-node'")["id"]
        agree_with_pins(conn, node_id)
        assert "rep-node" in [n.name for n in nodes.eligible_nodes(conn, node_pool="shared")]


def test_the_reporter_reads_and_writes_nothing_else(roles):
    assert _grant() == 0
    with roles(REPORTER) as conn:
        for statement in ("SELECT name FROM nodes", "SELECT 1 FROM projects",
                          "UPDATE nodes SET last_health_at = now()"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_a_role_mapped_to_no_node_is_refused_not_ignored(roles):
    assert _grant() == 0
    with db.connection() as conn:
        conn.execute(f'GRANT EXECUTE ON FUNCTION public.report_node_health(bigint) TO "{OTHER}"')
        conn.commit()
    with roles(OTHER) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege, match="reports health for no node"):
        conn.execute("SELECT public.report_node_health(1)")
    assert _node("rep-node")["last_health_at"] is None


def test_a_nonsense_free_disk_is_refused(roles):
    assert _grant() == 0
    with roles(REPORTER) as conn, pytest.raises(psycopg.errors.InvalidParameterValue):
        conn.execute("SELECT public.report_node_health(-1)")


def test_the_grant_refuses_roles_that_must_not_report(roles, capsys):
    assert _grant() == 0
    assert _grant(role=REPORTER, node="rep-other") == 2
    assert "already reports for node 'rep-node'" in capsys.readouterr().out
    assert _grant(role="mldb_no_such_role") == 2
    assert "no role named" in capsys.readouterr().out
    assert _grant(node="no-such-node") == 2
    with db.connection() as conn:
        conn.execute(f'ALTER ROLE "{OTHER}" NOLOGIN')
        conn.commit()
    assert _grant(role=OTHER, node="rep-other") == 2
    assert "cannot log in" in capsys.readouterr().out
    with db.connection() as conn:
        conn.execute(f'ALTER ROLE "{OTHER}" LOGIN')
        conn.execute("UPDATE nodes SET gateway_role = %s WHERE name = 'rep-other'", (OTHER,))
        conn.commit()
    assert _grant(role=OTHER, node="rep-other") == 2
    assert "is the gateway role of node 'rep-other'" in capsys.readouterr().out
    with db.connection() as conn:
        conn.execute("UPDATE nodes SET gateway_role = NULL")
        conn.commit()


def test_a_gateway_role_cannot_report(roles):
    with db.connection() as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{GATEWAY}"')
        conn.execute(f'CREATE ROLE "{GATEWAY}" NOLOGIN')
        for statement in gateway_grants.statements(GATEWAY):
            conn.execute(statement)
        conn.commit()
        try:
            assert not node_reporter.can_report(conn, GATEWAY)
        finally:
            for statement in gateway_grants.revocations(GATEWAY):
                conn.execute(statement)
            conn.execute(f'DROP ROLE "{GATEWAY}"')
            conn.commit()


def test_the_operator_health_command_merges_too(db_pool):  # noqa: ARG001
    with db.connection() as conn:
        nodes.register_node(conn, name="rep-cli", hostname="c.test", internal_host="10.0.9.8",
                            node_pool="shared", capacity={})
        conn.execute("UPDATE nodes SET metrics_json = '{\"backup_failures\": [\"x\"]}' WHERE name = 'rep-cli'")
        nodes.record_health(conn, name="rep-cli", metrics={"free_disk_bytes": 5})
        conn.commit()
    metrics = _node("rep-cli")["metrics_json"]
    assert metrics == {"backup_failures": ["x"], "free_disk_bytes": 5}


# -- the reporter process ------------------------------------------------------


def test_nothing_is_reported_while_local_postgresql_does_not_answer(monkeypatch):
    monkeypatch.setattr(node_reporter, "local_postgres_answers", lambda _c: False)

    def connect(*_a, **_k):
        raise AssertionError("reported for a node whose cluster is down")

    settings = node_reporter.Settings(database_url="postgresql://unused")
    assert node_reporter.report_once(settings, connect=connect) is None


def test_a_cluster_that_is_not_there_does_not_answer():
    assert not node_reporter.local_postgres_answers("host=127.0.0.1 port=1 connect_timeout=2")


def test_the_interval_cannot_outlast_freshness(monkeypatch):
    monkeypatch.setenv("MALUDB_NODE_REPORTER_DATABASE_URL", "postgresql://r@h/db")
    monkeypatch.setenv("MALUDB_NODE_REPORT_INTERVAL_SECONDS", "600")
    with pytest.raises(SystemExit, match="stale"):
        node_reporter.Settings.from_environment()
    assert node_reporter.MAX_INTERVAL_SECONDS * 3 < nodes.HEALTH_STALE_AFTER.total_seconds()


def test_the_gateway_grant_refuses_a_reporter_role(roles, capsys):
    assert _grant() == 0
    capsys.readouterr()
    assert manage._cmd_gateway_grant(argparse.Namespace(role=REPORTER, node="rep-other")) == 2
    assert "is the health reporter of node 'rep-node'" in capsys.readouterr().out
    with db.connection() as conn:
        assert node_reporter.wider_than_the_model(conn, REPORTER) == []


def test_preflight_names_a_node_without_a_reporter_and_passes_one_with(roles, app_config):
    from services.control_plane import preflight

    def check():
        report = preflight.Report()
        with db.connection() as conn:
            preflight._check_node_reporters(conn, report)
        return {c.name: c for c in report.checks}["node health reporters"]

    unmapped = check()
    assert not unmapped.ok and unmapped.advisory and "rep-node" in unmapped.detail
    assert _grant() == 0 and _grant(role=OTHER, node="rep-other") == 0
    assert check().ok
    with db.connection() as conn:
        conn.execute(f'GRANT SELECT ON nodes TO "{OTHER}"')
        conn.commit()
    try:
        widened = check()
        assert not widened.ok and not widened.advisory and "nodes:SELECT" in widened.detail
    finally:
        with db.connection() as conn:
            conn.execute(psycopg.sql.SQL("REVOKE SELECT ON nodes FROM {}").format(psycopg.sql.Identifier(OTHER)))
            conn.commit()
