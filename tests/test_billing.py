"""Taking money (Phase 09 slice 4, ADR-049 and ADR-053).

The properties under test are mostly refusals, and that is the shape the slice
asked for. A webhook endpoint is the one place in this platform where an
unauthenticated party on the internet can cause an entitlement to change, so
what matters is not that a payment grants a plan -- one test covers that -- but
that everything else does not.

Four controls, each tested where it can fail rather than where it is convenient:

1. **The signature, before parsing.** Including the case a hand-rolled
   verifier gets wrong: the MAC covers the *raw bytes*, so a body that has been
   through a JSON round trip must fail even though it means the same thing.
2. **The event id, claimed before the event is acted on.** A duplicate must
   change nothing -- asserted by counting what changed, not by reading a return
   value.
3. **`state_as_of`.** A stale `canceled` arriving after the `active` that
   superseded it must not downgrade a paying customer.
4. **The plan comes from a row the platform wrote.** A signed, well-formed
   event that asks for a plan nobody chose is refused. This is ADR-041, and it
   is the test that would fail against the tempting implementation -- the one
   that reads a plan code out of a payload.

And the property slice 3 exists for, re-asserted from the other side: **no path
through a webhook writes `projects.plan_id`.** Reconciliation is a separate
pass, in a separate process, with credentials this one does not have.

No test here reaches Stripe. The API is a `httpx.MockTransport`, and webhook
fixtures are signed with `stripe_api.sign` -- the same code path that verifies
them, so a change to one cannot silently pass the other.
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid

import httpx
import psycopg
import pytest

from services.control_plane import billing, db, maintenance, stripe_api, subscriptions
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_direct_sql import paid_project  # noqa: F401 - fixture
from tests.test_provisioning import ADMIN_DSN

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

TEST_KEY = "sk_test_" + "x" * 24  # noqa: S105 - shaped like a key, is not one
LIVE_KEY = "sk_live_" + "x" * 24  # noqa: S105 - same
WEBHOOK_SECRET = "whsec_" + "y" * 32  # noqa: S105 - same

PRICE = "price_pro_monthly"
ELIGIBLE = "txcd_10102000"  # PaaS, business use


# -- a Stripe that is not Stripe -------------------------------------------


class FakeStripe:
    """An `httpx` transport standing in for the API, recording what it was sent.

    Deliberately not a mock of the client. What is being tested includes the
    form encoding and the idempotency header, so the substitution has to happen
    below those -- a client-level fake would assert that this code calls a
    function it wrote itself.
    """

    def __init__(self, *, tax_code: str | None = ELIGIBLE, fail: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.tax_code = tax_code
        self.fail = fail
        self.session_counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500, json={"error": {"message": "Stripe is having a day"}})
        path = request.url.path
        if path == "/v1/checkout/sessions":
            self.session_counter += 1
            return httpx.Response(200, json={
                "id": f"cs_test_{self.session_counter}",
                "url": f"https://checkout.stripe.com/c/pay/cs_test_{self.session_counter}",
                "expires_at": int(time.time()) + 3600,
                "livemode": False,
            })
        if path.startswith("/v1/prices/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1],
                                             "product": "prod_maludb"})
        if path.startswith("/v1/products/"):
            body: dict = {"id": "prod_maludb"}
            if self.tax_code is not None:
                body["tax_code"] = self.tax_code
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"message": "no such thing"}})

    @property
    def last_form(self) -> dict[str, str]:
        raw = self.requests[-1].content.decode()
        out: dict[str, str] = {}
        for pair in raw.split("&"):
            key, _, value = pair.partition("=")
            out[_unquote(key)] = _unquote(value)
        return out


def _unquote(value: str) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(value)


def _client(fake: FakeStripe, *, key: str = TEST_KEY) -> stripe_api.Client:
    return stripe_api.Client(key, base_url="https://api.stripe.test",
                             transport=fake.transport())


# -- fixtures and helpers --------------------------------------------------


def _plan(code: str, config: dict | None = None) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, is_active, config_json) VALUES (%s, %s, TRUE, %s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json, "
            "                                 is_active = EXCLUDED.is_active",
            (code, code.title(), psycopg.types.json.Jsonb(config or {})),
        )
        conn.commit()


def _catalogue() -> None:
    _plan("free", {"direct_database_access": False})
    _plan("pro", {"direct_database_access": True})


def _map_price(plan: str = "pro", price: str = PRICE, *, livemode: bool = False) -> None:
    with db.connection() as conn:
        billing.set_price(conn, plan_code=plan, price_id=price, livemode=livemode,
                          tax_code=ELIGIBLE)


def _set_plan(project_id, code: str) -> None:
    """The raw UPDATE, for the same reason `test_subscriptions` gives: these
    need a project sitting on a plan with no node behind it."""
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET plan_id = (SELECT id FROM plans WHERE code = %s) WHERE id = %s",
            (code, project_id),
        )
        conn.commit()


def _plan_code(project_id) -> str:
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT pl.code FROM projects pr JOIN plans pl ON pl.id = pr.plan_id "
            " WHERE pr.id = %s",
            (project_id,),
        )["code"]


def _checkout_row(project_id) -> dict | None:
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT * FROM checkout_sessions WHERE project_id = %s "
            " ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )


def _event(
    event_type: str,
    obj: dict,
    *,
    event_id: str | None = None,
    created: int | None = None,
    livemode: bool = False,
) -> stripe_api.Event:
    return stripe_api.Event(
        id=event_id or f"evt_{uuid.uuid4().hex[:16]}",
        type=event_type,
        created=created or int(time.time()),
        livemode=livemode,
        obj=obj,
    )


def _subscription_obj(
    *,
    subscription_id: str = "sub_1",
    status: str = "active",
    price_id: str = PRICE,
    checkout_id: str | None = None,
    customer: str = "cus_1",
) -> dict:
    metadata = {"checkout_session_id": checkout_id} if checkout_id else {}
    return {
        "id": subscription_id,
        "status": status,
        "customer": customer,
        "metadata": metadata,
        "items": {"data": [{"price": {"id": price_id},
                            "current_period_start": 1_760_000_000,
                            "current_period_end": 1_762_000_000}]},
    }


def _handle(event: stripe_api.Event, *, livemode: bool = False) -> billing.Outcome:
    with db.connection() as conn:
        return billing.handle_event(conn, event, expected_livemode=livemode)


def _start(project_id, fake: FakeStripe, plan: str = "pro") -> billing.Checkout:
    with db.connection() as conn:
        return billing.start_checkout(
            conn, _client(fake), project_id=project_id, plan_code=plan,
            success_url="https://app.test/ok", cancel_url="https://app.test/no",
        )


# ==========================================================================
# 1. The signature, before anything is parsed
# ==========================================================================


def _signed(body: dict, *, secret: str = WEBHOOK_SECRET, at: int | None = None):
    payload = json.dumps(body).encode()
    moment = at if at is not None else int(time.time())
    return payload, stripe_api.sign(payload, secret=secret, timestamp=moment)


def test_a_valid_signature_parses_to_an_event():
    payload, header = _signed({"id": "evt_1", "type": "ping", "created": 1, "livemode": False,
                               "data": {"object": {"id": "x"}}})
    event = stripe_api.verify_and_parse(
        payload=payload, signature_header=header, secret=WEBHOOK_SECRET
    )
    assert event.id == "evt_1"
    assert event.obj == {"id": "x"}


def test_a_signature_from_another_secret_is_refused():
    payload, header = _signed({"id": "e", "type": "t", "created": 1, "data": {"object": {}}},
                              secret="whsec_" + "z" * 32)
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_and_parse(
            payload=payload, signature_header=header, secret=WEBHOOK_SECRET
        )


def test_a_tampered_body_is_refused():
    """The plan a customer gets is downstream of this, so flipping a byte must
    not survive."""
    payload, header = _signed({"id": "e", "type": "t", "created": 1, "data": {"object": {}}})
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_and_parse(
            payload=payload + b" ", signature_header=header, secret=WEBHOOK_SECRET
        )


def test_the_signature_covers_the_raw_bytes_not_the_parsed_value():
    """The mistake a hand-rolled verifier makes, asserted directly.

    A body that has been through `json.loads` and back means exactly the same
    thing and is a different string. If this passed, the route could safely bind
    a Pydantic model -- and the day Stripe changed its key order or spacing,
    every event would start failing verification in production.
    """
    body = {"id": "e", "type": "t", "created": 1, "data": {"object": {}}}
    payload, header = _signed(body)
    reserialised = json.dumps(body, indent=2).encode()
    assert reserialised != payload
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_and_parse(
            payload=reserialised, signature_header=header, secret=WEBHOOK_SECRET
        )


def test_a_stale_signature_is_refused_so_a_capture_cannot_replay_forever():
    payload, header = _signed({"id": "e", "type": "t", "created": 1, "data": {"object": {}}},
                              at=int(time.time()) - 4000)
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_and_parse(
            payload=payload, signature_header=header, secret=WEBHOOK_SECRET
        )


def test_a_second_signature_is_accepted_because_secrets_get_rotated():
    """Stripe sends several `v1` entries while an endpoint has two secrets. An
    implementation that read only the first would drop half the events during
    every rotation."""
    payload, header = _signed({"id": "e", "type": "t", "created": 1, "data": {"object": {}}})
    both = f"{header.split(',')[0]},v1=deadbeef,{header.split(',')[1]}"
    assert stripe_api.verify_and_parse(
        payload=payload, signature_header=both, secret=WEBHOOK_SECRET
    ).id == "e"


@pytest.mark.parametrize("header", ["", "t=1", "v1=abc", "garbage", "t=notanumber,v1=a"])
def test_a_malformed_signature_header_is_refused(header):
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_and_parse(
            payload=b"{}", signature_header=header, secret=WEBHOOK_SECRET
        )


def test_verification_without_a_secret_refuses_rather_than_passing():
    """Fail closed. An empty secret must not become an endpoint that accepts
    anything."""
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_and_parse(payload=b"{}", signature_header="t=1,v1=a", secret="")


# ==========================================================================
# 2. Mode, derived from the key rather than configured
# ==========================================================================


def test_live_mode_is_read_from_the_key_so_it_cannot_disagree_with_it():
    assert not stripe_api.Client(TEST_KEY).livemode
    assert stripe_api.Client(LIVE_KEY).livemode


# ==========================================================================
# 3. The price map
# ==========================================================================


def test_a_price_maps_to_a_plan_in_one_mode_only(db_pool):  # noqa: ARG001
    """A live event resolving through a test-mode row would sell a real
    customer a plan against a price nobody is charged for."""
    _catalogue()
    _map_price(livemode=False)
    with db.connection() as conn:
        assert billing.plan_for_price(conn, price_id=PRICE, livemode=False) == "pro"
        assert billing.plan_for_price(conn, price_id=PRICE, livemode=True) is None


def test_one_price_cannot_be_mapped_to_two_plans(db_pool):  # noqa: ARG001
    """Otherwise the plan a customer receives depends on row order."""
    _catalogue()
    _plan("team")
    _map_price("pro")
    with db.connection() as conn, pytest.raises(billing.BillingError):
        billing.set_price(conn, plan_code="team", price_id=PRICE, livemode=False)


def test_remapping_a_plan_to_a_new_price_replaces_the_old_one(db_pool):  # noqa: ARG001
    _catalogue()
    _map_price("pro", "price_old")
    _map_price("pro", "price_new")
    with db.connection() as conn:
        assert billing.price_for_plan(conn, plan_code="pro", livemode=False) == "price_new"
        assert billing.plan_for_price(conn, price_id="price_old", livemode=False) is None


def test_a_price_for_a_plan_that_does_not_exist_is_refused(db_pool):  # noqa: ARG001
    _catalogue()
    with db.connection() as conn, pytest.raises(billing.BillingError):
        billing.set_price(conn, plan_code="nonexistent", price_id="price_x", livemode=False)


def test_paid_plans_with_no_price_are_reported_because_nothing_else_says_so(db_pool):  # noqa: ARG001
    """A plan with no mapping cannot be bought and fails only when a customer
    tries."""
    _catalogue()
    _plan("team")
    _map_price("pro")
    with db.connection() as conn:
        missing = billing.unmapped_plans(conn, livemode=False)
    assert "team" in missing
    assert "pro" not in missing
    assert "free" not in missing, "the free plan is not sold and is not missing a price"


# -- the tax-code check ----------------------------------------------------


def test_an_eligible_tax_code_is_accepted():
    fake = FakeStripe(tax_code=ELIGIBLE)
    assert billing.verify_tax_code(_client(fake), PRICE) == ELIGIBLE


def test_an_ineligible_tax_code_is_refused_because_the_real_failure_is_silent():
    """ADR-049's sharpest edge. An ineligible product does not error at
    checkout -- the transaction leaves Managed Payments and this platform
    becomes the seller of record for it, acquiring the indirect-tax liability
    the whole choice was made to avoid. Nothing would say so."""
    fake = FakeStripe(tax_code="txcd_20030000")  # a physical-goods code
    with pytest.raises(billing.BillingError, match="not one this platform accepts"):
        billing.verify_tax_code(_client(fake), PRICE)


def test_a_product_with_no_tax_code_at_all_is_refused():
    fake = FakeStripe(tax_code=None)
    with pytest.raises(billing.BillingError, match="no tax code"):
        billing.verify_tax_code(_client(fake), PRICE)


# ==========================================================================
# 4. Starting a checkout
# ==========================================================================


def test_starting_a_checkout_records_what_it_is_for_before_returning_a_url(placed_project):
    """ADR-041 in its constructive form: the plan is decided here, by somebody
    the platform authenticated, and written down."""
    _catalogue()
    _map_price()
    project_id = placed_project("bill0001")
    _set_plan(project_id, "free")

    fake = FakeStripe()
    checkout = _start(project_id, fake)

    assert checkout.url.startswith("https://checkout.stripe.com/")
    row = _checkout_row(project_id)
    assert row["plan_code"] == "pro"
    assert row["state"] == "open"
    assert row["provider_session_id"] == "cs_test_1"
    assert not row["livemode"]


def test_the_checkout_call_is_a_subscription_with_managed_payments_on(placed_project):
    """ADR-049: hosted Checkout, `mode=subscription`, merchant of record on.
    Asserted against the wire form rather than against a call argument, because
    the encoding is the part that can be wrong."""
    _catalogue()
    _map_price()
    project_id = placed_project("bill0002")
    _set_plan(project_id, "free")

    fake = FakeStripe()
    _start(project_id, fake)

    form = fake.last_form
    assert form["mode"] == "subscription"
    assert form["managed_payments[enabled]"] == "true"
    assert form["line_items[0][price]"] == PRICE
    assert form["line_items[0][quantity]"] == "1"
    assert "Idempotency-Key" in fake.requests[-1].headers, (
        "without it a retried request opens a second checkout the customer can also pay"
    )
    assert fake.requests[-1].headers["Stripe-Version"] == stripe_api.API_VERSION


def test_the_free_plan_is_not_for_sale(placed_project):
    _catalogue()
    _map_price()
    project_id = placed_project("bill0003")
    _set_plan(project_id, "free")
    with pytest.raises(billing.BillingError, match="not sold"):
        _start(project_id, FakeStripe(), plan="free")


def test_a_plan_with_no_price_cannot_be_checked_out(placed_project):
    _catalogue()
    project_id = placed_project("bill0004")
    _set_plan(project_id, "free")
    with pytest.raises(billing.BillingError, match="no test-mode price"):
        _start(project_id, FakeStripe())


def test_a_project_that_already_has_a_subscription_cannot_start_another(placed_project):
    _catalogue()
    _map_price()
    project_id = placed_project("bill0005")
    _set_plan(project_id, "free")
    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro")
    with pytest.raises(billing.BillingError, match="already has a live subscription"):
        _start(project_id, FakeStripe())


def test_only_one_checkout_may_be_open_per_project(placed_project):
    """Without this a customer who opens the page twice can pay twice, and the
    second completion meets a project that is already sold -- refused
    correctly, but after the money moved."""
    _catalogue()
    _map_price()
    project_id = placed_project("bill0006")
    _set_plan(project_id, "free")

    fake = FakeStripe()
    _start(project_id, fake)
    with pytest.raises(billing.BillingError, match="already open"):
        _start(project_id, fake)


def test_a_stripe_failure_releases_the_open_slot(placed_project):
    """Otherwise a bad minute at Stripe locks the project out of checkout for
    an hour."""
    _catalogue()
    _map_price()
    project_id = placed_project("bill0007")
    _set_plan(project_id, "free")

    with pytest.raises(stripe_api.StripeError):
        _start(project_id, FakeStripe(fail=True))
    assert _checkout_row(project_id)["state"] == "expired"

    # And a second attempt is possible immediately.
    _start(project_id, FakeStripe())
    assert _checkout_row(project_id)["state"] == "open"


def test_expiring_stale_checkouts_frees_the_project(placed_project):
    _catalogue()
    _map_price()
    project_id = placed_project("bill0008")
    _set_plan(project_id, "free")
    _start(project_id, FakeStripe())

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE checkout_sessions SET expires_at = now() - interval '1 hour' "
            " WHERE project_id = %s",
            (project_id,),
        )
        conn.commit()
        assert billing.expire_stale_checkouts(conn) == 1
    assert _checkout_row(project_id)["state"] == "expired"


def test_starting_a_checkout_grants_nothing(placed_project):
    """The whole route is a URL. A customer who closes the Stripe page has
    changed nothing about their project."""
    _catalogue()
    _map_price()
    project_id = placed_project("bill0009")
    _set_plan(project_id, "free")
    _start(project_id, FakeStripe())

    assert _plan_code(project_id) == "free"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None


# ==========================================================================
# 5. Receiving events
# ==========================================================================


def _sold(ref: str, placed_project) -> tuple[uuid.UUID, str]:
    """A project with a checkout open, ready for the events that follow."""
    _catalogue()
    _map_price()
    project_id = placed_project(ref)
    _set_plan(project_id, "free")
    checkout = _start(project_id, FakeStripe())
    return project_id, str(checkout.id)


def test_a_completed_checkout_records_a_subscription_on_the_recorded_plan(placed_project):
    project_id, _ = _sold("bill0010", placed_project)

    outcome = _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "customer": "cus_1",
        "payment_status": "paid",
    }))

    assert outcome.outcome == "applied"
    with db.connection() as conn:
        subscription = subscriptions.for_project(conn, project_id)
    assert subscription.plan_code == "pro"
    assert subscription.state == "active"
    assert subscription.provider == "stripe"
    assert subscription.provider_subscription_id == "sub_1"
    assert _checkout_row(project_id)["state"] == "completed"


def test_a_completed_checkout_still_writes_no_entitlement(placed_project):
    """The property slice 3 exists for, from the other side. Between the
    payment and the maintenance pass the project is still on its old plan, and
    that is correct rather than a lag to be optimised away: applying a plan
    needs a node credential this process does not have (ADR-038)."""
    project_id, _ = _sold("bill0011", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }))
    assert _plan_code(project_id) == "free"


def test_a_completed_checkout_nobody_recorded_is_refused(placed_project):
    """ADR-041. A signed, well-formed event naming a session this platform
    never created has no row saying what it was for -- and the payload is not
    allowed to say."""
    _catalogue()
    _map_price()
    placed_project("bill0012")

    outcome = _handle(_event("checkout.session.completed", {
        "id": "cs_test_nobody_asked_for", "subscription": "sub_x", "payment_status": "paid",
    }))
    assert outcome.outcome == "refused"


def test_the_plan_comes_from_the_recorded_row_and_not_from_the_payload(placed_project):
    """The test that fails against the tempting implementation.

    The event carries a plan code in its metadata -- the shape a handler that
    trusted the payload would read -- and it names a plan that exists and is
    more expensive than the one bought. What the customer gets is what the
    checkout row says.
    """
    project_id, _ = _sold("bill0013", placed_project)
    _plan("enterprise", {"direct_database_access": True})

    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
        "metadata": {"plan_code": "enterprise", "plan": "enterprise"},
        "plan_code": "enterprise",
    }))

    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id).plan_code == "pro"


def test_a_subscription_event_whose_price_disagrees_with_the_checkout_is_refused(placed_project):
    """Two independent answers -- what a manager chose, and what Stripe says is
    being charged for. Neither is resolved in favour of the other, because
    resolving would be deciding."""
    project_id, checkout_id = _sold("bill0014", placed_project)
    _plan("team")
    _map_price("team", "price_team")

    outcome = _handle(_event("customer.subscription.created", _subscription_obj(
        price_id="price_team", checkout_id=checkout_id,
    )))

    assert outcome.outcome == "refused"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None


def test_a_subscription_on_an_unmapped_price_is_refused(placed_project):
    _, checkout_id = _sold("bill0015", placed_project)
    outcome = _handle(_event("customer.subscription.created", _subscription_obj(
        price_id="price_nobody_mapped", checkout_id=checkout_id,
    )))
    assert outcome.outcome == "refused"


def test_a_subscription_with_no_checkout_behind_it_is_refused(placed_project):
    _catalogue()
    _map_price()
    placed_project("bill0016")
    outcome = _handle(_event("customer.subscription.created", _subscription_obj()))
    assert outcome.outcome == "refused"


# -- idempotency and replay ------------------------------------------------


def test_a_duplicate_event_changes_nothing(placed_project):
    """Counted rather than trusted: the second delivery must not produce a
    second subscription, a second audit event, or a state change."""
    project_id, _ = _sold("bill0017", placed_project)
    payload = {"id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid"}

    first = _handle(_event("checkout.session.completed", payload, event_id="evt_same"))
    second = _handle(_event("checkout.session.completed", payload, event_id="evt_same"))

    assert first.outcome == "applied"
    assert second.outcome == "ignored"
    assert second.note == "duplicate delivery"

    with db.connection() as conn:
        rows = db.query(
            conn, "SELECT id FROM subscriptions WHERE project_id = %s", (project_id,)
        )
        audit = db.query(
            conn,
            "SELECT id FROM audit_events WHERE project_id = %s AND event_type = %s",
            (project_id, subscriptions.CREATED),
        )
    assert len(rows) == 1
    assert len(audit) == 1


def test_a_redelivery_under_a_new_event_id_does_not_open_a_second_subscription(placed_project):
    """The other half of idempotency, and the one the event ledger does not
    cover: Stripe re-sending the same *fact* as a new event."""
    project_id, _ = _sold("bill0018", placed_project)
    payload = {"id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid"}

    _handle(_event("checkout.session.completed", payload))
    second = _handle(_event("checkout.session.completed", payload))

    assert second.outcome == "ignored"
    with db.connection() as conn:
        rows = db.query(
            conn, "SELECT id FROM subscriptions WHERE project_id = %s", (project_id,)
        )
    assert len(rows) == 1


def test_every_event_is_recorded_even_when_it_is_refused(placed_project):
    """An endpoint that answers 200 to everything must be able to show what it
    declined, or a customer's missing upgrade has no trail at all."""
    _catalogue()
    placed_project("bill0019")
    _handle(_event("checkout.session.completed", {"id": "cs_nope"}, event_id="evt_refused"))

    with db.connection() as conn:
        row = db.one(
            conn, "SELECT outcome, note FROM billing_events WHERE event_id = %s",
            ("evt_refused",),
        )
    assert row["outcome"] == "refused"
    assert row["note"]


def test_an_unhandled_event_type_is_recorded_as_ignored(placed_project):  # noqa: ARG001
    outcome = _handle(_event("invoice.upcoming", {"id": "in_1"}))
    assert outcome.outcome == "ignored"


def test_an_event_from_the_wrong_mode_is_refused(placed_project):
    """A live event resolving through a test-mode price map would sell a real
    customer against a price nobody is charged for."""
    project_id, _ = _sold("bill0020", placed_project)
    outcome = _handle(
        _event("checkout.session.completed",
               {"id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid"},
               livemode=True),
        livemode=False,
    )
    assert outcome.outcome == "refused"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None


# -- state, and its ordering ----------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [("active", "active"), ("trialing", "trialing"), ("past_due", "past_due"),
     ("unpaid", "past_due"), ("paused", "past_due"), ("canceled", "canceled")],
)
def test_stripe_status_maps_onto_maludb_state(placed_project, status, expected):
    """Stripe's vocabulary stops here. `unpaid` and `paused` map to `past_due`
    rather than to a cancellation: both are the moment ADR-051's grace period
    should be starting, not ending."""
    ref = f"bs{status[:6]:0>8}"[:8]
    project_id, checkout_id = _sold(ref, placed_project)
    # Opened as whatever the checkout would actually have been. A *paid*
    # checkout opens `active`, and `active -> trialing` is refused on purpose:
    # a subscription does not go back into a trial, and the transition map is
    # what says so.
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1",
        "payment_status": "no_payment_required" if status == "trialing" else "paid",
    }))
    _handle(_event("customer.subscription.updated", _subscription_obj(
        status=status, checkout_id=checkout_id,
    )))

    with db.connection() as conn:
        row = db.one(
            conn, "SELECT state FROM subscriptions WHERE project_id = %s", (project_id,)
        )
    assert row["state"] == expected


def test_an_unrecognised_status_is_refused_rather_than_defaulted(placed_project):
    """Defaulting here is guessing about whether somebody has paid, and it is
    wrong in one of two expensive directions."""
    _, checkout_id = _sold("bill0021", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }))
    outcome = _handle(_event("customer.subscription.updated", _subscription_obj(
        status="something_stripe_added_last_week", checkout_id=checkout_id,
    )))
    assert outcome.outcome == "refused"


def test_a_stale_cancellation_does_not_downgrade_a_paying_customer(placed_project):
    """The risk `state_as_of` exists for, arriving the way it actually arrives:
    a `canceled` that was superseded, redelivered after the `active` that
    superseded it. Ordered by arrival this is a downgrade."""
    project_id, checkout_id = _sold("bill0022", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }, created=1_000))

    _handle(_event("customer.subscription.updated",
                   _subscription_obj(status="active", checkout_id=checkout_id),
                   created=3_000))
    stale = _handle(_event("customer.subscription.deleted",
                           _subscription_obj(status="canceled", checkout_id=checkout_id),
                           created=2_000))

    assert stale.outcome == "refused"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id).state == "active"


def test_a_cancellation_is_recorded_and_entitles_the_free_plan(placed_project):
    project_id, checkout_id = _sold("bill0023", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }, created=1_000))
    outcome = _handle(_event("customer.subscription.deleted",
                             _subscription_obj(status="canceled", checkout_id=checkout_id),
                             created=2_000))

    assert outcome.outcome == "applied"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None
        assert subscriptions.entitled_plan_code(conn, project_id) == "free"
    # And still nothing has been enforced.
    assert _plan_code(project_id) == "free"


def test_an_event_about_a_subscription_nobody_recorded_and_already_over_is_ignored(placed_project):  # noqa: ARG001
    """Recording it would create a dead row entitling nothing."""
    _catalogue()
    outcome = _handle(_event("customer.subscription.deleted",
                             _subscription_obj(status="canceled")))
    assert outcome.outcome == "ignored"


def test_a_trial_opens_trialing_rather_than_active(placed_project):
    """A paid checkout must never open `trialing` and a trial must never open
    `active`, because the transition map forbids going back and the
    subscription event that follows would be refused."""
    project_id, _ = _sold("bill0024", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "no_payment_required",
    }))
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id).state == "trialing"


# -- the events either way round ------------------------------------------


def test_a_subscription_event_arriving_first_still_opens_the_subscription(placed_project):
    """Stripe does not promise an order, and `customer.subscription.created`
    routinely beats `checkout.session.completed`. Resolving through the
    checkout row -- whose id the platform put in the subscription's metadata --
    is what makes either order work."""
    project_id, checkout_id = _sold("bill0025", placed_project)

    outcome = _handle(_event("customer.subscription.created", _subscription_obj(
        checkout_id=checkout_id,
    )))

    assert outcome.outcome == "applied"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id).plan_code == "pro"
    assert _checkout_row(project_id)["state"] == "completed"


def test_the_completed_checkout_arriving_second_does_not_duplicate_anything(placed_project):
    project_id, checkout_id = _sold("bill0026", placed_project)
    _handle(_event("customer.subscription.created",
                   _subscription_obj(checkout_id=checkout_id)))
    second = _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }))

    assert second.outcome == "ignored"
    with db.connection() as conn:
        rows = db.query(
            conn, "SELECT id FROM subscriptions WHERE project_id = %s", (project_id,)
        )
    assert len(rows) == 1


# ==========================================================================
# 6. What reaches a node, and when
# ==========================================================================


def test_a_recorded_payment_is_queued_for_reconciliation(placed_project):
    """The queue is a predicate over columns that already move, not a flag
    somebody has to remember to set -- so nothing can forget to enqueue."""
    project_id, _ = _sold("bill0027", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }))

    with db.connection() as conn:
        pending = subscriptions.pending_reconciliation(conn)
    assert [row["project_id"] for row in pending] == [project_id]


def test_marking_it_reconciled_takes_it_off_the_queue(placed_project):
    project_id, _ = _sold("bill0028", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }))

    with db.connection() as conn:
        row = subscriptions.pending_reconciliation(conn)[0]
        subscriptions.mark_reconciled(
            conn, subscription_id=row["id"], state=row["state"], plan_code=row["plan_code"]
        )
        assert subscriptions.pending_reconciliation(conn) == []

    # And a later event puts it back, because the fact changed again -- in the
    # same wall-clock second as the first, which is the case a queue keyed on
    # the provider's timestamp misses entirely.
    _handle(_event("customer.subscription.updated", _subscription_obj(status="past_due")))
    with db.connection() as conn:
        assert len(subscriptions.pending_reconciliation(conn)) == 1
    assert _plan_code(project_id) == "free", "still nothing applied"


def test_a_deleted_project_is_not_queued(placed_project):
    """Reconciling one would be work that can only fail."""
    project_id, _ = _sold("bill0029", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid",
    }))
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status = 'DELETING' WHERE id = %s", (project_id,))
        conn.commit()
        assert subscriptions.pending_reconciliation(conn) == []


# ==========================================================================
# 7. Cross-tenant, and what the audit trail may say
# ==========================================================================


def test_a_checkout_cannot_name_one_org_and_another_orgs_project(placed_project):
    """The composite foreign key `subscriptions` uses, for the same reason: two
    independent references would permit it, and it is a cross-tenant control
    rather than a typo."""
    _catalogue()
    victim = placed_project("bill0030")
    attacker = placed_project("bill0031")

    with db.connection() as conn:
        attacker_org = db.one(
            conn, "SELECT org_id FROM projects WHERE id = %s", (attacker,)
        )["org_id"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute(
                conn,
                "INSERT INTO checkout_sessions (id, org_id, project_id, plan_code, provider, "
                "                               livemode, provider_session_id, expires_at) "
                "VALUES (%s, %s, %s, 'pro', 'stripe', FALSE, 'cs_evil', now() + interval '1 hour')",
                (uuid.uuid4(), attacker_org, victim),
            )
        conn.rollback()


def test_the_audit_trail_carries_no_billing_identifiers(placed_project):
    """The promise `subscriptions._audit` made and warned this module would be
    tempted to break, now that there is a Stripe payload in scope. A customer
    needs to know what happened to their project; the audit table is not a
    second copy of a billing record."""
    project_id, _ = _sold("bill0032", placed_project)
    _handle(_event("checkout.session.completed", {
        "id": "cs_test_1", "subscription": "sub_1", "customer": "cus_secret",
        "payment_status": "paid", "amount_total": 2500, "currency": "usd",
    }))

    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT event_type, detail_json::text AS detail FROM audit_events "
            " WHERE project_id = %s",
            (project_id,),
        )

    assert rows, "the checkout and the subscription should both be recorded"
    for row in rows:
        blob = row["detail"]
        for forbidden in ("cus_", "sub_", "cs_test", "price_", "2500", "usd"):
            assert forbidden not in blob, f"{row['event_type']} leaked {forbidden}"


# ==========================================================================
# 8. The routes
# ==========================================================================


@pytest.fixture
def billing_client(app_config, db_pool):  # noqa: ARG001
    """An application configured to take money, with Stripe replaced."""
    from fastapi.testclient import TestClient

    from services.control_plane.main import create_public_app

    configured = dataclasses.replace(
        app_config,
        stripe_secret_key=TEST_KEY,
        stripe_webhook_secret=WEBHOOK_SECRET,
        stripe_api_base="https://api.stripe.test",
    )
    app = create_public_app(configured)
    with TestClient(app) as test_client:
        yield test_client


def _webhook(client, body: dict, *, secret: str = WEBHOOK_SECRET, at: int | None = None):
    payload, header = _signed(body, secret=secret, at=at)
    return client.post(
        "/webhooks/stripe", content=payload,
        headers={"stripe-signature": header, "content-type": "application/json"},
    )


def test_the_webhook_refuses_an_unsigned_body(billing_client):
    response = billing_client.post("/webhooks/stripe", content=b'{"id":"evt_x"}')
    assert response.status_code == 400
    with db.connection() as conn:
        assert db.query(conn, "SELECT id FROM billing_events") == []


def test_the_webhook_refuses_a_body_signed_with_the_wrong_secret(billing_client):
    response = _webhook(
        billing_client,
        {"id": "evt_forged", "type": "checkout.session.completed", "created": int(time.time()),
         "livemode": False, "data": {"object": {"id": "cs_test_1", "subscription": "sub_1"}}},
        secret="whsec_" + "0" * 32,
    )
    assert response.status_code == 400
    with db.connection() as conn:
        assert db.one(
            conn, "SELECT id FROM billing_events WHERE event_id = %s", ("evt_forged",)
        ) is None, "an unverified event must not reach the ledger"


def test_the_webhook_refusal_says_nothing_about_why(billing_client):
    """A caller who cannot produce a valid signature learns that the signature
    was wrong and nothing else -- not whether the timestamp was stale, not
    whether they have the right secret for the wrong endpoint."""
    stale = _webhook(billing_client, {"id": "e", "type": "t", "created": 1,
                                      "data": {"object": {}}}, at=int(time.time()) - 9999)
    wrong = _webhook(billing_client, {"id": "e", "type": "t", "created": 1,
                                      "data": {"object": {}}}, secret="whsec_" + "1" * 32)
    assert stale.status_code == wrong.status_code == 400
    assert stale.json() == wrong.json()


def test_a_refusal_still_answers_200_so_stripe_does_not_disable_the_endpoint(billing_client):
    """A retry cannot fix an unknown session, and days of redelivery ends with
    Stripe disabling the endpoint -- taking the events that would have worked."""
    response = _webhook(billing_client, {
        "id": "evt_unknown", "type": "checkout.session.completed",
        "created": int(time.time()), "livemode": False,
        "data": {"object": {"id": "cs_never_created", "subscription": "sub_z"}},
    })
    assert response.status_code == 200
    with db.connection() as conn:
        assert db.one(
            conn, "SELECT outcome FROM billing_events WHERE event_id = %s", ("evt_unknown",)
        )["outcome"] == "refused"


def test_the_webhook_says_it_cannot_check_when_no_secret_is_configured(app_config, db_pool):  # noqa: ARG001
    """503 rather than 400: the event may well have been valid, and Stripe
    should retry once somebody fixes the configuration."""
    from fastapi.testclient import TestClient

    from services.control_plane.main import create_public_app

    with TestClient(create_public_app(app_config)) as client:
        response = client.post("/webhooks/stripe", content=b"{}",
                               headers={"stripe-signature": "t=1,v1=a"})
    assert response.status_code == 503


def test_an_unauthenticated_caller_cannot_start_a_checkout(billing_client):
    assert billing_client.post(
        "/v1/projects/anyref/billing/checkout", json={"plan_code": "pro"}
    ).status_code == 401


def test_a_non_member_cannot_tell_the_project_exists(billing_client, placed_project):
    """404 rather than 403: a project ref is the customer's API subdomain."""
    _catalogue()
    _map_price()
    placed_project("bill0033")

    billing_client.post(
        "/v1/auth/signup",
        json={"email": "bill-outsider@example.com", "password": TEST_CREDENTIAL},
    )
    token = billing_client.post(
        "/v1/auth/signin",
        json={"email": "bill-outsider@example.com", "password": TEST_CREDENTIAL},
    ).json()["token"]

    response = billing_client.post(
        "/v1/projects/bill0033/billing/checkout",
        json={"plan_code": "pro"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_billing_is_503_when_the_deployment_has_no_key(app_config, db_pool, placed_project):  # noqa: ARG001
    """A deployment that is not selling yet is a real deployment, and it serves
    every other route."""
    from fastapi.testclient import TestClient

    from services.control_plane.main import create_public_app

    _catalogue()
    placed_project("bill0034")
    with TestClient(create_public_app(app_config)) as client:
        token = client.post(
            "/v1/auth/signin",
            json={"email": "bill0034@example.com", "password": TEST_CREDENTIAL},
        ).json()["token"]
        response = client.post(
            "/v1/projects/bill0034/billing/checkout",
            json={"plan_code": "pro"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503


# ==========================================================================
# 9. The whole path, on a real node
# ==========================================================================


@requires_node
def test_a_payment_reaches_the_node_only_when_the_maintenance_pass_runs(
    paid_project, admin_conn, key_ring,  # noqa: F811
):
    """The slice end to end, and the two halves of ADR-053 in one assertion.

    A customer pays. The webhook -- which runs in the process bound to the
    internet and holds no node credential -- records it and changes nothing on
    the node. Then the maintenance pass, which runs where those credentials
    live, applies it.

    The `rolcanlogin` check is the part that matters: `direct_database_access`
    is the entitlement Phase 09 exists to deliver, it is enforced by an
    attribute on a role, and nothing before the pass can set it.
    """
    _plan("free", {"direct_database_access": False})
    _plan("paid-tier", {"direct_database_access": True})
    _map_price("paid-tier")

    project_id, names, _ = paid_project("bill0040", direct_access=False)
    _set_plan(project_id, "free")

    checkout = _start(project_id, FakeStripe(), plan="paid-tier")
    _handle(_event("customer.subscription.created", _subscription_obj(
        checkout_id=str(checkout.id),
    )))

    # Recorded, and nothing has happened to the project or the node.
    assert _plan_code(project_id) == "free"
    assert not admin_conn.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (names.client,)
    ).fetchone()["rolcanlogin"]

    def connect(_conn, _node_id, _key_ring):
        return psycopg.connect(ADMIN_DSN), None

    with db.connection() as conn:
        result = maintenance.reconcile_subscriptions(
            conn, key_ring=key_ring, connect_to_node=connect
        )

    assert result.failed == 0, result.detail
    assert result.handled == 1
    assert _plan_code(project_id) == "paid-tier"
    assert admin_conn.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (names.client,)
    ).fetchone()["rolcanlogin"], "the pass did not grant what was paid for"

    # And it drains: a second run has nothing to do.
    with db.connection() as conn:
        assert subscriptions.pending_reconciliation(conn) == []
        assert maintenance.reconcile_subscriptions(
            conn, key_ring=key_ring, connect_to_node=connect
        ).handled == 0


# ==========================================================================
# 10. What slice 4's own security review found
# ==========================================================================


def test_a_checkout_cannot_open_a_second_subscription_after_the_first_is_gone(placed_project):
    """A paid plan must be granted by a payment, not by a replayed fact.

    A subscription event resolves its project through the checkout id the
    platform put in Stripe's metadata. Once the first subscription is canceled
    the project has nothing live to refuse a second, so without the
    one-checkout-one-subscription index an event replaying the old checkout id
    would open a new subscription on the plan that checkout bought.

    Reaching this needs a valid signature, so it is not a hole an outsider
    walks through. It is the class of thing worth making impossible rather than
    unlikely, and the schema is where that is cheap.
    """
    project_id, checkout_id = _sold("bill0041", placed_project)
    _handle(_event("customer.subscription.created",
                   _subscription_obj(checkout_id=checkout_id), created=1_000))
    _handle(_event("customer.subscription.deleted",
                   _subscription_obj(status="canceled", checkout_id=checkout_id),
                   created=2_000))
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None

    replayed = _handle(_event("customer.subscription.created", _subscription_obj(
        subscription_id="sub_replayed", checkout_id=checkout_id,
    ), created=3_000))

    assert replayed.outcome == "refused"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None
        assert subscriptions.entitled_plan_code(conn, project_id) == "free"


def test_the_webhook_refuses_a_body_larger_than_any_real_event(billing_client):
    """The signature cannot be checked until the body has been read, so without
    a cap an unauthenticated caller chooses how much memory this process
    buffers. Nothing else in the stack bounds it."""
    from services.control_plane.api import billing as billing_api

    oversized = b'{"padding":"' + b"A" * (billing_api.MAX_WEBHOOK_BODY + 1024) + b'"}'
    response = billing_client.post(
        "/webhooks/stripe", content=oversized,
        headers={"stripe-signature": "t=1,v1=abc", "content-type": "application/json"},
    )
    assert response.status_code == 413
    with db.connection() as conn:
        assert db.query(conn, "SELECT id FROM billing_events") == []


def test_a_body_that_lies_about_its_length_is_still_capped(billing_client):
    """`Content-Length` is a claim, and a chunked request makes none at all --
    so a check on the header alone is a check the caller opts out of."""
    from services.control_plane.api import billing as billing_api

    def chunks():
        for _ in range(8):
            yield b"B" * (billing_api.MAX_WEBHOOK_BODY // 4)

    response = billing_client.post(
        "/webhooks/stripe", content=chunks(),
        headers={"stripe-signature": "t=1,v1=abc", "content-type": "application/json"},
    )
    assert response.status_code == 413


def test_an_ordinary_event_is_well_under_the_cap(billing_client):
    """The cap must not be the thing that breaks billing."""
    response = _webhook(billing_client, {
        "id": "evt_size", "type": "invoice.upcoming", "created": int(time.time()),
        "livemode": False, "data": {"object": {"id": "in_1"}},
    })
    assert response.status_code == 200


def test_an_event_left_unfinished_is_retried_rather_than_lost(placed_project):
    """The failure the claim-before-acting design creates, and its lease.

    Claiming the event id before acting is what makes duplicates and concurrent
    deliveries safe. The cost is that a handler dying in between leaves a row at
    `received` -- and Stripe's redelivery, the very thing that would fix it,
    would be turned away as a duplicate. So the event would be lost for good, in
    the ledger built to guarantee it is not.
    """
    project_id, _ = _sold("bill0042", placed_project)
    payload = {"id": "cs_test_1", "subscription": "sub_1", "payment_status": "paid"}

    # A handler that claimed the event and then died.
    with db.connection() as conn:
        billing._claim(conn, _event("checkout.session.completed", payload,
                                    event_id="evt_stalled"), provider="stripe")

    # The immediate redelivery is still a duplicate: a slow handler must not
    # have its work done twice underneath it.
    assert _handle(_event("checkout.session.completed", payload,
                          event_id="evt_stalled")).outcome == "ignored"

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE billing_events SET received_at = now() - interval '1 hour' "
            " WHERE event_id = %s",
            ("evt_stalled",),
        )
        conn.commit()

    recovered = _handle(_event("checkout.session.completed", payload,
                               event_id="evt_stalled"))
    assert recovered.outcome == "applied"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id).plan_code == "pro"
        rows = db.query(
            conn, "SELECT id FROM billing_events WHERE event_id = %s", ("evt_stalled",)
        )
    assert len(rows) == 1, "the retry must take over the row, not add another"


@pytest.mark.parametrize(
    ("key", "live"),
    [(TEST_KEY, False), (LIVE_KEY, True), ("rk_test_abc", False), ("", False)],
)
def test_absent_configuration_fails_towards_refusing_money(key, live):
    """`livemode_of("")` must be False.

    A naive "does the key contain `_test_`" answers *live* for an empty string,
    and that is the one case it decides anything: a deployment holding a webhook
    secret and no API key would accept live events and resolve them through
    whatever price map it had. Absent configuration fails towards refusing
    money, never towards taking it.
    """
    assert stripe_api.livemode_of(key) is live
