"""Tenant movement (Phase 11 slice 7, ADR-066).

These tests cover the control-plane side of movement: a move is explicit,
records itself, takes the project out of service while it runs, and refuses
states that would silently move only part of a project. Most of these need no
second PostgreSQL node; the freeze tests and the end-to-end move at the bottom
(`test_a_tenant_with_vectors_moves_between_two_real_clusters`) use the backup
test cluster as one.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import textwrap
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, identity, provisioning, restore, tenant_movement
from tests.conftest import (
    BACKUP_NODE_DSN,
    NODE_ADMIN_DSN,
    TEST_CREDENTIAL,
    agree_with_pins,
    requires_db,
)

pytestmark = requires_db


@pytest.fixture
def tenant(request):
    """A real tenant on the second cluster, through the real provisioning path.

    Provisioned rather than faked because the freeze is asserted against the
    grants `create_*_role` and `lock_down_database` actually leave behind; a
    hand-built database would test the fixture's idea of a tenant instead.
    """
    if not BACKUP_NODE_DSN:
        pytest.skip("MALUDB_BACKUP_NODE_DSN is unset")
    ref = getattr(request, "param", "mvt00010")
    names = provisioning.TenantNames.for_ref(ref)
    admin = psycopg.connect(BACKUP_NODE_DSN, autocommit=True)
    _drop(admin, names)
    passwords = {
        k: provisioning.generate_password()
        for k in ("authenticator", "auth", "admin", "executor", "client", "storage")
    }
    with psycopg.connect(BACKUP_NODE_DSN) as conn:
        provisioning.ensure_shared_roles(conn)
        provisioning.create_roles(
            conn,
            names,
            passwords=passwords,
            connection_limits={"authenticator": 20, "auth": 10},
        )
        provisioning.create_executor_role(conn, names, password=passwords["executor"])
        provisioning.create_client_role(conn, names, password=passwords["client"])
        provisioning.create_storage_role(conn, names, password=passwords["storage"])
        conn.commit()
        provisioning.create_database(conn, names, owner="postgres")
        provisioning.lock_down_database(conn, names)
        provisioning.grant_executor_connect(conn, names)
        provisioning.grant_client_connect(conn, names)
        provisioning.grant_storage_connect(conn, names)
        conn.commit()
    try:
        yield admin, names, passwords
    finally:
        _drop(admin, names)
        admin.close()


def _drop(admin, names) -> None:
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (names.database,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
        # Retirement renames rather than drops, so a previous run leaves these.
        cur.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (f"{names.database}\\_pre\\_move\\_%", ),
        )
        for row in cur.fetchall():
            cur.execute(f'DROP DATABASE IF EXISTS "{row[0]}" WITH (FORCE)')
        for role in tenant_movement.tenant_roles(names):
            cur.execute(f'DROP ROLE IF EXISTS "{role}"')


def _tenant_dsn(names, passwords) -> str:
    info = psycopg.conninfo.conninfo_to_dict(BACKUP_NODE_DSN)
    info["dbname"] = names.database
    info["user"] = names.executor
    info["password"] = passwords["executor"]
    return psycopg.conninfo.make_conninfo(**info)


def _node(name: str, *, pool: str = "shared", status: str = "active") -> int:
    with db.connection() as conn:
        row = db.one(
            conn,
            """
            INSERT INTO nodes
                (name, hostname, internal_host, node_pool, status, last_health_at)
            VALUES (%s,%s,%s,%s,%s,now())
            ON CONFLICT (name) DO UPDATE
                SET node_pool = EXCLUDED.node_pool,
                    status = EXCLUDED.status,
                    last_health_at = now()
            RETURNING id
            """,
            (name, f"{name}.example", f"{name}.internal", pool, status),
        )
        conn.commit()
        agree_with_pins(conn, row["id"])
        return row["id"]


def _plan(code: str = "move-plan", *, pool: str = "shared") -> int:
    with db.connection() as conn:
        row = db.one(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES (%s,%s,%s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json "
            "RETURNING id",
            (code, code, Jsonb({"node_pool": pool})),
        )
        conn.commit()
        return row["id"]


def _project(
    ref: str = "mov00001",
    *,
    source_node: str = "move-source",
    target_node: str = "move-target",
    plan_pool: str = "shared",
    target_pool: str = "shared",
    status: str = "ACTIVE",
    realtime_enabled: bool = False,
) -> tuple[uuid.UUID, int, int]:
    source_id = _node(source_node, pool="shared")
    target_id = _node(target_node, pool=target_pool)
    plan_id = _plan(pool=plan_pool)
    project_id = uuid.uuid4()
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
        )
        db.execute(
            conn,
            """
            INSERT INTO projects
                (id, org_id, project_ref, display_name, plan_id, status,
                 node_id, database_name, realtime_enabled)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                project_id,
                org,
                ref,
                ref,
                plan_id,
                status,
                source_id,
                f"mldb_{ref}",
                realtime_enabled,
            ),
        )
        conn.commit()
    return project_id, source_id, target_id


def test_begin_records_the_move_and_takes_the_project_out_of_service(db_pool):  # noqa: ARG001
    project_id, source_id, target_id = _project()

    with db.connection() as conn:
        target = tenant_movement.begin(
            conn,
            project_ref="mov00001",
            source_node="move-source",
            target_node="move-target",
        )
        project = db.one(conn, "SELECT status, node_id FROM projects WHERE id = %s", (project_id,))
        move = db.one(conn, "SELECT * FROM tenant_moves WHERE id = %s", (target.move_id,))

    assert target.project_id == project_id
    assert target.source_node_id == source_id
    assert target.target_node_id == target_id
    assert project == {"status": "MOVING", "node_id": source_id}
    assert move["source_node_id"] == source_id
    assert move["target_node_id"] == target_id
    assert move["source_database"] == "mldb_mov00001"
    assert move["target_database"] == "mldb_mov00001"
    assert move["original_status"] == "ACTIVE"


def test_begin_requires_the_named_source_node(db_pool):  # noqa: ARG001
    _project()
    with db.connection() as conn, pytest.raises(tenant_movement.MovementError, match="not elsewhere"):
        tenant_movement.begin(
            conn,
            project_ref="mov00001",
            source_node="elsewhere",
            target_node="move-target",
        )


def test_begin_refuses_a_target_outside_the_entitled_pool(db_pool):  # noqa: ARG001
    _project(plan_pool="production", target_pool="shared")
    with db.connection() as conn, pytest.raises(tenant_movement.MovementError, match="entitled"):
        tenant_movement.begin(
            conn,
            project_ref="mov00001",
            source_node="move-source",
            target_node="move-target",
        )


def test_begin_refuses_realtime_enabled_projects(db_pool):  # noqa: ARG001
    _project(realtime_enabled=True)
    with db.connection() as conn, pytest.raises(tenant_movement.MovementError, match="Realtime-enabled"):
        tenant_movement.begin(
            conn,
            project_ref="mov00001",
            source_node="move-source",
            target_node="move-target",
        )


def test_begin_refuses_running_workers(db_pool):  # noqa: ARG001
    project_id, _, _ = _project()
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET worker_state = 'RUNNING' WHERE id = %s", (project_id,))
        conn.commit()
    with db.connection() as conn, pytest.raises(tenant_movement.MovementError, match="stop project workers"):
        tenant_movement.begin(
            conn,
            project_ref="mov00001",
            source_node="move-source",
            target_node="move-target",
        )


def test_complete_moves_must_have_verified_ownership(db_pool):  # noqa: ARG001
    project_id, source_id, target_id = _project()
    with db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            conn,
            """
            INSERT INTO tenant_moves
                (project_id, source_node_id, target_node_id, source_database,
                 target_database, original_status, status, finished_at,
                 ownership_verified)
            VALUES (%s,%s,%s,'mldb_mov00001','mldb_mov00001','ACTIVE',
                    'complete', now(), false)
            """,
            (project_id, source_id, target_id),
        )


def test_history_and_drain_report_name_the_operator_work(db_pool):  # noqa: ARG001
    project_id, _, _ = _project()
    with db.connection() as conn:
        target = tenant_movement.begin(
            conn,
            project_ref="mov00001",
            source_node="move-source",
            target_node="move-target",
        )
        history = tenant_movement.history(conn, project_id=project_id)
        drain = tenant_movement.drain_report(conn, node_name="move-source")

    assert history[0]["id"] == target.move_id
    assert history[0]["project_ref"] == "mov00001"
    assert history[0]["status"] == "running"
    assert drain == [
        {
            "project_ref": "mov00001",
            "status": "MOVING",
            "database_name": "mldb_mov00001",
            "plan_code": "move-plan",
        }
    ]


def test_move_preserves_control_plane_identity(monkeypatch, tmp_path, db_pool):  # noqa: ARG001
    project_id, source_id, target_id = _project()
    with db.connection() as conn:
        project = db.one(conn, "SELECT org_id FROM projects WHERE id = %s", (project_id,))
        db.execute(
            conn,
            """
            INSERT INTO api_keys
                (id, project_id, key_type, key_identifier, verification_data)
            VALUES (%s,%s,'secret','kid-move','hash')
            """,
            (uuid.uuid4(), project_id),
        )
        db.execute(
            conn,
            """
            INSERT INTO subscriptions
                (id, org_id, project_id, plan_code, state, state_as_of)
            VALUES (%s,%s,%s,'move-plan','active',now())
            """,
            (uuid.uuid4(), project["org_id"], project_id),
        )
        conn.commit()

    calls: list[str] = []

    # `preflight` and `freeze` both need real node connections; this test drives
    # the control-plane path with `object()` for both admins, so they are stubbed.
    # `test_preflight_*` and `test_a_freeze_*` exercise them against real clusters.
    monkeypatch.setattr(tenant_movement, "preflight", lambda *_: [])
    monkeypatch.setattr(tenant_movement, "roles_refusal", lambda *_, **__: None)
    monkeypatch.setattr(
        tenant_movement,
        "freeze",
        lambda _admin, names: calls.append("freeze")
        or tenant_movement.Freeze(database=names.database, had_connect=("a",)),
    )
    monkeypatch.setattr(
        tenant_movement,
        "dump_from_source",
        lambda *_, **__: calls.append("dump") or (1.5, 4096),
    )
    monkeypatch.setattr(
        tenant_movement,
        "prepare_target_roles",
        lambda conn, target_admin, *, project_id, names, key_ring: calls.append("roles")
        or tenant_movement.entitlements.for_project(conn, project_id),
    )
    monkeypatch.setattr(
        tenant_movement.restore,
        "load_into_target",
        lambda *_, **__: calls.append("load") or 2.5,
    )
    monkeypatch.setattr(
        tenant_movement.extension_data,
        "carry",
        lambda *_, **__: calls.append("carry") or tenant_movement.extension_data.CarryReport(),
    )
    monkeypatch.setattr(
        tenant_movement,
        "finish_target_database",
        lambda *_, **__: calls.append("finish"),
    )
    monkeypatch.setattr(
        tenant_movement,
        "retire_source",
        lambda *_, **__: (calls.append("retire") or "mldb_mov00001_pre_move_20260909000000"),
    )
    monkeypatch.setattr(
        tenant_movement.restore,
        "prepare_dump_dir",
        lambda **_: str(tmp_path),
    )
    monkeypatch.setattr(
        tenant_movement.restore,
        "_run",
        lambda *_, **__: None,
    )

    report = restore.OwnershipReport(
        database="mldb_mov00001",
        expected={"auth": "mldb_mov00001_auth", "storage": "mldb_mov00001_storage"},
        observed={"auth": "mldb_mov00001_auth", "storage": "mldb_mov00001_storage"},
    )
    monkeypatch.setattr(tenant_movement.restore, "verify_ownership", lambda *_, **__: report)

    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn,
            object(),
            object(),
            project_ref="mov00001",
            source_node="move-source",
            target_node="move-target",
            key_ring=object(),
            tenant_connect=lambda *_: nullcontext(object()),
        )
        row = db.one(
            conn,
            """
            SELECT p.project_ref, p.database_name, p.status, p.node_id,
                   k.key_identifier, s.state
              FROM projects p
              JOIN api_keys k ON k.project_id = p.id
              JOIN subscriptions s ON s.project_id = p.id
             WHERE p.id = %s
            """,
            (project_id,),
        )

    assert outcome.ok
    # Retained rather than dropped: the name is what a rollback renames back.
    assert outcome.source_retained
    assert outcome.retained_database == "mldb_mov00001_pre_move_20260909000000"
    # The carry after the load and before anything is verified or repointed, so a
    # carry that cannot be exact fails the move while the source is still live.
    assert calls == ["roles", "freeze", "dump", "load", "carry", "finish", "retire"]
    assert row == {
        "project_ref": "mov00001",
        "database_name": "mldb_mov00001",
        "status": "ACTIVE",
        "node_id": target_id,
        "key_identifier": "kid-move",
        "state": "active",
    }
    assert source_id != target_id


# --------------------------------------------------------------------------
# The freeze (ADR-071)
#
# Ported from a concurrently written second implementation of this slice. The
# freeze is the part worth testing against a real cluster, because every
# alternative to it fails in a way a stub would happily pretend did not happen.
#
# **ADR-040's existing restriction cannot be reused.** It revokes `INSERT` and
# `UPDATE` and leaves `DELETE` and `TRUNCATE` open on purpose, so a project over
# quota can shrink out of it. A tenant frozen that way can still empty a table
# while it is being copied.
#
# **`ALLOW_CONNECTIONS false` cannot be used either.** Measured: it locks out
# superusers too, so `pg_dump` could not run against a database frozen with it.
# --------------------------------------------------------------------------

requires_second_node = pytest.mark.skipif(
    not BACKUP_NODE_DSN,
    reason="MALUDB_BACKUP_NODE_DSN is needed for a real cluster to freeze a tenant on",
)

RUN_AS = os.environ.get("MALUDB_BACKUP_RUN_AS") or "postgres"


def _names(ref: str) -> provisioning.TenantNames:
    return provisioning.TenantNames.for_ref(ref)


def test_the_freeze_covers_every_tenant_role():
    """A role missing from this list keeps its CONNECT and keeps writing.

    Checked against `TenantNames`' own fields rather than a copy of the list, so
    a role added to the tenant model and forgotten here fails the test rather
    than silently surviving a freeze. This is the assertion that would have
    caught the shipped-first freeze, which reached only the direct-SQL roles.
    """
    names = _names("mvt00001")
    covered = set(tenant_movement.tenant_roles(names))

    for f in dataclasses.fields(names):
        if f.name in ("project_ref", "database"):
            continue
        value = getattr(names, f.name)
        assert value in covered, (
            f"{f.name} is a tenant role and is not covered by the freeze; a session "
            "as that role would keep writing while the tenant is copied"
        )


def test_the_retained_name_is_refused_rather_than_truncated():
    """PostgreSQL truncates identifiers at 63 bytes.

    A silently truncated retention name could collide with another project's,
    which would make the one database a move must never destroy the one it
    overwrites.
    """
    names = _names("mvt00002")
    long_names = dataclasses.replace(names, database="d" * 60)
    with pytest.raises(tenant_movement.MovementError, match="truncated"):
        tenant_movement.moved_aside_name(long_names, datetime.now(UTC))


def test_preflight_runs_before_the_freeze_rather_than_after():
    """Ordering, asserted as a property of the code rather than by timing it.

    Every preflight check is cheap, and each one is expensive to discover
    halfway through: a customer is offline for the whole of a move, and a move
    that fails on a missing role has spent that downtime for nothing.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(tenant_movement.move_tenant)))
    called: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("preflight", "prepare_target_roles", "roles_refusal", "freeze"):
                called.append((node.lineno, name))
    order = [name for _, name in sorted(called)]
    assert order == ["preflight", "prepare_target_roles", "roles_refusal", "freeze"], (
        "a move must prove the destination is another cluster, then create and check "
        f"the tenant's roles there, all before the freeze; the order is {order}"
    )


@requires_second_node
def test_a_freeze_stops_the_tenant_and_not_the_platform(tenant):
    """The whole mechanism, measured rather than argued.

    Before: the tenant connects. After: it cannot, and the platform still can --
    which is what makes the copy possible at all, and what rules out
    `ALLOW_CONNECTIONS false`.
    """
    admin, names, passwords = tenant
    dsn = _tenant_dsn(names, passwords)

    with psycopg.connect(dsn) as before:
        assert before.execute("SELECT 1").fetchone()[0] == 1

    state = tenant_movement.freeze(admin, names)
    try:
        assert names.executor in state.had_connect, (
            "the executor held CONNECT and the freeze did not record taking it"
        )
        with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
            psycopg.connect(dsn, connect_timeout=5)

        # The platform is unaffected, which is the half that makes a move possible.
        assert admin.execute("SELECT 1").fetchone()[0] == 1
        with psycopg.connect(
            psycopg.conninfo.make_conninfo(
                **{**psycopg.conninfo.conninfo_to_dict(BACKUP_NODE_DSN), "dbname": names.database}
            )
        ) as platform:
            assert platform.execute("SELECT 1").fetchone()[0] == 1
    finally:
        tenant_movement.release(admin, state)

    with psycopg.connect(dsn) as after:
        assert after.execute("SELECT 1").fetchone()[0] == 1, "release did not give CONNECT back"


@requires_second_node
def test_a_release_gives_back_exactly_what_the_freeze_took(tenant):
    """Not a blanket GRANT.

    `CONNECT` was granted to specific roles at provisioning time, and
    `lock_down_database` revoked it from PUBLIC. A release that issued
    `GRANT CONNECT TO PUBLIC` would leave the tenant reachable by every role on
    the cluster -- a privilege escalation performed by a recovery step.
    """
    admin, names, _ = tenant
    before_public = tenant_movement._one(  # noqa: SLF001
        admin, "SELECT has_database_privilege('public', %s, 'CONNECT') AS ok", (names.database,)
    )["ok"]
    assert before_public is False, "the fixture is not locked down; the assertion below is vacuous"

    state = tenant_movement.freeze(admin, names)
    tenant_movement.release(admin, state)

    after_public = tenant_movement._one(  # noqa: SLF001
        admin, "SELECT has_database_privilege('public', %s, 'CONNECT') AS ok", (names.database,)
    )["ok"]
    assert after_public is False, "the release opened the database to PUBLIC"


@requires_second_node
def test_the_platform_can_still_dump_a_frozen_tenant(tenant):
    """What rules out `ALLOW_CONNECTIONS false`, asserted positively.

    A freeze that also stopped the dump would make the move impossible rather
    than safe.
    """
    admin, names, _ = tenant
    state = tenant_movement.freeze(admin, names)
    try:
        path = f"{restore.prepare_dump_dir(run_as=RUN_AS)}/mvt-{uuid.uuid4().hex}.dump"
        try:
            _, size = tenant_movement.dump_from_source(
                admin, database=names.database, dump_path=path, run_as=RUN_AS
            )
            assert size > 0, "the dump of a frozen tenant produced nothing"
        finally:
            restore._run(["rm", "-f", path], sudo=True)  # noqa: SLF001
    finally:
        tenant_movement.release(admin, state)


@requires_second_node
def test_a_move_onto_the_same_cluster_is_refused(tenant):
    """The one failure with no recovery.

    `_project_for_move` refuses a target whose `nodes` row is the project's own,
    but that is a check on two rows. Two rows can address one cluster through a
    copy-pasted DSN or a node re-registered under a new name, and two
    connections that did would load the tenant's dump over the tenant's own live
    database. `system_identifier` is what makes "these are different clusters"
    checkable rather than assumed -- a host and port can be spelled two ways for
    one cluster; this cannot.
    """
    admin, names, _ = tenant
    second = psycopg.connect(BACKUP_NODE_DSN, autocommit=True)
    try:
        assert tenant_movement.cluster_identity(admin) == tenant_movement.cluster_identity(second)
        problems = tenant_movement.preflight(admin, second, names)
        assert any("same cluster" in p for p in problems), problems
        assert any("no recovery" in p for p in problems)
    finally:
        second.close()


@requires_second_node
def test_a_destination_missing_the_tenants_roles_is_refused_by_the_role_check_not_preflight(tenant):
    """ADR-059's finding, still caught before the customer is taken offline.

    Loading without the roles completes with "errors ignored" and silently
    reassigns `auth` and `storage` to the superuser, so the role check stands --
    but after `prepare_target_roles`, not in preflight, where it refused every
    move onto a cluster that had never had the tenant.
    """
    admin, names, _ = tenant
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset; no second cluster to move to")

    target = psycopg.connect(NODE_ADMIN_DSN, autocommit=True)
    try:
        if tenant_movement.cluster_identity(target) == tenant_movement.cluster_identity(admin):
            pytest.skip("the two DSNs address one cluster")
        problems = tenant_movement.preflight(admin, target, names)
        assert not any("roles" in p for p in problems), problems
        refusal = tenant_movement.roles_refusal(target, names)
        assert refusal and "missing this tenant's roles" in refusal and "ADR-059" in refusal
    finally:
        target.close()


def _stub_move(monkeypatch, tmp_path, *, released: list, dump_error: Exception, release_error=None):
    """The move harness, wired so the copy fails after the freeze is taken."""
    monkeypatch.setattr(tenant_movement, "preflight", lambda *_: [])
    monkeypatch.setattr(tenant_movement, "prepare_target_roles", lambda *_, **__: None)
    monkeypatch.setattr(tenant_movement, "roles_refusal", lambda *_, **__: None)
    monkeypatch.setattr(
        tenant_movement,
        "freeze",
        lambda _admin, names: tenant_movement.Freeze(
            database=names.database, had_connect=(names.executor,)
        ),
    )

    def _release(_admin, state):
        if release_error is not None:
            raise release_error
        released.append(state.database)

    monkeypatch.setattr(tenant_movement, "release", _release)

    def _dump(*_, **__):
        raise dump_error

    monkeypatch.setattr(tenant_movement, "dump_from_source", _dump)
    monkeypatch.setattr(tenant_movement.restore, "prepare_dump_dir", lambda **_: str(tmp_path))
    monkeypatch.setattr(tenant_movement.restore, "_run", lambda *_, **__: None)


def test_a_failed_move_unfreezes_the_source_and_restores_the_status(
    monkeypatch, tmp_path, db_pool
):  # noqa: ARG001
    """A failure before the repointing has to give the tenant back.

    Every failure path in `move_tenant` is raised before the repointing
    `UPDATE`, so the destination has never served traffic and the source is
    still the only live copy. Leaving it frozen would take a tenant offline to
    recover from a move that changed nothing (ADR-071).
    """
    project_id, source_id, _ = _project(ref="mov00002")
    released: list[str] = []
    _stub_move(monkeypatch, tmp_path, released=released, dump_error=RuntimeError("disk full"))

    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn,
            object(),
            object(),
            project_ref="mov00002",
            source_node="move-source",
            target_node="move-target",
            key_ring=object(),
        )
        row = db.one(
            conn, "SELECT status, node_id FROM projects WHERE id = %s", (project_id,)
        )

    assert not outcome.ok
    assert not outcome.still_frozen
    assert released == ["mldb_mov00002"], "the source was left frozen after a failed move"
    assert any("source unfrozen" in note for note in outcome.notes), outcome.notes
    # The project is serving again, on the node it never left.
    assert row == {"status": "ACTIVE", "node_id": source_id}


def test_a_release_that_fails_is_reported_rather_than_swallowed(
    monkeypatch, tmp_path, db_pool
):  # noqa: ARG001
    """The one case an operator has to finish by hand.

    If the release itself fails the tenant really is stranded, and the outcome
    has to say so by name -- a move that reported only "failed" would leave a
    tenant offline with nothing pointing at the reason.
    """
    _project(ref="mov00003")
    _stub_move(
        monkeypatch,
        tmp_path,
        released=[],
        dump_error=RuntimeError("disk full"),
        release_error=psycopg.OperationalError("the node went away"),
    )

    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn,
            object(),
            object(),
            project_ref="mov00003",
            source_node="move-source",
            target_node="move-target",
            key_ring=object(),
        )
        recorded = db.one(
            conn,
            "SELECT still_frozen FROM tenant_moves WHERE id = %s",
            (outcome.move_id,),
        )

    assert not outcome.ok
    assert outcome.still_frozen
    assert recorded["still_frozen"] is True, "a stranded freeze was not recorded for an operator"
    assert any("STILL FROZEN" in note for note in outcome.notes), outcome.notes
    assert any("release-freeze" in note for note in outcome.notes), outcome.notes


def test_a_stranded_freeze_records_exactly_what_it_took(monkeypatch, tmp_path, db_pool):  # noqa: ARG001
    """`release-freeze` restores from this record, so the record has to be exact.

    The tempting implementation of the recovery command reads "every tenant role
    that exists and lacks CONNECT" from the catalogue. After a freeze that is
    *every* role, so it cannot distinguish a role the freeze took CONNECT from
    from one that never had it -- a lingering `replicator` on a project whose
    Realtime was turned off, say. Recording what was taken is what makes giving
    it back safe; `test_a_role_without_connect_is_not_handed_it_by_the_release`
    asserts the same property against a real cluster.
    """
    _project(ref="mov00004")
    _stub_move(
        monkeypatch,
        tmp_path,
        released=[],
        dump_error=RuntimeError("disk full"),
        release_error=psycopg.OperationalError("the node went away"),
    )

    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn,
            object(),
            object(),
            project_ref="mov00004",
            source_node="move-source",
            target_node="move-target",
            key_ring=object(),
        )
        row = db.one(
            conn,
            "SELECT frozen_roles, frozen_public FROM tenant_moves WHERE id = %s",
            (outcome.move_id,),
        )

    names = _names("mov00004")
    assert row["frozen_roles"] == [names.executor], row["frozen_roles"]
    assert row["frozen_public"] is False
    # The roles that never hold CONNECT must not appear, or the release grants it.
    for never in (names.authenticator, names.auth, names.admin, names.replicator):
        assert never not in row["frozen_roles"]


@requires_second_node
def test_a_role_without_connect_is_not_handed_it_by_the_release(tenant):
    """The non-escalation property, against a real provisioned tenant.

    `release` restores from what `freeze` recorded, so a role that did not hold
    CONNECT going in must not hold it coming out. The alternative implementation
    -- inferring the set from "every tenant role that now lacks CONNECT", which
    after a freeze is all of them -- cannot tell the two cases apart and would
    grant CONNECT to a role that never had it.

    `admin` is the one revoked here because it is a real case: a tenant role
    that exists on the cluster and can be deliberately kept out of a database.
    A lingering `replicator` on a project whose Realtime was turned off is the
    same shape.
    """
    admin, names, _ = tenant
    admin.execute(
        psycopg.sql.SQL("REVOKE CONNECT ON DATABASE {db} FROM {role}").format(
            db=psycopg.sql.Identifier(names.database),
            role=psycopg.sql.Identifier(names.admin),
        )
    )
    assert not tenant_movement._has_connect(admin, names.database, names.admin)  # noqa: SLF001

    state = tenant_movement.freeze(admin, names)
    assert names.admin not in state.had_connect, (
        "the freeze recorded taking CONNECT from a role that did not have it"
    )
    assert names.executor in state.had_connect, "the freeze recorded nothing useful"
    assert state.public_had_connect is False

    tenant_movement.release(admin, state)

    assert not tenant_movement._has_connect(admin, names.database, names.admin), (  # noqa: SLF001
        "the release granted CONNECT to a role that did not have it before the freeze"
    )
    assert tenant_movement._has_connect(admin, names.database, names.executor)  # noqa: SLF001


@requires_second_node
def test_release_freeze_recovers_a_stranded_tenant(
    tenant, key_ring, capsys, monkeypatch, tmp_path
):
    """`cp-manage node release-freeze`, against a real frozen tenant.

    This is a recovery path for a state the platform reaches only when something
    else has already gone wrong, which is exactly the kind of code that is never
    exercised until it is needed. It drives the real command rather than the
    helper, because what has to work at that point is the command.
    """
    import argparse

    from services.control_plane import manage
    from services.control_plane import nodes as node_mod
    from tests.conftest import TEST_KEK, TEST_PEPPER

    admin, names, passwords = tenant
    dsn = _tenant_dsn(names, passwords)

    # `cp-manage` builds its configuration from the environment as a real
    # operator invocation does, so the test supplies both key-material refs
    # rather than inheriting whatever the developer's shell happens to export.
    for variable, material in (
        ("MALUDB_KEK_REF", TEST_KEK),
        ("MALUDB_TOKEN_PEPPER_REF", TEST_PEPPER),
    ):
        path = tmp_path / variable.lower()
        path.write_bytes(material)
        path.chmod(0o600)  # the loader refuses group/world-readable key material
        monkeypatch.setenv(variable, str(path))

    project_id, source_id, target_id = _project(ref="mvt00010", source_node="frozen-source")
    with db.connection() as conn:
        node_mod.set_admin_dsn(conn, name="frozen-source", dsn=BACKUP_NODE_DSN, key_ring=key_ring)
        conn.commit()

    # A stranded move: frozen, and the release failed. Written directly because
    # producing it for real means killing a node mid-move.
    state = tenant_movement.freeze(admin, names)
    with db.connection() as conn:
        db.execute(
            conn,
            """
            INSERT INTO tenant_moves
                (project_id, source_node_id, target_node_id, source_database,
                 target_database, original_status, status, finished_at,
                 still_frozen, frozen_roles, frozen_public, error)
            VALUES (%s,%s,%s,%s,%s,'ACTIVE','failed',now(),TRUE,%s,%s,'the node went away')
            """,
            (
                project_id,
                source_id,
                target_id,
                names.database,
                names.database,
                list(state.had_connect),
                state.public_had_connect,
            ),
        )
        conn.commit()

    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        psycopg.connect(dsn, connect_timeout=5)

    rc = manage._cmd_node_release_freeze(  # noqa: SLF001
        argparse.Namespace(name="frozen-source", database=names.database)
    )
    assert rc == 0, capsys.readouterr().out

    with psycopg.connect(dsn) as after:
        assert after.execute("SELECT 1").fetchone()[0] == 1, "the tenant is still frozen"

    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT still_frozen FROM tenant_moves WHERE source_database = %s",
            (names.database,),
        )
    assert row["still_frozen"] is False, "the stranded flag was not cleared"


@requires_second_node
def test_release_freeze_refuses_a_database_it_has_no_record_for(
    tenant, key_ring, capsys, monkeypatch, tmp_path
):
    """No record means nothing safe to give back, so it refuses rather than guesses.

    The failure mode this rules out is the command reconstructing the grant set
    from the catalogue and handing CONNECT to roles that never held it.
    """
    import argparse

    from services.control_plane import manage
    from services.control_plane import nodes as node_mod
    from tests.conftest import TEST_KEK, TEST_PEPPER

    admin, names, _ = tenant
    for variable, material in (
        ("MALUDB_KEK_REF", TEST_KEK),
        ("MALUDB_TOKEN_PEPPER_REF", TEST_PEPPER),
    ):
        path = tmp_path / variable.lower()
        path.write_bytes(material)
        path.chmod(0o600)
        monkeypatch.setenv(variable, str(path))

    _project(ref="mvt00010", source_node="frozen-source")
    with db.connection() as conn:
        node_mod.set_admin_dsn(conn, name="frozen-source", dsn=BACKUP_NODE_DSN, key_ring=key_ring)
        conn.commit()

    state = tenant_movement.freeze(admin, names)
    try:
        rc = manage._cmd_node_release_freeze(  # noqa: SLF001
            argparse.Namespace(name="frozen-source", database=names.database)
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "no freeze on record" in out
        assert "move-history" in out, "the refusal does not say where to look"
    finally:
        tenant_movement.release(admin, state)


# --------------------------------------------------------------------------
# A real move, end to end, between two real clusters
#
# Everything above either stubs the copy or stops at the freeze. This drives
# `move_tenant` itself from the node under test to the backup cluster, with
# nothing replaced: the freeze, `pg_dump`, the target's roles from the vault,
# `pg_restore`, the maludb_core carry, the lock-down, the ownership check, the
# repointing and the retirement. What it asserts is what a customer would
# notice: that their table rows and their vector search came across unchanged,
# through the same wrappers they call.
# --------------------------------------------------------------------------

MOVE_REF = "mve00001"
MOVE_SOURCE, MOVE_TARGET = "mve-source", "mve-target"
BEFORE_REGISTRATION = "0.104.0"
VECTOR_NS = ("mve", "doc", "about")

requires_two_nodes = pytest.mark.skipif(
    not (NODE_ADMIN_DSN and BACKUP_NODE_DSN),
    reason="a real move needs MALUDB_NODE_ADMIN_DSN and MALUDB_BACKUP_NODE_DSN",
)


def _dsn_for(admin_dsn: str, database: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    info["dbname"] = database
    return psycopg.conninfo.make_conninfo(**info)


def _drop_everywhere(admin, ref: str) -> None:
    names = _names(ref)
    for (datname,) in admin.execute(
        "SELECT datname FROM pg_database WHERE datname = %s OR datname LIKE %s",
        (names.database, f"{names.database}\\_pre\\_move\\_%"),
    ).fetchall():
        admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            psycopg.sql.Identifier(datname)))
    for (role,) in admin.execute(
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (f"mldb\\_{ref}\\_%",)
    ).fetchall():
        admin.execute(psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role)))


def _vector_search(dsn: str) -> list[tuple]:
    """The customer's search, through the wrapper PostgREST serves, as service_role."""
    with psycopg.connect(dsn) as t:
        t.execute("SET ROLE service_role")
        return t.execute(
            "SELECT id, content, metadata, similarity, distance "
            "FROM maludb.vector_search(%s, %s, %s, '[1,2,2]'::vector, 5)", VECTOR_NS
        ).fetchall()


@pytest.fixture
def movable_tenant(request, db_pool, key_ring):  # noqa: ARG001
    """A tenant on the node under test with vectors enabled and data in both stores.

    Provisioned the platform's way, with its credentials sealed in the vault --
    `prepare_target_roles` reads them back to recreate the roles on the target --
    and vector compartments enabled through `maludb_vectors.enable`, so the
    definer role, its grants and the wrappers are the real ones the dump has to
    carry. `request.param` is the source's maludb_core version, None for the node's.
    """
    from services.control_plane import maludb_vectors, tenant_bootstrap

    if not (NODE_ADMIN_DSN and BACKUP_NODE_DSN):
        pytest.skip("a real move needs MALUDB_NODE_ADMIN_DSN and MALUDB_BACKUP_NODE_DSN")
    version = getattr(request, "param", None)
    names = _names(MOVE_REF)
    source_admin = psycopg.connect(NODE_ADMIN_DSN, autocommit=True)
    target_admin = psycopg.connect(BACKUP_NODE_DSN, autocommit=True)
    try:
        if tenant_movement.cluster_identity(source_admin) == tenant_movement.cluster_identity(target_admin):
            pytest.skip("the two DSNs address one cluster")
        if version and not source_admin.execute(
            "SELECT 1 FROM pg_available_extension_versions WHERE name = 'maludb_core' AND version = %s",
            (version,),
        ).fetchone():
            pytest.skip(f"maludb_core {version} is not installable on the node under test")
        _drop_everywhere(source_admin, MOVE_REF)
        _drop_everywhere(target_admin, MOVE_REF)

        passwords = {
            k: provisioning.generate_password()
            for k in ("authenticator", "auth", "admin", "executor", "client", "storage")
        }
        owner = source_admin.info.user
        with psycopg.connect(NODE_ADMIN_DSN) as conn:
            provisioning.ensure_shared_roles(conn)
            provisioning.create_roles(conn, names, passwords=passwords,
                                      connection_limits={"authenticator": 20, "auth": 10})
            provisioning.create_executor_role(conn, names, password=passwords["executor"])
            provisioning.create_client_role(conn, names, password=passwords["client"])
            provisioning.create_storage_role(conn, names, password=passwords["storage"])
            conn.commit()
            provisioning.create_database(conn, names, owner=owner)
            provisioning.lock_down_database(conn, names)
            provisioning.grant_executor_connect(conn, names)
            provisioning.grant_client_connect(conn, names)
            provisioning.grant_storage_connect(conn, names)
            conn.commit()
        source_dsn = _dsn_for(NODE_ADMIN_DSN, names.database)
        with psycopg.connect(source_dsn, autocommit=True) as t:
            pins = dict(t.execute(
                "SELECT name, default_version FROM pg_available_extensions "
                "WHERE name IN ('vector', 'maludb_core')").fetchall())
            if version and version != pins["maludb_core"]:
                # A tenant provisioned before its node was upgraded, made
                # directly: `install_extension` refuses anything but the pin.
                t.execute(psycopg.sql.SQL("CREATE EXTENSION vector VERSION {}").format(
                    psycopg.sql.Literal(pins["vector"])))
                t.execute(psycopg.sql.SQL("CREATE EXTENSION maludb_core VERSION {} CASCADE").format(
                    psycopg.sql.Literal(version)))
            else:
                provisioning.install_extension(t, pins=pins)
            tenant_bootstrap.apply(t)

        source_id = _node(MOVE_SOURCE)
        target_id = _node(MOVE_TARGET)
        with db.connection() as conn:
            plan = db.one(
                conn,
                "INSERT INTO plans (code, name, config_json) VALUES ('mve-plan','mve',%s) "
                "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
                (Jsonb({"node_pool": "shared", "maludb_vectors": True}),),
            )["id"]
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{MOVE_REF}@example.com", password=TEST_CREDENTIAL
            )
            project_id = uuid.uuid4()
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                "node_id, database_name) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s)",
                (project_id, org, MOVE_REF, MOVE_REF, plan, source_id, names.database),
            )
            for kind, role in (
                ("authenticator", names.authenticator), ("auth", names.auth),
                ("admin", names.admin), ("executor", names.executor),
                ("client", names.client), ("storage", names.storage),
            ):
                provisioning.store_credential(
                    conn, project_id=project_id, credential_type=f"db_{kind}",
                    role_name=role, secret=passwords[kind], key_ring=key_ring,
                )
            conn.commit()
            maludb_vectors.enable(
                conn, project_id=project_id,
                tenant_connect=lambda database: psycopg.connect(
                    _dsn_for(NODE_ADMIN_DSN, database), autocommit=True),
            )

        # Customer data, both kinds: an ordinary table in `public`, and vectors
        # written the way a customer writes them.
        with psycopg.connect(source_dsn) as t:
            t.execute("CREATE TABLE public.move_marker (id bigserial PRIMARY KEY, note text NOT NULL)")
            t.execute("INSERT INTO public.move_marker (note) SELECT 'row ' || g FROM generate_series(1, 50) g")
            t.execute("SET ROLE service_role")
            t.execute("SELECT maludb.vector_compartment_create(%s, %s, %s, 3)", VECTOR_NS)
            for content, embedding in (("hello", "[1,2,3]"), ("world", "[3,2,1]"), ("near", "[1,2,2]")):
                t.execute("SELECT maludb.vector_insert(%s, %s, %s, %s, %s::vector, '{\"k\": 1}')",
                          (*VECTOR_NS, content, embedding))
        yield {
            "names": names, "project_id": project_id, "source_id": source_id,
            "target_id": target_id, "source_admin": source_admin,
            "target_admin": target_admin, "source_dsn": source_dsn, "version": version,
        }
    finally:
        for admin in (source_admin, target_admin):
            try:
                _drop_everywhere(admin, MOVE_REF)
            finally:
                admin.close()


@requires_two_nodes
@pytest.mark.parametrize(
    "movable_tenant",
    [None, BEFORE_REGISTRATION],
    ids=["registering-source", "source-at-0.104.0"],
    indirect=True,
)
def test_a_tenant_with_vectors_moves_between_two_real_clusters(movable_tenant, key_ring, monkeypatch):
    """`move_tenant` end to end, and the customer cannot tell it happened.

    Two variants, one per side of ADR-078. A source at the node's maludb_core
    (0.105.0, which registers its tables) has its vector store carried by the
    dump, and the carry must step aside -- a carry that copied too would find the
    target already holding the rows and fail every move. A source still at
    0.104.0 registers nothing, so the dump leaves the vector store behind and the
    carry copies it. Either way the search answers identically afterwards.
    """
    from services.control_plane import extension_data, maludb_vectors

    t = movable_tenant
    names = t["names"]
    before_search = _vector_search(t["source_dsn"])
    assert [row[1] for row in before_search][:1] == ["near"], before_search
    with psycopg.connect(t["source_dsn"]) as s:
        before_rows = s.execute("SELECT id, note FROM public.move_marker ORDER BY id").fetchall()
    assert len(before_rows) == 50

    # Observed, not replaced: the real carry runs and its report is kept.
    reports: list = []
    real_carry = extension_data.carry
    monkeypatch.setattr(
        tenant_movement.extension_data, "carry",
        lambda source, target: reports.append(real_carry(source, target)) or reports[-1],
    )

    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn, t["source_admin"], t["target_admin"],
            project_ref=MOVE_REF, source_node=MOVE_SOURCE, target_node=MOVE_TARGET,
            key_ring=key_ring, platform_owner="postgres", run_as=RUN_AS,
        )
        project = db.one(conn, "SELECT status, node_id FROM projects WHERE id = %s", (t["project_id"],))
        move = db.one(conn, "SELECT status, ownership_verified, retained_database, still_frozen "
                            "FROM tenant_moves WHERE id = %s", (outcome.move_id,))

    assert outcome.ok, f"{outcome.error} {outcome.notes}"

    # The carry ran once, and did what the source's extension version calls for.
    assert len(reports) == 1
    if t["version"] is None:
        assert reports[0].by_dump == list(extension_data.CARRIED_TABLES), reports[0]
        assert reports[0].rows == {}, "the dump carried the vector store and the carry copied it again"
    else:
        assert reports[0].by_dump == [], reports[0]
        assert reports[0].rows.get("malu$vector_chunk") == 3, reports[0]

    # Ownership verified, recorded, and the project repointed and serving.
    assert outcome.ownership is not None and outcome.ownership.verified, outcome.ownership
    assert move == {"status": "complete", "ownership_verified": True,
                    "retained_database": outcome.retained_database, "still_frozen": False}
    assert project == {"status": "ACTIVE", "node_id": t["target_id"]}

    # The customer's data arrived: rows, and the vector search answers identically.
    target_dsn = _dsn_for(BACKUP_NODE_DSN, names.database)
    with psycopg.connect(target_dsn) as d:
        assert d.execute("SELECT id, note FROM public.move_marker ORDER BY id").fetchall() == before_rows
        # The sequence came with it: a new row does not collide with a carried one.
        assert d.execute("INSERT INTO public.move_marker (note) VALUES ('after') RETURNING id").fetchone()[0] > 50
        d.rollback()
    after_search = _vector_search(target_dsn)
    # Same chunks, same order, same content and metadata. The scores agree only to
    # float4 print precision, and that is a finding rather than slack: maludb_core
    # stores embeddings normalized as float4 and `malu_vector`'s text output prints
    # six significant digits ([1,2,2] is stored and dumped as
    # [0.333333, 0.666667, 0.666667]), so a text-format `pg_dump` -- and the text
    # COPY the carry uses -- rounds every stored embedding. Measured here as a
    # cosine similarity of 1.0000000596 on the source and 1.0000003378 after.
    assert [row[:3] for row in after_search] == [row[:3] for row in before_search]
    for after, before in zip(after_search, before_search, strict=True):
        assert after[3:] == pytest.approx(before[3:], abs=1e-5), (after, before)

    with psycopg.connect(target_dsn) as d:
        # The target's version is the target's pin, whatever the source ran.
        assert d.execute("SELECT extversion FROM pg_extension WHERE extname = 'maludb_core'").fetchone()[0] \
            == tenant_movement._one(t["target_admin"], "SELECT default_version AS v FROM pg_available_extensions "  # noqa: SLF001
                                    "WHERE name = 'maludb_core'")["v"]
        # Locked down like any provisioned tenant, and not opened to PUBLIC.
        assert d.execute("SELECT has_database_privilege('public', %s, 'CONNECT')",
                         (names.database,)).fetchone()[0] is False
        # The definer came across holding no more than the target's extension
        # needs: its grants are in the dump and its role was made before the load
        # (ADR-077). And the wrappers work as a customer calls them.
        maludb_vectors.assert_definer(d, names, maludb_vectors.derive_reach(d))
        maludb_vectors.exercise_wrappers(d)
        d.rollback()

    # The source is retained under its renamed name, never dropped, and still frozen.
    assert outcome.source_retained
    assert outcome.retained_database and outcome.retained_database.startswith(f"{names.database}_pre_move_")
    source_admin = t["source_admin"]
    assert source_admin.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                                (names.database,)).fetchone() is None
    assert source_admin.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                                (outcome.retained_database,)).fetchone() is not None
    assert not tenant_movement._has_connect(source_admin, outcome.retained_database, names.executor)  # noqa: SLF001
    with psycopg.connect(_dsn_for(NODE_ADMIN_DSN, outcome.retained_database)) as r:
        assert r.execute("SELECT count(*) FROM public.move_marker").fetchone()[0] == 50


@requires_two_nodes
def test_a_move_onto_a_cluster_that_has_never_seen_the_tenant_is_not_refused(movable_tenant, key_ring):
    """The ordinary move, with nothing pre-created on the destination.

    Until the role check moved out of preflight, every such move was refused before
    it started; the end-to-end test above pre-created the roles to get past it.
    """
    t = movable_tenant
    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn, t["source_admin"], t["target_admin"],
            project_ref=MOVE_REF, source_node=MOVE_SOURCE, target_node=MOVE_TARGET,
            key_ring=key_ring, platform_owner="postgres", run_as=RUN_AS,
        )
    assert outcome.ok, outcome.error


@requires_two_nodes
def test_a_tenant_with_a_memory_space_moves_and_its_writer_writes_on_the_target(movable_tenant, key_ring):
    """ADR-079 memory slice 2b: a space, and the writer's grants on it, survive a move.

    Memory slice 1 measured that `pg_restore` drops every grant to a role the
    target lacks. So the writer is created on the target before the load, from
    its sealed password, and given CONNECT after -- and the proof is the worker's
    own path: the writer logs in to the moved database with the stored password
    and ingests into its space, beside the memory it wrote before the move.
    """
    from services.control_plane import maludb_memory
    from tests.test_memory_spaces import _writer_dsn, writer_ingests

    t = movable_tenant
    names = t["names"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET config_json = config_json || %s WHERE code = 'mve-plan'",
                   (Jsonb({"maludb_memory": True}),))
        db.execute(conn, "INSERT INTO memory_spaces (project_id, name, schema_name) VALUES (%s, 'bot', 'mem_bot')",
                   (t["project_id"],))
        conn.commit()
        built = maludb_memory.build_pending(
            conn, project_id=t["project_id"], key_ring=key_ring,
            tenant_connect=lambda database: psycopg.connect(_dsn_for(NODE_ADMIN_DSN, database), autocommit=True),
        )
        assert built.created == ["bot"], built.failed
        password = provisioning.load_credential(conn, project_id=t["project_id"], credential_type="db_memwriter",
                                                key_ring=key_ring)
    writer_ingests(_writer_dsn(NODE_ADMIN_DSN, names.database, names.memwriter, password), "mem_bot", "before")

    with db.connection() as conn:
        outcome = tenant_movement.move_tenant(
            conn, t["source_admin"], t["target_admin"],
            project_ref=MOVE_REF, source_node=MOVE_SOURCE, target_node=MOVE_TARGET,
            key_ring=key_ring, platform_owner="postgres", run_as=RUN_AS,
        )
    assert outcome.ok, outcome.error

    writer_ingests(_writer_dsn(BACKUP_NODE_DSN, names.database, names.memwriter, password), "mem_bot", "after")
    with psycopg.connect(_dsn_for(BACKUP_NODE_DSN, names.database)) as target:
        subjects = sorted(r[0] for r in target.execute(
            "SELECT s.canonical_name FROM maludb_core.\"malu$svpor_subject\" s "
            "WHERE s.owner_schema = 'mem_bot' AND s.canonical_name IN ('before', 'after')").fetchall())
        closed = target.execute(
            "SELECT bool_and(NOT has_schema_privilege(r, 'mem_bot', 'USAGE')) FROM unnest(%s::text[]) r",
            (["anon", "authenticated", "service_role", names.authenticator, names.admin],)).fetchone()[0]
    assert subjects == ["after", "before"], "the space's memory arrived and the writer wrote beside it"
    assert closed, "no customer role reaches the moved space"
