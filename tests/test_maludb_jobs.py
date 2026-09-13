"""Customer requests for the MaluDB data-model graph (Phase 12 slice 4, ADR-074).

Three layers, tested where each can fail:

- **the queue** -- coalescing, the per-plan limit and what it tells a refused
  customer -- against the control plane alone;
- **the routes** -- who may ask for what, and that they only ever queue;
- **the worker** -- that the provisioner does the work against a real tenant, and
  that what a customer is shown on failure is the platform's sentence, never a
  node's error text.

The gateway's opt-in check is in `tests/test_gateway.py`, beside the harness it
needs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, maludb, maludb_jobs, nodes, provisioner
from tests.conftest import NODE_ADMIN_DSN, TEST_CREDENTIAL, requires_db
from tests.test_maludb_enable import (  # noqa: F401 - fixtures, resolved by name
    _tenant_conn,
    admin_node_conn,
    requires_node,
    tenants,
)

pytestmark = requires_db

ENABLE = "/v1/projects/{ref}/maludb/datamodel/enable"
REFRESH = "/v1/projects/{ref}/maludb/datamodel/refresh"
STATUS = "/v1/projects/{ref}/maludb/datamodel"


def _set_plan(project_id, config: dict) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = (SELECT plan_id FROM projects WHERE id = %s)",
            (Jsonb(config), project_id),
        )
        conn.commit()


def _mark_enabled(project_id) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET maludb_datamodel_enabled = TRUE, maludb_datamodel_enabled_at = now(), "
            "maludb_memory_schema_version = '0.104.0' WHERE id = %s",
            (project_id,),
        )
        conn.commit()


def _jobs(project_id) -> list[dict]:
    with db.connection() as conn:
        return db.query(
            conn, "SELECT id, kind, state, requested_at FROM maludb_jobs WHERE project_id = %s ORDER BY id",
            (project_id,),
        )


def _request_refresh(project_id, **kw):
    with db.connection() as conn:
        try:
            return maludb_jobs.request_refresh(conn, project_id=project_id, requested_by=None, **kw)
        finally:
            conn.commit()


def _request_enable(project_id):
    with db.connection() as conn:
        try:
            return maludb_jobs.request_enable(conn, project_id=project_id, requested_by=None)
        finally:
            conn.commit()


def _set_state(job_id, state: str) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE maludb_jobs SET state = %s, "
            "started_at = CASE WHEN %s = 'pending' THEN NULL ELSE coalesce(started_at, now()) END, "
            "completed_at = CASE WHEN %s IN ('succeeded','failed') THEN now() ELSE NULL END "
            "WHERE id = %s",
            (state, state, state, job_id),
        )
        conn.commit()


# -- the queue -------------------------------------------------------------


def test_enabling_queues_once_and_a_second_request_joins_it(placed_project):
    project_id = placed_project("mjena001")

    first = _request_enable(project_id)
    second = _request_enable(project_id)

    assert first is not None and not first.coalesced
    assert second.coalesced and second.job_id == first.job_id
    assert len(_jobs(project_id)) == 1


def test_enabling_an_enabled_project_queues_nothing(placed_project):
    """Enablement takes a full copy, so re-enabling on request would be an
    unmetered refresh."""
    project_id = placed_project("mjena002")
    _mark_enabled(project_id)
    assert _request_enable(project_id) is None
    assert _jobs(project_id) == []


def test_a_plan_without_the_entitlement_is_refused(placed_project):
    project_id = placed_project("mjent001")
    _set_plan(project_id, {"maludb_datamodel": False})
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request_enable(project_id)
    assert refused.value.status == 403


def test_a_refresh_needs_the_graph_enabled_first(placed_project):
    project_id = placed_project("mjref001")
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request_refresh(project_id)
    assert refused.value.status == 409
    assert "enable it first" in str(refused.value)


def test_a_pending_refresh_absorbs_a_request_but_a_running_one_does_not(placed_project):
    """A running refresh may have read the catalogue before the migration the
    customer is refreshing for, so it must not swallow the new request."""
    project_id = placed_project("mjcoa001")
    _mark_enabled(project_id)

    first = _request_refresh(project_id)
    joined = _request_refresh(project_id)
    assert joined.coalesced and joined.job_id == first.job_id

    _set_state(first.job_id, "running")
    behind = _request_refresh(project_id)
    assert not behind.coalesced and behind.job_id != first.job_id
    assert [j["state"] for j in _jobs(project_id)] == ["running", "pending"]


def test_the_plans_limit_is_refused_at_the_request_with_when_it_frees(placed_project):
    """A refusal now, naming the limit and the wait -- never a queued request
    that silently never runs."""
    project_id = placed_project("mjlim001")
    _mark_enabled(project_id)
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 2}})

    now = datetime.now(UTC)
    for _ in range(2):
        queued = _request_refresh(project_id, now=now)
        _set_state(queued.job_id, "succeeded")
    oldest = _jobs(project_id)[0]["requested_at"]

    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request_refresh(project_id, now=now)

    assert refused.value.status == 429
    assert "2 data-model refresh(es) an hour" in str(refused.value)
    expected = (oldest + timedelta(hours=1) - now).total_seconds()
    assert abs(refused.value.retry_after - expected) <= 2, (refused.value.retry_after, expected)


def _fail(job_id, *, refused: bool) -> None:
    with db.connection() as conn:
        db.execute(conn, "UPDATE maludb_jobs SET state = 'running', started_at = now() WHERE id = %s",
                   (job_id,))
        maludb_jobs.finish(conn, job_id, succeeded=False, detail="x", refused=refused)
        conn.commit()


def test_a_refresh_the_platform_broke_does_not_count_against_the_limit(placed_project):
    """When the platform could not do what was asked, charging the customer for
    it makes the platform's failure the customer's problem."""
    project_id = placed_project("mjlim002")
    _mark_enabled(project_id)
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 1}})

    queued = _request_refresh(project_id)
    _fail(queued.job_id, refused=False)

    again = _request_refresh(project_id)
    assert not again.coalesced


def test_a_request_the_platform_refused_does_count(placed_project):
    """A refusal is something a customer can cause on purpose -- after superuser
    work has started. Exempting it would make that work free to repeat."""
    project_id = placed_project("mjlim004")
    _mark_enabled(project_id)
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 1}})

    queued = _request_refresh(project_id)
    _fail(queued.job_id, refused=True)

    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request_refresh(project_id)
    assert refused.value.status == 429


def test_enabling_draws_on_the_same_budget(placed_project):
    """An enablement that keeps failing on something the customer controls must
    not be an unmetered way to run the whole copy again and again."""
    project_id = placed_project("mjlim005")
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 1}})

    queued = _request_enable(project_id)
    _fail(queued.job_id, refused=True)

    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request_enable(project_id)
    assert refused.value.status == 429


def test_a_zero_limit_names_the_plan_rather_than_a_time(placed_project):
    project_id = placed_project("mjlim003")
    _mark_enabled(project_id)
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 0}})
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _request_refresh(project_id)
    assert refused.value.status == 429 and refused.value.retry_after is None


def test_a_job_abandoned_mid_run_is_failed_on_the_next_claim(placed_project):
    project_id = placed_project("mjabn001")
    _mark_enabled(project_id)
    queued = _request_refresh(project_id)
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE maludb_jobs SET state = 'running', started_at = now() - interval '1 hour' WHERE id = %s",
            (queued.job_id,),
        )
        conn.commit()
        assert maludb_jobs.claim(conn) is None
        conn.commit()
    state = _jobs(project_id)[0]["state"]
    assert state == "failed", "a job a dead worker left running still looks live"


# -- the routes ------------------------------------------------------------


def _headers(client, ref: str) -> dict:
    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _member(client, ref: str, *, email: str, role: str) -> dict:
    client.post("/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL})
    with db.connection() as conn:
        org = db.one(conn, "SELECT org_id FROM projects WHERE project_ref = %s", (ref,))["org_id"]
        user = db.one(conn, "SELECT id FROM users WHERE email = %s", (email,))["id"]
        db.execute(conn, "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
                   (org, user, role))
        conn.commit()
    token = client.post("/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_a_manager_queues_enablement_and_the_status_shows_it(client, placed_project):
    placed_project("mjrte001")
    headers = _headers(client, "mjrte001")

    queued = client.post(ENABLE.format(ref="mjrte001"), headers=headers)
    assert queued.status_code == 202, queued.text
    assert queued.json()["job"]["state"] == "pending"

    shown = client.get(STATUS.format(ref="mjrte001"), headers=headers).json()
    assert shown["enabled"] is False
    assert shown["latest_enable"]["id"] == queued.json()["job"]["id"]
    assert shown["refreshes_per_hour"] > 0


def test_a_developer_can_refresh_but_cannot_enable(client, placed_project):
    project_id = placed_project("mjrte002")
    developer = _member(client, "mjrte002", email="mj-dev@example.com", role="developer")

    refused = client.post(ENABLE.format(ref="mjrte002"), headers=developer)
    assert refused.status_code == 403

    _mark_enabled(project_id)
    assert client.post(REFRESH.format(ref="mjrte002"), headers=developer).status_code == 202


def test_a_non_member_cannot_tell_the_project_exists(client, placed_project):
    placed_project("mjrte003")
    outsider = _member(client, "mjrte003", email="mj-out@example.com", role="viewer")
    # Undo the membership: an outsider is someone with no row at all.
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM org_members WHERE user_id = (SELECT id FROM users WHERE email = %s)",
                   ("mj-out@example.com",))
        conn.commit()
    for method, path in (("post", ENABLE), ("post", REFRESH), ("get", STATUS)):
        answered = getattr(client, method)(path.format(ref="mjrte003"), headers=outsider)
        assert answered.status_code == 404, (path, answered.status_code)
        assert answered.json()["detail"] == "project not found"


def test_a_refused_refresh_carries_retry_after(client, placed_project):
    project_id = placed_project("mjrte004")
    _mark_enabled(project_id)
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 1}})
    headers = _headers(client, "mjrte004")
    first = client.post(REFRESH.format(ref="mjrte004"), headers=headers)
    assert first.status_code == 202
    _set_state(first.json()["job"]["id"], "succeeded")

    refused = client.post(REFRESH.format(ref="mjrte004"), headers=headers)

    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    assert "an hour" in refused.json()["detail"]


def test_enabling_an_enabled_project_answers_200_and_queues_nothing(client, placed_project):
    project_id = placed_project("mjrte005")
    _mark_enabled(project_id)
    answered = client.post(ENABLE.format(ref="mjrte005"), headers=_headers(client, "mjrte005"))
    assert answered.status_code == 200
    assert answered.json()["job"] is None
    assert _jobs(project_id) == []


# -- the worker ------------------------------------------------------------


@pytest.fixture
def worker_node(key_ring):
    """Give the node the tenants fixture registers a real, encrypted admin DSN."""
    def arm(node_name: str = "mdb-node") -> None:
        with db.connection() as conn:
            nodes.set_admin_dsn(conn, name=node_name, dsn=NODE_ADMIN_DSN, key_ring=key_ring)
            conn.commit()
    return arm


def _queue(project_id, kind: str) -> int:
    with db.connection() as conn:
        job = db.one(conn, "INSERT INTO maludb_jobs (project_id, kind) VALUES (%s, %s) RETURNING id",
                     (project_id, kind))["id"]
        conn.commit()
    return job


def _job(job_id) -> dict:
    with db.connection() as conn:
        return db.one(conn, "SELECT state, detail, result_json, refused FROM maludb_jobs WHERE id = %s",
                      (job_id,))


@requires_node
def test_the_provisioner_enables_then_refreshes_a_real_tenant(tenants, worker_node, key_ring):  # noqa: F811 - imported fixture
    project_id, names, _ = tenants("mjwrk001")
    worker_node()
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("CREATE TABLE public.orders (id bigint PRIMARY KEY)")

    enable_job = _queue(project_id, "enable")
    assert provisioner.run_maludb_once(key_ring=key_ring)
    done = _job(enable_job)
    assert done["state"] == "succeeded", done["detail"]
    assert done["result_json"]["relations"] == 1

    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("CREATE TABLE public.invoices (id bigint PRIMARY KEY)")
    refresh_job = _queue(project_id, "refresh")
    assert provisioner.run_maludb_once(key_ring=key_ring)
    refreshed = _job(refresh_job)
    assert refreshed["state"] == "succeeded", refreshed["detail"]
    assert refreshed["result_json"]["relations"] == 2
    assert provisioner.run_maludb_once(key_ring=key_ring) is False, "the queue should be empty"


@requires_node
def test_a_platform_refusal_reaches_the_customer_in_its_own_words(tenants, worker_node, key_ring):  # noqa: F811 - imported fixture
    project_id, names, _ = tenants("mjwrk002")
    worker_node()
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute(f'CREATE SCHEMA "{maludb.COPY_SCHEMA}"')

    job = _queue(project_id, "enable")
    provisioner.run_maludb_once(key_ring=key_ring)

    failed = _job(job)
    assert failed["state"] == "failed"
    assert "Rename or drop" in failed["detail"]
    assert failed["refused"] is True, "a refusal must count against the plan's limit"


@requires_node
def test_an_unexpected_failure_shows_the_customer_nothing_of_the_error(
    tenants, worker_node, key_ring, monkeypatch  # noqa: F811 - imported fixture
):
    """The status route shows `detail`. An exception's own text came from a node,
    and is the one thing that could carry an internal detail to a customer."""
    project_id, _, _ = tenants("mjwrk003")
    worker_node()
    _mark_enabled(project_id)

    def boom(*args, **kwargs):
        raise RuntimeError("could not connect to postgresql://postgres:hunter2@10.0.0.9/mldb_x")

    monkeypatch.setattr(maludb, "refresh", boom)
    job = _queue(project_id, "refresh")
    provisioner.run_maludb_once(key_ring=key_ring)

    failed = _job(job)
    assert failed["state"] == "failed"
    assert "hunter2" not in (failed["detail"] or "") and "postgresql://" not in (failed["detail"] or "")
    assert "could not complete" in failed["detail"]
    assert failed["refused"] is False, "the platform's own failure must not cost the customer"


def test_the_gateway_and_the_platform_agree_on_the_schema_name():
    """The gateway does not import `maludb` -- it has no reason to load a module
    that runs superuser code -- so the two names are held together here."""
    from services.gateway import app as gateway_app

    assert gateway_app.MALUDB_SCHEMA == maludb.COPY_SCHEMA


def test_the_queue_module_imports_nothing_that_does_node_work():
    """The public routes import `maludb_jobs`. If it ever imported `maludb`, the
    surface test would catch a forbidden call only if one were reachable; this
    says the boundary plainly, where the next reader looks."""
    import ast
    import pathlib

    source = pathlib.Path(maludb_jobs.__file__).read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module == "services.control_plane"
        for alias in node.names
    }
    assert not imported & {"maludb", "provisioning", "nodes", "jobs", "workers"}, imported


# -- turning it off (slice 6) ----------------------------------------------

DISABLE = "/v1/projects/{ref}/maludb/datamodel/disable"


def _request_disable(project_id):
    with db.connection() as conn:
        try:
            return maludb_jobs.request_disable(conn, project_id=project_id, requested_by=None)
        finally:
            conn.commit()


def test_disabling_an_enabled_project_queues_once_and_joins(placed_project):
    project_id = placed_project("mjdis001")
    _mark_enabled(project_id)
    first = _request_disable(project_id)
    second = _request_disable(project_id)
    assert first is not None and not first.coalesced
    assert second.coalesced and second.job_id == first.job_id


def test_disabling_a_project_that_is_off_queues_nothing(placed_project):
    project_id = placed_project("mjdis002")
    assert _request_disable(project_id) is None
    assert _jobs(project_id) == []


def test_a_disable_behind_a_pending_enable_is_queued(placed_project):
    """The customer changed their mind; the later request has to win."""
    project_id = placed_project("mjdis003")
    _request_enable(project_id)
    queued = _request_disable(project_id)
    assert queued is not None
    assert [j["kind"] for j in _jobs(project_id)] == ["enable", "disable"]


def test_a_plan_without_the_entitlement_can_still_disable(placed_project):
    """Refusing would leave a project's structure published because of a billing change."""
    project_id = placed_project("mjdis004")
    _mark_enabled(project_id)
    _set_plan(project_id, {"maludb_datamodel": False})
    assert _request_disable(project_id) is not None


def test_disabling_does_not_spend_the_budget(placed_project):
    project_id = placed_project("mjdis005")
    _mark_enabled(project_id)
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 1}})
    refresh = _request_refresh(project_id)
    _set_state(refresh.job_id, "succeeded")

    assert _request_disable(project_id) is not None, "a disable was refused by the refresh budget"


def test_only_a_manager_can_disable(client, placed_project):
    project_id = placed_project("mjdis006")
    _mark_enabled(project_id)
    developer = _member(client, "mjdis006", email="mj-dis-dev@example.com", role="developer")
    assert client.post(DISABLE.format(ref="mjdis006"), headers=developer).status_code == 403

    queued = client.post(DISABLE.format(ref="mjdis006"), headers=_headers(client, "mjdis006"))
    assert queued.status_code == 202, queued.text
    shown = client.get(STATUS.format(ref="mjdis006"), headers=_headers(client, "mjdis006")).json()
    assert shown["latest_disable"]["id"] == queued.json()["job"]["id"]


@requires_node
def test_the_provisioner_disables_a_real_tenant(tenants, worker_node, key_ring):  # noqa: F811 - imported fixture
    project_id, names, _ = tenants("mjwrk004")
    worker_node()
    _queue(project_id, "enable")
    assert provisioner.run_maludb_once(key_ring=key_ring)

    job = _queue(project_id, "disable")
    assert provisioner.run_maludb_once(key_ring=key_ring)

    done = _job(job)
    assert done["state"] == "succeeded", done["detail"]
    with _tenant_conn(names.database) as t:
        published = t.execute("SELECT count(*) FROM pg_db_role_setting WHERE setrole = %s::regrole",
                              (names.authenticator,)).fetchone()[0]
    assert published == 0


# -- vector compartments (ADR-077, compartments slice 2b) --------------------

VECTORS_ENABLE = "/v1/projects/{ref}/maludb/vectors/enable"
VECTORS_DISABLE = "/v1/projects/{ref}/maludb/vectors/disable"
VECTORS_STATUS = "/v1/projects/{ref}/maludb/vectors"


def _vectors_request(project_id, *, on: bool = True):
    with db.connection() as conn:
        try:
            action = maludb_jobs.request_vectors_enable if on else maludb_jobs.request_vectors_disable
            return action(conn, project_id=project_id, requested_by=None)
        finally:
            conn.commit()


def _mark_vectors_enabled(project_id) -> None:
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET maludb_vectors_enabled = TRUE, "
                   "maludb_vectors_enabled_at = now() WHERE id = %s", (project_id,))
        conn.commit()


def test_vectors_enabling_queues_once_and_joins(placed_project):
    project_id = placed_project("mjvec001")
    first = _vectors_request(project_id)
    second = _vectors_request(project_id)
    assert not first.coalesced and second.coalesced and second.job_id == first.job_id
    assert [j["kind"] for j in _jobs(project_id)] == ["vectors_enable"]


def test_vectors_enabling_an_enabled_project_queues_nothing(placed_project):
    project_id = placed_project("mjvec002")
    _mark_vectors_enabled(project_id)
    assert _vectors_request(project_id) is None
    assert _jobs(project_id) == []


def test_the_two_features_queue_independently(placed_project):
    """A pending graph enablement does not absorb a vectors one, or the reverse."""
    project_id = placed_project("mjvec003")
    graph = _request_enable(project_id)
    vectors = _vectors_request(project_id)
    assert not vectors.coalesced and vectors.job_id != graph.job_id


def test_a_plan_without_vectors_is_refused(placed_project):
    project_id = placed_project("mjvec004")
    _set_plan(project_id, {"maludb_vectors": False})
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _vectors_request(project_id)
    assert refused.value.status == 403


def test_vectors_enabling_draws_on_the_budget_and_disabling_does_not(placed_project):
    """Alternating enable and disable would otherwise make superuser work free."""
    project_id = placed_project("mjvec005")
    _set_plan(project_id, {"limits": {"datamodel_refreshes_per_hour": 1}})
    first = _vectors_request(project_id)
    _set_state(first.job_id, "succeeded")
    _mark_vectors_enabled(project_id)
    assert _vectors_request(project_id, on=False) is not None  # disabling is never refused
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET maludb_vectors_enabled = FALSE WHERE id = %s", (project_id,))
        db.execute(conn, "UPDATE maludb_jobs SET state = 'succeeded', started_at = now(), completed_at = now() "
                   "WHERE project_id = %s AND kind = 'vectors_disable'", (project_id,))
        conn.commit()
    with pytest.raises(maludb_jobs.JobRefused) as refused:
        _vectors_request(project_id)
    assert refused.value.status == 429


def test_vectors_disabling_a_project_that_is_off_queues_nothing(placed_project):
    project_id = placed_project("mjvec006")
    assert _vectors_request(project_id, on=False) is None


def test_a_plan_without_vectors_can_still_turn_them_off(placed_project):
    project_id = placed_project("mjvec007")
    _mark_vectors_enabled(project_id)
    _set_plan(project_id, {"maludb_vectors": False})
    assert _vectors_request(project_id, on=False) is not None


def test_a_manager_queues_vectors_and_the_status_shows_limits(client, placed_project):
    placed_project("mjvec008")
    headers = _headers(client, "mjvec008")
    queued = client.post(VECTORS_ENABLE.format(ref="mjvec008"), headers=headers)
    assert queued.status_code == 202, queued.text
    shown = client.get(VECTORS_STATUS.format(ref="mjvec008"), headers=headers).json()
    assert shown["enabled"] is False and shown["entitled"] is True
    assert shown["latest_enable"]["id"] == queued.json()["job"]["id"]
    assert shown["max_vectors"] > 0 and shown["max_dimensions"] > 0 and shown["max_compartments"] > 0


def test_only_a_manager_can_turn_vectors_on_or_off(client, placed_project):
    project_id = placed_project("mjvec009")
    developer = _member(client, "mjvec009", email="mjv-dev@example.com", role="developer")
    assert client.post(VECTORS_ENABLE.format(ref="mjvec009"), headers=developer).status_code == 403
    _mark_vectors_enabled(project_id)
    assert client.post(VECTORS_DISABLE.format(ref="mjvec009"), headers=developer).status_code == 403


def test_a_non_member_cannot_tell_a_vectors_project_exists(client, placed_project):
    placed_project("mjvec010")
    outsider = _member(client, "mjvec010", email="mjv-out@example.com", role="viewer")
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM org_members WHERE user_id = (SELECT id FROM users WHERE email = %s)",
                   ("mjv-out@example.com",))
        conn.commit()
    for path in (VECTORS_ENABLE, VECTORS_DISABLE):
        assert client.post(path.format(ref="mjvec010"), headers=outsider).status_code == 404
    assert client.get(VECTORS_STATUS.format(ref="mjvec010"), headers=outsider).status_code == 404


@requires_node
def test_the_provisioner_turns_vectors_on_and_off_for_a_real_tenant(tenants, worker_node, key_ring):  # noqa: F811 - imported fixture
    project_id, names, _ = tenants("mjvwk001")
    worker_node()

    on = _queue(project_id, "vectors_enable")
    assert provisioner.run_maludb_once(key_ring=key_ring)
    done = _job(on)
    assert done["state"] == "succeeded", done["detail"]
    with _tenant_conn(names.database) as t:
        assert t.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                         "WHERE n.nspname = 'maludb' AND p.proname = 'vector_search'").fetchone()[0] == 1

    off = _queue(project_id, "vectors_disable")
    assert provisioner.run_maludb_once(key_ring=key_ring)
    assert _job(off)["state"] == "succeeded"
    with db.connection() as conn:
        assert db.one(conn, "SELECT maludb_vectors_enabled FROM projects WHERE id = %s",
                      (project_id,))["maludb_vectors_enabled"] is False


@requires_node
def test_a_vectors_refusal_reaches_the_customer_in_its_own_words(tenants, worker_node, key_ring):  # noqa: F811 - imported fixture
    project_id, names, _ = tenants("mjvwk002")
    worker_node()
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute('CREATE SCHEMA "maludb_private"')
    job = _queue(project_id, "vectors_enable")
    provisioner.run_maludb_once(key_ring=key_ring)
    failed = _job(job)
    assert failed["state"] == "failed" and failed["refused"] is True
    assert "maludb_private" in failed["detail"]
