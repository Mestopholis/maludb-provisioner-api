"""What a project has used, and asking for more.

Phase 07 slice 3. The rule these tests exist to hold is that this surface
*reports* and never *grants*: an upgrade request is a row in a queue, and a
project's entitlements after asking are exactly what they were before.

Phase 09 slice 6 added the billing period at the bottom of this file, under the
same rule: it reports the window and never a metered quantity, because ADR-050
makes ceilings hard and nothing this platform counts is ever sent to a payment
provider.

The other theme is the difference between **unknown** and **none**. Storage that
has never been measured reports null, not zero, and requests report their limit
with `metered: false` rather than a consumption figure the platform does not
record. A dashboard showing "0 bytes used" for a project nobody has measured
would be telling a customer something nobody knows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from services.control_plane import db, entitlements, models, subscriptions
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

    # Identical bodies, not merely identical statuses: see the api-key suite for
    # what a distinguishable 404 discloses.
    real = client.get(f"/v1/projects/{ref}/usage", headers=_auth(stranger))
    invented = client.get("/v1/projects/zzzz9999/usage", headers=_auth(stranger))
    assert real.status_code == invented.status_code == 404
    assert real.text == invented.text, "usage tells a stranger which refs are real"

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


# -- the billing period (Phase 09 slice 6, ADR-050) ------------------------
#
# The rule above holds here too, in a sharper form: this reports the period, it
# does not meter it. Hard limits mean nothing accumulates into a charge, so no
# figure in this response is ever sent to a provider -- and the last test in
# this file is the one that keeps it that way.


def _project_id(ref: str):
    with db.connection() as conn:
        return models.get_project_by_ref(conn, ref).id


def _subscribe(ref: str, **kwargs):
    with db.connection() as conn:
        return subscriptions.create(conn, project_id=_project_id(ref), **kwargs)


def _billing(client, token: str, ref: str) -> dict:
    response = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["billing"]


def _moment(client, token: str, ref: str, key: str):
    raw = _billing(client, token, ref)[key]
    return datetime.fromisoformat(raw) if raw is not None else None


def test_a_free_project_says_it_has_no_subscription_rather_than_reporting_null(client, catalogue):  # noqa: ARG001
    """`subscribed: false` is the whole reason this object is never null.

    Everything else in this response spends null on *unknown* -- storage nobody
    has measured, a quantity nobody counts. A free project's billing period is
    not unknown, it is absent, and returning null for the object would make a
    dashboard guess which of the two it was looking at.
    """
    token, org_id = _account(client, "billingfree@example.com")
    ref = _project(client, token, org_id)

    billing = _billing(client, token, ref)
    assert billing["subscribed"] is False
    assert billing["state"] is None
    assert billing["plan_code"] is None
    assert billing["period_start"] is None
    assert billing["period_end"] is None
    assert billing["grace_ends_at"] is None


def test_the_period_a_project_is_counted_against_comes_from_the_subscription(client, catalogue):  # noqa: ARG001
    """The slice, in one assertion: the customer's month, not the calendar's."""
    token, org_id = _account(client, "billingperiod@example.com")
    ref = _project(client, token, org_id)
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(days=9)
    end = start + timedelta(days=30)
    _subscribe(ref, plan_code="starter", period_start=start, period_end=end)

    billing = _billing(client, token, ref)
    assert billing["subscribed"] is True
    assert billing["state"] == "active"
    assert datetime.fromisoformat(billing["period_start"]) == start
    assert datetime.fromisoformat(billing["period_end"]) == end


def test_a_subscription_with_no_period_yet_reports_unknown_rather_than_inventing_one(
    client, catalogue,  # noqa: ARG001
):
    """A subscription exists before its provider has said anything about dates.

    An operator's comped row never has a period at all, and a completed Checkout
    has none until `customer.subscription.*` arrives. Defaulting either to "now
    to a month from now" would put a renewal date on a dashboard that nobody
    promised and nothing will honour.
    """
    token, org_id = _account(client, "billingnoperiod@example.com")
    ref = _project(client, token, org_id)
    _subscribe(ref, plan_code="starter")

    billing = _billing(client, token, ref)
    assert billing["subscribed"] is True
    assert billing["period_start"] is None
    assert billing["period_end"] is None


def test_the_plan_paid_for_and_the_plan_enforced_are_reported_separately(client, catalogue):  # noqa: ARG001
    """ADR-048's separation, made visible instead of papered over.

    A subscription is written by a webhook and applied by the maintenance pass,
    so between the two there is a project paying for `starter` and running on
    `free`. Reporting the subscription's plan as the project's would promise
    capacity the node has not been told about; reporting only the project's
    would leave a customer who has just paid with no evidence that anything
    happened. Both, named for what they are.
    """
    token, org_id = _account(client, "billingsplit@example.com")
    ref = _project(client, token, org_id)
    _subscribe(ref, plan_code="starter")

    body = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()
    assert body["plan_code"] == "free", "an unreconciled subscription changed an entitlement"
    assert body["billing"]["plan_code"] == "starter"
    # And the entitlements really are still the free ones -- the plan code is
    # not the only thing that could have moved.
    assert body["storage"]["limit_bytes"] == entitlements.resolve("free", {}).database_storage_bytes


def test_a_failed_payment_shows_the_customer_when_grace_runs_out(client, catalogue):  # noqa: ARG001
    """ADR-051 gives fourteen days. A countdown nobody can see is not a warning.

    The date is the earliest the restriction can arrive, not the moment it will:
    grace expires when the maintenance pass next runs. Late is the safe
    direction -- a customer told their writes stopped while they had not is the
    error that generates the support ticket.
    """
    token, org_id = _account(client, "billingpastdue@example.com")
    ref = _project(client, token, org_id)
    began = datetime.now(UTC).replace(microsecond=0) - timedelta(days=3)
    _subscribe(ref, plan_code="starter", state="past_due", as_of=began)

    billing = _billing(client, token, ref)
    assert billing["state"] == "past_due"
    assert datetime.fromisoformat(billing["grace_ends_at"]) == began + timedelta(days=14)


def test_grace_has_no_deadline_in_any_other_state(client, catalogue):  # noqa: ARG001
    """A date on a healthy subscription would be read as a date that means
    something, and there is nothing it could mean."""
    token, org_id = _account(client, "billingactive@example.com")
    ref = _project(client, token, org_id)
    _subscribe(ref, plan_code="starter")

    assert _billing(client, token, ref)["grace_ends_at"] is None


def test_the_grace_deadline_is_deployment_configuration_rather_than_a_constant(
    client, app_config, catalogue,  # noqa: ARG001
):
    """`MALUDB_BILLING_GRACE_DAYS`, the same value the maintenance pass expires
    on. Two answers to "when do my writes stop" is worse than none.

    The second application is built without entering its lifespan on purpose:
    the pool is already up and the lifespan's shutdown closes it, so a nested
    client would take the connection pool down for the rest of the test.
    """
    import dataclasses

    from fastapi.testclient import TestClient

    from services.control_plane.main import create_app

    token, org_id = _account(client, "billinggrace@example.com")
    ref = _project(client, token, org_id)
    began = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    _subscribe(ref, plan_code="starter", state="past_due", as_of=began)

    impatient = TestClient(create_app(dataclasses.replace(app_config, billing_grace_days=3)))
    body = impatient.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()
    assert datetime.fromisoformat(body["billing"]["grace_ends_at"]) == began + timedelta(days=3)


def test_the_grace_deadline_does_not_move_when_a_dunning_retry_arrives(client, catalogue):  # noqa: ARG001
    """The slice-5 clock bug, asserted where a customer would see it.

    Stripe re-sends `past_due` on every retry with a newer timestamp. A deadline
    computed from when the fact was last asserted slides forward on each one, so
    the countdown a customer is watching never reaches zero -- and the failure
    looks exactly like the system working. `state_since` is what migration 0022
    added to stop that, and this is the visible half of it.
    """
    token, org_id = _account(client, "billingdunning@example.com")
    ref = _project(client, token, org_id)
    began = datetime.now(UTC).replace(microsecond=0) - timedelta(days=5)
    _subscribe(ref, plan_code="starter", state="past_due", as_of=began)
    first = _moment(client, token, ref, "grace_ends_at")

    with db.connection() as conn:
        subscriptions.record_state(
            conn, project_id=_project_id(ref), state="past_due",
            as_of=began + timedelta(days=2),
        )

    assert _moment(client, token, ref, "grace_ends_at") == first


def test_a_canceled_subscription_leaves_a_project_reporting_no_subscription(client, catalogue):  # noqa: ARG001
    """`canceled` is not a state this route can show, because it is not live.

    A project that had a subscription and a project that never had one report
    the same thing here, which is correct: neither is being paid for. What
    ADR-051 guarantees is that the *project* is otherwise untouched, and the
    response proves it -- same ref, same plan resolution, still answering.
    """
    token, org_id = _account(client, "billingcanceled@example.com")
    ref = _project(client, token, org_id)
    _subscribe(ref, plan_code="starter", state="past_due")
    with db.connection() as conn:
        subscriptions.record_state(conn, project_id=_project_id(ref), state="canceled")

    body = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).json()
    assert body["project_ref"] == ref
    assert body["billing"]["subscribed"] is False
    assert body["billing"]["state"] is None
    assert body["billing"]["grace_ends_at"] is None


def test_no_provider_identifier_reaches_the_customer(client, catalogue):  # noqa: ARG001
    """The provider is identity for the platform's own bookkeeping (ADR-049).

    A Stripe subscription id, customer id or price id in a customer-facing
    response is an internal key handed out with no way to take it back, and the
    customer id in particular is the handle on an organization's whole billing
    relationship. None of them are needed to display a period.
    """
    token, org_id = _account(client, "billingids@example.com")
    ref = _project(client, token, org_id)
    _subscribe(
        ref, plan_code="starter", provider="stripe",
        provider_subscription_id="sub_leakcanary", provider_customer_id="cus_leakcanary",
    )

    text = client.get(f"/v1/projects/{ref}/usage", headers=_auth(token)).text
    for forbidden in ("sub_", "cus_", "price_", "leakcanary"):
        assert forbidden not in text, f"{forbidden} reached a customer"


def test_nothing_in_the_billing_period_is_a_quantity(client, catalogue):  # noqa: ARG001
    """ADR-050 as a shape test, and it is here to fail on the day somebody adds
    an amount.

    Hard limits mean no number this platform computes is ever reported to a
    payment provider, so no usage bug can become a billing bug. That property
    survives exactly as long as nothing in this response starts looking like
    something worth sending -- a metered quantity, an amount, a currency. The
    field list is pinned rather than described.
    """
    token, org_id = _account(client, "billingshape@example.com")
    ref = _project(client, token, org_id)
    _subscribe(ref, plan_code="starter")

    assert sorted(_billing(client, token, ref)) == [
        "grace_ends_at", "period_end", "period_start", "plan_code", "state", "subscribed",
    ]
