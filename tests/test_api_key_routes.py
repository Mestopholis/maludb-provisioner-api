"""A customer managing their project's keys.

Phase 07 slice 2, and the phase's second acceptance criterion: secret key
material follows a one-time/reveal/reset policy. The interesting half is what
*cannot* be done here, so most of these tests assert an absence -- a secret key
that never comes back, a project's keys another organization cannot see, a
response with nothing in it that could open a PostgreSQL connection.

The policy is not enforced by these routes. It is enforced by ADR-023's storage
classes: a secret key is a verifier, so there is no plaintext anywhere to
return, and no amount of adding routes would produce one. These tests exist to
prove that stayed true once a customer-facing surface was put on top.
"""

from __future__ import annotations

import uuid

from services.control_plane import api_keys, db, models
from tests.conftest import requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret

pytestmark = requires_db


def _account(client, email: str) -> tuple[str, str]:
    created = client.post(
        "/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL}
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL}
    ).json()["token"]
    return token, created.json()["organizations"][0]["org_id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _project(client, token: str, org_id: str, name: str = "Keys") -> str:
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('free','Free','{}') "
            "ON CONFLICT (code) DO NOTHING",
        )
        db.execute(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES ('key-node','k.example','k.internal','shared','active', now()) "
            "ON CONFLICT (name) DO UPDATE SET status = 'active', last_health_at = now()",
        )
        conn.commit()
    response = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": name},
        headers=_auth(token),
    )
    assert response.status_code == 202, response.text
    return response.json()["project_ref"]


# -- the policy ------------------------------------------------------------


def test_a_secret_key_is_shown_once_and_never_again(client):
    """The criterion, and the reason it holds without a route enforcing it.

    A secret key is stored as a verifier. There is no ciphertext, so listing
    cannot return the value however the route is written -- which is a stronger
    guarantee than a route that chooses not to.
    """
    token, org_id = _account(client, "secret@example.com")
    ref = _project(client, token, org_id)

    issued = client.post(
        f"/v1/projects/{ref}/api-keys",
        json={"key_type": "secret", "name": "server"},
        headers=_auth(token),
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    plaintext = body["key"]
    assert plaintext, "a secret key must be returned at creation or it is unusable"
    assert body["shown_once"] is True

    listed = client.get(f"/v1/projects/{ref}/api-keys", headers=_auth(token)).json()
    secret = next(k for k in listed if k["key_type"] == "secret")
    assert secret["key"] is None, "a secret key came back from a list call"
    assert plaintext not in str(listed), "the secret key's value appeared in the listing"

    # Not merely absent from the response: absent from storage.
    with db.connection() as conn:
        stored = db.one(
            conn, "SELECT ciphertext FROM api_keys WHERE id = %s", (uuid.UUID(body["id"]),)
        )
    assert stored["ciphertext"] is None, "a secret key was stored recoverably"


def test_a_publishable_key_can_be_read_back_indefinitely(client):
    """It ships in a client bundle; a dashboard must show it on every page load."""
    token, org_id = _account(client, "publishable@example.com")
    ref = _project(client, token, org_id)

    issued = client.post(
        f"/v1/projects/{ref}/api-keys",
        json={"key_type": "publishable"},
        headers=_auth(token),
    ).json()
    assert issued["shown_once"] is False

    for _ in range(3):
        listed = client.get(f"/v1/projects/{ref}/api-keys", headers=_auth(token)).json()
        publishable = next(k for k in listed if k["key_type"] == "publishable")
        assert publishable["key"] == issued["key"]


def test_a_revoked_publishable_key_is_not_handed_back(client):
    """Of no use to a client, and showing it invites pasting a dead key."""
    token, org_id = _account(client, "revoked@example.com")
    ref = _project(client, token, org_id)
    issued = client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "publishable"}, headers=_auth(token)
    ).json()

    gone = client.delete(f"/v1/projects/{ref}/api-keys/{issued['id']}", headers=_auth(token))
    assert gone.status_code == 204

    listed = client.get(f"/v1/projects/{ref}/api-keys", headers=_auth(token)).json()
    revoked = next(k for k in listed if k["id"] == issued["id"])
    assert revoked["revoked_at"] is not None
    assert revoked["key"] is None


def test_reset_is_create_then_revoke_and_the_old_key_stops_working(client):
    """Two calls rather than a rotate endpoint, and the order is the point.

    Revoking at the moment of minting would break every running client between
    two deployments. A customer who wants that can do it in this order anyway;
    a customer who does not cannot undo it.
    """
    token, org_id = _account(client, "reset@example.com")
    ref = _project(client, token, org_id)
    old = client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "secret"}, headers=_auth(token)
    ).json()
    new = client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "secret"}, headers=_auth(token)
    ).json()
    client.delete(f"/v1/projects/{ref}/api-keys/{old['id']}", headers=_auth(token))

    with db.connection() as conn:
        project = models.get_project_by_ref(conn, ref)
        from tests.conftest import TEST_PEPPER

        # As if the provisioner had finished. A key does not authenticate for a
        # project that is not yet serving -- see the test below, which is about
        # that rather than about revocation.
        db.execute(
            conn, "UPDATE projects SET status = 'PROVISIONED' WHERE id = %s", (project.id,)
        )
        conn.commit()

        # The gateway's own question: is this key good for this project?
        assert api_keys.authenticate(
            conn, presented=new["key"], project_id=project.id, pepper=TEST_PEPPER
        ) is not None
        assert api_keys.authenticate(
            conn, presented=old["key"], project_id=project.id, pepper=TEST_PEPPER
        ) is None, "a revoked key still authenticated"


def test_a_key_may_be_created_before_the_project_finishes_provisioning(client):
    """And it does not work until the project does.

    A dashboard shows a project's keys as soon as the project appears, so
    creation cannot wait for a node. What must wait is the key *working*:
    `api_keys.authenticate` refuses any key whose project is not yet serving,
    so a key handed out early is inert rather than a way into a half-built
    tenant. Asserted here because the two halves live in different modules and
    could drift apart without anything noticing.
    """
    token, org_id = _account(client, "early@example.com")
    ref = _project(client, token, org_id)

    issued = client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "secret"}, headers=_auth(token)
    )
    assert issued.status_code == 201, "a key could not be created while provisioning"

    from tests.conftest import TEST_PEPPER

    with db.connection() as conn:
        project = models.get_project_by_ref(conn, ref)
        assert project.status == "PLACEMENT_RESERVED"
        assert api_keys.authenticate(
            conn, presented=issued.json()["key"], project_id=project.id, pepper=TEST_PEPPER
        ) is None, "a key worked against a project that is not serving yet"

        db.execute(
            conn, "UPDATE projects SET status = 'PROVISIONED' WHERE id = %s", (project.id,)
        )
        conn.commit()
        assert api_keys.authenticate(
            conn, presented=issued.json()["key"], project_id=project.id, pepper=TEST_PEPPER
        ) is not None, "the key did not start working when the project did"


# -- who may do it ---------------------------------------------------------


def test_another_organization_cannot_see_a_project_s_keys(client):
    """404, so the route cannot be used to discover which refs exist."""
    owner, org_id = _account(client, "keyowner@example.com")
    ref = _project(client, owner, org_id)
    client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "publishable"}, headers=_auth(owner)
    )

    stranger, _ = _account(client, "keystranger@example.com")

    # Byte-identical to the answer for a ref that does not exist. Asserting only
    # the status code -- which this test did originally -- passes while the body
    # says "organization not found" for a real project and "project not found"
    # for an invented one, which is an oracle for which refs are live. A ref is
    # the customer's API subdomain (ADR-008), so confirming one confirms a
    # target. Found by the Phase 07 security review.
    real = client.get(f"/v1/projects/{ref}/api-keys", headers=_auth(stranger))
    invented = client.get("/v1/projects/zzzz9999/api-keys", headers=_auth(stranger))
    assert real.status_code == invented.status_code == 404
    assert real.text == invented.text, "a stranger can tell a real project from an invented one"

    created_real = client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "secret"}, headers=_auth(stranger)
    )
    created_invented = client.post(
        "/v1/projects/zzzz9999/api-keys", json={"key_type": "secret"}, headers=_auth(stranger)
    )
    assert created_real.status_code == created_invented.status_code == 404
    assert created_real.text == created_invented.text


def test_an_unauthenticated_caller_gets_nothing(client):
    owner, org_id = _account(client, "anon@example.com")
    ref = _project(client, owner, org_id)
    assert client.get(f"/v1/projects/{ref}/api-keys").status_code == 401


def test_revoking_a_key_that_is_not_this_project_s_is_a_404(client):
    """Scoped by project in the model, and asserted here because the route is
    where a forgotten scope would show up as a customer revoking someone else's
    key by guessing an id."""
    first, first_org = _account(client, "revoke-a@example.com")
    second, second_org = _account(client, "revoke-b@example.com")
    first_ref = _project(client, first, first_org, "A")
    second_ref = _project(client, second, second_org, "B")

    victim = client.post(
        f"/v1/projects/{second_ref}/api-keys",
        json={"key_type": "publishable"},
        headers=_auth(second),
    ).json()

    refused = client.delete(
        f"/v1/projects/{first_ref}/api-keys/{victim['id']}", headers=_auth(first)
    )
    assert refused.status_code == 404

    still_live = client.get(
        f"/v1/projects/{second_ref}/api-keys", headers=_auth(second)
    ).json()[0]
    assert still_live["revoked_at"] is None, "one project revoked another's key"


# -- what a response may contain -------------------------------------------


def test_no_key_response_carries_anything_that_could_reach_postgresql(client):
    """The phase's first acceptance criterion, on the surface closest to it.

    A key authenticates to the gateway, which holds the tenant's real
    credentials. The database name, the node's hostname and the tenant roles
    appear in no response here -- a free project that could learn them would
    have most of what it needs to try connecting directly.
    """
    token, org_id = _account(client, "leak@example.com")
    ref = _project(client, token, org_id)
    created = client.post(
        f"/v1/projects/{ref}/api-keys", json={"key_type": "secret"}, headers=_auth(token)
    )
    listed = client.get(f"/v1/projects/{ref}/api-keys", headers=_auth(token))

    for response in (created, listed):
        text = response.text.lower()
        for forbidden in (
            "postgres://", "postgresql://", "dsn", "password",
            f"mldb_{ref}", "k.internal", "5432",
        ):
            assert forbidden not in text, f"{forbidden} reached a customer"
