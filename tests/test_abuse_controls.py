"""What stands between an open signup form and shared nodes.

Phase 07 slice 5. Signup is public at launch, so everything here is adversarial
by construction: a challenge on the one route that creates durable state, a cap
on what an organization can accumulate, and an audit trail a customer can read
without it becoming a window into the platform.

The theme is that each control fails in the direction that costs the platform
rather than the direction that costs its customers' safety. A challenge service
that is down blocks signups instead of waving them through. A deployment that
requires a challenge but configured no provider refuses rather than accepting
everybody. An event type nobody has classified is invisible rather than
published.
"""

from __future__ import annotations

import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.control_plane import captcha, db, entitlements, models
from services.control_plane.main import create_app
from tests.conftest import requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret
CHALLENGE_SECRET = "test-challenge-secret"  # noqa: S105 - test fixture, not a real secret

pytestmark = requires_db


class _Challenge:
    """A verifier the test drives: passes, refuses, or is unreachable."""

    def __init__(self, verdict: captcha.Verdict) -> None:
        self.verdict = verdict
        self.seen: list[tuple[str, str | None]] = []

    def verify(self, token: str, *, remote_ip: str | None = None) -> captcha.Verdict:
        self.seen.append((token, remote_ip))
        return self.verdict


def _client(app_config, **overrides):
    return TestClient(create_app(dataclasses.replace(app_config, **overrides)))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# -- the challenge ---------------------------------------------------------


def test_signup_without_a_challenge_is_refused_when_one_is_required(app_config, db_pool):  # noqa: ARG001
    with _client(app_config, captcha_required=True, captcha_secret=CHALLENGE_SECRET) as client:
        client.app.state.captcha = _Challenge(captcha.Verdict(passed=False, reason="missing-input"))
        response = client.post(
            "/v1/auth/signup", json={"email": "nochallenge@example.com", "password": TEST_CREDENTIAL}
        )
    assert response.status_code == 400
    # The reason upstream gave is logged, never returned: those codes name
    # exactly what an automated client would need to correct.
    assert "missing-input" not in response.text


def test_a_solved_challenge_lets_signup_through(app_config, db_pool):  # noqa: ARG001
    with _client(app_config, captcha_required=True, captcha_secret=CHALLENGE_SECRET) as client:
        challenge = _Challenge(captcha.PASSED)
        client.app.state.captcha = challenge
        response = client.post(
            "/v1/auth/signup",
            json={
                "email": "solved@example.com",
                "password": TEST_CREDENTIAL,
                "captcha_token": "widget-token",
            },
        )
    assert response.status_code == 201, response.text
    assert challenge.seen[0][0] == "widget-token", "the token was not passed to the verifier"


def test_requiring_a_challenge_without_configuring_one_refuses_rather_than_accepts(
    app_config, db_pool  # noqa: ARG001
):
    """The failure direction that matters.

    A deployment that means to require a challenge and forgot the secret would
    otherwise build a NullVerifier -- which says yes to everything -- and
    believe itself protected while accepting every signup.
    """
    with _client(app_config, captcha_required=True, captcha_secret=None) as client:
        response = client.post(
            "/v1/auth/signup",
            json={"email": "unconfigured@example.com", "password": TEST_CREDENTIAL},
        )
        assert response.status_code == 503
        # Checked inside the context: the application owns the connection pool
        # and closes it on exit.
        with db.connection() as conn:
            assert db.one(
                conn, "SELECT id FROM users WHERE email = %s", ("unconfigured@example.com",)
            ) is None, "an account was created while the challenge was unconfigured"


def test_an_unreachable_challenge_service_fails_closed_by_default():
    """A third party's bad afternoon must not become an unbounded open window.

    Failing open is the option that turns an outage into exactly the moment
    somebody watching for it farms accounts, and the outage is not something
    the platform can see coming.
    """
    class _Dead:
        def post(self, *a, **k):  # noqa: ANN001, ANN201, ARG002
            raise __import__("httpx").ConnectError("unreachable")

    verifier = captcha.TurnstileVerifier(CHALLENGE_SECRET, client=_Dead())
    assert verifier.verify("token").passed is False


def test_failing_open_is_available_but_must_be_asked_for():
    """The override exists so the choice is made in configuration by somebody
    who means it, rather than by editing the module during an incident."""
    class _Dead:
        def post(self, *a, **k):  # noqa: ANN001, ANN201, ARG002
            raise __import__("httpx").ConnectError("unreachable")

    verifier = captcha.TurnstileVerifier(CHALLENGE_SECRET, fail_open=True, client=_Dead())
    assert verifier.verify("token").passed is True


def test_a_missing_token_is_not_an_outage(app_config):  # noqa: ARG001
    """`fail_open` is about the service being unreachable, not about the
    challenge being skipped -- otherwise the override would disable the control
    rather than soften its failure mode."""
    class _Unused:
        def post(self, *a, **k):  # noqa: ANN001, ANN201, ARG002
            raise AssertionError("no request should be made for an empty token")

    verifier = captcha.TurnstileVerifier(CHALLENGE_SECRET, fail_open=True, client=_Unused())
    assert verifier.verify("").passed is False


def test_a_deployment_with_no_provider_gets_a_verifier_that_cannot_be_relied_on(app_config):
    """NullVerifier accepts everything, which is why `captcha_required` and not
    the verifier is what decides whether a route may trust it."""
    built = captcha.build(dataclasses.replace(app_config, captcha_secret=None))
    assert isinstance(built, captcha.NullVerifier)
    assert built.verify("").passed is True


# -- what an organization may accumulate -----------------------------------


@pytest.fixture
def platform(db_pool):  # noqa: ARG001 - db_pool prepares the database
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('free','Free','{}') "
            "ON CONFLICT (code) DO NOTHING",
        )
        db.execute(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES ('abuse-node','a.example','a.internal','shared','active', now()) "
            "ON CONFLICT (name) DO UPDATE SET status='active', last_health_at = now()",
        )
        conn.commit()


def _account(client, email: str) -> tuple[str, str]:
    created = client.post(
        "/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL}
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL}
    ).json()["token"]
    return token, created.json()["organizations"][0]["org_id"]


def test_a_free_organization_cannot_farm_projects(client, platform):  # noqa: ARG001
    """Each project is a database, four roles and a slot on a node, whether or
    not anybody ever connects to it."""
    token, org_id = _account(client, "farmer@example.com")
    allowed = entitlements.resolve("free", {}).max_projects

    for i in range(allowed):
        created = client.post(
            f"/v1/organizations/{org_id}/projects",
            json={"display_name": f"p{i}"},
            headers=_auth(token),
        )
        assert created.status_code == 202, created.text

    refused = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "one too many"},
        headers=_auth(token),
    )
    assert refused.status_code == 409
    assert "limit" in refused.json()["detail"]

    with db.connection() as conn:
        assert len(models.list_projects_for_org(conn, org_id)) == allowed


def test_the_cap_is_configuration_not_logic(client, platform):  # noqa: ARG001
    """`AGENTS.md`: production plan limits are never hard-coded. A plan whose
    config raises the number raises it, without a release."""
    token, org_id = _account(client, "configured@example.com")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE code = 'free'",
            (psycopg.types.json.Jsonb({"limits": {"max_projects": 3}}),),
        )
        conn.commit()

    for i in range(3):
        assert client.post(
            f"/v1/organizations/{org_id}/projects",
            json={"display_name": f"c{i}"},
            headers=_auth(token),
        ).status_code == 202

    assert client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "fourth"},
        headers=_auth(token),
    ).status_code == 409


def test_a_deleted_project_does_not_count_against_the_cap(client, platform):  # noqa: ARG001
    """A customer who created two, deleted one and cannot create another has
    been charged for a mistake they already corrected."""
    token, org_id = _account(client, "tidy@example.com")
    first = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "a"}, headers=_auth(token)
    ).json()["project_ref"]
    client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "b"}, headers=_auth(token)
    )

    with db.connection() as conn:
        db.execute(
            conn, "UPDATE projects SET deleted_at = now() WHERE project_ref = %s", (first,)
        )
        conn.commit()

    assert client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "c"}, headers=_auth(token)
    ).status_code == 202


def test_a_customer_cannot_grant_themselves_a_paid_plan(client, platform):  # noqa: ARG001
    """The slice 5 security review's finding, and the sharpest one in the phase.

    `plan_code` shipped in slice 1 as a convenience and was an entitlement the
    caller granted themselves: any active plan was accepted, nothing checked
    whether the organization was entitled to it, and `GET /v1/plans` hands every
    authenticated user the codes. Naming a paid plan gave an unbilled project
    100 projects instead of 2, production resource settings, and
    `direct_database_access: True` -- which is the "free projects are API-only"
    invariant in AGENTS.md, and a named item in its own review rules.
    """
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('production','Production','{}') "
            "ON CONFLICT (code) DO NOTHING",
        )
        conn.commit()

    token, org_id = _account(client, "upgrader@example.com")

    refused = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "free upgrade", "plan_code": "production"},
        headers=_auth(token),
    )
    assert refused.status_code == 404, "a caller granted themselves a paid plan"
    # The same answer an unknown code gets, so it is not a probe for which
    # plans exist and which are merely forbidden.
    assert refused.json()["detail"] == "unknown plan"

    with db.connection() as conn:
        assert models.list_projects_for_org(conn, org_id) == []


def test_the_default_plan_may_still_be_named_explicitly(client, platform):  # noqa: ARG001
    """A dashboard that sends the plan it is showing must keep working."""
    token, org_id = _account(client, "explicit@example.com")
    created = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "named", "plan_code": "free"},
        headers=_auth(token),
    )
    assert created.status_code == 202, created.text


def test_the_cap_refusal_does_not_name_the_ceiling(client, platform):  # noqa: ARG001
    """Naming it tells a caller which plan would raise it."""
    token, org_id = _account(client, "quiet@example.com")
    for i in range(entitlements.resolve("free", {}).max_projects):
        client.post(
            f"/v1/organizations/{org_id}/projects",
            json={"display_name": f"q{i}"},
            headers=_auth(token),
        )
    refused = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "over"},
        headers=_auth(token),
    )
    assert refused.status_code == 409
    assert "2" not in refused.json()["detail"]


# -- what a customer may read ----------------------------------------------


def _record(project_id, event_type: str, detail: dict, actor_type: str = "system") -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
            "VALUES (%s, %s, %s, %s)",
            (project_id, actor_type, event_type, psycopg.types.json.Jsonb(detail)),
        )
        conn.commit()


def test_a_customer_sees_the_events_that_explain_their_own_breakage(client, platform):  # noqa: ARG001
    token, org_id = _account(client, "reader@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "audited"},
        headers=_auth(token),
    ).json()["project_ref"]
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, ref)

    _record(project.id, "storage.restricted", {"gross_bytes": 5, "quota_bytes": 4, "fraction": 1.25})

    events = client.get(f"/v1/projects/{ref}/audit-events", headers=_auth(token))
    assert events.status_code == 200, events.text
    [event] = events.json()
    assert event["event_type"] == "storage.restricted"
    assert "storage limit" in event["description"]
    assert event["detail"]["quota_bytes"] == 4
    assert event["actor"] == "platform"


def test_an_unclassified_event_is_invisible_rather_than_published(client, platform):  # noqa: ARG001
    """The failure direction that costs a support question rather than a
    disclosure.

    `detail_json` is free-form and written by several subsystems. An event type
    nobody has classified must not appear the moment somebody adds it, because
    the first one to write a node hostname or an internal error string into that
    column would publish it to customers without anybody deciding to.
    """
    token, org_id = _account(client, "unclassified@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "hidden"},
        headers=_auth(token),
    ).json()["project_ref"]
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, ref)

    _record(
        project.id,
        "placement.moved",
        {"node_hostname": "node-7.internal", "reason": "rebalance"},
    )

    events = client.get(f"/v1/projects/{ref}/audit-events", headers=_auth(token)).json()
    assert events == [], "an unclassified event reached a customer"


def test_only_allowlisted_detail_keys_are_returned(client, platform):  # noqa: ARG001
    """Projected key by key, so a new key in a known event type is invisible
    until somebody classifies it too."""
    token, org_id = _account(client, "keys@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "keys"},
        headers=_auth(token),
    ).json()["project_ref"]
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, ref)

    _record(
        project.id,
        "realtime.slot_invalidated",
        {
            "reason": "the slot exceeded max_slot_wal_keep_size",
            "replayed_on_recovery": False,
            "slot_name": "supabase_realtime_replication_slot_abcd1234",
            "wal_status": "lost",
        },
    )

    [event] = client.get(f"/v1/projects/{ref}/audit-events", headers=_auth(token)).json()
    assert set(event["detail"]) == {"reason", "replayed_on_recovery"}
    assert "wal_status" not in event["detail"]
    assert "slot_name" not in str(event)


def test_another_organization_cannot_read_the_audit_trail(client, platform):  # noqa: ARG001
    """Identical bodies for a stranger's project and an invented one -- the rule
    the Phase 07 security review found broken on two other routers."""
    owner, org_id = _account(client, "auditowner@example.com")
    ref = client.post(
        f"/v1/organizations/{org_id}/projects", json={"display_name": "private"},
        headers=_auth(owner),
    ).json()["project_ref"]
    stranger, _ = _account(client, "auditstranger@example.com")

    real = client.get(f"/v1/projects/{ref}/audit-events", headers=_auth(stranger))
    invented = client.get("/v1/projects/zzzz9999/audit-events", headers=_auth(stranger))
    assert real.status_code == invented.status_code == 404
    assert real.text == invented.text
