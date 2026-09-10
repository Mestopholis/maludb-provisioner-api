"""Rebuilding a lost node, and what must not happen while doing it.

Phase 11 slice 8. The slice exists for a number -- how long a lost node takes to
come back -- but the number is worthless if the rebuild is unsafe, so these
assert the three orderings that make it safe:

- a target that still serves tenants is refused, because rebuilding onto one
  turns one outage into two;
- `projects.node_id` is repointed **after** ownership is verified and only for
  tenants that passed, because repointing is what sends customer traffic at the
  result (ADR-059);
- the lost node keeps its row, which carries the encrypted admin DSN and the
  backup stanza -- the only record of what was lost and where its backups are.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, identity, node_rebuild, restore
from tests.conftest import TEST_CREDENTIAL, requires_db

pytestmark = requires_db


def _node(name: str, *, status: str = "active") -> int:
    with db.connection() as conn:
        row = db.one(
            conn,
            """
            INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at)
            VALUES (%s,%s,%s,'shared',%s,now())
            ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status
            RETURNING id
            """,
            (name, f"{name}.example.com", f"10.0.0.{len(name)}", status),
        )
        conn.commit()
        return row["id"]


def _project(ref: str, node_id: int) -> uuid.UUID:
    plan_id = None
    with db.connection() as conn:
        plan = db.one(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('rb','rb',%s) "
            "ON CONFLICT (code) DO UPDATE SET name='rb' RETURNING id",
            (Jsonb({}),),
        )
        plan_id = plan["id"]
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
        )
        pid = uuid.uuid4()
        db.execute(
            conn,
            """
            INSERT INTO projects (id, org_id, project_ref, display_name, plan_id,
                                  status, node_id, database_name)
            VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s)
            """,
            (pid, org, ref, ref, plan_id, node_id, f"mldb_{ref}"),
        )
        conn.commit()
        return pid


class _Admin:
    """A stand-in for a superuser connection to the restored cluster."""

    def __init__(self, databases: list[str]):
        self._databases = databases

    def cursor(self):
        databases = self._databases

        @contextmanager
        def _cm():
            class _Cur:
                def execute(self, *_a, **_k):
                    return None

                def fetchall(self):
                    return [(d,) for d in databases]

            yield _Cur()

        return _cm()


def _node_of(ref: str) -> str:
    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT n.name FROM projects p JOIN nodes n ON n.id = p.node_id "
            "WHERE p.project_ref = %s",
            (ref,),
        )
        return row["name"]


def _verifying(*, verified: bool):
    """A `connect` that reports every tenant's ownership as `verified`."""

    @contextmanager
    def _connect(_admin, database):
        yield database

    def _verify(_tenant_conn, _admin, names, *, database):
        return restore.OwnershipReport(
            database=database,
            expected={"auth": names.auth},
            observed={"auth": names.auth if verified else "postgres"},
        )

    return _connect, _verify


# -- preflight -------------------------------------------------------------


def test_a_target_that_still_serves_tenants_is_refused(db_pool):  # noqa: ARG001
    """One outage becoming two is the failure this prevents."""
    _node("lost-01")
    target = _node("new-01")
    _project("rbd00001", target)

    problems = node_rebuild.preflight(
        _conn(), source_node="lost-01", target_node="new-01", stanza="s"
    )
    assert any("already carries" in p for p in problems), problems
    assert any("turns one outage into two" in p for p in problems)


def test_rebuilding_onto_the_source_is_refused(db_pool):  # noqa: ARG001
    """Restoring over the machine you are recovering from loses both copies."""
    _node("lost-01")
    problems = node_rebuild.preflight(
        _conn(), source_node="lost-01", target_node="lost-01", stanza="s"
    )
    assert any("same node" in p for p in problems), problems


def test_an_unregistered_target_is_refused(db_pool):  # noqa: ARG001
    _node("lost-01")
    problems = node_rebuild.preflight(
        _conn(), source_node="lost-01", target_node="nowhere", stanza="s"
    )
    assert any("Register it first" in p for p in problems), problems


def test_a_clean_target_passes_preflight(db_pool):  # noqa: ARG001
    _node("lost-01")
    _node("new-01")
    assert node_rebuild.preflight(
        _conn(), source_node="lost-01", target_node="new-01", stanza="s"
    ) == []


def _conn():
    """A connection the preflight helpers can use in a test body."""
    ctx = db.connection()
    conn = ctx.__enter__()
    _conn._open.append(ctx)  # noqa: SLF001
    return conn


_conn._open = []  # noqa: SLF001


@pytest.fixture(autouse=True)
def _close_borrowed_connections():
    yield
    while _conn._open:  # noqa: SLF001
        _conn._open.pop().__exit__(None, None, None)  # noqa: SLF001


# -- the rebuild itself ----------------------------------------------------


def test_a_verified_tenant_is_repointed_and_the_lost_node_is_retired(monkeypatch, db_pool):  # noqa: ARG001
    lost = _node("lost-01")
    _node("new-01")
    _project("rbd00002", lost)

    connect, verify = _verifying(verified=True)
    monkeypatch.setattr(restore, "verify_ownership", verify)

    with db.connection() as conn:
        outcome = node_rebuild.rebuild(
            conn,
            _Admin(["mldb_rbd00002"]),
            source_node="lost-01",
            target_node="new-01",
            stanza="maludb-lost-01",
            connect=connect,
        )

    assert outcome.ok, outcome.error
    assert outcome.repointed == 1
    assert _node_of("rbd00002") == "new-01"

    with db.connection() as conn:
        status = db.one(conn, "SELECT status FROM nodes WHERE name = 'lost-01'")["status"]
    # Retired, not deleted: the row carries the stanza and the admin DSN.
    assert status == node_rebuild.LOST_STATUS
    assert db.one(_conn(), "SELECT 1 AS x FROM nodes WHERE name = 'lost-01'") is not None


def test_a_database_the_platform_left_behind_does_not_stop_the_rebuild():
    """A retained database must be skipped, not raise.

    `restore.activate` keeps `<db>_pre_restore_<stamp>` and a move keeps
    `<db>_pre_move_<stamp>` (ADR-071), both deliberately, and both start with
    `mldb_` -- so a rebuild sees them. Their refs do not round-trip through
    `models.database_name_for`, which *raises* rather than returning a
    non-match, and that exception used to escape `verify_tenants` and fail the
    entire rebuild.

    Found by measuring a whole-node restore on a cluster that had been used for
    restore tests, which is to say: found by doing the thing the runbook
    describes, on a node in a state the platform itself produces.
    """
    checks = node_rebuild.verify_tenants(
        None,
        [
            "mldb_rst00009_restore_20260909212454",
            "mldb_abc00001_pre_move_20260909212454",
        ],
        connect=None,
    )
    assert [c.verified for c in checks] == [False, False]
    assert all("not a name this platform generates" in c.detail for c in checks)


def test_the_lost_node_gives_up_its_gateway_role(monkeypatch, db_pool):  # noqa: ARG001
    """A lost node keeps its row and loses its identity (ADR-072).

    `nodes.gateway_role` is UNIQUE because it is one node's, and the row
    policies resolve `current_user` through it. Left on the retired node, the
    replacement cannot be granted the same role -- `gateway grant` refuses and
    advises giving the new node its own, which is the wrong advice at the one
    moment somebody is following a disaster runbook. The machine is gone; the
    identity moves with the tenants.
    """
    lost = _node("lost-01")
    _node("new-01")
    _project("rbd00007", lost)
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE name = 'lost-01'", ("gw-probe",))
        conn.commit()

    connect, verify = _verifying(verified=True)
    monkeypatch.setattr(restore, "verify_ownership", verify)

    with db.connection() as conn:
        outcome = node_rebuild.rebuild(
            conn,
            _Admin(["mldb_rbd00007"]),
            source_node="lost-01",
            target_node="new-01",
            stanza="maludb-lost-01",
            connect=connect,
        )

    assert outcome.ok, outcome.error
    with db.connection() as conn:
        held = db.one(conn, "SELECT gateway_role FROM nodes WHERE name = 'lost-01'")
    assert held["gateway_role"] is None, (
        "the retired node still claims its gateway role, so the rebuilt node cannot be granted it"
    )
    # And the operator is told, because nothing else will: a rebuilt node with
    # no gateway role serves 404 for every tenant that was just recovered.
    assert any("gw-probe" in n for n in outcome.notes), outcome.notes


def test_a_tenant_that_does_not_verify_is_left_on_the_lost_node(monkeypatch, db_pool):  # noqa: ARG001
    """ADR-059, and the reason repointing comes last.

    A project left pointing at a node that is gone is visibly broken, and an
    operator fixes it. A project pointing at a database whose `auth` schema
    silently belongs to the superuser is not visibly anything.
    """
    lost = _node("lost-01")
    _node("new-01")
    _project("rbd00003", lost)

    connect, verify = _verifying(verified=False)
    monkeypatch.setattr(restore, "verify_ownership", verify)

    with db.connection() as conn:
        outcome = node_rebuild.rebuild(
            conn,
            _Admin(["mldb_rbd00003"]),
            source_node="lost-01",
            target_node="new-01",
            stanza="maludb-lost-01",
            connect=connect,
        )

    assert outcome.ok, outcome.error
    assert outcome.repointed == 0
    assert outcome.unverified
    assert _node_of("rbd00003") == "lost-01", "an unverified tenant was repointed"
    assert any("did not verify" in n for n in outcome.notes), outcome.notes


def test_a_restore_with_no_tenant_databases_fails(monkeypatch, db_pool):  # noqa: ARG001
    """Either the stanza is not this node's, or the restore did not complete."""
    _node("lost-01")
    _node("new-01")

    with db.connection() as conn:
        outcome = node_rebuild.rebuild(
            conn,
            _Admin([]),
            source_node="lost-01",
            target_node="new-01",
            stanza="maludb-lost-01",
        )
    assert not outcome.ok
    assert "no tenant databases" in outcome.error


def test_a_failed_rebuild_repoints_nothing(monkeypatch, db_pool):  # noqa: ARG001
    """Half a rebuild is worse than none: it splits a node's tenants in two."""
    lost = _node("lost-01")
    target = _node("new-01")
    _project("rbd00004", lost)
    _project("rbd00005", target)  # makes the target unclean, so preflight refuses

    with db.connection() as conn:
        outcome = node_rebuild.rebuild(
            conn,
            _Admin(["mldb_rbd00004"]),
            source_node="lost-01",
            target_node="new-01",
            stanza="maludb-lost-01",
        )

    assert not outcome.ok
    assert outcome.repointed == 0
    assert _node_of("rbd00004") == "lost-01"
    with db.connection() as conn:
        assert db.one(conn, "SELECT status FROM nodes WHERE name='lost-01'")["status"] == "active"


def test_the_outcome_reports_how_long_it_took(monkeypatch, db_pool):  # noqa: ARG001
    """The number the slice exists for."""
    lost = _node("lost-01")
    _node("new-01")
    _project("rbd00006", lost)
    connect, verify = _verifying(verified=True)
    monkeypatch.setattr(restore, "verify_ownership", verify)

    with db.connection() as conn:
        outcome = node_rebuild.rebuild(
            conn,
            _Admin(["mldb_rbd00006"]),
            source_node="lost-01",
            target_node="new-01",
            stanza="s",
            connect=connect,
        )
    assert outcome.total_seconds > 0
    assert outcome.databases_found == 1


# -- the capacity pass -----------------------------------------------------


def test_capacity_reports_a_node_near_a_ceiling_and_never_moves_anything(db_pool):  # noqa: ARG001
    """`docs/CAPACITY.md`'s ceilings, asked a fraction before placement refuses.

    Reaching a ceiling is too late to be told: at that point a customer's
    project creation has already been refused. And the pass reports rather than
    repairs -- ADR-066 makes movement operator-initiated, so an alert that
    relieved itself by moving projects would be the data-moving control plane
    that ADR forbids.
    """
    from services.control_plane import maintenance

    node_id = _node("cap-01")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = %s WHERE id = %s",
            (Jsonb({"max_projects": 10, "max_warm_projects": 10}), node_id),
        )
        conn.commit()
    for i in range(9):
        _project(f"cap0000{i}", node_id)

    with db.connection() as conn:
        before = [
            r["project_ref"] for r in db.query(
                conn, "SELECT project_ref, node_id FROM projects ORDER BY project_ref"
            )
        ]
        result = maintenance.check_capacity(conn)
        after = [
            r["project_ref"] for r in db.query(
                conn, "SELECT project_ref, node_id FROM projects ORDER BY project_ref"
            )
        ]

    assert result.handled >= 1
    assert any("cap-01" in d and "projects at 9/10" in d for d in result.detail), result.detail
    assert before == after, "the capacity pass moved something; it must only report"


def test_capacity_says_at_capacity_rather_than_a_percentage_once_full(db_pool):  # noqa: ARG001
    """Past the ceiling is not an early warning any more, and should not read as one."""
    from services.control_plane import maintenance

    node_id = _node("cap-02")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = %s WHERE id = %s",
            (Jsonb({"max_projects": 2, "max_warm_projects": 2}), node_id),
        )
        conn.commit()
    for i in range(2):
        _project(f"cpf0000{i}", node_id)

    with db.connection() as conn:
        result = maintenance.check_capacity(conn)
    assert any("AT CAPACITY" in d for d in result.detail), result.detail
