"""Asking for a project, and the worker that builds it.

Phase 07 slice 1. Two halves that must stay apart, and the tests are arranged
to say so: the API allocates a reference, reserves a place and records a
request, and the provisioner does everything that touches a node. ADR-038 is
the reason, and `tests/test_control_plane_surfaces.py` is what stops the halves
merging again by accident.

The API half runs anywhere a control-plane database does. The worker half needs
a real node, because provisioning a tenant means creating databases and roles
on one -- and a test that mocked that would be asserting that the platform can
provision against a mock.
"""

from __future__ import annotations

import psycopg
import pytest

from services.control_plane import db, models, nodes, provisioner
from tests.conftest import DATABASE_URL, NODE_ADMIN_DSN, requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret

pytestmark = requires_db


def _account(client, email: str) -> tuple[str, str]:
    """A user and their personal organization."""
    created = client.post(
        "/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL}
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL}
    ).json()["token"]
    org_id = created.json()["organizations"][0]["org_id"]
    return token, org_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def node(db_pool):  # noqa: ARG001 - db_pool prepares the database
    """A node with capacity and a plan catalogue with a default in it.

    Both are operator-provided in production: nothing seeds `plans`, because
    entitlements resolve their own defaults by code and the table is a
    catalogue rather than the source of the limits. A deployment that forgets
    it cannot create projects at all, which is why the route says so with a 503
    naming the platform rather than a 404 naming the caller's plan.
    """
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('free','Free','{}') "
            "ON CONFLICT (code) DO NOTHING",
        )
        db.execute(
            conn,
            # last_health_at matters: a node nobody has heard from recently is
            # not placeable, which is the behaviour that stops projects being
            # sent to a node that has gone away.
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES ('cp-node','cp.example','cp.internal','shared','active', now()) "
            "ON CONFLICT (name) DO UPDATE SET status = 'active', last_health_at = now()",
        )
        conn.commit()


@pytest.fixture
def node_with_admin_credential(node, key_ring):  # noqa: ARG001 - node creates the row
    """The same node, carrying the credential the provisioner will decrypt.

    Stored rather than passed in: reading each node's own credential from its
    row is what the worker does in production, and a test that handed it a DSN
    directly would skip the part ADR-038 is about.
    """
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    with db.connection() as conn:
        nodes.set_admin_dsn(conn, name="cp-node", dsn=NODE_ADMIN_DSN, key_ring=key_ring)
        conn.commit()


# -- asking for a project --------------------------------------------------


def test_a_project_can_be_asked_for_and_is_not_provisioned_in_the_request(client, node):
    """202, not 201: the thing named in the response does not exist on a node yet.

    Answering 201 would assert the platform had created something it has only
    written a row about, and a dashboard that believed it would show a customer
    a connection string for a database that is not there.
    """
    token, org_id = _account(client, "creator@example.com")

    response = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "My App"},
        headers=_auth(token),
    )
    assert response.status_code == 202, response.text
    body = response.json()

    assert body["display_name"] == "My App"
    assert len(body["project_ref"]) == models.PROJECT_REF_LENGTH
    assert models.is_valid_project_ref(body["project_ref"])
    # Placed, not provisioned. The worker takes it from here.
    assert body["status"] == "PLACEMENT_RESERVED"


def test_the_response_carries_nothing_that_could_reach_the_database(client, node):
    """The phase's first acceptance criterion, on the route that would break it.

    A create response is where a database name, a node hostname or a role
    password would be most tempting to include -- a dashboard wants to show
    *something* -- and it is exactly what a free project must never receive.
    """
    token, org_id = _account(client, "nocreds@example.com")
    body = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Quiet"},
        headers=_auth(token),
    ).json()

    # An exact field set rather than a subset check: the failure this guards
    # against is a field being *added* that should not be there, and a subset
    # assertion is blind to exactly that.
    assert set(body) == {"project_ref", "display_name", "status", "created_at", "api_url"}

    # api_url is the public hostname the gateway routes on, which is the one
    # address a customer is supposed to have. It must not be the node's.
    assert body["api_url"] == f"https://{body['project_ref']}.maludb.local"

    serialised = str(body).lower()
    for forbidden in ("password", "dsn", "postgres://", "postgresql://", "mldb_", "cp.internal"):
        assert forbidden not in serialised, f"{forbidden} reached a customer"


def test_the_same_idempotency_key_returns_the_same_project(client, node):
    """A double-clicked button must not buy two databases.

    A project is a database, four roles and a slot on a node. The second one
    costs all of that and serves nobody, and on a free tier the cost lands on
    the platform rather than on the person who clicked.
    """
    token, org_id = _account(client, "twice@example.com")
    headers = {**_auth(token), "Idempotency-Key": "button-click-1"}

    first = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "One"}, headers=headers
    )
    second = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "One"}, headers=headers
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["project_ref"] == second.json()["project_ref"]

    with db.connection() as conn:
        projects = models.list_projects_for_org(conn, org_id)
    assert len(projects) == 1, "the replay created a second project"


def test_a_reused_key_for_a_different_request_is_a_conflict(client, node):
    """Answering with the first project would hide a client bug rather than report it."""
    token, org_id = _account(client, "reuse@example.com")
    headers = {**_auth(token), "Idempotency-Key": "same-key"}

    client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "First"}, headers=headers
    )
    clash = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "Second"}, headers=headers
    )
    assert clash.status_code == 409


def test_two_organizations_may_use_the_same_key(client, node):
    """The key is the caller's, so scoping it globally would let one customer's
    choice of string refuse another customer's creation."""
    first_token, first_org = _account(client, "orga@example.com")
    second_token, second_org = _account(client, "orgb@example.com")

    a = client.post(
        f"/v1/organizations/{first_org}/projects",
        json={"display_name": "A"},
        headers={**_auth(first_token), "Idempotency-Key": "shared"},
    )
    b = client.post(
        f"/v1/organizations/{second_org}/projects",
        json={"display_name": "B"},
        headers={**_auth(second_token), "Idempotency-Key": "shared"},
    )
    assert a.status_code == 202
    assert b.status_code == 202
    assert a.json()["project_ref"] != b.json()["project_ref"]


def test_a_stranger_cannot_create_a_project_in_another_organization(client, node):
    """404 rather than 403, so the API does not confirm the organization exists."""
    _owner_token, org_id = _account(client, "owner@example.com")
    stranger, _ = _account(client, "stranger@example.com")

    refused = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Not mine"},
        headers=_auth(stranger),
    )
    assert refused.status_code == 404

    with db.connection() as conn:
        assert models.list_projects_for_org(conn, org_id) == []


def test_an_unknown_plan_is_refused_rather_than_defaulted(client, node):
    """Silently placing someone on another plan is a billing surprise."""
    token, org_id = _account(client, "plan@example.com")
    refused = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "X", "plan_code": "no-such-plan"},
        headers=_auth(token),
    )
    assert refused.status_code == 404


def test_an_empty_plan_catalogue_is_the_platform_s_fault_not_the_caller_s(client, db_pool):  # noqa: ARG001
    """Found by writing this suite: nothing seeds `plans`.

    Entitlements resolve their limits by plan *code* with their own defaults, so
    the table is a catalogue an operator populates rather than the source of the
    numbers -- and a deployment that never populates it cannot create a project.
    That is worth an error naming the platform, because a 404 saying "unknown
    plan" sends a customer hunting for a plan name that was never the problem.
    """
    token, org_id = _account(client, "nocatalogue@example.com")
    refused = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "X"},
        headers=_auth(token),
    )
    assert refused.status_code == 503
    assert "plan" in refused.json()["detail"]


def test_no_capacity_leaves_no_project_behind(client, db_pool):  # noqa: ARG001
    """A project with nowhere to go is a row the worker would pick up forever.

    So placement runs in the same transaction as the insert, and a fleet with no
    room answers 503 with nothing written -- rather than accepting a request it
    has no way to finish and leaving the customer watching a status that never
    changes.
    """
    token, org_id = _account(client, "nocapacity@example.com")

    response = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Homeless"},
        headers=_auth(token),
    )
    assert response.status_code == 503

    with db.connection() as conn:
        assert models.list_projects_for_org(conn, org_id) == []


# -- the worker ------------------------------------------------------------


def test_the_worker_claims_the_project_the_api_recorded(client, node):
    """The two halves meet here, without a node: claiming is control-plane only."""
    token, org_id = _account(client, "claim@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Claimable"},
        headers=_auth(token),
    ).json()["project_ref"]

    with db.connection() as conn:
        claim = provisioner.claim_one(conn)
        conn.commit()

    assert claim is not None, "the worker did not see a project the API had recorded"
    assert claim.project_ref == ref


def test_a_second_worker_skips_a_claimed_project(client, node):
    """FOR UPDATE SKIP LOCKED, asserted rather than assumed.

    Two workers taking the same project would both run `jobs.provision` against
    one tenant, and the second would be creating roles the first was still
    creating.
    """
    token, org_id = _account(client, "two-workers@example.com")
    client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Contended"},
        headers=_auth(token),
    )

    # Two connections of their own, because two workers are two processes.
    # Sharing the test's pool is not a model of that -- and the first version of
    # this test did exactly that and passed itself: `db.connection().__enter__()`
    # without holding the context manager lets it be garbage-collected, which
    # returns the connection to the pool and releases the very lock under test.
    # Both "workers" were then the same connection, which of course sees its own
    # lock as its own.
    first_conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
    second_conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
    try:
        first = provisioner.claim_one(first_conn)
        assert first is not None, "the first worker found nothing to claim"
        # The second worker looks while the first still holds its transaction.
        second = provisioner.claim_one(second_conn)
        assert second is None, "two workers claimed the same project"
    finally:
        first_conn.rollback()
        second_conn.rollback()
        first_conn.close()
        second_conn.close()


def test_a_project_that_has_exhausted_its_attempts_is_not_claimed(client, node):
    """The cap belongs to the project; a worker that ignored it would spin on it."""
    token, org_id = _account(client, "exhausted@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Doomed"},
        headers=_auth(token),
    ).json()["project_ref"]

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET provisioning_failures = %s, status = 'RETRY_WAIT' "
            "WHERE project_ref = %s",
            (10, ref),
        )
        conn.commit()
        assert provisioner.claim_one(conn) is None
        conn.commit()


def test_a_project_waiting_for_its_retry_time_is_not_claimed_yet(client, node):
    token, org_id = _account(client, "waiting@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Later"},
        headers=_auth(token),
    ).json()["project_ref"]

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET status = 'RETRY_WAIT', retry_after = now() + interval '1 hour' "
            "WHERE project_ref = %s",
            (ref,),
        )
        conn.commit()
        assert provisioner.claim_one(conn) is None
        conn.commit()


def test_a_requested_project_is_provisioned_end_to_end(
    client, key_ring, node_with_admin_credential  # noqa: ARG001 - fixture prepares the node
):
    """The whole slice, on a real node: asked for over HTTP, built by the worker.

    Nothing here mocks the node. A test that did would assert that the platform
    can provision against a mock, which is the one thing nobody needs to know.
    """
    import os

    if not os.environ.get("MALUDB_PLATFORM_OWNER"):
        pytest.skip("MALUDB_PLATFORM_OWNER is unset")

    token, org_id = _account(client, "endtoend@example.com")
    created = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Real"},
        headers=_auth(token),
    )
    assert created.status_code == 202, created.text
    ref = created.json()["project_ref"]

    # The customer's view while it is being built.
    assert client.get(f"/v1/projects/{ref}", headers=_auth(token)).json()["status"] == (
        "PLACEMENT_RESERVED"
    )

    assert provisioner.run_once(
        key_ring=key_ring, platform_owner=os.environ["MALUDB_PLATFORM_OWNER"]
    ), "the worker found nothing to do"

    seen = client.get(f"/v1/projects/{ref}", headers=_auth(token)).json()
    # PROVISIONED, not ACTIVE. Provisioning's terminal state is a tenant that
    # exists on a node; ACTIVE is what Phase 03 records once the project's
    # workers are serving, which is why the gateway treats both as serving
    # statuses. Asserting ACTIVE here would be asserting that this slice does
    # something a later one does.
    assert seen["status"] == "PROVISIONED", f"the project did not finish provisioning: {seen}"

    # And the database it names actually exists on the node.
    database = models.database_name_for(ref)
    try:
        with psycopg.connect(NODE_ADMIN_DSN, autocommit=True) as admin:
            found = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
            ).fetchone()
        assert found, "the worker reported success without creating the database"
    finally:
        # This test provisions a real tenant, so it cleans one up. Left behind,
        # the next run finds a database whose roles it is about to recreate.
        with psycopg.connect(NODE_ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            for role in ("authenticator", "auth", "admin", "replicator"):
                admin.execute(f'DROP ROLE IF EXISTS "{database}_{role}"')
