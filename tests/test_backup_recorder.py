"""A node records its own backups as a role that can do nothing else (ADR-086, free slice 7a).

Found surveying the two-machine deployment: `cp-manage node backup` runs pgBackRest where the
control-plane database is, and no host has both. What is held here:

- **the recorder reads nothing and writes nothing** but its three functions;
- **its own node only**, decided in the database from the login role, and the stanza a row
  claims is the node's, never the caller's;
- **history is not rewritable**: only a running row of its own node can be finished, and a
  complete one needs a pgBackRest label;
- **the repository report merges under its own keys**, so it cannot overwrite what the control
  plane's backup-check recorded;
- **no role holds two models**: the grant refuses a gateway, reporter, memory or console role,
  and the gateway and reporter grants refuse a recorder;
- **the gateway can no longer write `node_backups`**, so it cannot mark its node backed up.
"""

from __future__ import annotations

import argparse
import json

import psycopg
import pytest

from services.control_plane import backup_recorder, db, gateway_grants, manage, nodes
from tests.conftest import requires_db

pytestmark = requires_db

RECORDER = "mldb_test_backup_recorder"
OTHER = "mldb_test_backup_recorder_other"
PASSWORD = "recorder-test-only-7a2e"  # noqa: S105 - a throwaway role in a scratch database
GATEWAY = "mldb_test_recorder_gateway"
LABEL = "20260917-020000F"
WAL = "000000010000000000000003"


def _drop(conn, role: str) -> None:
    if db.one(conn, "SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (role,)):
        for function in backup_recorder.FUNCTIONS:
            conn.execute(f'REVOKE ALL ON FUNCTION {function} FROM "{role}"')
        conn.execute(f'REVOKE ALL ON FUNCTION public.report_node_health(bigint) FROM "{role}"')
        conn.execute(f'REVOKE ALL ON SCHEMA public FROM "{role}"')
        conn.execute(f'DROP ROLE "{role}"')


@pytest.fixture
def roles(db_pool, migrated_database):  # noqa: ARG001
    with db.connection() as conn:
        try:
            for role in (RECORDER, OTHER):
                _drop(conn, role)
                conn.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip("the control-plane role cannot CREATE ROLE here")
        for name, stanza in (("bk-node", "maludb-bk-node"), ("bk-other", "maludb-bk-other")):
            nodes.register_node(conn, name=name, hostname=f"{name}.test", internal_host="10.0.9.8",
                                node_pool="shared", capacity={})
            nodes.set_status(conn, name=name, status="active")
            conn.execute("UPDATE nodes SET backup_stanza = %s WHERE name = %s", (stanza, name))
        conn.commit()

    def login(role: str) -> psycopg.Connection:
        parts = psycopg.conninfo.conninfo_to_dict(migrated_database)
        parts.update(user=role, password=PASSWORD)
        return psycopg.connect(psycopg.conninfo.make_conninfo(**parts), autocommit=True)

    try:
        yield login
    finally:
        with db.connection() as conn:
            conn.execute("UPDATE nodes SET backup_recorder_role = NULL, health_reporter_role = NULL, "
                         "gateway_role = NULL")
            conn.execute("DELETE FROM node_backups WHERE node_id IN (SELECT id FROM nodes WHERE name LIKE 'bk-%')")
            for role in (RECORDER, OTHER):
                _drop(conn, role)
            conn.commit()


def _grant(role: str = RECORDER, node: str = "bk-node") -> int:
    return manage._cmd_node_backup_recorder_grant(argparse.Namespace(role=role, node=node))


def _backups(node: str) -> list[dict]:
    with db.connection() as conn:
        return db.query(conn, "SELECT b.* FROM node_backups b JOIN nodes n ON n.id = b.node_id "
                              "WHERE n.name = %s ORDER BY b.id", (node,))


def _finish(conn, backup_id, status="complete", label=LABEL, wal=WAL, error=None):
    conn.execute("SELECT public.finish_node_backup(%s, %s, %s, %s, %s, %s, %s, %s)",
                 (backup_id, status, label, 50_000_000, 5_000_000, wal, wal, error))


def test_the_grant_maps_the_role_and_proves_it_holds_no_table_privilege(roles, capsys):
    assert _grant() == 0
    assert "table privileges: none" in capsys.readouterr().out
    with db.connection() as conn:
        assert backup_recorder.wider_than_the_model(conn, RECORDER) == []
        assert backup_recorder.missing_functions(conn, RECORDER) == []
        assert backup_recorder.missing_functions(conn, OTHER) == list(backup_recorder.FUNCTIONS), \
            "EXECUTE is revoked from PUBLIC"


def test_a_backup_is_recorded_for_its_own_node_with_the_nodes_stanza(roles):
    assert _grant() == 0
    with roles(RECORDER) as conn:
        backup_id = conn.execute("SELECT public.start_node_backup('full')").fetchone()[0]
        running = _backups("bk-node")
        assert [(r["status"], r["stanza"], r["backup_type"]) for r in running] == [
            ("running", "maludb-bk-node", "full")], "written before pgBackRest runs, with the node's stanza"
        _finish(conn, backup_id)
    [row] = _backups("bk-node")
    assert (row["status"], row["label"], row["database_bytes"], row["wal_start"]) == (
        "complete", LABEL, 50_000_000, WAL)
    assert _backups("bk-other") == []


def test_the_recorder_reads_and_writes_nothing_else(roles):
    assert _grant() == 0
    with roles(RECORDER) as conn:
        for statement in ("SELECT name FROM nodes", "SELECT 1 FROM node_backups", "SELECT 1 FROM projects",
                          "UPDATE nodes SET backup_stanza = 'x'",
                          "INSERT INTO node_backups (node_id, stanza, backup_type) VALUES (1, 'x', 'full')",
                          "SELECT public.report_node_health(1)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_history_cannot_be_rewritten_or_forged(roles):
    assert _grant() == 0 and _grant(role=OTHER, node="bk-other") == 0
    with roles(RECORDER) as conn:
        backup_id = conn.execute("SELECT public.start_node_backup('full')").fetchone()[0]
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="pgBackRest label"):
            _finish(conn, backup_id, label="looks-fine")
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="WAL segment"):
            _finish(conn, backup_id, wal="not-a-segment")
        _finish(conn, backup_id, status="failed", label=None, error="archive-push failed")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="no running backup"):
            _finish(conn, backup_id)
    with roles(OTHER) as conn, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="no running backup"):
        _finish(conn, backup_id)
    [row] = _backups("bk-node")
    assert row["status"] == "failed" and row["label"] is None, "a failure stays a failure"


def test_starts_are_limited_and_need_a_stanza_and_a_known_type(roles):
    assert _grant() == 0
    with roles(RECORDER) as conn:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            conn.execute("SELECT public.start_node_backup('snapshot')")
        conn.execute("SELECT public.start_node_backup('diff')")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="less than a minute ago"):
            conn.execute("SELECT public.start_node_backup('diff')")
    with db.connection() as conn:
        conn.execute("UPDATE nodes SET backup_stanza = NULL WHERE name = 'bk-node'")
        conn.execute("DELETE FROM node_backups WHERE node_id = (SELECT id FROM nodes WHERE name = 'bk-node')")
        conn.commit()
    with roles(RECORDER) as conn, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="no stanza"):
        conn.execute("SELECT public.start_node_backup('full')")


def test_a_role_mapped_to_no_node_is_refused_not_ignored(roles):
    assert _grant() == 0
    with db.connection() as conn:
        for statement in backup_recorder.statements(OTHER):
            conn.execute(statement)
        conn.commit()
    with roles(OTHER) as conn:
        for call in ("SELECT public.start_node_backup('full')", "SELECT public.record_node_backup_check('{}')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="records backups for no node"):
                conn.execute(call)
    assert _backups("bk-node") == []


def test_the_repository_report_merges_under_its_own_keys(roles):
    with db.connection() as conn:
        conn.execute("UPDATE nodes SET metrics_json = %s, capacity_json = %s WHERE name = 'bk-node'",
                     (json.dumps({"backup_checked_at": "2026-09-17T00:00:00Z", "free_disk_bytes": 7}),
                      json.dumps({"backup_ready": False, "archive_mode": "off"})))
        conn.commit()
    assert _grant() == 0
    report = {"repos": {"1": {"reachable": True}, "2": {"reachable": True}}, "backup_ready": True,
              "archive_mode": "on"}
    with roles(RECORDER) as conn:
        recorded = conn.execute("SELECT public.record_node_backup_check(%s)", (json.dumps(report),)).fetchone()[0]
        assert recorded == "bk-node"
        for bad in ("[]", json.dumps({"x": "y" * 70_000})):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                conn.execute("SELECT public.record_node_backup_check(%s)", (bad,))
    with db.connection() as conn:
        row = db.one(conn, "SELECT metrics_json, capacity_json FROM nodes WHERE name = 'bk-node'")
    metrics, capacity = row["metrics_json"], row["capacity_json"]
    assert metrics["backup_repository"] == report
    assert metrics["backup_repository_reported_by"] == RECORDER and metrics["backup_repository_checked_at"]
    assert metrics["backup_checked_at"] == "2026-09-17T00:00:00Z" and metrics["free_disk_bytes"] == 7
    assert capacity == {"backup_ready": False, "archive_mode": "off"}, \
        "a node's report cannot overwrite what the control plane's backup-check recorded"


def test_no_role_holds_two_models(roles, capsys):
    assert _grant() == 0
    assert _grant(role=RECORDER, node="bk-other") == 2
    assert "already records backups for node 'bk-node'" in capsys.readouterr().out
    assert _grant(role="mldb_no_such_role") == 2
    assert "no role named" in capsys.readouterr().out

    # The recorder refused by the gateway grant and the reporter grant.
    assert manage._cmd_gateway_grant(argparse.Namespace(role=RECORDER, node="bk-other")) == 2
    assert "records backups for node 'bk-node'" in capsys.readouterr().out
    assert manage._cmd_node_reporter_grant(argparse.Namespace(role=RECORDER, node="bk-other")) == 2
    assert "records backups for node 'bk-node'" in capsys.readouterr().out

    # And a reporter or a gateway refused as a recorder.
    with db.connection() as conn:
        conn.execute("UPDATE nodes SET health_reporter_role = %s WHERE name = 'bk-other'", (OTHER,))
        conn.commit()
    assert _grant(role=OTHER, node="bk-other") == 2
    assert "is the health reporter of node 'bk-other'" in capsys.readouterr().out
    with db.connection() as conn:
        conn.execute("UPDATE nodes SET health_reporter_role = NULL, gateway_role = %s WHERE name = 'bk-other'",
                     (OTHER,))
        conn.commit()
    assert _grant(role=OTHER, node="bk-other") == 2
    assert "is the gateway role of node 'bk-other'" in capsys.readouterr().out
    with db.connection() as conn:
        assert backup_recorder.wider_than_the_model(conn, RECORDER) == []


def test_the_gateway_can_no_longer_write_node_backups(db_pool):  # noqa: ARG001
    """Migration 0031 gave the gateway its own node's backup rows; nothing in the gateway reads
    them, and writing them would let it mark its node backed up."""
    assert "node_backups" in gateway_grants.UNREACHABLE_TABLES
    with db.connection() as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{GATEWAY}"')
        conn.execute(f'CREATE ROLE "{GATEWAY}" NOLOGIN')
        for statement in gateway_grants.statements(GATEWAY):
            conn.execute(statement)
        conn.commit()
        try:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not db.one(conn, "SELECT has_table_privilege(%s, 'node_backups', %s) AS yes",
                                  (GATEWAY, privilege))["yes"], privilege
            assert backup_recorder.missing_functions(conn, GATEWAY) == list(backup_recorder.FUNCTIONS)
        finally:
            for statement in gateway_grants.revocations(GATEWAY):
                conn.execute(statement)
            conn.execute(f'DROP ROLE "{GATEWAY}"')
            conn.commit()
