"""Slot accounting, placement, and the invalidation pass.

Phase 06 slice 1. The node-level security properties -- whether `pg_hba.conf`
actually rejects a base backup, whether a role without `REPLICATION` is actually
refused -- are in `tests/test_realtime_node.py`, because they need a cluster with
`wal_level = logical` and cannot be asserted against a mock. What is here is the
half that lives in the control plane: whether the ceiling is *enforced* rather
than merely computed, which is the mistake Phase 05 spent four slices undoing,
and whether an invalidated slot produces a report rather than silence.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from services.control_plane import db, identity, maintenance, nodes, realtime
from tests.conftest import TEST_CREDENTIAL, requires_db

# --------------------------------------------------------------------------
# Node readiness. Pure: no database, no node.
# --------------------------------------------------------------------------


def _readiness(**overrides) -> realtime.NodeReadiness:
    base = {
        "wal_level": "logical",
        "max_replication_slots": 10,
        "max_wal_senders": 10,
        "max_slot_wal_keep_mb": 1024,
        "physical_replication_rejected": True,
        "probe_detail": "pg_hba.conf rejects replication connections",
    }
    return realtime.NodeReadiness(**{**base, **overrides})


def test_a_prepared_node_is_ready():
    assert _readiness().ready
    assert _readiness().failures == []


def test_wal_level_replica_is_refused():
    failures = _readiness(wal_level="replica").failures
    assert any("wal_level" in f and "restart" in f for f in failures)


def test_unbounded_wal_retention_is_refused():
    """ADR-032: the default is -1, and the default is the dangerous value."""
    failures = _readiness(max_slot_wal_keep_mb=-1).failures
    assert any("unbounded" in f and "every tenant" in f for f in failures)


def test_a_retention_bound_below_the_floor_is_refused():
    failures = _readiness(max_slot_wal_keep_mb=16).failures
    assert any("floor" in f for f in failures)


def test_a_node_that_accepts_physical_replication_is_refused():
    """ADR-031. This is the one that decides whether the tenancy model holds."""
    failures = _readiness(
        physical_replication_rejected=False,
        probe_detail="the node accepted a physical replication connection",
    ).failures
    assert any("readable copy of every tenant" in f for f in failures)


def test_a_probe_that_could_not_run_is_not_a_pass():
    """An unknown answer must not be recorded as a safe one.

    The failure mode of the opposite choice is a node marked ready because the
    check was unable to run, which is how a security control becomes decorative.
    """
    readiness = _readiness(physical_replication_rejected=None, probe_detail="no DSN supplied")
    assert not readiness.ready
    assert readiness.as_capacity()["realtime_ready"] is False


def test_a_rule_admitting_another_role_is_refused_even_though_the_probe_passed():
    """The probe runs as one role. pg_hba matches on the user as well.

        host replication postgres   127.0.0.1/32 reject
        host replication all        127.0.0.1/32 trust

    answers the probe correctly -- the platform's own role *is* rejected -- while
    every tenant replicator on the node is admitted by the line underneath and
    can take a base backup of every database on the cluster. A check that
    trusted the probe alone would mark this node prepared.
    """
    readiness = _readiness(
        physical_replication_rejected=True,
        permissive_hba_rules=["line 92: host replication all 127.0.0.1/32 trust"],
    )
    assert not readiness.ready
    assert any("admits physical replication for some roles" in f for f in readiness.failures)


def test_slots_that_no_sender_can_attach_to_are_refused():
    assert any(
        "max_wal_senders" in f
        for f in _readiness(max_wal_senders=2, max_replication_slots=10).failures
    )


def test_a_node_with_no_tenant_slots_is_refused():
    assert any(
        "leaving nothing for tenants" in f
        for f in _readiness(max_replication_slots=realtime.PLATFORM_SLOT_ALLOWANCE).failures
    )


def test_slot_names_come_from_the_validated_ref():
    assert realtime.slot_name_for("abcd1234") == "mldb_abcd1234_rt"
    with pytest.raises(ValueError):
        realtime.slot_name_for("../etc/passwd")


# --------------------------------------------------------------------------
# Capacity and placement.
# --------------------------------------------------------------------------


@pytest.fixture
def node_factory(db_pool):
    """Nodes with arbitrary capacity_json, healthy and placeable."""

    def make(name: str, *, capacity: dict | None = None, status: str = "active") -> int:
        with db.connection() as conn:
            node_id = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, "
                "capacity_json, last_health_at) "
                "VALUES (%s, %s, %s, 'shared', %s, %s::jsonb, now()) "
                "ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status, "
                "  capacity_json = EXCLUDED.capacity_json, last_health_at = now() "
                "RETURNING id",
                (name, f"{name}.example", f"{name}.internal", status,
                 psycopg.types.json.Jsonb(capacity or {})),
            )["id"]
            conn.commit()
        return node_id

    return make


@pytest.fixture
def rt_project(db_pool):
    def make(ref: str, *, node_id: int | None = None, realtime_enabled: bool = False,
             slot_state: str = "none") -> uuid.UUID:
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES ('rt-test','RT') "
                "ON CONFLICT (code) DO UPDATE SET name='RT' RETURNING id",
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                " node_id, realtime_enabled, realtime_slot_name, realtime_slot_state) "
                "VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s)",
                (project_id, org, ref, ref, plan, node_id, realtime_enabled,
                 realtime.slot_name_for(ref) if realtime_enabled else None, slot_state),
            )
            conn.commit()
        return project_id

    return make


PREPARED = {"realtime_ready": True, "max_replication_slots": 4}


@requires_db
def test_an_unchecked_node_refuses_realtime_but_still_takes_projects(node_factory):
    """Realtime readiness must not strand ordinary capacity.

    Every node in production today is unprepared, and folding the slot ceiling
    into general placement would stop all of them accepting anything.
    """
    node_id = node_factory("rt-unchecked")
    with db.connection() as conn:
        capacity = nodes.capacity_of(conn, node_id)
    assert capacity.can_accept
    assert not capacity.realtime_ready
    assert "realtime-check" in capacity.realtime_rejection_reason()
    assert not capacity.can_accept_realtime


@requires_db
def test_a_malformed_readiness_flag_reads_as_unprepared(node_factory):
    node_id = node_factory("rt-malformed", capacity={"realtime_ready": "yes"})
    with db.connection() as conn:
        assert not nodes.capacity_of(conn, node_id).realtime_ready


@requires_db
def test_committed_slots_are_counted_from_enablement(node_factory, rt_project):
    """Not from what the node reports.

    A slot that is missing or invalidated is still a slot the platform owes that
    project. Counting live slots instead would hand a stalled project's slot to
    somebody else and then fail to give it back.
    """
    node_id = node_factory("rt-counted", capacity=PREPARED)
    rt_project("rtc00001", node_id=node_id, realtime_enabled=True, slot_state="lost")
    rt_project("rtc00002", node_id=node_id, realtime_enabled=True)
    rt_project("rtc00003", node_id=node_id)  # no Realtime; must not be counted

    with db.connection() as conn:
        capacity = nodes.capacity_of(conn, node_id)
    assert capacity.committed_slots == 2
    assert capacity.usable_replication_slots == 4 - realtime.PLATFORM_SLOT_ALLOWANCE
    assert capacity.realtime_headroom == 0


@requires_db
def test_placement_refuses_a_realtime_project_past_the_slot_ceiling(node_factory, rt_project):
    """The Phase 05 lesson, applied to the tightest ceiling of the three.

    A ceiling that is computed and not consulted is not a ceiling. This is the
    acceptance criterion in tasks/PHASE-06-REALTIME.md that says *enforced in
    placement, not merely measured*.
    """
    node_id = node_factory("rt-full", capacity=PREPARED)
    for i in range(realtime.PLATFORM_SLOT_ALLOWANCE):
        rt_project(f"rtf0000{i}", node_id=node_id, realtime_enabled=True)

    unplaced = rt_project("rtf00009")
    with db.connection() as conn:
        assert nodes.capacity_of(conn, node_id).realtime_headroom == 0
        # A node out of slots is still a good node for a project that does not
        # want Realtime.
        assert nodes.reserve_placement(conn, project_id=unplaced) == node_id
        conn.commit()

    needs_slot = rt_project("rtf00010")
    with db.connection() as conn, pytest.raises(nodes.PlacementError, match="Realtime"):
        nodes.reserve_placement(conn, project_id=needs_slot, needs_realtime=True)


@requires_db
def test_placement_prefers_a_prepared_node_for_a_realtime_project(node_factory, rt_project):
    node_factory("rt-plain", capacity={"realtime_ready": False})
    prepared = node_factory("rt-prepared", capacity=PREPARED)
    project_id = rt_project("rtp00001")

    with db.connection() as conn:
        assert nodes.reserve_placement(conn, project_id=project_id, needs_realtime=True) == prepared
        conn.commit()


# --------------------------------------------------------------------------
# Invalidation detection. ADR-032's whole point is that this failure is silent
# unless something looks for it.
# --------------------------------------------------------------------------


class _FakeAdmin:
    """Stands in for a node connection. Closed by the pass, never queried."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _slot(name: str, *, wal_status: str = "reserved", slot_type: str = "logical") -> realtime.Slot:
    return realtime.Slot(
        slot_name=name, database="mldb_x", slot_type=slot_type,
        active=wal_status != "lost", wal_status=wal_status, safe_wal_size=None,
    )


def _audit_events(project_id: uuid.UUID) -> list[dict]:
    with db.connection() as conn:
        return db.query(
            conn,
            "SELECT event_type, detail_json FROM audit_events "
            " WHERE project_id = %s ORDER BY id",
            (project_id,),
        )


@requires_db
def test_an_invalidated_slot_becomes_a_project_visible_incident(
    node_factory, rt_project, monkeypatch
):
    node_id = node_factory("rt-lost", capacity=PREPARED)
    project_id = rt_project("rtl00001", node_id=node_id, realtime_enabled=True,
                            slot_state="active")

    monkeypatch.setattr(
        realtime, "slots_on_node", lambda _: [_slot("mldb_rtl00001_rt", wal_status="lost")]
    )
    with db.connection() as conn:
        report = realtime.reconcile_slots(conn, _FakeAdmin(), node_id=node_id)

    assert report.invalidated == ["rtl00001"]
    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT realtime_slot_state, realtime_slot_lost_at FROM projects WHERE id = %s",
            (project_id,),
        )
    assert row["realtime_slot_state"] == realtime.LOST
    assert row["realtime_slot_lost_at"] is not None

    events = _audit_events(project_id)
    assert [e["event_type"] for e in events] == ["realtime.slot_invalidated"]
    # ADR-032: recovery resumes from the present. Saying so is the difference
    # between a customer knowing they lost events and assuming a backfill.
    assert events[0]["detail_json"]["replayed_on_recovery"] is False


@requires_db
def test_a_slot_that_stays_lost_is_not_re_audited(node_factory, rt_project, monkeypatch):
    """The transition is the event, not the state.

    The pass runs every few minutes under a timer; an audit row per run would
    bury the one that mattered.
    """
    node_id = node_factory("rt-lost-twice", capacity=PREPARED)
    project_id = rt_project("rtl00002", node_id=node_id, realtime_enabled=True,
                            slot_state="active")
    monkeypatch.setattr(
        realtime, "slots_on_node", lambda _: [_slot("mldb_rtl00002_rt", wal_status="lost")]
    )
    with db.connection() as conn:
        realtime.reconcile_slots(conn, _FakeAdmin(), node_id=node_id)
        realtime.reconcile_slots(conn, _FakeAdmin(), node_id=node_id)

    assert len(_audit_events(project_id)) == 1


@requires_db
def test_a_recreated_slot_is_reported_as_restored(node_factory, rt_project, monkeypatch):
    node_id = node_factory("rt-restored", capacity=PREPARED)
    project_id = rt_project("rtl00003", node_id=node_id, realtime_enabled=True,
                            slot_state="lost")
    monkeypatch.setattr(
        realtime, "slots_on_node", lambda _: [_slot("mldb_rtl00003_rt")]
    )
    with db.connection() as conn:
        realtime.reconcile_slots(conn, _FakeAdmin(), node_id=node_id)
        row = db.one(
            conn,
            "SELECT realtime_slot_state, realtime_slot_lost_at FROM projects WHERE id = %s",
            (project_id,),
        )

    assert row["realtime_slot_state"] == realtime.ACTIVE
    assert row["realtime_slot_lost_at"] is None
    assert [e["event_type"] for e in _audit_events(project_id)] == ["realtime.slot_restored"]


@requires_db
def test_a_slot_the_node_never_had_is_distinguished_from_an_invalidated_one(
    node_factory, rt_project, monkeypatch
):
    """'lost' is PostgreSQL doing what ADR-032 asked. 'missing' is drift."""
    node_id = node_factory("rt-missing", capacity=PREPARED)
    project_id = rt_project("rtl00004", node_id=node_id, realtime_enabled=True,
                            slot_state="active")
    monkeypatch.setattr(realtime, "slots_on_node", lambda _: [])
    with db.connection() as conn:
        report = realtime.reconcile_slots(conn, _FakeAdmin(), node_id=node_id)

    assert report.missing == ["rtl00004"]
    assert [e["event_type"] for e in _audit_events(project_id)] == ["realtime.slot_missing"]


@requires_db
def test_a_physical_slot_nobody_asked_for_is_reported(node_factory, monkeypatch):
    """ADR-032: the pg_hba reject does not close the SQL path to a physical slot.

    A role holding REPLICATION for legitimate reasons can create one through an
    ordinary connection, and it pins WAL exactly as a logical one does. No
    project would ever point the pass at it, so the pass has to look.
    """
    node_id = node_factory("rt-strays", capacity=PREPARED)
    monkeypatch.setattr(
        realtime, "slots_on_node",
        lambda _: [_slot("dos_slot", slot_type="physical", wal_status="reserved")],
    )
    with db.connection() as conn:
        report = realtime.reconcile_slots(conn, _FakeAdmin(), node_id=node_id)

    assert report.unaccounted and "dos_slot" in report.unaccounted[0]


@requires_db
def test_the_maintenance_pass_checks_prepared_nodes_and_says_what_it_found(
    node_factory, rt_project, monkeypatch, key_ring
):
    """The failure this repository keeps having is a check nothing calls.

    `idle_workers`, `due_for_measurement` and `due_for_retry` were each written,
    tested and inert. So the assertion that matters is not that reconciliation
    works -- it is that a maintenance run reaches it.
    """
    node_id = node_factory("rt-pass", capacity=PREPARED)
    rt_project("rtm00001", node_id=node_id, realtime_enabled=True, slot_state="active")
    monkeypatch.setattr(
        realtime, "slots_on_node", lambda _: [_slot("mldb_rtm00001_rt", wal_status="lost")]
    )

    def connect(conn, node_id_arg, key_ring_arg):
        assert node_id_arg == node_id
        return _FakeAdmin(), None

    with db.connection() as conn:
        result = maintenance.check_replication_slots(
            conn, key_ring=key_ring, connect_to_node=connect
        )

    assert result.handled == 1
    assert any("not receiving changes" in line for line in result.detail)
    assert any("without replaying the gap" in line for line in result.detail)


@requires_db
def test_an_unreachable_node_does_not_stop_the_pass(node_factory, key_ring):
    node_factory("rt-unreachable", capacity=PREPARED)

    def refuse(conn, node_id_arg, key_ring_arg):
        raise psycopg.OperationalError("connection refused")

    with db.connection() as conn:
        result = maintenance.check_replication_slots(
            conn, key_ring=key_ring, connect_to_node=refuse
        )
    assert result.failed == 1
