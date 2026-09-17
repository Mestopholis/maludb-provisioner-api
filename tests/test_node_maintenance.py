"""The maintenance pass, split (ADR-083).

The node half runs **as a real gateway role** against two nodes, because the property that
matters is what that role can and cannot do rather than what the code intends:

- it sleeps its own node's idle workers and marks them stopped, and nothing on another node;
- it records its runs for its own node, and cannot record or read one for another;
- the gateway role cannot touch `maintenance_runs`, which preflight trusts for the control plane;
- a role that is no node's gateway sleeps nothing;
- the module reaches no key ring, node credential or provisioning code.

The control-plane half: `run_all(skip=...)` does not call a skipped pass at all.
"""

from __future__ import annotations

import contextlib
import dataclasses

import psycopg
import pytest
from psycopg.rows import dict_row

from services.control_plane import (
    auth_workers,
    db,
    maintenance,
    node_maintenance,
    preflight,
    realtime_workers,
    supervision,
    workers,
)
from services.control_plane import config as config_module
from tests.conftest import DATABASE_URL, requires_db
from tests.test_control_plane_surfaces import FORBIDDEN_MODULES, _import_closure, _module_file
from tests.test_gateway_grants import gateway_role, two_nodes_two_projects  # noqa: F401 - fixtures

pytestmark = requires_db


class FakeSupervisor:
    def __init__(self, fail: set[str] | None = None):
        self.stopped: list[str] = []
        self.fail = fail or set()

    def start(self, project_ref):  # pragma: no cover - not used by the pass
        raise AssertionError("the node pass never starts a worker")

    def stop(self, project_ref):
        if project_ref in self.fail:
            raise supervision.WorkerError(f"could not stop {project_ref}")
        self.stopped.append(project_ref)

    def is_active(self, project_ref):  # pragma: no cover
        return True


@contextlib.contextmanager
def _as_role(role: str):
    """A connection that is the gateway role for its whole life, commits included."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute(f'SET ROLE "{role}"')
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def mapped(gateway_role, two_nodes_two_projects):  # noqa: F811
    """Alpha's node is served by the gateway role; both projects have idle API and Auth workers running."""
    nodes = two_nodes_two_projects
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s", (gateway_role, nodes["alpha"]["node_id"]))
        db.execute(conn, "UPDATE projects SET worker_state = 'RUNNING', auth_worker_state = 'RUNNING', "
                         "worker_last_active_at = now() - interval '2 hours', "
                         "auth_worker_last_active_at = now() - interval '2 hours'")
        conn.commit()
    yield nodes
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM node_maintenance_runs")
        db.execute(conn, "UPDATE nodes SET gateway_role = NULL WHERE gateway_role = %s", (gateway_role,))
        conn.commit()


def _states(ref: str) -> dict:
    with db.connection() as conn:
        return db.one(conn, "SELECT worker_state, auth_worker_state FROM projects WHERE project_ref = %s", (ref,))


def test_the_gateway_sleeps_its_own_nodes_idle_workers_and_nothing_elsewhere(gateway_role, mapped):  # noqa: F811
    api, auth = FakeSupervisor(), FakeSupervisor()
    with _as_role(gateway_role) as conn:
        result = node_maintenance.run(conn, supervisors={"api": api, "auth": auth}, idle_minutes=15,
                                      realtime_idle_minutes=60)
    assert (result.slept, result.failed) == (2, 0)
    assert api.stopped == ["rlsalpha"] and auth.stopped == ["rlsalpha"]
    assert _states("rlsalpha") == {"worker_state": "STOPPED", "auth_worker_state": "STOPPED"}
    assert _states("rlsbeta") == {"worker_state": "RUNNING", "auth_worker_state": "RUNNING"}, "another node's"
    with db.connection() as conn:
        runs = db.query(conn, "SELECT node_id, slept, failed, finished_at FROM node_maintenance_runs")
    assert len(runs) == 1 and runs[0]["node_id"] == mapped["alpha"]["node_id"]
    assert runs[0]["slept"] == 2 and runs[0]["finished_at"] is not None


def test_a_recently_used_worker_is_not_slept_and_a_failure_is_counted(gateway_role, mapped):  # noqa: F811
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET auth_worker_last_active_at = now() WHERE project_ref = 'rlsalpha'")
        conn.commit()
    api, auth = FakeSupervisor(fail={"rlsalpha"}), FakeSupervisor()
    with _as_role(gateway_role) as conn:
        result = node_maintenance.run(conn, supervisors={"api": api, "auth": auth}, idle_minutes=15,
                                      realtime_idle_minutes=60)
    assert (result.slept, result.failed) == (0, 1)
    assert auth.stopped == [], "used a moment ago"
    assert _states("rlsalpha") == {"worker_state": "RUNNING", "auth_worker_state": "RUNNING"}, \
        "a unit that did not stop is not recorded as stopped"


def test_a_gateway_records_runs_only_for_its_own_node(gateway_role, mapped):  # noqa: F811
    with db.connection() as conn:
        db.execute(conn, "INSERT INTO node_maintenance_runs (node_id, finished_at, slept, failed) "
                         "VALUES (%s, now(), 0, 0)", (mapped["beta"]["node_id"],))
        conn.commit()
    with _as_role(gateway_role) as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_maintenance_runs").fetchone()["n"] == 0, \
            "beta's run is invisible to alpha's gateway"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO node_maintenance_runs (node_id, finished_at) VALUES (%s, now())",
                         (mapped["beta"]["node_id"],))


def test_the_gateway_cannot_touch_the_control_planes_run_record(gateway_role, mapped):  # noqa: ARG001, F811
    with _as_role(gateway_role) as conn:
        for statement in ("SELECT * FROM maintenance_runs",
                          "INSERT INTO maintenance_runs (finished_at, passes, failed) VALUES (now(), 12, 0)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_a_role_that_is_no_nodes_gateway_sleeps_nothing(gateway_role, two_nodes_two_projects):  # noqa: F811, ARG001
    with _as_role(gateway_role) as conn, pytest.raises(node_maintenance.NotAGateway):
        node_maintenance.run(conn, supervisors={"api": FakeSupervisor()}, idle_minutes=15, realtime_idle_minutes=60)


def test_the_node_pass_reaches_no_credential_or_provisioning_code():
    modules, calls = _import_closure(["services.control_plane.node_maintenance"])
    assert "services.control_plane.supervision" in modules, "the walk found nothing"
    heavy = {"services.control_plane.workers", "services.control_plane.provisioning", "services.control_plane.crypto",
             "services.control_plane.maintenance", "services.control_plane.nodes", "services.control_plane.config"}
    assert not modules & (FORBIDDEN_MODULES | heavy), modules & (FORBIDDEN_MODULES | heavy)
    assert calls == {}, calls
    for module in modules:
        path = _module_file(module)
        if path is not None:
            assert "KeyRing(" not in path.read_text() and "MALUDB_KEK_REF" not in path.read_text(), module


def test_the_node_pass_agrees_with_the_worker_modules():
    assert {k.name: k.template for k in node_maintenance.KINDS} == {
        "api": workers.SERVICE_TEMPLATE, "auth": auth_workers.SERVICE_TEMPLATE,
        "realtime": realtime_workers.SERVICE_TEMPLATE}
    assert node_maintenance.DEFAULT_IDLE_MINUTES == maintenance.DEFAULT_IDLE_MINUTES
    assert node_maintenance.REALTIME_IDLE_MINUTES == maintenance.REALTIME_IDLE_MINUTES
    assert workers.SystemdSupervisor is supervision.SystemdSupervisor and workers.WorkerError is supervision.WorkerError


# -- the control plane's half ---------------------------------------------------------


def test_a_skipped_pass_is_not_called(monkeypatch, db_pool):  # noqa: ARG001
    called = []

    def recorder(name):
        return lambda *a, **k: called.append(name) or maintenance.PassResult()

    names = {"retry": "retry_failed_provisioning", "grace": "expire_billing_grace",
             "billing": "reconcile_subscriptions",
             "storage": "measure_storage", "object_storage": "measure_object_storage",
             "storage_tenants": "reconcile_storage_tenants", "slots": "check_replication_slots",
             "capacity": "check_capacity", "backups": "check_backups", "objects": "reconcile_objects",
             "plan_drift": "report_plan_drift", "sleep": "sleep_idle_workers"}
    assert set(names) == set(maintenance.PASS_NAMES)
    for pass_name, function in names.items():
        monkeypatch.setattr(maintenance, function, recorder(pass_name))
    with db.connection() as conn:
        results = maintenance.run_all(conn, key_ring=None, platform_owner="postgres", supervisor=None,
                                      auth_supervisor=None, skip=frozenset({"sleep"}))
        assert list(results) == [n for n in maintenance.PASS_NAMES if n != "sleep"]
        assert called == list(results) and "sleep" not in called
        with pytest.raises(ValueError, match="unknown"):
            maintenance.run_all(conn, key_ring=None, platform_owner="postgres", supervisor=None,
                                auth_supervisor=None, skip=frozenset({"slep"}))


# -- preflight --------------------------------------------------------------------------


def _cfg():
    base = config_module.Config(environment="production", database_url="postgresql://x/y", gateway_domain="e.com",
                                database_domain="db.e.com", docs_enabled=False, kek=b"k" * 32, token_pepper=b"p" * 32)
    return dataclasses.replace(base)


def _node_check():
    with db.connection() as conn:
        return next(c for c in preflight.run(conn, _cfg()).checks if c.name == "node maintenance")


def test_preflight_fails_an_active_node_that_never_sleeps_its_workers(gateway_role, mapped):  # noqa: F811, ARG001
    check = _node_check()
    assert not check.ok and not check.advisory and "rls-alpha" in check.detail and "rls-beta" in check.detail
    with db.connection() as conn:
        for node in (mapped["alpha"]["node_id"], mapped["beta"]["node_id"]):
            db.execute(conn, "INSERT INTO node_maintenance_runs (node_id, finished_at, slept, failed) "
                             "VALUES (%s, now(), 1, 0)", (node,))
        conn.commit()
    assert _node_check().ok
    with db.connection() as conn:
        db.execute(conn, "INSERT INTO node_maintenance_runs (node_id, started_at, finished_at, slept, failed) "
                         "VALUES (%s, now(), now() + interval '1 second', 0, 2)", (mapped["alpha"]["node_id"],))
        conn.commit()
    check = _node_check()
    assert not check.ok and check.advisory and "rls-alpha (2)" in check.detail
