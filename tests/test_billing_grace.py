"""What a failed payment costs, and what it must never cost (slice 5, ADR-051).

Fourteen days of unchanged service, then the plan ends and the ADR-040 storage
restriction takes over. **No deletion at any point.**

Acceptance criterion 4 is the one whose failure cannot be undone, so the
assertions here are mostly about survival: the rows, the database, the
`project_ref` and the API keys all still there after the whole transition, and
the customer still able to read and still able to shrink out.

Two of these test a clock, and clocks are where this slice could quietly fail.
Stripe re-sends `past_due` on every dunning retry, so a grace period measured
from "when was this last true" restarts on every retry and never expires -- the
customer keeps a paid plan forever and it looks exactly like the system working.
`test_the_grace_clock_does_not_restart_on_a_dunning_retry` is the one that
would catch that, and it is the reason `state_since` exists.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from services.control_plane import api_keys, billing, db, maintenance, subscriptions
from tests.conftest import TEST_PEPPER, requires_db
from tests.test_billing import (
    FakeStripe,
    _catalogue,
    _client,
    _event,
    _handle,
    _map_price,
    _plan,
    _plan_code,
    _set_plan,
    _sold,
    _subscription_obj,
)
from tests.test_direct_sql import paid_project  # noqa: F401 - fixture
from tests.test_provisioning import ADMIN_DSN, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

GRACE = 14


def _ts(offset_seconds: int = 0) -> int:
    """A provider timestamp near now.

    Not a small integer. `state_as_of` is compared against real timestamps once
    a test writes one, and a fixture second-count from 1970 is *older* than
    anything on the row -- so it is refused as stale, and a test meaning to
    exercise a dunning retry exercises the ordering guard instead.
    """
    return int(time.time()) + offset_seconds


def _tenant_dsn_for(database: str) -> str:
    """The platform's own superuser connection to a tenant, for assertions.

    Not a customer's route in: the harness reading a tenant database to prove
    its contents survived. Same helper `test_subscriptions` uses, for the same
    reason.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    parsed["dbname"] = database
    return psycopg.conninfo.make_conninfo(**parsed)


class CancellingStripe(FakeStripe):
    """A Stripe that records cancellations, and can refuse them."""

    def __init__(self, *, unreachable: bool = False, already_gone: bool = False) -> None:
        super().__init__()
        self.canceled: list[str] = []
        self.unreachable = unreachable
        self.already_gone = already_gone

    def _handle(self, request):
        if request.method == "DELETE" and "/v1/subscriptions/" in request.url.path:
            subscription_id = request.url.path.rsplit("/", 1)[-1]
            if self.unreachable:
                import httpx

                return httpx.Response(500, json={"error": {"message": "unavailable"}})
            if self.already_gone:
                import httpx

                return httpx.Response(404, json={"error": {"message": "No such subscription"}})
            self.canceled.append(subscription_id)
            import httpx

            return httpx.Response(200, json={"id": subscription_id, "status": "canceled"})
        return super()._handle(request)


def _past_due(project_id, *, days_ago: float, subscription_id: str = "sub_1") -> None:
    """A project whose payment failed `days_ago`, through the real path."""
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE subscriptions SET state_since = now() - (%s * interval '1 day'), "
            "       state_as_of = now() - (%s * interval '1 day') "
            " WHERE project_id = %s",
            (days_ago, days_ago, project_id),
        )
        conn.commit()


def _state(project_id) -> str | None:
    with db.connection() as conn:
        live = subscriptions.for_project(conn, project_id)
        return live.state if live else None


def _fail_payment(checkout_id: str, *, at: int) -> None:
    """Stripe telling us a payment failed, the way it actually does."""
    _handle(_event("customer.subscription.updated", _subscription_obj(
        status="past_due", checkout_id=checkout_id,
    ), created=at))


def _sell(ref: str, placed_project) -> tuple:
    """A project that has paid, and is on the plan it paid for.

    `_set_plan` rather than a reconciliation, because these tests are about
    billing state and want a project sitting on a paid plan with no node behind
    it -- the state slice 1's operation refuses to create and the one a
    downgrade has to start from.
    """
    project_id, checkout_id = _sold(ref, placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }, created=_ts(-3600)))
    _set_plan(project_id, "pro")
    return project_id, checkout_id


# -- the clock -------------------------------------------------------------


def test_a_failed_payment_changes_nothing_at_all_at_first(placed_project):
    """Fourteen days of *unchanged* service. Cards expire and banks decline for
    reasons unrelated to intent; a platform that restricts on the first failure
    punishes the wrong thing."""
    project_id, checkout_id = _sell("grc00001", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))

    assert _state(project_id) == "past_due"
    with db.connection() as conn:
        # Still entitled to the plan they are paying for -- `past_due` is in
        # ENTITLING, which is the whole of slice 3's opinion about this.
        assert subscriptions.entitled_plan_code(conn, project_id) == "pro"
        assert billing.end_expired_grace(
            conn, _client(CancellingStripe()), grace_days=GRACE
        ) == []


def test_the_grace_clock_does_not_restart_on_a_dunning_retry(placed_project):
    """The failure this slice could quietly have, and the reason `state_since`
    exists.

    Stripe re-sends `customer.subscription.updated` with `status=past_due` on
    every retry, each carrying a newer `created`. A clock keyed on when the fact
    was last asserted restarts on every one of them and never runs out: the
    customer keeps a paid plan indefinitely, and nothing anywhere looks wrong.
    """
    project_id, checkout_id = _sell("grc00002", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        began = subscriptions.for_project(conn, project_id).state_since

    # Three more dunning retries, each newer than the last.
    for offset in (-1200, -600, -60):
        _fail_payment(checkout_id, at=_ts(offset))

    with db.connection() as conn:
        live = subscriptions.for_project(conn, project_id)
        assert live.state_since == began, "a retry moved the clock"
        assert live.state_as_of > began, "state_as_of should track the latest fact"
        expired = subscriptions.in_expired_grace(conn, grace_days=GRACE)
    assert [row["project_id"] for row in expired] == [project_id]


def test_recovering_and_failing_again_starts_a_new_clock(placed_project):
    """A customer who pays, then fails months later, gets the full period
    again. The clock is per episode, not per subscription."""
    project_id, checkout_id = _sell("grc00003", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 5)

    _handle(_event("customer.subscription.updated", _subscription_obj(
        status="active", checkout_id=checkout_id,
    ), created=_ts(-120)))
    _fail_payment(checkout_id, at=_ts(-60))

    with db.connection() as conn:
        live = subscriptions.for_project(conn, project_id)
        assert live.state == "past_due"
        assert (datetime.now(UTC) - live.state_since) < timedelta(days=1)
        assert subscriptions.in_expired_grace(conn, grace_days=GRACE) == []


def test_the_grace_period_is_configuration_and_not_a_constant(placed_project):
    """A grace period is a plan limit, and the development rules forbid
    hard-coding those. Asserted by changing it and watching the answer move."""
    project_id, checkout_id = _sell("grc00004", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=10)

    with db.connection() as conn:
        assert subscriptions.in_expired_grace(conn, grace_days=14) == []
        assert len(subscriptions.in_expired_grace(conn, grace_days=7)) == 1
        assert len(subscriptions.in_expired_grace(conn, grace_days=0)) == 1


def test_a_project_in_grace_is_reported_before_the_grace_runs_out(placed_project):
    """A number that only appears once the grace has expired is not a warning."""
    project_id, checkout_id = _sell("grc00005", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=3)

    with db.connection() as conn:
        rows = subscriptions.in_grace(conn, grace_days=GRACE)
    assert len(rows) == 1
    assert rows[0]["project_ref"] == "grc00005"
    assert rows[0]["expires_at"] > datetime.now(UTC)


# -- what happens at the end of it ----------------------------------------


def test_the_end_of_grace_cancels_at_the_provider_before_taking_anything_away(placed_project):
    """The platform must not stop providing a plan while the provider keeps
    trying to collect for it. A card retry succeeding days later would charge a
    customer for something already taken away."""
    project_id, checkout_id = _sell("grc00006", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    stripe = CancellingStripe()
    with db.connection() as conn:
        outcomes = billing.end_expired_grace(conn, _client(stripe), grace_days=GRACE)

    assert [o.outcome for o in outcomes] == ["ended"]
    assert stripe.canceled == ["sub_1"]
    assert _state(project_id) is None, "the subscription should be canceled"


def test_a_provider_that_cannot_be_reached_takes_nothing_away(placed_project):
    """Failing towards not revoking the plan. The cost is a few days of service
    the platform is not paid for; the cost of the other direction is a customer
    charged for a plan they no longer have."""
    project_id, checkout_id = _sell("grc00007", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        outcomes = billing.end_expired_grace(
            conn, _client(CancellingStripe(unreachable=True)), grace_days=GRACE
        )

    assert [o.outcome for o in outcomes] == ["deferred"]
    assert _state(project_id) == "past_due", "nothing may be taken away"
    with db.connection() as conn:
        assert subscriptions.entitled_plan_code(conn, project_id) == "pro"


def test_a_deployment_with_no_provider_configured_defers_too(placed_project):
    project_id, checkout_id = _sell("grc00008", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        outcomes = billing.end_expired_grace(conn, None, grace_days=GRACE)

    assert [o.outcome for o in outcomes] == ["deferred"]
    assert _state(project_id) == "past_due"


def test_a_subscription_already_gone_at_the_provider_still_ends_locally(placed_project):
    """Cancelling something already cancelled is the desired state, not a
    failure: the goal is that it not be running, and it is not running."""
    project_id, checkout_id = _sell("grc00009", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        outcomes = billing.end_expired_grace(
            conn, _client(CancellingStripe(already_gone=True)), grace_days=GRACE
        )

    assert [o.outcome for o in outcomes] == ["ended"]
    assert _state(project_id) is None


def test_a_subscription_nobody_paid_a_provider_for_ends_without_one(placed_project):
    """A comped project, or one migrated from another system. There is nothing
    to cancel at Stripe and that must not stop the grace period ending."""
    _catalogue()
    project_id = placed_project("grc00010")
    _set_plan(project_id, "free")
    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro")
        subscriptions.record_state(conn, project_id=project_id, state="past_due")
    _past_due(project_id, days_ago=GRACE + 1)

    stripe = CancellingStripe()
    with db.connection() as conn:
        outcomes = billing.end_expired_grace(conn, _client(stripe), grace_days=GRACE)

    assert [o.outcome for o in outcomes] == ["ended"]
    assert stripe.canceled == [], "there was no provider subscription to cancel"
    assert _state(project_id) is None


def test_ending_grace_writes_no_entitlement_by_itself(placed_project):
    """The same separation slice 3 built and slice 4 kept. This records a
    billing fact; reconciliation is what moves the project, in another pass."""
    project_id, checkout_id = _sell("grc00011", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        billing.end_expired_grace(conn, _client(CancellingStripe()), grace_days=GRACE)

    assert _plan_code(project_id) == "pro", "the plan must not move here"
    with db.connection() as conn:
        assert subscriptions.entitled_plan_code(conn, project_id) == "free"
        assert len(subscriptions.pending_reconciliation(conn)) == 1


def test_the_customer_can_see_both_ends_of_it(placed_project):
    """A customer whose writes stopped should be able to find out why without
    asking support. Both transitions are allowlisted audit events."""
    project_id, checkout_id = _sell("grc00012", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)
    with db.connection() as conn:
        billing.end_expired_grace(conn, _client(CancellingStripe()), grace_days=GRACE)

    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT detail_json::text AS detail FROM audit_events "
            " WHERE project_id = %s AND event_type = %s ORDER BY created_at",
            (project_id, subscriptions.STATE_CHANGED),
        )

    transitions = " ".join(row["detail"] for row in rows)
    assert "past_due" in transitions
    assert "canceled" in transitions
    for forbidden in ("sub_", "cus_", "cs_test", "price_"):
        assert forbidden not in transitions


# -- the pass, in the order it runs ---------------------------------------


def test_the_grace_pass_defers_rather_than_failing_the_run(placed_project):
    """A deferral is not a failure. `cp-manage maintenance run` exits non-zero
    on failures, and a provider having a bad minute must not page anybody."""
    project_id, checkout_id = _sell("grc00013", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        result = maintenance.expire_billing_grace(
            conn, grace_days=GRACE, client=_client(CancellingStripe(unreachable=True))
        )
    assert result.failed == 0
    assert result.handled == 0
    assert any("deferred" in line for line in result.detail)


def test_grace_runs_before_reconciliation_so_one_run_finishes_the_job(placed_project):
    """Ordering, asserted rather than assumed. Grace expiring, the plan moving
    and the storage measurement all happen in one pass -- a customer downgraded
    over three separate runs is correct eventually and unreadable in the
    meantime."""
    project_id, checkout_id = _sell("grc00014", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        maintenance.expire_billing_grace(
            conn, grace_days=GRACE, client=_client(CancellingStripe())
        )
        # Which is exactly what `reconcile_subscriptions` picks up next.
        assert len(subscriptions.pending_reconciliation(conn)) == 1


# -- the criterion whose failure cannot be undone -------------------------


@requires_node
@requires_maludb_core
def test_the_end_of_grace_keeps_every_row_the_database_and_the_keys(
    paid_project, admin_conn, key_ring,  # noqa: F811
):
    """Acceptance criterion 4, end to end and on a real node.

    A paying customer's card fails. Fourteen days pass. The subscription ends,
    the project reverts to the free tier, direct access is revoked -- and the
    database, its tables, its rows, its `project_ref` and its API keys are all
    exactly where they were. The customer can still read, and can still delete
    their way back under the free quota.

    This is the test the slice exists for. Everything else here is about when;
    this is about what must never happen at all.
    """
    _plan("free", {"direct_database_access": False})
    _plan("grace-tier", {"direct_database_access": True})
    _map_price("grace-tier")

    project_id, names, _ = paid_project("grc00020", direct_access=True)
    _set_plan(project_id, "grace-tier")

    with psycopg.connect(_tenant_dsn_for(names.database)) as tenant:
        tenant.execute("CREATE TABLE IF NOT EXISTS receipts (id int, note text)")
        tenant.execute("INSERT INTO receipts VALUES (1, 'paid for this')")
        tenant.commit()

    with db.connection() as conn:
        # Minted explicitly, because the assertion below is that they survive
        # and a fixture that happened to create none would make it vacuous.
        api_keys.create(
            conn, project_id=project_id, key_type="publishable",
            pepper=TEST_PEPPER, key_ring=key_ring,
        )
        api_keys.create(
            conn, project_id=project_id, key_type="secret", pepper=TEST_PEPPER,
        )
        conn.commit()
        subscriptions.create(
            conn, project_id=project_id, plan_code="grace-tier",
            provider="stripe", provider_subscription_id="sub_grace",
        )
        subscriptions.record_state(conn, project_id=project_id, state="past_due")
        keys_before = db.query(
            conn,
            "SELECT id, key_type, key_identifier, revoked_at FROM api_keys "
            " WHERE project_id = %s ORDER BY id",
            (project_id,),
        )
        ref_before = db.one(
            conn, "SELECT project_ref, database_name FROM projects WHERE id = %s", (project_id,)
        )
    _past_due(project_id, days_ago=GRACE + 1)

    def connect(_conn, _node_id, _key_ring):
        return psycopg.connect(ADMIN_DSN), None

    with db.connection() as conn:
        ended = maintenance.expire_billing_grace(
            conn, grace_days=GRACE, client=_client(CancellingStripe())
        )
        assert ended.handled == 1, ended.detail
        applied = maintenance.reconcile_subscriptions(
            conn, key_ring=key_ring, connect_to_node=connect
        )
        assert applied.failed == 0, applied.detail

    # The plan is gone.
    assert _plan_code(project_id) == "free"
    assert not admin_conn.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (names.client,)
    ).fetchone()["rolcanlogin"], "direct access should end with the subscription"

    # And nothing else is.
    assert admin_conn.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (names.database,)
    ).fetchone(), "the tenant database must survive"

    with db.connection() as conn:
        ref_after = db.one(
            conn, "SELECT project_ref, database_name, deleted_at, status FROM projects "
            " WHERE id = %s", (project_id,)
        )
        keys_after = db.query(
            conn,
            "SELECT id, key_type, key_identifier, revoked_at FROM api_keys "
            " WHERE project_id = %s ORDER BY id",
            (project_id,),
        )
    assert ref_after["project_ref"] == ref_before["project_ref"]
    assert ref_after["database_name"] == ref_before["database_name"]
    assert ref_after["deleted_at"] is None
    assert keys_before, "the fixture should have provisioned API keys to compare"
    assert keys_after == keys_before, "API keys must survive a downgrade (ADR-006)"
    assert all(row["revoked_at"] is None for row in keys_after), (
        "a downgrade must not revoke the project's API keys -- the project keeps "
        "serving, on a smaller plan"
    )

    with psycopg.connect(_tenant_dsn_for(names.database)) as tenant:
        assert tenant.execute("SELECT note FROM receipts").fetchone()[0] == "paid for this"
        # And the customer can still shrink out of trouble, which is what makes
        # the restricted state recoverable rather than terminal (ADR-040).
        tenant.execute("DELETE FROM receipts")
        tenant.commit()
        assert tenant.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_nothing_in_the_grace_path_can_delete_a_project():
    """Criterion 4 asserted against the code rather than against a run.

    A test that exercised one path could pass while another path deleted
    something. What makes the criterion true is that no code in this path
    contains a statement that could -- so that is what is checked.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "services" / "control_plane"
    for name in ("billing.py", "subscriptions.py"):
        source = (root / name).read_text().lower()
        assert "drop database" not in source
        assert "delete from projects" not in source
        assert "drop table" not in source
        # `DELETE FROM billing_prices` is a mapping, not customer data, so the
        # check is narrowed to the tables that hold something a customer would
        # miss rather than to the word.
        for table in ("subscriptions", "api_keys", "audit_events", "project_credentials"):
            assert f"delete from {table}" not in source, f"{name} deletes from {table}"  # noqa: S608 - a substring check over source text, not a query


def test_the_time_module_is_not_what_decides(placed_project):
    """The clock is the database's `now()`, not the process's.

    A pass running on a node whose clock has drifted must not end somebody's
    grace period early, and `state_since` is compared in SQL for that reason.
    """
    project_id, checkout_id = _sell("grc00015", placed_project)
    _fail_payment(checkout_id, at=_ts(-60))
    _past_due(project_id, days_ago=GRACE - 0.5)

    with db.connection() as conn:
        assert subscriptions.in_expired_grace(conn, grace_days=GRACE) == []
    _past_due(project_id, days_ago=GRACE + 0.5)
    with db.connection() as conn:
        assert len(subscriptions.in_expired_grace(conn, grace_days=GRACE)) == 1


def test_a_failure_that_merely_mentions_404_is_not_read_as_already_gone():
    """The difference between "it is already cancelled" and "we could not tell".

    `is_missing` decides whether `end_expired_grace` goes on to revoke a
    customer's plan. Deciding it by looking for `404` in the message would also
    match a 500 whose text happened to contain it -- and a false "already gone"
    ends the plan while Stripe is still collecting for it, which is exactly the
    outcome ADR-051 exists to prevent.
    """
    from services.control_plane.stripe_api import StripeError, is_missing

    assert is_missing(StripeError("gone", status_code=404))
    assert not is_missing(StripeError("error 404 in upstream", status_code=500))
    assert not is_missing(StripeError("could not be reached", retryable=True))


def test_a_server_error_mentioning_404_still_defers(placed_project):
    """The same property one layer up, where it costs something."""
    import httpx

    class Confusing(CancellingStripe):
        def _handle(self, request):
            if request.method == "DELETE":
                return httpx.Response(
                    500, json={"error": {"message": "upstream returned 404 unexpectedly"}}
                )
            return FakeStripe._handle(self, request)

    project_id, checkout_id = _sell("grc00016", placed_project)
    _fail_payment(checkout_id, at=_ts(-1800))
    _past_due(project_id, days_ago=GRACE + 1)

    with db.connection() as conn:
        outcomes = billing.end_expired_grace(conn, _client(Confusing()), grace_days=GRACE)

    assert [o.outcome for o in outcomes] == ["deferred"]
    assert _state(project_id) == "past_due", "nothing may be taken away"
