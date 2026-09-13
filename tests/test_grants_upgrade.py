"""The grants fleet run (ADR-076 grants slice 2).

Every tenant here is a real one, built by the provisioning module and bootstrapped
the way a tenant provisioned before the grants would be: up to 013, or before it.
The claims that matter are about outcomes -- who can execute `gen_salt` from SQL,
what a real PostgREST answers on `/rpc` -- because the run's whole purpose is to
never have a tenant in the state where the first is true and the second is not.

Needs `MALUDB_NODE_ADMIN_DSN` and `maludb_core`; the PostgREST test also needs the
binary. Skips without them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

import psycopg
import pytest

from services.control_plane import db, grants_upgrade, provisioning, tenant_bootstrap, workers
from tests.conftest import NODE_ADMIN_DSN, PLATFORM_OWNER, requires_db
from tests.test_extension_upgrade import _drop_tenant, _node, _project, _tenant
from tests.test_provisioning import requires_maludb_core

pytestmark = [
    requires_db,
    requires_maludb_core,
    pytest.mark.skipif(not NODE_ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset"),
]

POSTGREST_BIN = os.environ.get("MALUDB_POSTGREST_BIN", "postgrest")
requires_postgrest = pytest.mark.skipif(
    shutil.which(POSTGREST_BIN) is None and not os.path.exists(POSTGREST_BIN),
    reason="PostgREST binary not available",
)
CHECK_SETTING = f"pgrst.db_pre_request={tenant_bootstrap.RPC_CHECK_FUNCTION}"


@pytest.fixture(autouse=True)
def _control_plane(db_pool):  # noqa: ARG001
    yield


@pytest.fixture
def admin_node_conn():
    conn = psycopg.connect(NODE_ADMIN_DSN, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def serving_tenants(admin_node_conn, monkeypatch):
    """Tenants as a serving project has them before the grants run.

    `through` is the last bootstrap file applied: "013" is a tenant bootstrapped
    since grants slice 1 shipped (apply held 014), "012" one from before.
    """
    made: list[str] = []

    def make(ref: str, node_id: int, *, through: str = "013") -> tuple[provisioning.TenantNames, dict]:
        _drop_tenant(admin_node_conn, ref)
        made.append(ref)
        names = provisioning.TenantNames.for_ref(ref)
        passwords = {k: provisioning.generate_password()
                     for k in ("authenticator", "auth", "admin", "executor", "client", "storage")}
        with psycopg.connect(NODE_ADMIN_DSN) as conn:
            provisioning.ensure_shared_roles(conn)
            provisioning.create_roles(conn, names, passwords=passwords,
                                      connection_limits={"authenticator": 10, "auth": 5})
            provisioning.create_executor_role(conn, names, password=passwords["executor"])
            provisioning.create_client_role(conn, names, password=passwords["client"])
            provisioning.create_storage_role(conn, names, password=passwords["storage"])
            conn.commit()
            provisioning.create_database(conn, names, owner=PLATFORM_OWNER)
            provisioning.lock_down_database(conn, names)
            conn.commit()
        files = [(v, p) for v, p in tenant_bootstrap.discover() if v.split("_", 1)[0] <= through]
        with _tenant(names.database) as t:
            t.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
            t.commit()
            with monkeypatch.context() as patch:
                patch.setattr(tenant_bootstrap, "discover", lambda: files)
                tenant_bootstrap.apply(t)
            tenant_bootstrap.verify(t)
            t.commit()
        _project(ref, node_id)
        return names, passwords

    yield make
    for ref in made:
        _drop_tenant(admin_node_conn, ref)


def _run(admin_node_conn, node: str, **kw) -> grants_upgrade.GrantsOutcome:
    kw.setdefault("reload_seconds", 0)
    with db.connection() as conn:
        return grants_upgrade.upgrade_node(conn, admin_node_conn, node_name=node, **kw)


def _anon_runs_gen_salt(database: str) -> bool:
    with _tenant(database) as t:
        return t.execute(
            "SELECT has_function_privilege('anon', 'public.gen_salt(text)', 'EXECUTE')"
        ).fetchone()[0]


def _has(database: str, version: str) -> bool:
    with _tenant(database) as t:
        return version in tenant_bootstrap.applied(t)


def _check_setting(admin, names) -> list[str]:
    rows = admin.execute(
        "SELECT unnest(s.setconfig) FROM pg_db_role_setting s "
        "JOIN pg_roles r ON r.oid = s.setrole JOIN pg_database d ON d.oid = s.setdatabase "
        "WHERE r.rolname = %s AND d.datname = %s",
        (names.authenticator, names.database),
    ).fetchall()
    return [r[0] for r in rows if r[0].startswith("pgrst.db_pre_request")]


# --------------------------------------------------------------------------


def test_a_canary_first_then_the_rest_in_a_batch(admin_node_conn, serving_tenants):
    node = _node("gu-canary")
    tenants = [serving_tenants(ref, node)[0] for ref in ("guc00001", "guc00002", "guc00003")]
    assert not any(_anon_runs_gen_salt(n.database) for n in tenants)

    first = _run(admin_node_conn, "gu-canary")
    assert first.status == "complete" and first.canary_run
    assert [t.status for t in first.tenants] == ["upgraded"]
    assert first.left == ["guc00002", "guc00003"]
    canary = tenants[0]
    assert _has(canary.database, grants_upgrade.GRANTS_VERSION)
    assert _anon_runs_gen_salt(canary.database)
    assert _check_setting(admin_node_conn, canary) == [CHECK_SETTING]
    assert not _anon_runs_gen_salt(tenants[1].database), "the canary run touched a second tenant"

    second = _run(admin_node_conn, "gu-canary", batch_size=10)
    assert not second.canary_run
    assert sorted((t.project_ref, t.status) for t in second.tenants) == [
        ("guc00001", "current"), ("guc00002", "upgraded"), ("guc00003", "upgraded"),
    ]
    for names in tenants:
        assert _anon_runs_gen_salt(names.database)
        with _tenant(names.database) as t:
            tenant_bootstrap.verify(t)

    with db.connection() as conn:
        versions = {r["bootstrap_version"] for r in db.query(
            conn, "SELECT bootstrap_version FROM projects WHERE project_ref LIKE 'guc%%'")}
        recorded = db.query(
            conn, "SELECT g.status, g.canary FROM extension_grant_upgrades g JOIN projects p "
                  "ON p.id = g.project_id WHERE p.project_ref = 'guc00001' ORDER BY g.id")
    assert versions == {tenant_bootstrap.latest_version()}
    assert [(r["status"], r["canary"]) for r in recorded] == [("upgraded", True), ("current", False)]


def test_a_tenant_from_before_the_check_gets_the_check_then_the_grants(admin_node_conn, serving_tenants):
    node = _node("gu-old")
    names, _ = serving_tenants("guo00001", node, through="012")
    assert not _has(names.database, grants_upgrade.CHECK_VERSION)

    outcome = _run(admin_node_conn, "gu-old")
    assert [t.status for t in outcome.tenants] == ["upgraded"], outcome.tenants
    assert _has(names.database, grants_upgrade.CHECK_VERSION)
    assert _has(names.database, grants_upgrade.GRANTS_VERSION)
    assert _anon_runs_gen_salt(names.database)


def test_a_worker_connected_without_a_listener_stops_the_run_with_nothing_granted(
    admin_node_conn, serving_tenants
):
    """The run's evidence that a reload reached a worker is its LISTEN connection.
    A pooled connection with no listener is a worker that evidence does not cover."""
    node = _node("gu-deaf")
    deaf, passwords = serving_tenants("gud00001", node)
    after, _ = serving_tenants("gud00002", node)
    info = psycopg.conninfo.conninfo_to_dict(NODE_ADMIN_DSN)
    info.update(dbname=deaf.database, user=deaf.authenticator, password=passwords["authenticator"])

    with psycopg.connect(**info, autocommit=True) as worker:
        worker.execute("SELECT 1")
        stopped = _run(admin_node_conn, "gu-deaf")
        assert stopped.status == "stopped" and stopped.stopped_at == "gud00001"
        assert "without a LISTEN connection" in stopped.tenants[0].detail
        assert stopped.left == ["gud00002"]
        assert not _has(deaf.database, grants_upgrade.GRANTS_VERSION)
        assert not _anon_runs_gen_salt(deaf.database)
        assert not _anon_runs_gen_salt(after.database), "the run went past the tenant it stopped at"

        # The control: the same worker, now listening, is granted.
        worker.execute("LISTEN pgrst")
        resumed = _run(admin_node_conn, "gu-deaf")
    assert [t.status for t in resumed.tenants] == ["upgraded"]
    assert "listening" in resumed.tenants[0].worker
    assert _anon_runs_gen_salt(deaf.database)


def test_grants_that_fail_verification_are_rolled_back(admin_node_conn, serving_tenants):
    """014 and verify share a transaction: a grant that verified badly must not
    stay applied. Sabotaged with a posture verify refuses and 014's repair pass
    does not undo: a maludb_core function executable by service_role. (Granting
    it to anon would not do -- 014 revokes that, and the run rightly succeeds.)"""
    node = _node("gu-rollback")
    names, _ = serving_tenants("gur00001", node)
    with _tenant(names.database) as t:
        t.execute("GRANT EXECUTE ON FUNCTION mc2db.create_server(text, text, text, text[], text) TO service_role")
        t.commit()

    outcome = _run(admin_node_conn, "gu-rollback")
    assert outcome.status == "stopped"
    assert "maludb_core" in outcome.tenants[0].detail
    assert not _has(names.database, grants_upgrade.GRANTS_VERSION)
    assert not _anon_runs_gen_salt(names.database), "the grants survived a failed verification"


def test_a_second_run_on_the_node_is_refused_while_one_holds_the_lock(admin_node_conn, serving_tenants):
    node = _node("gu-lock")
    from services.control_plane.maludb import NODE_LOCK_NAMESPACE

    with db.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s, %s)", (NODE_LOCK_NAMESPACE, node))
        outcome = _run(admin_node_conn, "gu-lock")
        holder.execute("SELECT pg_advisory_unlock(%s, %s)", (NODE_LOCK_NAMESPACE, node))
    assert outcome.status == "refused" and "already running" in outcome.error


@requires_postgrest
@pytest.mark.parametrize("set_in_database", [True, False], ids=["run", "control-without-setting"])
def test_a_running_worker_whose_file_predates_the_check_refuses_after_the_run(
    admin_node_conn, serving_tenants, tmp_path, monkeypatch, set_in_database
):
    """The situation the run exists for, end to end: a serving worker started from
    a file with no `db-pre-request`, which the control plane cannot rewrite.

    The control skips writing the in-database setting and otherwise runs the
    same: the listener is there, so the run grants -- and `/rpc/gen_salt` answers
    a salt. That is what shows the setting, not the run's bookkeeping, is what
    puts the check live, and it is the exact exposure the ordering prevents.
    """
    ref = "gup00001" if set_in_database else "gup00002"
    port = 27441 if set_in_database else 27442
    node = _node(f"gu-{ref}")
    names, passwords = serving_tenants(ref, node)

    settings = workers.WorkerSettings(
        project_ref=ref, database=names.database, authenticator_role=names.authenticator,
        authenticator_password=passwords["authenticator"],
        jwt_secret="grants-run-test-jwt-secret-long-enough-for-hs256",  # noqa: S106
        port=port,  # and no pre_request: rendered before the check existed
    )
    config = workers.write_config(settings, config_dir=tmp_path)
    process = subprocess.Popen(  # noqa: S603 - fixed binary, generated config
        [POSTGREST_BIN, str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    def gen_salt() -> tuple[int, str]:
        request = urllib.request.Request(  # noqa: S310 - loopback
            f"http://127.0.0.1:{port}/rpc/gen_salt", data=b"bf", method="POST",
            headers={"Content-Type": "text/plain"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    try:
        workers.wait_until_ready(port, timeout=30)
        before, _ = gen_salt()
        assert before != 200, "anon reached gen_salt before any grant"
        if not set_in_database:
            monkeypatch.setattr(grants_upgrade, "set_check_in_database", lambda *a, **k: None)

        outcome = _run(admin_node_conn, f"gu-{ref}", batch_size=10, reload_seconds=3.0)
        mine = [t for t in outcome.tenants if t.project_ref == ref]
        assert mine and mine[0].status in ("upgraded", "current"), outcome.tenants
        assert _anon_runs_gen_salt(names.database)

        status, body = gen_salt()
        if set_in_database:
            assert (status, json.loads(body)["code"]) == (403, "PT403"), f"{status} {body}"
        else:
            assert status == 200 and body.strip('"').startswith("$2a$"), (
                f"control: without the setting the grants should be exposed, got {status} {body}"
            )
    finally:
        process.terminate()
        process.wait(timeout=10)
