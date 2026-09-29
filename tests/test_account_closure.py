"""Closing an account: the promise the terms make, and the one thing it must not become.

Free slice 11. `frontend/terms.html` says: *"To close an account, delete its projects and write to
support@maludb.org; we remove the account and its record of you."* Nothing could do that -- no
route, no command, no `DELETE FROM users` anywhere -- so the sentence was untrue the day it was
published. Found while filling the legal pages' placeholders (H-4), one slice after the same shape
of gap was found in project deletion.

What is held here:

- **it refuses while anything of value is attached**: a live project, a subscription that still
  entitles a plan, or an organization this account owns with other members in it. Closure is not a
  cascade that destroys databases as a side effect of an email, and the refusal says which;
- **the person goes, the record does not**: address, name, password hash, sessions, tokens and MFA
  factors are destroyed, and the row survives with an opaque id so `audit_events.actor_user_id`
  still answers "who deleted this project";
- **the address is freed**, so somebody may sign up again with it;
- **a closed account cannot sign in** by password, session or personal access token;
- **a personal organization's name is scrubbed**, because it is the person's own name;
- **the operator command makes you name the address twice**, as `project delete` does, and prints
  what blocks a closure rather than a bare refusal.
"""

from __future__ import annotations

import argparse
import uuid

import pytest

from services.control_plane import db, identity, manage, models
from tests.conftest import TEST_CREDENTIAL, requires_db

pytestmark = requires_db


def _free_plan_id(conn) -> int:
    """A plan row, so a project can exist. Closure cares that a project *is* there, not what it is on."""
    db.execute(conn, "INSERT INTO plans (code, name, config_json) VALUES ('free','Developer','{}') "
                     "ON CONFLICT (code) DO NOTHING")
    return db.one(conn, "SELECT id FROM plans WHERE code = 'free'")["id"]


def _signed_up(client, email: str) -> tuple[uuid.UUID, str, str]:
    created = client.post("/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL,
                                                   "display_name": "Ada Lovelace"})
    assert created.status_code == 201, created.text
    org_id = created.json()["organizations"][0]["org_id"]
    token = client.post("/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL}
                        ).json()["token"]
    with db.connection() as conn:
        user_id = db.one(conn, "SELECT id FROM users WHERE email = %s", (email,))["id"]
    return user_id, org_id, token


def test_the_person_goes_and_the_record_of_what_they_did_stays(client):
    user_id, org_id, token = _signed_up(client, "closing@example.com")
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO audit_events (org_id, actor_type, actor_user_id, event_type) "
            "VALUES (%s, 'user', %s, 'project.deleted')", (org_id, user_id),
        )
        conn.commit()
        closure = identity.close_account(conn, user_id=user_id, actor_id="ticket-9")
        conn.commit()

        row = db.one(conn, "SELECT email, display_name, password_hash, status, deleted_at, "
                           "       email_verified_at, last_login_at FROM users WHERE id = %s", (user_id,))
        attributed = db.one(
            conn,
            "SELECT count(*) AS n FROM audit_events WHERE actor_user_id = %s AND event_type = 'project.deleted'",
            (user_id,),
        )["n"]
        event = db.one(
            conn,
            "SELECT actor_type, actor_id, detail_json FROM audit_events "
            " WHERE actor_user_id = %s AND event_type = 'account.closed'", (user_id,),
        )

    assert row["email"] == f"closed+{user_id}@account.invalid", "the address is gone, the row is not"
    assert row["display_name"] is None and row["password_hash"] is None
    assert row["email_verified_at"] is None and row["last_login_at"] is None
    assert row["status"] == "deleted" and row["deleted_at"] is not None
    assert attributed == 1, "the audit trail still says who deleted the project"
    assert event["actor_type"] == "staff" and event["actor_id"] == "ticket-9", "and who closed the account"
    assert closure.sessions == 1 and closure.memberships == 1
    assert len(closure.organizations_closed) == 1
    assert token, "the session existed before closure and is counted above"


def test_a_closed_account_cannot_sign_in_by_any_route(client):
    user_id, _, token = _signed_up(client, "locked-out@example.com")
    pat = client.post("/v1/auth/tokens", json={"name": "ci"},
                      headers={"Authorization": f"Bearer {token}"})
    assert pat.status_code == 201, pat.text
    presented = pat.json()["token"]

    with db.connection() as conn:
        identity.close_account(conn, user_id=user_id)
        conn.commit()

    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {presented}"}).status_code == 401
    refused = client.post("/v1/auth/signin",
                          json={"email": "locked-out@example.com", "password": TEST_CREDENTIAL})
    assert refused.status_code == 401, "the password is gone, and so is the row's claim to be active"


def test_the_address_is_free_for_a_new_account(client):
    user_id, _, _ = _signed_up(client, "again@example.com")
    with db.connection() as conn:
        identity.close_account(conn, user_id=user_id)
        conn.commit()

    created = client.post("/v1/auth/signup", json={"email": "again@example.com",
                                                   "password": TEST_CREDENTIAL})
    assert created.status_code == 201, "a closed account must not lock its own address away"
    with db.connection() as conn:
        rows = db.query(conn, "SELECT status FROM users WHERE lower(email) = 'again@example.com'")
    assert [r["status"] for r in rows] == ["active"], "the new account is its own row"


def test_a_live_project_refuses_the_closure_and_says_so(client):
    user_id, org_id, _ = _signed_up(client, "still-running@example.com")
    with db.connection() as conn:
        models.create_project(conn, org_id=uuid.UUID(org_id), display_name="keeps running",
                              plan_id=_free_plan_id(conn))
        conn.commit()
        with pytest.raises(identity.ClosureBlocked, match="delete them first"):
            identity.close_account(conn, user_id=user_id)
        conn.rollback()
        row = db.one(conn, "SELECT status, email FROM users WHERE id = %s", (user_id,))
    assert row["status"] == "active" and row["email"] == "still-running@example.com", "nothing changed"


def test_an_entitling_subscription_refuses_the_closure(client):
    user_id, org_id, _ = _signed_up(client, "paying@example.com")
    with db.connection() as conn:
        project_id = models.create_project(conn, org_id=uuid.UUID(org_id), display_name="paid",
                                           plan_id=_free_plan_id(conn))
        db.execute(
            conn,
            "INSERT INTO subscriptions (id, org_id, project_id, plan_code, state, state_as_of) "
            "VALUES (%s, %s, %s, 'starter', 'active', now())",
            (uuid.uuid4(), org_id, project_id),
        )
        # The project itself is deleted, so the subscription is the only thing left to find.
        db.execute(conn, "UPDATE projects SET deleted_at = now(), status = 'DELETED' WHERE id = %s",
                   (project_id,))
        conn.commit()
        with pytest.raises(identity.ClosureBlocked, match="cancel it first"):
            identity.close_account(conn, user_id=user_id)
        conn.rollback()


def test_an_organization_with_other_members_refuses_the_closure(client):
    owner_id, org_id, _ = _signed_up(client, "owner-of-two@example.com")
    other_id, _, _ = _signed_up(client, "colleague@example.com")
    with db.connection() as conn:
        db.execute(conn, "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'developer')",
                   (org_id, other_id))
        conn.commit()
        with pytest.raises(identity.ClosureBlocked, match="transfer"):
            identity.close_account(conn, user_id=owner_id)
        conn.rollback()
        # The colleague is not an owner, so closing *their* account is not blocked by the same org.
        identity.close_account(conn, user_id=other_id)
        conn.commit()
        left = db.one(conn, "SELECT count(*) AS n FROM org_members WHERE org_id = %s", (org_id,))["n"]
    assert left == 1, "the owner keeps the organization; only the leaving member's row goes"


def test_closing_twice_is_refused_rather_than_repeated(client):
    user_id, _, _ = _signed_up(client, "twice@example.com")
    with db.connection() as conn:
        identity.close_account(conn, user_id=user_id)
        conn.commit()
        with pytest.raises(identity.IdentityError, match="already closed"):
            identity.close_account(conn, user_id=user_id)


def test_a_personal_organizations_name_is_scrubbed_because_it_is_a_persons_name(client):
    user_id, org_id, _ = _signed_up(client, "ada@example.com")
    with db.connection() as conn:
        before = db.one(conn, "SELECT display_name, slug FROM organizations WHERE id = %s", (org_id,))
        identity.close_account(conn, user_id=user_id)
        conn.commit()
        after = db.one(conn, "SELECT display_name, slug, deleted_at FROM organizations WHERE id = %s",
                       (org_id,))
    assert "Ada" in before["display_name"] or "ada" in before["slug"]
    assert after["display_name"] == "Closed organization"
    assert "ada" not in after["slug"] and after["deleted_at"] is not None


def test_pending_invitations_to_and_from_the_account_are_dealt_with(client):
    user_id, org_id, token = _signed_up(client, "inviter@example.com")
    sent = client.post(f"/v1/organizations/{org_id}/invitations",
                       json={"email": "guest@example.com", "role": "developer"},
                       headers={"Authorization": f"Bearer {token}"})
    assert sent.status_code == 201, sent.text
    other_id, other_org, other_token = _signed_up(client, "host@example.com")
    to_them = client.post(f"/v1/organizations/{other_org}/invitations",
                          json={"email": "inviter@example.com", "role": "developer"},
                          headers={"Authorization": f"Bearer {other_token}"})
    assert to_them.status_code == 201, to_them.text

    with db.connection() as conn:
        closure = identity.close_account(conn, user_id=user_id)
        conn.commit()
        addressed = db.one(conn, "SELECT count(*) AS n FROM org_invitations "
                                 " WHERE lower(email) = 'inviter@example.com'")["n"]
        theirs = db.one(conn, "SELECT revoked_at FROM org_invitations WHERE invited_by = %s", (user_id,))
    assert addressed == 0 and closure.invitations_removed == 1, "an invitation to them carries the address"
    assert theirs["revoked_at"] is not None and closure.invitations_revoked == 1, (
        "an invitation they sent carries somebody else's address: revoked, and the record kept"
    )
    assert other_id


def test_the_operator_command_makes_you_name_the_address_twice(client, capsys):
    user_id, _, _ = _signed_up(client, "by-command@example.com")
    assert manage._cmd_user_close(
        argparse.Namespace(email="by-command@example.com", confirm=None, actor=None)) == 2
    assert "refusing" in capsys.readouterr().out
    assert manage._cmd_user_close(
        argparse.Namespace(email="by-command@example.com", confirm="someone-else@example.com",
                           actor=None)) == 2
    capsys.readouterr()

    assert manage._cmd_user_close(
        argparse.Namespace(email="by-command@example.com", confirm="by-command@example.com",
                           actor="ticket-11")) == 0
    assert "closed" in capsys.readouterr().out
    with db.connection() as conn:
        assert db.one(conn, "SELECT status FROM users WHERE id = %s", (user_id,))["status"] == "deleted"

    # Closing it again reports no such account, and that is right rather than a rough edge: the
    # address was freed by the closure, so it names nobody. `identity.close_account` still refuses a
    # second closure by id -- see the test above -- which is the path a route would take.
    assert manage._cmd_user_close(
        argparse.Namespace(email="by-command@example.com", confirm="by-command@example.com",
                           actor=None)) == 1
    assert "no account" in capsys.readouterr().out
    assert manage._cmd_user_close(
        argparse.Namespace(email="nobody@example.com", confirm="nobody@example.com", actor=None)) == 1
    assert "no account" in capsys.readouterr().out


def test_the_operator_command_says_what_blocks_a_closure(client, capsys):
    _, org_id, _ = _signed_up(client, "blocked@example.com")
    with db.connection() as conn:
        models.create_project(conn, org_id=uuid.UUID(org_id), display_name="in the way",
                              plan_id=_free_plan_id(conn))
        conn.commit()

    assert manage._cmd_user_show(argparse.Namespace(email="blocked@example.com")) == 0
    shown = capsys.readouterr().out
    assert "cannot be closed yet" in shown and "delete them first" in shown
    assert "1 live project(s)" in shown, "and what it holds, so support can answer the email"
