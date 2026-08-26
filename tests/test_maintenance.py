"""The periodic passes, and the capacity ceilings they exist alongside.

Three functions were written, tested and inert before this: `idle_workers`,
`due_for_measurement` and `due_for_retry`. So these tests are mostly about
whether the passes actually call them and act on what comes back -- the failure
that had already happened three times is code that computes the right answer and
nothing consumes it.

The other half is capacity. `rejection_reason()` computed warm counts from the
start and never consulted them, so ADR-022's ceiling was measured and
unenforced: a node could be filled well past the point where tenants begin
failing to connect.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from services.control_plane import (
    db,
    entitlements,
    identity,
    maintenance,
    nodes,
    storage,
)
from tests.conftest import TEST_CREDENTIAL, requires_db

pytestmark = [requires_db]


class _RecordingSupervisor:
    """Records what would have been stopped."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def start(self, project_ref: str) -> None:
        return None

    def stop(self, project_ref: str) -> None:
        self.stopped.append(project_ref)

    def is_active(self, project_ref: str) -> bool:
        return False


@pytest.fixture
def node(db_pool) -> int:
    with db.connection() as conn:
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, capacity_json) "
            "VALUES ('mt-node','mt.example','mt.internal','shared','active','{}'::jsonb) "
            "ON CONFLICT (name) DO UPDATE SET status='active' RETURNING id",
        )["id"]
        conn.commit()
    return node_id


@pytest.fixture
def project(db_pool, node):
    def make(ref: str, *, plan_code="free", worker_state="STOPPED",
             auth_worker_state="STOPPED", idle_minutes=0, plan_limits=None):
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name,config_json) VALUES (%s,'T',%s) "
                "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
                (plan_code, psycopg.types.json.Jsonb({"limits": plan_limits or {}})),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                "node_id, database_name, worker_state, auth_worker_state, "
                "worker_last_active_at, auth_worker_last_active_at) "
                "VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s, "
                "now() - make_interval(mins => %s), now() - make_interval(mins => %s))",
                (project_id, org, ref, ref, plan, node, f"mldb_{ref}",
                 worker_state, auth_worker_state, idle_minutes, idle_minutes),
            )
            conn.commit()
        return project_id

    return make


# -- the pass that ADR-022's economics depend on ---------------------------


def test_an_idle_worker_is_slept(project):
    """ADR-022 says free-tier economics rest entirely on sleep, and nothing was
    putting anything to sleep."""
    project("mt000001", worker_state="RUNNING", idle_minutes=60)
    supervisor, auth_supervisor = _RecordingSupervisor(), _RecordingSupervisor()

    with db.connection() as conn:
        result = maintenance.sleep_idle_workers(
            conn, supervisor=supervisor, auth_supervisor=auth_supervisor, idle_minutes=15
        )
        state = db.one(
            conn, "SELECT worker_state FROM projects WHERE project_ref = 'mt000001'"
        )["worker_state"]

    assert supervisor.stopped == ["mt000001"]
    assert state == "STOPPED"
    assert result.handled == 1


def test_a_busy_worker_is_left_alone(project):
    project("mt000002", worker_state="RUNNING", idle_minutes=1)
    supervisor = _RecordingSupervisor()
    with db.connection() as conn:
        maintenance.sleep_idle_workers(
            conn, supervisor=supervisor, auth_supervisor=_RecordingSupervisor(), idle_minutes=15
        )
    assert supervisor.stopped == []


def test_auth_and_api_workers_sleep_independently(project):
    """A project whose Data API is busy while nothing touches Auth should give
    the Auth worker back -- it is 17.6 MB of the 31.8 MB a warm project costs."""
    project_id = project("mt000003", worker_state="RUNNING", auth_worker_state="RUNNING",
                         idle_minutes=60)
    with db.connection() as conn:
        db.execute(
            conn, "UPDATE projects SET worker_last_active_at = now() WHERE id = %s", (project_id,)
        )
        conn.commit()

    supervisor, auth_supervisor = _RecordingSupervisor(), _RecordingSupervisor()
    with db.connection() as conn:
        maintenance.sleep_idle_workers(
            conn, supervisor=supervisor, auth_supervisor=auth_supervisor, idle_minutes=15
        )
    assert supervisor.stopped == [], "a busy API worker was slept"
    assert auth_supervisor.stopped == ["mt000003"], "an idle Auth worker was kept"


def test_a_worker_that_refuses_to_stop_does_not_halt_the_pass(project):
    """One bad project must not leave every later one running. A pass that
    raises on the first failure sleeps nothing on a node with one sick tenant."""

    class _Failing(_RecordingSupervisor):
        def stop(self, project_ref: str) -> None:
            from services.control_plane import workers

            raise workers.WorkerError("systemctl said no")

    project("mt000004", worker_state="RUNNING", idle_minutes=60)
    project("mt000005", worker_state="RUNNING", idle_minutes=60)
    with db.connection() as conn:
        result = maintenance.sleep_idle_workers(
            conn, supervisor=_Failing(), auth_supervisor=_RecordingSupervisor(), idle_minutes=15
        )
    assert result.failed == 2
    assert result.handled == 0


def test_a_dry_run_counts_without_stopping(project):
    project("mt000006", worker_state="RUNNING", idle_minutes=60)
    with db.connection() as conn:
        assert maintenance.sleepable_now(conn, idle_minutes=15) == 1
        state = db.one(
            conn, "SELECT worker_state FROM projects WHERE project_ref = 'mt000006'"
        )["worker_state"]
    assert state == "RUNNING", "a dry run stopped something"


# -- capacity, measured since Phase 02 and enforced only now ---------------


def test_warm_capacity_is_enforced_not_merely_counted(project, node):
    """`max_warm_projects` was computed from the start and never consulted."""
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = '{\"max_warm_projects\": 1}'::jsonb WHERE id = %s",
            (node,),
        )
        conn.commit()
    project("mt000007", worker_state="RUNNING")
    project("mt000008", worker_state="RUNNING")

    with db.connection() as conn:
        capacity = nodes.capacity_of(conn, node)
    assert capacity.current_warm_projects == 2
    assert capacity.warm_headroom <= 0
    reason = capacity.rejection_reason()
    assert reason and "warm capacity" in reason
    assert capacity.can_accept is False


def test_a_sleeping_project_does_not_count_against_warm_capacity(project, node):
    """The whole basis of free-tier density: a slept project costs nothing."""
    project("mt000009", worker_state="STOPPED")
    project("mt00000a", worker_state="STOPPED")
    with db.connection() as conn:
        capacity = nodes.capacity_of(conn, node)
    assert capacity.current_warm_projects == 0
    assert capacity.can_accept is True


def test_connection_headroom_is_projected_from_real_plans(project, node):
    """Pool size became a plan entitlement in slice 1, so a node full of
    production projects is a very different shape from one full of free ones.
    An average would describe neither."""
    project("mt00000b", plan_code="free", worker_state="RUNNING")
    with db.connection() as conn:
        free_only = nodes.capacity_of(conn, node).projected_connections

    project("mt00000c", plan_code="production", worker_state="RUNNING",
            plan_limits={"postgrest_pool_size": 12})
    with db.connection() as conn:
        with_production = nodes.capacity_of(conn, node).projected_connections

    free_pool = entitlements.resolve("free", None).postgrest_pool_size
    assert free_only == free_pool + 1
    assert with_production == free_only + 13, "the production project's own pool was not used"


def test_an_auth_worker_adds_to_the_projection(project, node):
    """ADR-022 counted Auth as a per-project cost. A projection that ignored it
    would let a node fill past its connection ceiling."""
    project("mt00000d", worker_state="RUNNING", auth_worker_state="STOPPED")
    with db.connection() as conn:
        without = nodes.capacity_of(conn, node).projected_connections
    project("mt00000e", worker_state="STOPPED", auth_worker_state="RUNNING")
    with db.connection() as conn:
        with_auth = nodes.capacity_of(conn, node).projected_connections
    assert with_auth == without + entitlements.AUTH_ROLE_CONNECTIONS


def test_a_node_out_of_connection_headroom_stops_accepting(project, node):
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = "
            "'{\"max_connections\": 20, \"reserved_connections\": 3, "
            "  \"max_warm_projects\": 100}'::jsonb WHERE id = %s",
            (node,),
        )
        conn.commit()
    for i in range(3):
        project(f"mt0000{i}f", worker_state="RUNNING")

    with db.connection() as conn:
        capacity = nodes.capacity_of(conn, node)
    reason = capacity.rejection_reason()
    assert reason and "connection headroom" in reason, (
        f"projected {capacity.projected_connections} of {capacity.usable_connections} usable"
    )


def test_the_platform_keeps_connections_for_itself(project, node):
    """A node full of tenants must still be administrable: provisioning,
    measurement and health checks all connect."""
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = '{\"max_connections\": 100}'::jsonb WHERE id = %s",
            (node,),
        )
        conn.commit()
        capacity = nodes.capacity_of(conn, node)
    assert capacity.usable_connections == (
        100 - capacity.reserved_connections - nodes.PLATFORM_CONNECTION_ALLOWANCE
    )


def test_capacity_over_a_ceiling_is_reportable_before_it_bites(project, node):
    """Enforcing changes placement for nodes that accept projects today. An
    operator should learn that from a command, not from a failed provisioning
    run."""
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = '{\"max_warm_projects\": 1}'::jsonb WHERE id = %s",
            (node,),
        )
        conn.commit()
    project("mt00000g", worker_state="RUNNING")
    project("mt00000h", worker_state="RUNNING")

    with db.connection() as conn:
        over = maintenance.unenforced_capacity(conn)
    assert len(over) == 1
    assert over[0]["name"] == "mt-node"
    assert "warm capacity" in over[0]["reason"]


def test_a_healthy_node_reports_nothing(project, node):
    project("mt00000i", worker_state="RUNNING")
    with db.connection() as conn:
        assert maintenance.unenforced_capacity(conn) == []


# -- the other two inert functions -----------------------------------------


def test_the_storage_pass_reaches_every_project_eventually(project):
    """Ordering by measurement age is what stops a limited pass re-measuring the
    same head of the list while the tail goes years without."""
    for i in range(3):
        project(f"mt0000{i}j")
    calls: list[str] = []

    def connect(conn, node_id, key_ring):
        raise RuntimeError("node unreachable")

    with db.connection() as conn:
        result = maintenance.measure_storage(conn, key_ring=None, connect_to_node=connect)
    assert result.failed == 3, "an unreachable node stopped the pass instead of noting it"
    assert calls == []


def test_the_object_storage_pass_is_independent_of_the_database_one(project):
    """Its own cursor and its own pass, so the two can fail separately.

    They read different things -- `pg_database_size` on the node admin
    connection, `storage.objects` inside the tenant -- and a project whose
    tenant connection fails must not stop its database size being measured, or
    the reverse. Asserted by advancing one and checking the other still sees
    every project as due.
    """
    refs = [f"mt0000{i}o" for i in range(3)]
    ids = [project(ref) for ref in refs]

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET object_measured_at = now() WHERE id = ANY(%s)",
            (ids,),
        )
        conn.commit()
        # The object pass has just seen all three; the database pass has seen
        # none of them, and says so.
        assert len(storage.due_for_measurement(conn)) >= 3


def test_the_object_storage_pass_notes_an_unreachable_node_rather_than_stopping(project):
    for i in range(3):
        project(f"mt0000{i}p")

    def connect(conn, node_id, key_ring):
        raise RuntimeError("node unreachable")

    with db.connection() as conn:
        result = maintenance.measure_object_storage(
            conn, key_ring=None, connect_to_node=connect
        )
    assert result.failed == 3, "an unreachable node stopped the pass instead of noting it"


def test_the_retry_pass_only_picks_up_projects_whose_backoff_elapsed(project):
    project_id = project("mt00000k")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET status = 'RETRY_WAIT', retry_after = now() + interval '1 hour' "
            "WHERE id = %s",
            (project_id,),
        )
        conn.commit()

        def connect(c, n, k):
            raise AssertionError("a project still inside its backoff was retried")

        result = maintenance.retry_failed_provisioning(
            conn, key_ring=None, platform_owner="postgres", connect_to_node=connect
        )
    assert result.handled == 0


# -- the pass slice 3 wrote a query for and slice 7 gave a caller ----------
#
# `storage_workers.registered_projects` shipped in slice 3 and nothing called
# it, which is the shape this module's docstring opens by naming. The case is
# narrow: a container restart keeps its tenants, so only a worker whose
# multitenant database was rebuilt forgets them -- and when it does, every
# Storage request for those projects answers `400 TenantNotFound` and nothing
# says why.


@pytest.fixture
def storage_config(app_config):
    """A config that claims an object store, which is all this pass checks."""
    import dataclasses

    return dataclasses.replace(
        app_config,
        storage_s3_endpoint="http://10.91.0.1:8333",
        storage_admin_port=5001,
    )


@pytest.fixture
def storage_node(db_pool, node, key_ring):
    """`node`, given the root secret a node that has run a worker would hold."""
    from services.control_plane import storage_workers

    with db.connection() as conn:
        storage_workers.ensure_node_secret(conn, node_id=node, key_ring=key_ring)
        conn.commit()
    return node


@pytest.fixture
def worker_admin(monkeypatch):
    """Stand in for the worker's admin API, recording what it was asked.

    Patched at the seam rather than over HTTP because what is under test is the
    pass's decision -- ask, then re-register only what is missing. The admin
    API itself is exercised against a real container in
    `tests/test_storage_workers.py`.
    """
    from services.control_plane import storage_workers

    state = {"known": set(), "ready": True, "asked": [], "registered": [], "fail": set()}

    def is_ready(*, admin_port, api_key, timeout=2.0):
        return state["ready"]

    def tenant_known(*, admin_port, api_key, project_ref):
        state["asked"].append(project_ref)
        return project_ref in state["known"]

    def ensure_registered(conn, *, project_id, project_ref, node_id, config, key_ring):
        if project_ref in state["fail"]:
            raise storage_workers.StorageWorkerError("the worker refused")
        state["registered"].append(project_ref)
        state["known"].add(project_ref)
        return True

    monkeypatch.setattr(maintenance.storage_workers, "is_ready", is_ready)
    monkeypatch.setattr(maintenance.storage_workers, "tenant_known", tenant_known)
    monkeypatch.setattr(maintenance.storage_workers, "ensure_registered", ensure_registered)
    return state


def _mark_registered(*project_ids):
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET storage_registered_at = now() WHERE id = ANY(%s)",
            (list(project_ids),),
        )
        conn.commit()


def test_a_worker_that_has_forgotten_a_tenant_is_told_about_it_again(
    project, storage_node, storage_config, worker_admin, key_ring
):
    forgotten = project("mt0000sa")
    _mark_registered(forgotten)

    with db.connection() as conn:
        result = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )

    assert worker_admin["registered"] == ["mt0000sa"], (
        "a project the worker had forgotten was left answering TenantNotFound"
    )
    assert result.handled == 1
    assert any("re-registered" in line for line in result.detail), result.detail


def test_a_worker_that_remembers_its_tenants_is_left_alone(
    project, storage_node, storage_config, worker_admin, key_ring
):
    """Registration is a PUT and would succeed either way, which is exactly why
    this is worth pinning: a pass that re-registered unconditionally would look
    identical in its output and rewrite every tenant's configuration on every
    run."""
    known = project("mt0000sb")
    _mark_registered(known)
    worker_admin["known"].add("mt0000sb")

    with db.connection() as conn:
        result = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )

    assert worker_admin["asked"] == ["mt0000sb"]
    assert worker_admin["registered"] == []
    assert result.handled == 1
    assert result.detail == []


def test_a_project_the_control_plane_never_registered_is_not_registered_now(
    project, storage_node, storage_config, worker_admin, key_ring
):
    """`storage_registered_at` NULL is the on-demand case, not the broken one.

    Migration 0025 says a project that is not registered is simply one whose
    next Storage request registers it. A repair pass that also did the first
    registration would warm every project on the node whether or not anybody
    had ever asked it for an object.
    """
    project("mt0000sc")

    with db.connection() as conn:
        result = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )

    assert worker_admin["asked"] == []
    assert worker_admin["registered"] == []
    assert result.handled == 0


def test_a_worker_that_is_not_ready_is_not_registered_into(
    project, storage_node, storage_config, worker_admin, key_ring
):
    """A stopped worker is not a rebuilt one, and a half-started one is worse
    than either: it accepts connections before it has migrated the database the
    tenant row goes into, which is what `is_ready` exists to catch."""
    _mark_registered(project("mt0000sd"))
    worker_admin["ready"] = False

    with db.connection() as conn:
        result = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )

    assert worker_admin["asked"] == []
    assert worker_admin["registered"] == []
    assert result.failed == 0, "a stopped worker is not a failure of this pass"
    assert any("not ready" in line for line in result.detail), result.detail


def test_a_deployment_with_no_object_store_reconciles_nothing(
    project, storage_node, app_config, worker_admin, key_ring
):
    _mark_registered(project("mt0000se"))

    with db.connection() as conn:
        result = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=app_config
        )

    assert worker_admin["asked"] == []
    assert (result.handled, result.failed, result.detail) == (0, 0, [])


def test_a_second_storage_node_stops_the_pass_rather_than_making_it_guess(
    project, storage_node, storage_config, worker_admin, key_ring
):
    """The one place this pass differs from every other one here.

    ADR-058 puts the worker's admin port on loopback, so this repairs the
    worker on the host it runs on -- and with two storage nodes in the table,
    only an operator knows which host that is. Guessing would re-register
    another node's tenants into this node's worker, which is worse than doing
    nothing.
    """
    from services.control_plane import storage_workers

    _mark_registered(project("mt0000sf"))
    with db.connection() as conn:
        other = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, capacity_json) "
            "VALUES ('mt-node-2','mt2.example','mt2.internal','shared','active','{}'::jsonb) "
            "ON CONFLICT (name) DO UPDATE SET status='active' RETURNING id",
        )["id"]
        storage_workers.ensure_node_secret(conn, node_id=other, key_ring=key_ring)
        db.execute(
            conn,
            "UPDATE projects SET node_id = %s WHERE project_ref = 'mt0000sf'",
            (other,),
        )
        conn.commit()

    _mark_registered(project("mt0000sg"))

    with db.connection() as conn:
        ambiguous = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )

    assert worker_admin["asked"] == [], "the pass guessed which node it was on"
    assert worker_admin["registered"] == []
    assert ambiguous.handled == 0
    assert any("--node" in line for line in ambiguous.detail), ambiguous.detail

    # Named, it acts -- and only on that node's projects. `mt0000sg` is on the
    # other node and is not touched, which is the half that would silently be
    # wrong if the pass took the whole table.
    with db.connection() as conn:
        named = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config, node_name="mt-node-2"
        )

    assert worker_admin["asked"] == ["mt0000sf"], "--node did not scope the pass to one node"
    assert worker_admin["registered"] == ["mt0000sf"]
    assert named.handled == 1


def test_a_node_that_never_ran_a_worker_is_not_given_a_secret(
    project, node, storage_config, worker_admin, key_ring
):
    """`node_secret` rather than `ensure_node_secret`, and the difference is not
    stylistic. AUTH_ENCRYPTION_KEY is derived from that root and decrypts every
    registered tenant's connection settings, so minting one from a *repair*
    would leave the node's own worker unable to read what it had already
    written."""
    _mark_registered(project("mt0000sh"))

    with db.connection() as conn:
        maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )
        still_none = db.one(
            conn, "SELECT storage_secret_ciphertext FROM nodes WHERE id = %s", (node,)
        )

    assert still_none["storage_secret_ciphertext"] is None, (
        "a reconciliation pass minted a storage root secret for a node that never had one"
    )
    assert worker_admin["registered"] == []


def test_one_project_the_worker_refuses_does_not_stop_the_others(
    project, storage_node, storage_config, worker_admin, key_ring
):
    _mark_registered(project("mt0000si"), project("mt0000sj"))
    worker_admin["fail"].add("mt0000si")

    with db.connection() as conn:
        result = maintenance.reconcile_storage_tenants(
            conn, key_ring=key_ring, config=storage_config
        )

    assert worker_admin["registered"] == ["mt0000sj"]
    assert (result.handled, result.failed) == (1, 1)


def test_the_object_storage_pass_says_when_it_is_using_the_forgeable_figure(project):
    """The finding slice 7 closed, turned into something a run shows.

    `measure_object_storage` falls back to the tenant's own `storage.objects`
    when it has no object store, and that column is writable by anything that
    can reach `service_role` -- which is why slice 3 replaced it. The fallback
    is right on a node with no store and wrong everywhere else, and for four
    slices the only production caller took it without a word, because `run_all`
    defaults `config` to None and nothing passed one.
    """
    project("mt0000sk")

    def connect(conn, node_id, key_ring):
        raise RuntimeError("not reached")

    with db.connection() as conn:
        result = maintenance.measure_object_storage(
            conn, key_ring=None, connect_to_node=connect, config=None
        )

    assert any("service_role" in line for line in result.detail), (
        "the pass used the customer-writable figure and said nothing"
    )
