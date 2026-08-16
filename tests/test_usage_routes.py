"""What a project has used, and asking for more.

Phase 07 slice 3. The rule these tests exist to hold is that this surface
*reports* and never *grants*: an upgrade request is a row in a queue, and a
project's entitlements after asking are exactly what they were before.

The other theme is the difference between **unknown** and **none**. Storage that
has never been measured reports null, not zero, and requests report their limit
with `metered: false` rather than a consumption figure the platform does not
record. A dashboard showing "0 bytes used" for a project nobody has measured
would be telling a customer something nobody knows.
"""

from __future__ import annotations

import psycopg
import pytest

from services.control_plane import db, entitlements, models
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


@pytest.fixture
def catalogue(db_pool):  # noqa: ARG001 - db_pool prepares the database
    """A free plan to land on and a paid one to ask for."""
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES "
            "  ('free','Free','{}'), ('starter','Starter','{}') "
            "ON CONFLICT (code) DO NOTHING",
        )
        db.execute(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES ('usage-node','u.example','u.internal','shared','active', now()) "
            "ON CONFLICT (name) DO UPDATE SET status='active', last_health_at = now()",
        )
        conn.commit()


def _project(client, token: str, org_id: str) -> str:
    response = client.post(
        f"/v1/organizations/{org_id}/projects",
        json={"display_name": "Usage"},
        headers=_auth(token),
    )
    assert response.status_code == 202, response.text
    return response.json()["project_ref"]


# -- unknown is not none ---------------------------------------------------


def test_a_project_never_measured_reports_unknown_rather_than_zero(client, catalogue):  # noqa: ARG001
    """The distinction this whole response shape exists for.

    A project measured five minutes ago and a project never measured at all are
    different situations. Reporting both as zero would tell a customer their
    database is empty on the strength of nobody having looked.
    """
    token, org_id = _account(client, "unmeasured@example.com")
    ref = _project(client, token, org_id)

    usage = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token))
    assert usage.status_code == 200, usage.text
    storage = usage.json()["storage"]

    assert storage["used_bytes"] is None, "an unmeasured project reported a usage figure"
    assert storage["measured_at"] is None
    assert storage["limit_bytes"] > 0, "the limit is known even when the usage is not"


def test_a_measured_project_reports_the_figure_and_when_it_was_taken(client, catalogue):  # noqa: ARG001
    """The timestamp is not decoration: a maintenance pass that stopped running
    is visible here and nowhere else a customer can see."""
    token, org_id = _account(client, "measured@example.com")
    ref = _project(client, token, org_id)

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET database_bytes = %s, database_measured_at = now(), "
            "       storage_state = 'ok' WHERE project_ref = %s",
            (41_943_040, ref),
        )
        conn.commit()

    storage = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()["storage"]
    assert storage["used_bytes"] == 41_943_040
    assert storage["measured_at"] is not None
    assert storage["state"] == "ok"


def test_requests_report_their_limit_and_say_they_are_not_metered(client, catalogue):  # noqa: ARG001
    """ADR-030 keeps the limiter in-process, so nothing accumulates a figure.

    The limit is real and enforced; the consumption is not recorded anywhere
    this API could read. Saying so is the honest answer, and a zero would be a
    claim about usage that the platform cannot make.
    """
    token, org_id = _account(client, "unmetered@example.com")
    ref = _project(client, token, org_id)

    body = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()
    for field in ("api_requests", "database_connections"):
        assert body[field]["metered"] is False, f"{field} claimed to be metered"
        assert body[field]["used"] is None, f"{field} reported a consumption figure"
        assert body[field]["limit"] > 0, f"{field} reported no limit"


def test_a_storage_restriction_is_visible_to_the_customer(client, catalogue):  # noqa: ARG001
    """A customer whose writes are being refused should learn it here rather
    than from their application's error log."""
    token, org_id = _account(client, "restricted@example.com")
    ref = _project(client, token, org_id)
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET storage_state = 'restricted', database_bytes = 1, "
            "       database_measured_at = now() WHERE project_ref = %s",
            (ref,),
        )
        conn.commit()

    assert client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()["storage"][
        "state"
    ] == "restricted"


# -- reporting, never granting ---------------------------------------------


def test_asking_to_upgrade_changes_no_entitlement(client, catalogue):  # noqa: ARG001
    """The rule the whole route exists under.

    Phase 09 owns payment. A route here that moved the project onto the paid
    plan would grant paid capacity to a project nobody had billed, and the
    platform would find out at renewal rather than at purchase.
    """
    token, org_id = _account(client, "upgrade@example.com")
    ref = _project(client, token, org_id)

    with db.connection() as conn:
        before = models.get_project_by_ref(conn, ref)

    asked = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(token),
    )
    assert asked.status_code == 202, asked.text
    assert asked.json()["state"] == "REQUESTED"

    with db.connection() as conn:
        after = models.get_project_by_ref(conn, ref)
        row = db.one(
            conn,
            "SELECT pl.code, pl.config_json FROM projects pr "
            "  JOIN plans pl ON pl.id = pr.plan_id WHERE pr.id = %s",
            (after.id,),
        )
    assert after.plan_id == before.plan_id, "the upgrade request moved the project's plan"

    # And the resolved allowances are the free ones, not the requested ones.
    allowed = entitlements.resolve(row["code"], row["config_json"])
    assert allowed.plan_code == "free"

    usage = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()
    assert usage["plan_code"] == "free", "usage reported the plan that was merely asked for"


def test_pressing_the_button_twice_does_not_queue_two_requests(client, catalogue):  # noqa: ARG001
    """The customer is asking the same question; two rows have an operator
    answer it twice."""
    token, org_id = _account(client, "twice-upgrade@example.com")
    ref = _project(client, token, org_id)

    first = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(token),
    ).json()
    second = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(token),
    ).json()
    assert first["id"] == second["id"]

    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT id FROM upgrade_requests WHERE project_id = "
            "  (SELECT id FROM projects WHERE project_ref = %s)",
            (ref,),
        )
    assert len(rows) == 1


def test_a_closed_request_lets_a_customer_ask_again(client, catalogue):  # noqa: ARG001
    """The partial index is on open requests for exactly this reason: a customer
    whose request was closed may legitimately want to ask a second time."""
    token, org_id = _account(client, "again@example.com")
    ref = _project(client, token, org_id)
    first = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(token),
    ).json()

    with db.connection() as conn:
        db.execute(
            conn, "UPDATE upgrade_requests SET state = 'CLOSED' WHERE id = %s", (first["id"],)
        )
        conn.commit()

    second = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(token),
    )
    assert second.status_code == 202
    assert second.json()["id"] != first["id"]


def test_an_unknown_plan_cannot_be_requested(client, catalogue):  # noqa: ARG001
    token, org_id = _account(client, "unknownplan@example.com")
    ref = _project(client, token, org_id)
    refused = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "enterprise-unicorn"},
        headers=_auth(token),
    )
    assert refused.status_code == 404


def test_the_operator_s_note_is_never_returned_to_the_customer(client, catalogue):  # noqa: ARG001
    """It is written for whoever works the queue. A note written in the belief
    that it is private should not become a support reply."""
    token, org_id = _account(client, "note@example.com")
    ref = _project(client, token, org_id)
    asked = client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(token),
    ).json()

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE upgrade_requests SET operator_note = %s, state = 'CONTACTED' WHERE id = %s",
            ("card declined twice, chase before offering credit", asked["id"]),
        )
        conn.commit()

    seen = client.get(f"/v1/projects/{ref}/upgrade-request", headers=_auth(token))
    assert seen.status_code == 200
    assert "operator_note" not in seen.json()
    assert "declined" not in seen.text


# -- who may look ----------------------------------------------------------


def test_another_organization_sees_nothing(client, catalogue):  # noqa: ARG001
    owner, org_id = _account(client, "usageowner@example.com")
    ref = _project(client, owner, org_id)
    stranger, _ = _account(client, "usagestranger@example.com")

    assert client.get(f"/v1/projects/{ref}/usage", headers=_auth(stranger)).status_code == 404
    assert client.post(
        f"/v1/projects/{ref}/upgrade-request",
        json={"requested_plan_code": "starter"},
        headers=_auth(stranger),
    ).status_code == 404


def test_usage_carries_nothing_that_could_reach_the_database(client, catalogue):  # noqa: ARG001
    """Usage is where a database name would be most natural to include: the
    figure came from measuring that database."""
    token, org_id = _account(client, "usageleak@example.com")
    ref = _project(client, token, org_id)
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET database_bytes = 1024, database_measured_at = now() "
            " WHERE project_ref = %s",
            (ref,),
        )
        conn.commit()

    text = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).text.lower()
    for forbidden in ("postgres://", "postgresql://", "dsn", "password", f"mldb_{ref}", "u.internal"):
        assert forbidden not in text, f"{forbidden} reached a customer"


def test_a_project_on_an_unrecognised_plan_falls_back_rather_than_failing(client, catalogue):  # noqa: ARG001
    """A plan code the code has no defaults for still resolves to a floor.

    `projects.plan_id` is NOT NULL, so a project always names *a* plan -- but a
    catalogue can carry a code this deployment's `entitlements.DEFAULTS` has
    never heard of, from a rename or an older release. `resolve` falls back to
    free rather than raising, and a usage endpoint that raised on it would take
    the dashboard down for the customers least able to explain why.
    """
    token, org_id = _account(client, "orphanplan@example.com")
    ref = _project(client, token, org_id)
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('legacy-gold','Legacy','{}') "
            "ON CONFLICT (code) DO NOTHING",
        )
        db.execute(
            conn,
            "UPDATE projects SET plan_id = (SELECT id FROM plans WHERE code = 'legacy-gold') "
            " WHERE project_ref = %s",
            (ref,),
        )
        conn.commit()

    response = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["plan_code"] == "free", "an unrecognised plan lost its floor"
    assert response.json()["storage"]["limit_bytes"] > 0


def test_email_usage_is_counted_rather_than_guessed(client, catalogue):  # noqa: ARG001
    """It is one of the two things the platform genuinely counts."""
    token, org_id = _account(client, "emailusage@example.com")
    ref = _project(client, token, org_id)
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, ref)
        for _ in range(3):
            db.execute(
                conn,
                "INSERT INTO email_events (project_id, event_type, recipient_hash, occurred_at) "
                "VALUES (%s, 'sent', %s, now())",
                (project.id, psycopg.Binary(b"x" * 32)),
            )
        conn.commit()

    email = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()["email"]
    assert email["used"] == 3
    assert email["limit"] > 0
