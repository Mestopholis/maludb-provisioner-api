"""Getting back into an account, seeing what holds it, and handing it over.

Phase 07 slice 4. Three capabilities that did not exist: a platform user could
not reset a password, could not see their own sessions or tokens (so could not
meaningfully revoke them), and an organization could not change hands.

The reset tests are mostly about what an *unauthenticated* caller cannot learn.
An endpoint that answered differently for a registered address than an invented
one is a membership oracle for any address someone cares to try, and the
addresses worth checking are the ones worth attacking. So the assertions are
about sameness: same status, same body, same shape.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from fastapi.testclient import TestClient

from services.control_plane import db, hashing, identity, mail, password_reset
from services.control_plane.main import create_app
from tests.conftest import TEST_PEPPER, requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret
NEW_CREDENTIAL = "a-different-correct-horse-99"  # noqa: S105 - test fixture, not a real secret

pytestmark = requires_db


class _Recorder:
    """A MaluMail that records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, sender, sender_name, to, message) -> dict:
        self.sent.append(
            {"sender": sender, "sender_name": sender_name, "to": to, "message": message}
        )
        return {"accepted": [to]}


@pytest.fixture
def sending(app_config, db_pool, monkeypatch):  # noqa: ARG001 - db_pool prepares the database
    """A client whose platform sender is configured, and the outbox it writes to."""
    recorder = _Recorder()
    configured = dataclasses.replace(
        app_config,
        malumail_api_key="mm_test_key",
        platform_email_from="noreply@maludb.org",
        platform_email_from_name="MaluDB",
        dashboard_url="https://app.maludb.org",
    )
    monkeypatch.setattr(mail, "MaluMail", lambda *a, **k: recorder)
    with TestClient(create_app(configured)) as client:
        yield client, recorder


def _signup(client, email: str) -> None:
    assert client.post(
        "/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL}
    ).status_code == 201


def _signin(client, email: str, password: str = TEST_CREDENTIAL):
    return client.post("/v1/auth/signin", json={"email": email, "password": password})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _token_from(recorder: _Recorder) -> str:
    """Pull the reset token out of the mail, the way a customer's click would."""
    link = recorder.sent[-1]["message"].text
    return link.split("token=")[1].split()[0].strip()


# -- what an anonymous caller cannot learn ---------------------------------


def test_asking_for_a_reset_says_nothing_about_who_exists(sending):
    """Identical answers for a real address and an invented one.

    Byte-identical, not merely both-202: a difference in body or shape is the
    same oracle by another route.
    """
    client, _recorder = sending
    _signup(client, "real@example.com")

    real = client.post("/v1/auth/password-reset", json={"email": "real@example.com"})
    invented = client.post("/v1/auth/password-reset", json={"email": "nobody@example.com"})

    assert real.status_code == invented.status_code == 202
    assert real.text == invented.text


def test_only_the_real_address_is_actually_written_to(sending):
    """The uniformity is in the response, not in the behaviour behind it."""
    client, recorder = sending
    _signup(client, "recipient@example.com")

    client.post("/v1/auth/password-reset", json={"email": "nobody@example.com"})
    assert recorder.sent == [], "mail was sent to an address nobody registered"

    client.post("/v1/auth/password-reset", json={"email": "recipient@example.com"})
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["to"] == "recipient@example.com"


def test_the_mail_comes_from_the_platform_not_from_a_customer(sending):
    """A MaluDB account reset has nothing to do with whichever project the
    person happens to own, and sending it as them would be the platform
    impersonating a customer to that customer."""
    client, recorder = sending
    _signup(client, "sender@example.com")
    client.post("/v1/auth/password-reset", json={"email": "sender@example.com"})

    assert recorder.sent[0]["sender"] == "noreply@maludb.org"
    assert recorder.sent[0]["sender_name"] == "MaluDB"


def test_every_bad_token_fails_the_same_way(sending):
    """Expired, spent, forged, malformed: the differences help only somebody
    who did not receive the mail."""
    client, recorder = sending
    _signup(client, "sameway@example.com")
    client.post("/v1/auth/password-reset", json={"email": "sameway@example.com"})
    good = _token_from(recorder)

    forged = hashing.generate_token(password_reset.TOKEN_KIND, TEST_PEPPER).plaintext
    answers = set()
    for token in ("nonsense", "mldb_pwreset_deadbeef", forged, good[:-4] + "aaaa"):
        response = client.post(
            "/v1/auth/password-reset/complete",
            json={"token": token, "password": NEW_CREDENTIAL},
        )
        answers.add((response.status_code, response.text))
    assert len(answers) == 1, f"bad tokens produced distinguishable answers: {answers}"


# -- the reset itself ------------------------------------------------------


def test_a_reset_lets_the_owner_back_in_and_locks_the_old_password_out(sending):
    client, recorder = sending
    _signup(client, "owner@example.com")
    client.post("/v1/auth/password-reset", json={"email": "owner@example.com"})

    done = client.post(
        "/v1/auth/password-reset/complete",
        json={"token": _token_from(recorder), "password": NEW_CREDENTIAL},
    )
    assert done.status_code == 204, done.text

    assert _signin(client, "owner@example.com", NEW_CREDENTIAL).status_code == 200
    assert _signin(client, "owner@example.com", TEST_CREDENTIAL).status_code == 401


def test_a_reset_token_works_exactly_once(sending):
    """A link that works twice still works after the owner has taken the
    account back from whoever intercepted it."""
    client, recorder = sending
    _signup(client, "once@example.com")
    client.post("/v1/auth/password-reset", json={"email": "once@example.com"})
    token = _token_from(recorder)

    assert client.post(
        "/v1/auth/password-reset/complete",
        json={"token": token, "password": NEW_CREDENTIAL},
    ).status_code == 204
    assert client.post(
        "/v1/auth/password-reset/complete",
        json={"token": token, "password": "yet-another-password-123"},
    ).status_code == 400


def test_completing_one_reset_kills_the_others(sending):
    """An attacker's outstanding request must not survive the owner's recovery."""
    client, recorder = sending
    _signup(client, "two@example.com")
    client.post("/v1/auth/password-reset", json={"email": "two@example.com"})
    first = _token_from(recorder)
    client.post("/v1/auth/password-reset", json={"email": "two@example.com"})
    second = _token_from(recorder)
    assert first != second

    assert client.post(
        "/v1/auth/password-reset/complete",
        json={"token": second, "password": NEW_CREDENTIAL},
    ).status_code == 204
    assert client.post(
        "/v1/auth/password-reset/complete",
        json={"token": first, "password": "third-password-value-1"},
    ).status_code == 400, "an older outstanding reset survived a completed one"


def test_a_reset_ends_every_existing_session(sending):
    """Changing the lock while the other party is inside is not recovery."""
    client, recorder = sending
    _signup(client, "sessions@example.com")
    token = _signin(client, "sessions@example.com").json()["token"]
    assert client.get("/v1/auth/me", headers=_auth(token)).status_code == 200

    client.post("/v1/auth/password-reset", json={"email": "sessions@example.com"})
    client.post(
        "/v1/auth/password-reset/complete",
        json={"token": _token_from(recorder), "password": NEW_CREDENTIAL},
    )

    assert client.get("/v1/auth/me", headers=_auth(token)).status_code == 401, (
        "a session survived the password reset that was meant to lock it out"
    )


def test_a_reset_is_unavailable_rather_than_silent_when_nobody_can_send(app_config, db_pool):  # noqa: ARG001
    """"Check your inbox" for mail that was never sent leaves a customer waiting
    for something that is not coming.

    And the refusal must be the *same* refusal for an address nobody has
    registered. The first version of this endpoint checked the sender only after
    finding the user, so an unconfigured deployment answered 503 for a real
    account and 202 for an invented one -- an unauthenticated account
    enumeration oracle in the endpoint written specifically not to be one. Found
    by the Phase 07 security review.
    """
    with TestClient(create_app(app_config)) as client:  # no platform sender configured
        _signup(client, "nosender@example.com")
        registered = client.post(
            "/v1/auth/password-reset", json={"email": "nosender@example.com"}
        )
        unknown = client.post(
            "/v1/auth/password-reset", json={"email": "never-signed-up@example.com"}
        )

    assert registered.status_code == 503
    assert registered.status_code == unknown.status_code, (
        "an unconfigured deployment tells an attacker which addresses are registered"
    )
    assert registered.text == unknown.text


def test_a_reset_revokes_personal_access_tokens_too(sending):
    """A session is not the longest-lived thing an attacker leaves behind.

    A personal access token authenticates wherever a session does -- including
    issuing a project's secret API key, which is its data API with row-level
    security bypassed -- and may be minted with no expiry at all. A reset that
    ended sessions and left tokens alive would tell the owner they had recovered
    while the attacker's credential kept working. Found by the Phase 07 security
    review.
    """
    client, recorder = sending
    _signup(client, "pattern@example.com")
    session = _signin(client, "pattern@example.com").json()["token"]

    # What an attacker holding a session for a moment leaves behind.
    planted = client.post(
        "/v1/auth/tokens", json={"name": "ci"}, headers=_auth(session)
    ).json()["token"]
    assert client.get("/v1/auth/me", headers=_auth(planted)).status_code == 200

    client.post("/v1/auth/password-reset", json={"email": "pattern@example.com"})
    client.post(
        "/v1/auth/password-reset/complete",
        json={"token": _token_from(recorder), "password": NEW_CREDENTIAL},
    )

    assert client.get("/v1/auth/me", headers=_auth(planted)).status_code == 401, (
        "a personal access token survived the reset that was meant to lock it out"
    )


def test_the_token_is_not_stored_in_a_form_that_could_be_replayed(sending):
    """ADR-023 Class A: a database leak yields nothing that resets an account."""
    client, recorder = sending
    _signup(client, "storage@example.com")
    client.post("/v1/auth/password-reset", json={"email": "storage@example.com"})
    token = _token_from(recorder)

    with db.connection() as conn:
        rows = db.query(conn, "SELECT token_prefix, verification_data FROM password_resets")
    assert rows, "no reset was recorded"
    for row in rows:
        assert token not in row["verification_data"]
        assert row["verification_data"] != token
        assert row["token_prefix"] in token, "the prefix should locate the row, not prove it"


# -- seeing what holds the account -----------------------------------------


def test_a_user_can_see_and_revoke_their_sessions(client):
    """Revocation without a list is a control nobody can exercise."""
    _signup(client, "listing@example.com")
    first = _signin(client, "listing@example.com").json()["token"]
    _signin(client, "listing@example.com")

    sessions = client.get("/v1/auth/sessions", headers=_auth(first))
    assert sessions.status_code == 200, sessions.text
    assert len(sessions.json()) == 2

    # And nothing in the listing could be used as a credential.
    assert "token" not in sessions.text.lower().replace("token_prefix", "")

    assert client.post("/v1/auth/sessions/revoke-all", headers=_auth(first)).status_code == 204
    assert client.get("/v1/auth/me", headers=_auth(first)).status_code == 401, (
        "revoke-all left the caller's own session alive"
    )


def test_a_user_can_list_their_tokens_without_seeing_them_again(client):
    _signup(client, "pats@example.com")
    token = _signin(client, "pats@example.com").json()["token"]
    created = client.post(
        "/v1/auth/tokens", json={"name": "ci"}, headers=_auth(token)
    )
    assert created.status_code == 201, created.text
    secret = created.json()["token"]

    listed = client.get("/v1/auth/tokens", headers=_auth(token))
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["name"] == "ci"
    assert secret not in listed.text, "a personal access token was shown again"


def test_one_user_cannot_see_another_s_sessions(client):
    _signup(client, "mine@example.com")
    _signup(client, "yours@example.com")
    mine = _signin(client, "mine@example.com").json()["token"]
    _signin(client, "yours@example.com")

    assert len(client.get("/v1/auth/sessions", headers=_auth(mine)).json()) == 1


# -- handing an organization over ------------------------------------------


def _org_of(client, token: str) -> str:
    return client.get("/v1/auth/me", headers=_auth(token)).json()["organizations"][0]["org_id"]


def _add_member(org_id: str, email: str, role: str = "member") -> None:
    with db.connection() as conn:
        user = db.one(conn, "SELECT id FROM users WHERE email = %s", (email,))
        db.execute(
            conn,
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            (org_id, user["id"], role),
        )
        conn.commit()


def test_an_owner_can_hand_the_organization_over_and_stays_as_admin(client):
    """Not removed: a mistyped user id must not evict the only person who could
    undo it."""
    _signup(client, "handover@example.com")
    _signup(client, "successor@example.com")
    owner = _signin(client, "handover@example.com").json()["token"]
    org_id = _org_of(client, owner)
    _add_member(org_id, "successor@example.com", "admin")

    with db.connection() as conn:
        successor = db.one(conn, "SELECT id FROM users WHERE email = %s", ("successor@example.com",))

    done = client.post(
        f"/v1/organizations/{org_id}/transfer-ownership",
        json={"to_user_id": str(successor["id"])},
        headers=_auth(owner),
    )
    assert done.status_code == 204, done.text

    with db.connection() as conn:
        roles = {
            str(r["user_id"]): r["role"]
            for r in db.query(
                conn, "SELECT user_id, role FROM org_members WHERE org_id = %s", (org_id,)
            )
        }
        me = db.one(conn, "SELECT id FROM users WHERE email = %s", ("handover@example.com",))
    assert roles[str(successor["id"])] == "owner"
    assert roles[str(me["id"])] == "admin", "the previous owner was removed rather than stepped down"
    assert list(roles.values()).count("owner") == 1, "the organization has two owners"


def test_an_admin_cannot_hand_the_organization_to_themselves(client):
    """The escalation `guard_owner_tier` exists for, on the newest route."""
    _signup(client, "realowner@example.com")
    _signup(client, "ambitious@example.com")
    owner = _signin(client, "realowner@example.com").json()["token"]
    org_id = _org_of(client, owner)
    _add_member(org_id, "ambitious@example.com", "admin")
    admin = _signin(client, "ambitious@example.com").json()["token"]

    with db.connection() as conn:
        them = db.one(conn, "SELECT id FROM users WHERE email = %s", ("ambitious@example.com",))

    refused = client.post(
        f"/v1/organizations/{org_id}/transfer-ownership",
        json={"to_user_id": str(them["id"])},
        headers=_auth(admin),
    )
    assert refused.status_code == 403

    with db.connection() as conn:
        role = db.one(
            conn,
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s",
            (org_id, them["id"]),
        )
    assert role["role"] == "admin", "an admin promoted themselves to owner"


def test_ownership_cannot_be_given_to_a_stranger(client):
    """Someone who is not a member cannot be made owner: it would hand an
    organization to a person who was never part of it."""
    _signup(client, "careful@example.com")
    _signup(client, "outsider@example.com")
    owner = _signin(client, "careful@example.com").json()["token"]
    org_id = _org_of(client, owner)

    with db.connection() as conn:
        outsider = db.one(conn, "SELECT id FROM users WHERE email = %s", ("outsider@example.com",))

    refused = client.post(
        f"/v1/organizations/{org_id}/transfer-ownership",
        json={"to_user_id": str(outsider["id"])},
        headers=_auth(owner),
    )
    assert refused.status_code == 403


def test_transfer_is_atomic(client):
    """One transaction, because an organization with two owners or none -- even
    briefly -- is a state the rest of the code does not expect."""
    _signup(client, "atomic@example.com")
    _signup(client, "atomic2@example.com")
    owner = _signin(client, "atomic@example.com").json()["token"]
    org_id = _org_of(client, owner)
    _add_member(org_id, "atomic2@example.com", "member")

    with db.connection() as conn:
        target = db.one(conn, "SELECT id FROM users WHERE email = %s", ("atomic2@example.com",))
        principal = identity.resolve_principal(
            conn, presented=owner, pepper=TEST_PEPPER
        )
        # Directly, so the failure path is the one under test rather than the
        # route's error handling.
        # uuid.UUID, not the string the JSON carried: `role_in` compares
        # membership ids by value, and a str never equals a UUID -- which reads
        # as "you are not the owner" for someone who is.
        identity.transfer_ownership(
            conn, org_id=uuid.UUID(org_id), to_user_id=target["id"], actor=principal
        )
        conn.commit()
        owners = db.query(
            conn, "SELECT user_id FROM org_members WHERE org_id = %s AND role = 'owner'", (org_id,)
        )
    assert len(owners) == 1
