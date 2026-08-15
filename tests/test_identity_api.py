"""The blocking identity tests from docs/TESTING.md.

Each of these corresponds to a line in that document's required negative tests.
They run end to end through the API, because that is where the guarantee has to
hold -- a repository function that enforces a rule the route forgets to call is
not a guarantee.
"""

from __future__ import annotations

import uuid

from tests.conftest import requires_db

pytestmark = requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret


def signup(client, email: str) -> dict:
    response = client.post("/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL})
    assert response.status_code == 201, response.text
    return response.json()


def signin(client, email: str) -> str:
    response = client.post("/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def role_in(client, token: str, org_id: str) -> str | None:
    """Every user also owns a personal organization, so select by id."""
    orgs = client.get("/v1/organizations", headers=auth(token)).json()
    return next((o["role"] for o in orgs if o["org_id"] == org_id), None)


# -- authentication is required -------------------------------------------


def test_every_data_route_requires_authentication(client):
    """Deny by default. A missing credential is 401, never data."""
    for method, path in (
        ("get", "/v1/plans"),
        ("get", "/v1/auth/me"),
        ("get", "/v1/organizations"),
        ("get", f"/v1/organizations/{uuid.uuid4()}/projects"),
        ("get", "/v1/projects/ab12cd34"),
        ("get", f"/v1/organizations/{uuid.uuid4()}/members"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path} returned {response.status_code}"


def test_garbage_and_malformed_credentials_are_rejected(client):
    for bad in ("", "not-a-token", "mldb_pat_", "mldb_xxx_abcdefgh" + "y" * 40, "Bearer nested"):
        assert client.get("/v1/auth/me", headers=auth(bad)).status_code == 401


# -- signup and personal organization --------------------------------------


def test_signup_creates_a_personal_organization_with_the_user_as_owner(client):
    """ADR-020: ownership is an organization from the first row written."""
    body = signup(client, "solo@example.com")
    assert len(body["organizations"]) == 1
    assert body["organizations"][0]["role"] == "owner"


def test_duplicate_signup_is_refused(client):
    signup(client, "dupe@example.com")
    response = client.post("/v1/auth/signup", json={"email": "dupe@example.com", "password": TEST_CREDENTIAL})
    assert response.status_code == 409


def test_signin_with_a_wrong_password_is_refused(client):
    signup(client, "user@example.com")
    response = client.post("/v1/auth/signin", json={"email": "user@example.com", "password": "wrong-password-here"})
    assert response.status_code == 401


def test_unknown_and_wrong_password_are_indistinguishable(client):
    signup(client, "known@example.com")
    wrong = client.post("/v1/auth/signin", json={"email": "known@example.com", "password": "wrong-password-here"})
    unknown = client.post("/v1/auth/signin", json={"email": "nobody@example.com", "password": "wrong-password-here"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


# -- cross-organization isolation ------------------------------------------


def test_a_user_cannot_read_an_organization_they_do_not_belong_to(client):
    signup(client, "alice@example.com")
    bob = signup(client, "bob@example.com")
    alice_token = signin(client, "alice@example.com")

    bob_org = bob["organizations"][0]["org_id"]
    assert client.get(f"/v1/organizations/{bob_org}/members", headers=auth(alice_token)).status_code == 404
    assert client.get(f"/v1/organizations/{bob_org}/projects", headers=auth(alice_token)).status_code == 404


def test_organization_listing_shows_only_own_memberships(client):
    signup(client, "alice2@example.com")
    signup(client, "bob2@example.com")
    token = signin(client, "alice2@example.com")

    orgs = client.get("/v1/organizations", headers=auth(token)).json()
    assert len(orgs) == 1
    assert orgs[0]["role"] == "owner"


def test_non_membership_is_indistinguishable_from_absence(client):
    """404, not 403: a 403 would confirm the organization exists."""
    signup(client, "alice3@example.com")
    bob = signup(client, "bob3@example.com")
    token = signin(client, "alice3@example.com")

    real_org = bob["organizations"][0]["org_id"]
    imaginary_org = uuid.uuid4()
    real = client.get(f"/v1/organizations/{real_org}/members", headers=auth(token))
    fake = client.get(f"/v1/organizations/{imaginary_org}/members", headers=auth(token))
    assert real.status_code == fake.status_code == 404
    assert real.json() == fake.json()


# -- revocation ------------------------------------------------------------


def test_signout_revokes_the_session_immediately(client):
    signup(client, "revoke@example.com")
    token = signin(client, "revoke@example.com")
    assert client.get("/v1/auth/me", headers=auth(token)).status_code == 200
    assert client.post("/v1/auth/signout", headers=auth(token)).status_code == 204
    assert client.get("/v1/auth/me", headers=auth(token)).status_code == 401


def test_revoking_a_personal_access_token_takes_effect_immediately(client):
    signup(client, "pat@example.com")
    session = signin(client, "pat@example.com")

    created = client.post("/v1/auth/tokens", json={"name": "ci"}, headers=auth(session))
    assert created.status_code == 201
    pat = created.json()["token"]
    assert client.get("/v1/auth/me", headers=auth(pat)).status_code == 200

    from services.control_plane import db, identity

    with db.connection() as conn:
        row = db.one(conn, "SELECT id FROM personal_access_tokens LIMIT 1")
        me = client.get("/v1/auth/me", headers=auth(session)).json()
        identity.revoke_pat(conn, token_id=row["id"], user_id=me["id"])
        conn.commit()

    assert client.get("/v1/auth/me", headers=auth(pat)).status_code == 401


def test_a_personal_access_token_cannot_mint_further_tokens(client):
    """A leaked token must not be self-renewing."""
    signup(client, "mint@example.com")
    session = signin(client, "mint@example.com")
    pat = client.post("/v1/auth/tokens", json={"name": "ci"}, headers=auth(session)).json()["token"]
    assert client.post("/v1/auth/tokens", json={"name": "second"}, headers=auth(pat)).status_code == 403


def test_a_user_cannot_revoke_another_users_token(client):
    signup(client, "owner-a@example.com")
    signup(client, "owner-b@example.com")
    a_session = signin(client, "owner-a@example.com")
    b_session = signin(client, "owner-b@example.com")
    client.post("/v1/auth/tokens", json={"name": "a-token"}, headers=auth(a_session))

    from services.control_plane import db

    with db.connection() as conn:
        token_id = db.one(conn, "SELECT id FROM personal_access_tokens LIMIT 1")["id"]

    assert client.delete(f"/v1/auth/tokens/{token_id}", headers=auth(b_session)).status_code == 404
    assert client.delete(f"/v1/auth/tokens/{token_id}", headers=auth(a_session)).status_code == 204


# -- the last-owner rule ---------------------------------------------------


def test_a_sole_owner_cannot_demote_themselves(client):
    """Since the escalation fix, self-role-change is refused before the
    last-owner rule is reached. The organization still cannot lose its owner;
    the guard simply fires earlier and for a stricter reason."""
    body = signup(client, "lastowner@example.com")
    token = signin(client, "lastowner@example.com")
    org = body["organizations"][0]["org_id"]

    response = client.put(
        f"/v1/organizations/{org}/members/{body['id']}",
        json={"role": "viewer"},
        headers=auth(token),
    )
    assert response.status_code == 403
    assert role_in(client, token, org) == "owner"


def test_the_last_owner_cannot_be_removed(client):
    body = signup(client, "lastowner2@example.com")
    token = signin(client, "lastowner2@example.com")
    org = body["organizations"][0]["org_id"]

    response = client.delete(f"/v1/organizations/{org}/members/{body['id']}", headers=auth(token))
    assert response.status_code == 409


# -- invitations -----------------------------------------------------------


def test_invitation_flow_grants_the_invited_role(client):
    inviter = signup(client, "inviter@example.com")
    signup(client, "invitee@example.com")
    inviter_token = signin(client, "inviter@example.com")
    invitee_token = signin(client, "invitee@example.com")
    org = inviter["organizations"][0]["org_id"]

    created = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "invitee@example.com", "role": "developer"},
        headers=auth(inviter_token),
    )
    assert created.status_code == 201
    token = created.json()["token"]

    accepted = client.post(
        "/v1/organizations/invitations/accept",
        params={"token": token},
        headers=auth(invitee_token),
    )
    assert accepted.status_code == 200
    assert accepted.json()["role"] == "developer"


def test_an_invitation_is_single_use(client):
    inviter = signup(client, "inviter2@example.com")
    signup(client, "invitee2@example.com")
    inviter_token = signin(client, "inviter2@example.com")
    invitee_token = signin(client, "invitee2@example.com")
    org = inviter["organizations"][0]["org_id"]

    token = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "invitee2@example.com", "role": "viewer"},
        headers=auth(inviter_token),
    ).json()["token"]

    first = client.post("/v1/organizations/invitations/accept", params={"token": token}, headers=auth(invitee_token))
    second = client.post("/v1/organizations/invitations/accept", params={"token": token}, headers=auth(invitee_token))
    assert first.status_code == 200
    assert second.status_code == 400


def test_an_invitation_is_only_acceptable_by_the_invited_address(client):
    inviter = signup(client, "inviter3@example.com")
    signup(client, "intended@example.com")
    signup(client, "interloper@example.com")
    inviter_token = signin(client, "inviter3@example.com")
    interloper_token = signin(client, "interloper@example.com")
    org = inviter["organizations"][0]["org_id"]

    token = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "intended@example.com", "role": "admin"},
        headers=auth(inviter_token),
    ).json()["token"]

    response = client.post(
        "/v1/organizations/invitations/accept",
        params={"token": token},
        headers=auth(interloper_token),
    )
    assert response.status_code == 400
    assert "different address" in response.json()["detail"]


def test_a_non_manager_cannot_invite(client):
    inviter = signup(client, "inviter4@example.com")
    signup(client, "dev@example.com")
    inviter_token = signin(client, "inviter4@example.com")
    dev_token = signin(client, "dev@example.com")
    org = inviter["organizations"][0]["org_id"]

    token = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "dev@example.com", "role": "developer"},
        headers=auth(inviter_token),
    ).json()["token"]
    client.post("/v1/organizations/invitations/accept", params={"token": token}, headers=auth(dev_token))

    response = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "another@example.com", "role": "viewer"},
        headers=auth(dev_token),
    )
    assert response.status_code == 403


# -- the owner tier is closed to admins (security review finding) ----------


def _org_with_admin(client) -> tuple[str, str, str, str, str]:
    """Owner org with a second member holding the admin role."""
    owner = signup(client, "esc-owner@example.com")
    admin = signup(client, "esc-admin@example.com")
    owner_token = signin(client, "esc-owner@example.com")
    admin_token = signin(client, "esc-admin@example.com")
    org = owner["organizations"][0]["org_id"]

    invite = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "esc-admin@example.com", "role": "admin"},
        headers=auth(owner_token),
    ).json()["token"]
    client.post("/v1/organizations/invitations/accept", params={"token": invite}, headers=auth(admin_token))
    return org, owner["id"], owner_token, admin["id"], admin_token


def test_admin_cannot_promote_itself_to_owner(client):
    """Confirmed exploitable before the fix: full organization takeover."""
    org, _, _, admin_id, admin_token = _org_with_admin(client)
    response = client.put(
        f"/v1/organizations/{org}/members/{admin_id}",
        json={"role": "owner"},
        headers=auth(admin_token),
    )
    assert response.status_code == 403
    assert role_in(client, admin_token, org) == "admin"


def test_nobody_can_change_their_own_role(client):
    org, owner_id, owner_token, _, _ = _org_with_admin(client)
    response = client.put(
        f"/v1/organizations/{org}/members/{owner_id}",
        json={"role": "viewer"},
        headers=auth(owner_token),
    )
    assert response.status_code == 403
    assert "your own role" in response.json()["detail"]


def test_admin_cannot_promote_another_member_to_owner(client):
    org, owner_id, _, _, admin_token = _org_with_admin(client)
    response = client.put(
        f"/v1/organizations/{org}/members/{owner_id}",
        json={"role": "owner"},
        headers=auth(admin_token),
    )
    assert response.status_code == 403


def test_admin_cannot_invite_a_new_owner(client):
    """Inviting at owner level is the same grant, and must be gated identically."""
    org, _, _, _, admin_token = _org_with_admin(client)
    response = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "attacker@example.com", "role": "owner"},
        headers=auth(admin_token),
    )
    assert response.status_code == 403


def test_admin_cannot_remove_an_owner(client):
    org, owner_id, owner_token, _, admin_token = _org_with_admin(client)
    assert client.delete(f"/v1/organizations/{org}/members/{owner_id}", headers=auth(admin_token)).status_code == 403
    # the owner still has access
    assert client.get(f"/v1/organizations/{org}/members", headers=auth(owner_token)).status_code == 200


def test_admin_may_still_manage_non_owner_members(client):
    """The fix must not break the documented admin capability."""
    org, _, owner_token, _, admin_token = _org_with_admin(client)
    signup(client, "esc-dev@example.com")
    dev_token = signin(client, "esc-dev@example.com")
    invite = client.post(
        f"/v1/organizations/{org}/invitations",
        json={"email": "esc-dev@example.com", "role": "developer"},
        headers=auth(owner_token),
    ).json()["token"]
    client.post("/v1/organizations/invitations/accept", params={"token": invite}, headers=auth(dev_token))
    dev_id = client.get("/v1/auth/me", headers=auth(dev_token)).json()["id"]

    assert client.put(
        f"/v1/organizations/{org}/members/{dev_id}", json={"role": "viewer"}, headers=auth(admin_token)
    ).status_code == 204
    assert client.delete(f"/v1/organizations/{org}/members/{dev_id}", headers=auth(admin_token)).status_code == 204


def test_an_owner_may_still_grant_ownership(client):
    org, _, owner_token, admin_id, admin_token = _org_with_admin(client)
    assert client.put(
        f"/v1/organizations/{org}/members/{admin_id}", json={"role": "owner"}, headers=auth(owner_token)
    ).status_code == 204
    assert role_in(client, admin_token, org) == "owner"


def test_a_member_may_still_leave_voluntarily(client):
    org, _, _, admin_id, admin_token = _org_with_admin(client)
    assert client.delete(f"/v1/organizations/{org}/members/{admin_id}", headers=auth(admin_token)).status_code == 204
    assert role_in(client, admin_token, org) is None
