"""Tenant movement (Phase 11 slice 7, ADR-066).

These tests cover the control-plane side of movement: a move is explicit,
records itself, takes the project out of service while it runs, and refuses
states that would silently move only part of a project. The end-to-end database
copy uses node-local pg_dump/pg_restore and is exercised by the operator path;
these guards are the part that can be checked without a second PostgreSQL node.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, identity, restore, tenant_movement
from tests.conftest import TEST_CREDENTIAL, requires_db

pytestmark = requires_db


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
            VALUES (%s,%s,'anon','kid-move','hash')
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

    monkeypatch.setattr(tenant_movement, "freeze_source", lambda *_: calls.append("freeze"))
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
        tenant_movement,
        "finish_target_database",
        lambda *_, **__: calls.append("finish"),
    )
    monkeypatch.setattr(
        tenant_movement,
        "clean_source",
        lambda *_: calls.append("clean"),
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
    assert outcome.source_cleaned
    assert calls == ["freeze", "dump", "roles", "load", "finish", "clean"]
    assert row == {
        "project_ref": "mov00001",
        "database_name": "mldb_mov00001",
        "status": "ACTIVE",
        "node_id": target_id,
        "key_identifier": "kid-move",
        "state": "active",
    }
    assert source_id != target_id
