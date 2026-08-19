"""Changing a project's plan (Phase 09 slice 1).

Slice 0 made a plan true on its node. This is the operation that changes which
plan that is, and the acceptance criterion it answers is the one ADR-006 has
promised since Phase 01: an upgrade keeps the database.

The design differs from the plan in one place, and the reason is measured
rather than argued. `projects.status` reserves `UPGRADING` and nothing sets it,
so it was the obvious in-flight marker -- until three separate gates turned out
to serve only `("PROVISIONED", "ACTIVE")`: the gateway's `SERVING_STATUSES`,
`api/tenant_access.py` for the SQL and schema routes, and `workers.py` for
starting a worker. Parking a project there would take its data API, its console
and its workers offline for the duration of a purchase. `plan_changes` carries
the marker instead and the status is not touched, which the first test asserts.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from services.control_plane import db, plan_apply, plan_change
from tests.conftest import requires_db
from tests.test_direct_sql import paid_project  # noqa: F401 - fixture
from tests.test_plan_apply import _entitlements_for
from tests.test_provisioning import ADMIN_DSN, _tenant_dsn, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")


def _plan(code: str, config: dict) -> None:
    """A plan in the catalogue, or the same one updated."""
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, is_active, config_json) "
            "VALUES (%s, %s, TRUE, %s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json, "
            "                                 is_active = EXCLUDED.is_active",
            (code, code.title(), psycopg.types.json.Jsonb(config)),
        )
        conn.commit()


def _status(project_id) -> str:
    with db.connection() as conn:
        return db.one(conn, "SELECT status FROM projects WHERE id = %s", (project_id,))["status"]


def _plan_code(project_id) -> str:
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT pl.code FROM projects pr JOIN plans pl ON pl.id = pr.plan_id "
            " WHERE pr.id = %s",
            (project_id,),
        )["code"]


# -- refusals, which need no node ------------------------------------------


@requires_node
@requires_maludb_core
def test_an_unknown_plan_is_refused_rather_than_half_applied(paid_project, admin_conn):  # noqa: F811
    project_id, _, _ = paid_project("chg00001")
    before = _plan_code(project_id)

    with db.connection() as conn, pytest.raises(plan_change.PlanChangeError, match="no active plan"):
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="no-such-plan"
        )

    assert _plan_code(project_id) == before


@requires_node
@requires_maludb_core
def test_a_retired_plan_is_refused_the_same_way_a_misspelled_one_is(paid_project, admin_conn):  # noqa: F811
    """`plan_by_code` filters on `is_active`, so both are the operator's to fix
    and neither should half-run."""
    project_id, _, _ = paid_project("chg00002")
    _plan("retired-tier", {"direct_database_access": True})
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET is_active = FALSE WHERE code = 'retired-tier'")
        conn.commit()

    with db.connection() as conn, pytest.raises(plan_change.PlanChangeError):
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="retired-tier"
        )


# -- the operation ---------------------------------------------------------


@requires_node
@requires_maludb_core
def test_a_plan_change_keeps_the_database_the_ref_the_node_and_the_keys(paid_project, admin_conn):  # noqa: F811
    """Acceptance criterion 1. Asserted rather than assumed: "we wrote no code
    that moves a database" is a claim about the present."""
    project_id, _, _ = paid_project("chg00003")
    _plan("bigger-tier", {"direct_database_access": True, "limits": {"work_mem_mb": 32}})

    with db.connection() as conn:
        before = plan_change.identity(conn, project_id)
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="bigger-tier"
        )
        after = plan_change.identity(conn, project_id)

    assert after == before
    assert _plan_code(project_id) == "bigger-tier"


@requires_node
@requires_maludb_core
def test_the_project_never_leaves_a_serving_status(paid_project, admin_conn):  # noqa: F811
    """The measured reason this does not use `UPGRADING`. Three gates serve
    only PROVISIONED and ACTIVE, so a status change here is an outage for the
    duration of a purchase."""
    from services.control_plane.api import tenant_access
    from services.gateway import app as gateway_app

    project_id, _, _ = paid_project("chg00004")
    _plan("serving-tier", {"direct_database_access": True})
    before = _status(project_id)

    with db.connection() as conn:
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="serving-tier"
        )

    assert _status(project_id) == before
    assert before in gateway_app.SERVING_STATUSES
    assert before in tenant_access.SERVING_STATUSES


@requires_node
@requires_maludb_core
def test_the_change_reaches_the_node_rather_than_only_the_row(paid_project, admin_conn):  # noqa: F811
    """The whole point of slice 0 being first. Before it, this assertion would
    have passed on the row and failed on the node."""
    project_id, names, _ = paid_project("chg00005")
    _plan("nodey-tier", {"direct_database_access": True, "limits": {"work_mem_mb": 55}})

    with db.connection() as conn:
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="nodey-tier"
        )

    observed = plan_apply.read_roles(admin_conn, names)
    assert observed[names.admin].settings["work_mem"] == "55MB"
    assert plan_apply.inspect(admin_conn, names, _entitlements_for(project_id)).clean


@requires_node
@requires_maludb_core
def test_a_downgrade_revokes_direct_access_on_the_node(paid_project, admin_conn):  # noqa: F811
    """And the reason the node is written before the row: a downgrade that
    updated the row first and then failed would leave access live on a node
    while the plan says the project no longer has it."""
    project_id, names, passwords = paid_project("chg00006")
    _plan("no-direct-tier", {"direct_database_access": False})

    with db.connection() as conn:
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="no-direct-tier"
        )

    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"]))


@requires_node
@requires_maludb_core
def test_moving_to_the_same_plan_changes_nothing_and_says_so(paid_project, admin_conn):  # noqa: F811
    """An operator running the command twice, and a webhook delivered twice in
    slice 4, are the same event. Neither should write a history row."""
    project_id, _, _ = paid_project("chg00007")
    current = _plan_code(project_id)

    with db.connection() as conn:
        change = plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code=current
        )
        assert change.unchanged
        assert plan_change.history(conn, project_id) == []


@requires_node
@requires_maludb_core
def test_a_second_change_is_refused_while_one_is_running(paid_project, admin_conn):  # noqa: F811
    """Two concurrent changes could interleave `ALTER ROLE`s and leave the
    tenant on neither plan. The unique index is what stops it, so the test
    leaves a RUNNING row rather than trying to race."""
    project_id, _, _ = paid_project("chg00008")
    _plan("second-tier", {"direct_database_access": True})
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plan_changes (id, project_id, from_plan_code, to_plan_code) "
            "VALUES (%s, %s, 'free', 'second-tier')",
            (uuid.uuid4(), project_id),
        )
        conn.commit()

    with db.connection() as conn, pytest.raises(plan_change.PlanChangeError, match="already running"):
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="second-tier"
        )


@requires_node
@requires_maludb_core
def test_a_failure_records_itself_and_leaves_the_plan_alone(paid_project, admin_conn):  # noqa: F811
    """A node that cannot be written to must not leave the row saying the
    customer got something they did not."""
    project_id, _, _ = paid_project("chg00009")
    _plan("failing-tier", {"direct_database_access": True})
    before = _plan_code(project_id)
    # A node that cannot be written to, which is the realistic failure -- an
    # unreachable node or a connection lost mid-change. Deliberately not a
    # `PlanChangeError`: this function only summarises what an operator can act
    # on, and "the node went away" is not something it should paraphrase.
    admin_conn.close()

    with db.connection() as conn, pytest.raises(psycopg.Error):
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="failing-tier"
        )

    assert _plan_code(project_id) == before
    with db.connection() as conn:
        recorded = plan_change.history(conn, project_id)
    assert recorded and recorded[0]["state"] == "FAILED"
    assert recorded[0]["error"]
    # And what it recorded is not the driver's own message. The node work runs
    # on a decrypted superuser DSN, a psycopg connection error can echo it, and
    # `cp-manage project plan-history` prints this column back.
    assert "postgresql://" not in recorded[0]["error"]
    assert ADMIN_DSN.split("@")[-1] not in recorded[0]["error"]


def test_failure_text_never_carries_the_drivers_own_message():
    """The unit of the above, so it holds without a node. `str(exc)` on a
    connection failure is the shape that leaks a DSN."""
    leaky = psycopg.OperationalError(
        'connection to server failed: FATAL: password authentication failed for user "cp"'
    )
    recorded = plan_change._failure_text(leaky)

    assert "password" not in recorded
    assert recorded.startswith("OperationalError")
    # This module's own refusals are safe and are kept in full.
    assert "no active plan" in plan_change._failure_text(
        plan_change.PlanChangeError("no active plan with code 'x'")
    )


@requires_node
@requires_maludb_core
def test_an_open_upgrade_request_for_that_plan_is_closed(paid_project, admin_conn):  # noqa: F811
    """The queue Phase 07 built has rows waiting in it, and an operator who
    fulfils one should not have to close it by hand."""
    project_id, _, _ = paid_project("chg00010")
    _plan("asked-for-tier", {"direct_database_access": True})
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO upgrade_requests (id, project_id, requested_plan_code) "
            "VALUES (%s, %s, 'asked-for-tier')",
            (uuid.uuid4(), project_id),
        )
        conn.commit()

    with db.connection() as conn:
        change = plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="asked-for-tier"
        )
        assert change.closed_request is not None
        assert db.one(
            conn,
            "SELECT count(*) AS n FROM upgrade_requests "
            " WHERE project_id = %s AND state <> 'CLOSED'",
            (project_id,),
        )["n"] == 0


@requires_node
@requires_maludb_core
def test_a_request_for_a_different_plan_stays_open(paid_project, admin_conn):  # noqa: F811
    """A project moved somewhere other than where it asked to go has not had
    its question answered, and closing the row would drop it out of the queue."""
    project_id, _, _ = paid_project("chg00011")
    _plan("asked-tier", {"direct_database_access": True})
    _plan("given-tier", {"direct_database_access": True})
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO upgrade_requests (id, project_id, requested_plan_code) "
            "VALUES (%s, %s, 'asked-tier')",
            (uuid.uuid4(), project_id),
        )
        conn.commit()

    with db.connection() as conn:
        change = plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="given-tier"
        )
        assert change.closed_request is None
        assert db.one(
            conn,
            "SELECT count(*) AS n FROM upgrade_requests "
            " WHERE project_id = %s AND state <> 'CLOSED'",
            (project_id,),
        )["n"] == 1


@requires_node
@requires_maludb_core
def test_the_change_is_visible_in_the_customers_own_audit_trail(paid_project, admin_conn):  # noqa: F811
    """Allowlisted event by event, and `database_retained` is stated by the
    operation rather than left for the customer to trust."""
    from services.control_plane.api import audit

    project_id, _, _ = paid_project("chg00012")
    _plan("audited-tier", {"direct_database_access": True})

    with db.connection() as conn:
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="audited-tier"
        )
        event = db.one(
            conn,
            "SELECT event_type, detail_json FROM audit_events "
            " WHERE project_id = %s AND event_type = %s",
            (project_id, plan_change.CHANGED),
        )

    assert event is not None
    assert event["detail_json"]["to_plan"] == "audited-tier"
    assert event["detail_json"]["database_retained"] is True
    assert plan_change.CHANGED in audit.VISIBLE_EVENTS


@requires_node
@requires_maludb_core
def test_a_deleting_project_cannot_be_moved_to_a_paid_plan(paid_project, admin_conn):  # noqa: F811
    """Otherwise a project on its way out acquires entitlements, and whatever
    bills for them in slice 4 has a row saying it should."""
    project_id, _, _ = paid_project("chg00013")
    _plan("late-tier", {"direct_database_access": True})
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status = 'DELETING' WHERE id = %s", (project_id,))
        conn.commit()

    with db.connection() as conn, pytest.raises(plan_change.PlanChangeError, match="deleted"):
        plan_change.change_plan(
            conn, admin_conn, project_id=project_id, to_plan_code="late-tier"
        )
