"""Node registry, capacity scoring, and placement.

The property that matters most here is that two concurrent provisioning runs
cannot oversubscribe a node. That is tested with real concurrent connections
rather than asserted, because a capacity check that is merely *usually* atomic
is the kind of defect that only appears under load.
"""

from __future__ import annotations

import threading
import uuid

import psycopg
import pytest

from services.control_plane import db, nodes
from tests.conftest import requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple"  # noqa: S105 - test fixture, not a real secret

pytestmark = requires_db


@pytest.fixture
def org_id(db_pool) -> uuid.UUID:
    from services.control_plane import identity

    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email="placement@example.com", password=TEST_CREDENTIAL
        )
        conn.commit()
    return org


@pytest.fixture
def plan_id(db_pool) -> int:
    with db.connection() as conn:
        row = db.one(
            conn,
            "INSERT INTO plans (code, name) VALUES ('free','Free') "
            "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        )
        conn.commit()
    return int(row["id"])


def make_node(
    name: str, *, status: str = "active", healthy: bool = True, node_pool: str = "shared", **capacity
) -> int:
    with db.connection() as conn:
        node_id = nodes.register_node(
            conn, name=name, hostname=f"{name}.test", internal_host=f"10.0.0.{len(name)}",
            node_pool=node_pool, capacity=capacity,
        )
        nodes.set_status(conn, name=name, status=status)
        if healthy:
            nodes.record_health(conn, name=name, metrics={"free_disk_bytes": 10**12})
        conn.commit()
    return node_id


def make_project(org_id: uuid.UUID, plan_id: int, ref: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    with db.connection() as conn:
        db.execute(
            conn,
            """
            INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status)
            VALUES (%s, %s, %s, %s, %s, 'REQUESTED')
            """,
            (project_id, org_id, ref, ref, plan_id),
        )
        conn.commit()
    return project_id


# -- registry --------------------------------------------------------------


def test_a_new_node_starts_in_maintenance(db_pool):
    """An operator confirms a node is ready before projects land on it."""
    make_node("n-maint", status="maintenance")
    with db.connection() as conn:
        row = db.one(conn, "SELECT status FROM nodes WHERE name = 'n-maint'")
    assert row["status"] == "maintenance"


def test_registering_twice_updates_rather_than_duplicates(db_pool):
    first = make_node("n-dup")
    second = make_node("n-dup")
    assert first == second


def test_unknown_status_is_refused(db_pool):
    make_node("n-status")
    with db.connection() as conn, pytest.raises(ValueError, match="unknown node status"):
        nodes.set_status(conn, name="n-status", status="deleted")


# -- capacity --------------------------------------------------------------


def test_capacity_counts_projects_on_the_node(db_pool, org_id, plan_id):
    node_id = make_node("n-count", max_projects=5)
    project = make_project(org_id, plan_id, "cnt00001")
    with db.connection() as conn:
        assert nodes.capacity_of(conn, node_id).current_projects == 0
        nodes.reserve_placement(conn, project_id=project)
        conn.commit()
        assert nodes.capacity_of(conn, node_id).current_projects == 1


def test_a_full_node_is_not_eligible(db_pool, org_id, plan_id):
    make_node("n-full", max_projects=1)
    make_project(org_id, plan_id, "full0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=make_project(org_id, plan_id, "full0002"))
        conn.commit()
        assert nodes.eligible_nodes(conn) == []


def test_insufficient_disk_blocks_placement(db_pool):
    with db.connection() as conn:
        nodes.register_node(
            conn, name="n-disk", hostname="d.test", internal_host="10.0.0.9",
            capacity={"min_free_disk_bytes": 10**12},
        )
        nodes.set_status(conn, name="n-disk", status="active")
        nodes.record_health(conn, name="n-disk", metrics={"free_disk_bytes": 1000})
        conn.commit()
        assert nodes.eligible_nodes(conn) == []


def test_malformed_capacity_falls_back_to_conservative_defaults(db_pool):
    """Operator-supplied JSON must never read as unlimited capacity."""
    node_id = make_node("n-bad", max_projects="lots", max_warm_projects=-5)
    with db.connection() as conn:
        capacity = nodes.capacity_of(conn, node_id)
    assert capacity.max_projects == nodes.DEFAULT_MAX_PROJECTS
    assert capacity.max_warm_projects == nodes.DEFAULT_MAX_WARM_PROJECTS


# -- eligibility gates -----------------------------------------------------


@pytest.mark.parametrize("status", ["maintenance", "draining", "unhealthy"])
def test_only_active_nodes_receive_projects(db_pool, status):
    make_node(f"n-{status}", status=status)
    with db.connection() as conn:
        assert nodes.eligible_nodes(conn) == []


def test_a_node_that_never_reported_health_is_not_eligible(db_pool):
    make_node("n-silent", healthy=False)
    with db.connection() as conn:
        assert nodes.eligible_nodes(conn) == []


def test_stale_health_makes_a_node_ineligible(db_pool):
    """Stale metrics are indistinguishable from a dead node."""
    make_node("n-stale")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET last_health_at = now() - interval '1 hour' WHERE name = 'n-stale'",
        )
        conn.commit()
        assert nodes.eligible_nodes(conn) == []


def test_placement_respects_the_node_pool(db_pool, org_id, plan_id):
    with db.connection() as conn:
        nodes.register_node(conn, name="n-prod", hostname="p.test", internal_host="10.0.1.1", node_pool="production")
        nodes.set_status(conn, name="n-prod", status="active")
        nodes.record_health(conn, name="n-prod", metrics={"free_disk_bytes": 10**12})
        conn.commit()
        assert nodes.eligible_nodes(conn, node_pool="shared") == []
        assert len(nodes.eligible_nodes(conn, node_pool="production")) == 1


def test_placement_prefers_the_least_utilised_node(db_pool, org_id, plan_id):
    busy = make_node("n-busy", max_projects=10)
    make_node("n-idle", max_projects=10)
    with db.connection() as conn:
        for i in range(4):
            nodes.reserve_placement(conn, project_id=make_project(org_id, plan_id, f"busy000{i}"))
            db.execute(conn, "UPDATE projects SET node_id = %s WHERE project_ref = %s", (busy, f"busy000{i}"))
        conn.commit()
        assert nodes.eligible_nodes(conn)[0].name == "n-idle"


# -- reservation -----------------------------------------------------------


def test_reservation_sets_node_and_state(db_pool, org_id, plan_id):
    node_id = make_node("n-reserve")
    project = make_project(org_id, plan_id, "resv0001")
    with db.connection() as conn:
        assert nodes.reserve_placement(conn, project_id=project) == node_id
        conn.commit()
        row = db.one(conn, "SELECT node_id, status FROM projects WHERE id = %s", (project,))
    assert row["node_id"] == node_id
    assert row["status"] == "PLACEMENT_RESERVED"


def test_reserving_twice_is_refused(db_pool, org_id, plan_id):
    make_node("n-twice")
    project = make_project(org_id, plan_id, "twic0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project)
        conn.commit()
        with pytest.raises(nodes.PlacementError, match="already placed"):
            nodes.reserve_placement(conn, project_id=project)


def test_placement_fails_cleanly_when_no_node_can_accept(db_pool, org_id, plan_id):
    project = make_project(org_id, plan_id, "none0001")
    with db.connection() as conn, pytest.raises(nodes.PlacementError, match="no healthy node"):
        nodes.reserve_placement(conn, project_id=project)


def test_release_returns_a_project_to_unplaced(db_pool, org_id, plan_id):
    make_node("n-release")
    project = make_project(org_id, plan_id, "rels0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project)
        conn.commit()
        nodes.release_placement(conn, project_id=project)
        conn.commit()
        row = db.one(conn, "SELECT node_id, status FROM projects WHERE id = %s", (project,))
    assert row["node_id"] is None
    assert row["status"] == "REQUESTED"


def test_release_refuses_once_objects_may_exist_on_the_node(db_pool, org_id, plan_id):
    """Forgetting the node would strand a real database on a real cluster."""
    make_node("n-strand")
    project = make_project(org_id, plan_id, "strd0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project)
        db.execute(conn, "UPDATE projects SET status = 'DATABASE_CREATING' WHERE id = %s", (project,))
        conn.commit()
        with pytest.raises(nodes.PlacementError, match="objects may already exist"):
            nodes.release_placement(conn, project_id=project)


# -- concurrency -----------------------------------------------------------


def test_concurrent_placement_cannot_oversubscribe_a_node(db_pool, org_id, plan_id, migrated_database):
    """The one test that justifies row-level locking.

    Eight threads race to place onto a node with room for three. Exactly three
    must succeed; a non-atomic check-then-assign lets more through.
    """
    make_node("n-race", max_projects=3)
    projects = [make_project(org_id, plan_id, f"race000{i}") for i in range(8)]

    successes: list[int] = []
    failures: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(projects))

    def attempt(project_id: uuid.UUID) -> None:
        import psycopg

        conn = psycopg.connect(migrated_database, row_factory=psycopg.rows.dict_row)
        try:
            barrier.wait(timeout=10)
            node_id = nodes.reserve_placement(conn, project_id=project_id)
            conn.commit()
            with lock:
                successes.append(node_id)
        except nodes.PlacementError as exc:
            conn.rollback()
            with lock:
                failures.append(str(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(p,)) for p in projects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(successes) == 3, f"expected exactly 3 placements, got {len(successes)}: {failures}"
    with db.connection() as conn:
        assert nodes.capacity_of(conn, successes[0]).current_projects == 3


def test_release_refuses_a_failed_project_that_has_a_database(db_pool, org_id, plan_id):
    """Security review finding: FAILED is reachable from any operational state.

    A project that failed during BOOTSTRAPPING already has a database on the
    node. Releasing it cleared node_id and orphaned that database -- still
    holding customer data, no longer reachable by deletion, suspension, or
    accounting. Confirmed exploitable before the fix.
    """
    make_node("n-orphan")
    project = make_project(org_id, plan_id, "orph0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project)
        # got as far as bootstrap -- the database exists -- then failed
        db.execute(
            conn,
            "UPDATE projects SET status='FAILED', database_name='mldb_orph0001' WHERE id=%s",
            (project,),
        )
        conn.commit()

        with pytest.raises(nodes.PlacementError, match="database mldb_orph0001 exists"):
            nodes.release_placement(conn, project_id=project)

        row = db.one(conn, "SELECT node_id FROM projects WHERE id = %s", (project,))
    assert row["node_id"] is not None, "the node must still be recorded"


def test_release_refuses_any_project_that_has_a_database(db_pool, org_id, plan_id):
    """The gate is the recorded fact, not the status label."""
    make_node("n-dbset")
    project = make_project(org_id, plan_id, "dbst0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project)
        db.execute(conn, "UPDATE projects SET database_name='mldb_dbst0001' WHERE id=%s", (project,))
        conn.commit()
        # status is still PLACEMENT_RESERVED, which the status check allows
        with pytest.raises(nodes.PlacementError, match="exists on the node"):
            nodes.release_placement(conn, project_id=project)


def test_release_still_works_for_a_project_with_no_database(db_pool, org_id, plan_id):
    """The fix must not break the legitimate case."""
    make_node("n-clean")
    project = make_project(org_id, plan_id, "clen0001")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project)
        conn.commit()
        nodes.release_placement(conn, project_id=project)
        conn.commit()
        row = db.one(conn, "SELECT node_id, status FROM projects WHERE id = %s", (project,))
    assert row["node_id"] is None
    assert row["status"] == "REQUESTED"


# -- pools (Phase 11 slice 6, ADR-065) -------------------------------------


def test_every_tier_ships_entitled_to_the_shared_pool():
    """The mechanism arrives switched off, deliberately.

    Defaulting a paid tier to a pool no deployment has built would answer 503 to
    every paid signup the moment this merged. Separation is an explicit act
    instead: set `node_pool` on the plan and register nodes in that pool.
    """
    from services.control_plane import entitlements

    for code in ("free", "starter", "production"):
        assert entitlements.resolve(code, None).node_pool == nodes.DEFAULT_POOL


def test_a_plan_can_be_moved_to_another_pool_by_configuration():
    from services.control_plane import entitlements

    assert entitlements.resolve("production", {"node_pool": "prod-a"}).node_pool == "prod-a"


def test_an_unusable_pool_name_falls_back_rather_than_matching_nothing():
    """`plans.config_json` is operator-supplied and the value reaches a WHERE clause."""
    from services.control_plane import entitlements

    for bad in ("drop table nodes;", "", 5, None, "UPPER CASE WITH SPACES"):
        assert entitlements.resolve("free", {"node_pool": bad}).node_pool == nodes.DEFAULT_POOL


def test_placement_uses_the_requested_pool(db_pool, org_id, plan_id):
    make_node("n-shared-a", node_pool="shared")
    prod = make_node("n-prod-a", node_pool="production")
    project = make_project(org_id, plan_id, "pool0001")
    with db.connection() as conn:
        placed = nodes.reserve_placement(conn, project_id=project, node_pool="production")
    assert placed == prod, "placement ignored the pool it was given"


def test_a_project_entitled_to_an_empty_pool_is_refused_not_placed_elsewhere(
    db_pool, org_id, plan_id
):
    """The isolation property, and the one worth being strict about.

    A pool exists so a paying tenant is not co-resident with the free tier. If
    the entitled pool has no node, falling back to `shared` would place that
    customer exactly where the pool was meant to keep them out of -- a control
    that reports itself as applied and is not. ADR-065 refuses instead, and the
    project stays unplaced so a retry can succeed once the pool is built.
    """
    make_node("n-only-shared", node_pool="shared")
    project = make_project(org_id, plan_id, "pool0002")
    with db.connection() as conn:
        with pytest.raises(nodes.PlacementError, match="production"):
            nodes.reserve_placement(conn, project_id=project, node_pool="production")

    with db.connection() as conn:
        row = db.one(conn, "SELECT node_id, status FROM projects WHERE id = %s", (project,))
    assert row["node_id"] is None, "a refused placement left the project on a node anyway"
    assert row["status"] == "REQUESTED", "a refused placement advanced the project's state"


def test_a_node_in_another_pool_is_not_eligible(db_pool, org_id, plan_id):
    make_node("n-elsewhere", node_pool="production")
    with db.connection() as conn:
        assert nodes.eligible_nodes(conn, node_pool="shared") == []
        assert len(nodes.eligible_nodes(conn, node_pool="production")) == 1


def test_the_pool_report_says_plainly_when_no_separation_is_in_effect(db_pool, plan_id):
    """The risk the safe default creates is a false belief, not an outage.

    A deployment can have the mechanism and no separation, and nothing about
    that state announces itself. So the report announces it.
    """
    make_node("n-report", node_pool="shared")
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code,name) VALUES ('pool-b','B') ON CONFLICT (code) DO NOTHING",
        )
        conn.commit()
        report = nodes.pool_report(conn)

    assert report.pools == {"shared": 1}
    assert set(report.plan_pools.values()) == {"shared"}
    assert any("no separation is in effect" in note for note in report.notes())
    assert report.problems() == [], "sharing a pool by default is not a fault"


def test_the_pool_report_flags_a_pool_with_no_node(db_pool):
    """Entitled to a pool that does not exist: every project on that plan is unplaceable."""
    make_node("n-shared-only", node_pool="shared")
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code,name,config_json) VALUES ('pool-p','P',%s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json",
            (psycopg.types.json.Jsonb({"node_pool": "production"}),),
        )
        conn.commit()
        report = nodes.pool_report(conn)

    problems = report.problems()
    assert any("'production' has no node" in p for p in problems), problems
    assert any("503" in p for p in problems), "the report should say what a customer sees"


def test_the_pool_report_finds_a_project_left_behind_by_a_plan_change(
    db_pool, org_id, plan_id
):
    """A plan change does not move a tenant. Reported, never moved.

    Moving one is slice 7, and ADR-066 makes movement operator-initiated -- so
    the honest thing for this report to do is name the project and stop.
    """
    make_node("n-stay", node_pool="shared")
    project = make_project(org_id, plan_id, "pool0003")
    with db.connection() as conn:
        nodes.reserve_placement(conn, project_id=project, node_pool="shared")
        conn.commit()
        # The plan is now entitled to somewhere else; the project has not moved.
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = %s",
            (psycopg.types.json.Jsonb({"node_pool": "production"}), plan_id),
        )
        conn.commit()
        report = nodes.pool_report(conn)

    assert [row["project_ref"] for row in report.misplaced] == ["pool0003"]
    assert any("does not move a tenant" in p for p in report.problems())


def test_a_pool_name_is_normalised_on_both_sides_of_the_comparison():
    """The silent, total mismatch this normalisation exists to prevent.

    An operator who registers `Production` and entitles a plan to `Production`
    would otherwise get an entitlement lowercased to `production`, a node that
    stayed `Production`, and every project on that plan refused with "no healthy
    node in pool" -- correct-looking, fails closed, and giving no hint that two
    spellings are the cause.
    """
    from services.control_plane import entitlements

    assert nodes.checked_pool("  Production  ") == "production"
    assert entitlements.resolve("free", {"node_pool": "  Production  "}).node_pool == "production"


def test_registering_a_node_with_a_mixed_case_pool_still_matches(db_pool, org_id, plan_id):
    """End to end: the two halves agree after normalisation."""
    node_id = make_node("n-cased", node_pool="Production")
    project = make_project(org_id, plan_id, "pool0004")
    with db.connection() as conn:
        assert nodes.reserve_placement(conn, project_id=project, node_pool="PRODUCTION") == node_id


def test_an_unusable_pool_name_is_refused_at_registration(db_pool):
    """Refused here, unlike the entitlement side which falls back.

    An operator is present at registration and a clear error is the most useful
    thing to hand them; a request path refusing a customer for an operator's
    typo would not be.
    """
    with pytest.raises(ValueError, match="unusable node pool"):
        make_node("n-bad-pool", node_pool="Prod Cluster #1")
