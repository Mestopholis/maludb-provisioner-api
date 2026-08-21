"""Taking money, in MaluDB's vocabulary (Phase 09 slice 4, ADR-049).

This module is the middle of three layers and the only one that has to be read
carefully. `stripe_api` holds Stripe's protocol and nothing else; `subscriptions`
holds what has been paid for and cannot reach a node; this sits between them and
decides what an event *means*.

Four controls do the work, and they are separable on purpose -- none of them is
trusted to be the only one:

1. **The signature, before anything is parsed.** `api/billing.py` hands raw
   bytes to `stripe_api.verify_and_parse`, which refuses an unverified body
   before a parsed object exists. Nothing here ever sees an unsigned payload.
2. **The event id, inserted before the event is acted on.** Idempotency and
   replay refusal are the same unique constraint, and it is claimed *first*, so
   two concurrent deliveries of one event cannot both proceed. A check-then-act
   would let both through, which for a webhook endpoint is the normal case
   rather than the unlucky one.
3. **`state_as_of`, from the provider's own timestamp.** ADR-048 built this in
   slice 3 for the risk that arrives here: providers retry and deliver out of
   order, and ordered by arrival a stale `canceled` downgrades a paying
   customer.
4. **The plan comes from a row the platform wrote, never from the payload.**
   This is ADR-041 -- a value the customer influences cannot be the control --
   and it is why `checkout_sessions` exists. What a customer buys is decided by
   an authenticated manager before the customer ever reaches Stripe. The price
   id in the event is resolved through the platform's own map and required to
   *agree* with that row; disagreement is refused rather than reconciled.

**Nothing here reconciles.** Applying a plan needs a node's superuser
credential, and ADR-038 keeps those out of the process bound to the internet --
which the webhook endpoint must be, because Stripe posts from the internet. So
this records the billing fact and the maintenance pass, running where node
credentials live, is what makes it true. See ADR-053.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from services.control_plane import db, models, stripe_api, subscriptions

log = logging.getLogger(__name__)

CHECKOUT_STARTED = "project.billing.checkout_started"

#: Events acted on. Everything else Stripe sends is recorded as `ignored`,
#: which is a deliberate outcome rather than a silence: an endpoint subscribed
#: to more than it handles should be able to show that it looked.
HANDLED: frozenset[str] = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)

#: How long a checkout may stay open before the platform stops expecting it.
#: Stripe expires its own sessions after 24 hours; this is the platform's view,
#: and it is shorter so that a customer who abandoned a checkout can start
#: another one without waiting a day.
CHECKOUT_TTL_MINUTES = 60

#: How long an event may sit unfinished before another delivery may take it on.
#:
#: An event is claimed *before* it is acted on, which is what makes duplicates
#: and concurrent deliveries safe. The cost is that a handler dying between the
#: claim and the outcome leaves a row nothing will ever finish -- and the
#: redelivery Stripe is about to send would be turned away as a duplicate. So a
#: row still at `received` after this long can be taken by a later delivery,
#: which is a lease rather than a retry: the conditional UPDATE means exactly
#: one taker, even if several arrive together.
#:
#: Long enough that it cannot fire while a handler is merely slow -- the
#: handler's own work is a few statements against the control plane, and
#: nothing in it waits on a network.
STALLED_EVENT_MINUTES = 5


class BillingError(RuntimeError):
    """A refusal safe to show a customer or an operator."""


@dataclass(frozen=True)
class Checkout:
    id: uuid.UUID
    url: str
    plan_code: str
    expires_at: datetime


@dataclass(frozen=True)
class Outcome:
    """What happened to one event, and what gets written to `billing_events`."""

    outcome: str
    note: str
    project_id: uuid.UUID | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("applied", "ignored")


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(unix: int | None) -> datetime | None:
    return datetime.fromtimestamp(unix, tz=UTC) if unix else None


# -- the price map ---------------------------------------------------------


def set_price(
    conn: psycopg.Connection,
    *,
    plan_code: str,
    price_id: str,
    livemode: bool,
    tax_code: str | None = None,
    provider: str = stripe_api.PROVIDER,
) -> None:
    """Map a plan to a provider price. Upsert, because operators re-run things.

    The plan must exist in the catalogue: a mapping to a plan nothing offers
    would resolve at checkout and fail at reconciliation, which is the worst
    place to discover it because the money has already moved.
    """
    plan = models.plan_by_code(conn, plan_code)
    if plan is None:
        raise BillingError(f"no active plan with code {plan_code!r}")
    if not price_id.strip():
        raise BillingError("a price id is required")

    try:
        db.execute(
            conn,
            """
            INSERT INTO billing_prices (id, provider, livemode, plan_code, price_id, tax_code)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider, livemode, plan_code) DO UPDATE
                SET price_id = EXCLUDED.price_id,
                    tax_code = EXCLUDED.tax_code,
                    updated_at = now()
            """,
            (uuid.uuid4(), provider, livemode, plan.code, price_id.strip(), tax_code),
        )
        conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        # The other unique constraint: this price id is already mapped to a
        # different plan. Allowing it would make the plan a customer receives
        # depend on which row a query happened to return.
        conn.rollback()
        raise BillingError(
            f"price {price_id} is already mapped to another plan; "
            "remove that mapping first"
        ) from exc


def remove_price(
    conn: psycopg.Connection, *, plan_code: str, livemode: bool,
    provider: str = stripe_api.PROVIDER,
) -> bool:
    removed = db.execute(
        conn,
        "DELETE FROM billing_prices WHERE provider = %s AND livemode = %s AND plan_code = %s",
        (provider, livemode, plan_code),
    )
    conn.commit()
    return removed > 0


def prices(
    conn: psycopg.Connection, *, livemode: bool | None = None,
    provider: str = stripe_api.PROVIDER,
) -> list[dict]:
    if livemode is None:
        return db.query(
            conn,
            "SELECT plan_code, price_id, livemode, tax_code, updated_at FROM billing_prices "
            " WHERE provider = %s ORDER BY livemode, plan_code",
            (provider,),
        )
    return db.query(
        conn,
        "SELECT plan_code, price_id, livemode, tax_code, updated_at FROM billing_prices "
        " WHERE provider = %s AND livemode = %s ORDER BY plan_code",
        (provider, livemode),
    )


def price_for_plan(
    conn: psycopg.Connection, *, plan_code: str, livemode: bool,
    provider: str = stripe_api.PROVIDER,
) -> str | None:
    row = db.one(
        conn,
        "SELECT price_id FROM billing_prices "
        " WHERE provider = %s AND livemode = %s AND plan_code = %s",
        (provider, livemode, plan_code),
    )
    return row["price_id"] if row else None


def plan_for_price(
    conn: psycopg.Connection, *, price_id: str, livemode: bool,
    provider: str = stripe_api.PROVIDER,
) -> str | None:
    """The plan a provider price entitles, or None if the platform has no map.

    The direction a webhook resolves in, and the reason migration 0021 makes
    `(provider, livemode, price_id)` unique: two rows for one price id would
    make this a coin toss over what somebody bought.

    `livemode` is part of the key rather than an afterthought. A live event
    resolving through a test-mode mapping would sell a real customer a plan
    against a price nobody is charged for.
    """
    row = db.one(
        conn,
        "SELECT plan_code FROM billing_prices "
        " WHERE provider = %s AND livemode = %s AND price_id = %s",
        (provider, livemode, price_id),
    )
    return row["plan_code"] if row else None


def verify_tax_code(client: stripe_api.Client, price_id: str) -> str:
    """Confirm the product behind a price is eligible for Managed Payments.

    **The failure this prevents is silent, which is why it is a refusal.** An
    ineligible product does not error at checkout: Stripe drops that transaction
    out of Managed Payments and MaluDB becomes the seller of record for it --
    acquiring exactly the indirect-tax liability ADR-049 chose Managed Payments
    to avoid, with no error, on one plan, discoverable only by reconciling a tax
    return months later.

    Called when a price is registered rather than at checkout: it is an
    operator's action, a network round trip is acceptable there, and a wrong
    answer is cheap to correct before anything has been sold.
    """
    price = client.get_price(price_id)
    product_id = price.get("product")
    if not isinstance(product_id, str) or not product_id:
        raise BillingError(f"price {price_id} has no product to read a tax code from")
    product = client.get_product(product_id)
    tax_code = product.get("tax_code")
    # Stripe returns the tax code either as an id or as an expanded object.
    if isinstance(tax_code, dict):
        tax_code = tax_code.get("id")
    if not isinstance(tax_code, str) or not tax_code:
        raise BillingError(
            f"the Stripe product behind {price_id} has no tax code. Managed "
            "Payments requires one, and a product without it falls back to "
            "MaluDB being the seller of record"
        )
    if tax_code not in stripe_api.ELIGIBLE_TAX_CODES:
        raise BillingError(
            f"tax code {tax_code} is not one this platform accepts for Managed "
            f"Payments. Expected one of: {', '.join(sorted(stripe_api.ELIGIBLE_TAX_CODES))}"
        )
    return tax_code


# -- starting a checkout ---------------------------------------------------


def start_checkout(
    conn: psycopg.Connection,
    client: stripe_api.Client,
    *,
    project_id: uuid.UUID,
    plan_code: str,
    success_url: str,
    cancel_url: str,
    actor_user_id: uuid.UUID | None = None,
    customer_email: str | None = None,
    provider: str = stripe_api.PROVIDER,
) -> Checkout:
    """Open a hosted Checkout Session for one project on one plan.

    **The row is written before the URL is returned**, and it is written with a
    placeholder session id so that migration 0021's `one open per project` index
    does the work under concurrency: two requests racing to start a checkout
    meet the index, and one of them is refused before any money can move. A
    design that called Stripe first and recorded afterwards would let both
    through and discover the duplicate when the second payment arrived.

    The metadata carried to Stripe -- the checkout row's id -- comes back on the
    subscription events. It is a **correlation key into a row the platform
    wrote**, not a claim to be believed: what it resolves to is the plan an
    authenticated manager chose here.
    """
    project = _project(conn, project_id)
    plan = models.plan_by_code(conn, plan_code)
    if plan is None:
        raise BillingError(f"no active plan with code {plan_code!r}")

    default = models.default_plan(conn)
    if default is not None and plan.code == default.code:
        raise BillingError(
            "the free plan is not sold; cancel the subscription instead"
        )
    if project["plan_code"] == plan.code:
        raise BillingError(f"this project is already on {plan.code}")
    if subscriptions.for_project(conn, project_id) is not None:
        raise BillingError(
            "this project already has a live subscription; change it in the "
            "billing portal rather than starting a second checkout"
        )

    price_id = price_for_plan(conn, plan_code=plan.code, livemode=client.livemode, provider=provider)
    if price_id is None:
        mode = "live" if client.livemode else "test"
        raise BillingError(
            f"no {mode}-mode price is mapped for plan {plan.code!r}; "
            f"an operator must run `cp-manage billing price set`"
        )

    row_id = uuid.uuid4()
    expires_at = _now() + timedelta(minutes=CHECKOUT_TTL_MINUTES)
    try:
        db.execute(
            conn,
            "INSERT INTO checkout_sessions (id, org_id, project_id, plan_code, provider, "
            "                               livemode, provider_session_id, created_by, "
            "                               expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (row_id, project["org_id"], project_id, plan.code, provider,
             client.livemode, f"pending:{row_id}", actor_user_id, expires_at),
        )
        conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        conn.rollback()
        raise BillingError(
            "a checkout is already open for this project; finish it or wait for "
            "it to expire"
        ) from exc

    try:
        session = client.create_checkout_session(
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(project_id),
            # The platform's own row id. Stripe replays the original response
            # for 24 hours against it, so a retried request returns the session
            # that already exists rather than opening a second one.
            idempotency_key=str(row_id),
            customer_email=customer_email,
            metadata={"checkout_session_id": str(row_id), "project_id": str(project_id)},
        )
    except stripe_api.StripeError:
        # Release the open slot. Leaving it would lock the project out of
        # checkout for an hour because Stripe had a bad minute.
        _close_checkout(conn, row_id, "expired")
        raise

    if session.livemode != client.livemode:
        # Cannot happen with a correctly configured key, and if it ever does the
        # mapping the plan was resolved through belongs to the other mode.
        _close_checkout(conn, row_id, "expired")
        raise BillingError("Stripe returned a session in the wrong mode")

    session_expiry = _moment(session.expires_at) or expires_at
    db.execute(
        conn,
        "UPDATE checkout_sessions SET provider_session_id = %s, expires_at = %s, "
        "       updated_at = now() WHERE id = %s",
        (session.id, session_expiry, row_id),
    )
    _audit(conn, project_id, CHECKOUT_STARTED, actor_user_id, {"plan": plan.code})
    conn.commit()

    return Checkout(id=row_id, url=session.url, plan_code=plan.code,
                    expires_at=session_expiry)


def expire_stale_checkouts(conn: psycopg.Connection) -> int:
    """Close checkouts nobody completed, so the project can start another.

    Time-based rather than event-based on purpose: Stripe does send
    `checkout.session.expired`, but relying on it means a project whose event
    was lost stays locked out of checkout forever.
    """
    closed = db.execute(
        conn,
        "UPDATE checkout_sessions SET state = 'expired', updated_at = now() "
        " WHERE state = 'open' AND expires_at < now()",
    )
    conn.commit()
    return closed


# -- receiving events ------------------------------------------------------


def handle_event(
    conn: psycopg.Connection,
    event: stripe_api.Event,
    *,
    expected_livemode: bool,
    provider: str = stripe_api.PROVIDER,
) -> Outcome:
    """Record a verified event and act on it, exactly once.

    The event id is claimed **before** the event is acted on, and that ordering
    is the whole of both idempotency and replay refusal. A duplicate delivery
    loses the insert and returns without touching anything; two simultaneous
    deliveries of the same event cannot both proceed, because only one insert
    survives.

    Errors are caught and recorded rather than raised. An endpoint that answers
    5xx makes Stripe retry, and a retry cannot fix a malformed event, an unknown
    price, or a project that has been deleted -- it just delivers the same
    failure every few hours until Stripe gives up and disables the endpoint,
    taking the events that *would* have worked with it.
    """
    event_row = _claim(conn, event, provider=provider)
    if event_row is None:
        return Outcome("ignored", "duplicate delivery")

    if event.livemode != expected_livemode:
        # A test-mode event reaching a live deployment, or the reverse. The
        # signature already proves it came from Stripe, so this is a
        # misconfiguration rather than an attack -- but acting on it would
        # resolve prices through the wrong half of the mapping table.
        return _finish(conn, event_row, Outcome(
            "refused",
            f"event is {'live' if event.livemode else 'test'} mode and this "
            f"deployment is {'live' if expected_livemode else 'test'} mode",
        ))

    if event.type not in HANDLED:
        return _finish(conn, event_row, Outcome("ignored", "event type not handled"))

    try:
        if event.type == "checkout.session.completed":
            outcome = _on_checkout_completed(conn, event, provider=provider)
        else:
            outcome = _on_subscription_event(conn, event, provider=provider)
    except (BillingError, subscriptions.SubscriptionError) as exc:
        conn.rollback()
        outcome = Outcome("refused", str(exc)[:500])
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed; see docstring
        conn.rollback()
        log.exception("billing event %s failed", event.id)
        outcome = Outcome("failed", f"{type(exc).__name__}")

    return _finish(conn, event_row, outcome)


def _claim(
    conn: psycopg.Connection, event: stripe_api.Event, *, provider: str
) -> uuid.UUID | None:
    """Take exclusive responsibility for this event, or None if somebody has it.

    The insert is the claim, and it comes before any action -- so a duplicate
    delivery loses it, and two simultaneous deliveries of one event cannot both
    proceed. A check-then-act would let both through, which for a webhook
    endpoint is the ordinary case rather than the unlucky one.

    The second half is the failure the first half creates. A handler that dies
    between claiming and finishing leaves a row at `received`, and Stripe's
    redelivery -- which is exactly the thing that would fix it -- would be
    turned away as a duplicate. So a row stalled for `STALLED_EVENT_MINUTES` can
    be taken over, by a conditional UPDATE that exactly one caller wins.
    """
    event_row = uuid.uuid4()
    try:
        db.execute(
            conn,
            "INSERT INTO billing_events (id, provider, event_id, event_type, livemode, event_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (event_row, provider, event.id, event.type, event.livemode,
             _moment(event.created)),
        )
        conn.commit()
        return event_row
    except psycopg.errors.UniqueViolation:
        conn.rollback()

    # The interval is a bound parameter rather than an f-string. It is a module
    # constant and interpolating it would be safe today, which is exactly how
    # the habit survives long enough to be applied to something that is not.
    taken = db.one(
        conn,
        "UPDATE billing_events SET received_at = now() "
        " WHERE provider = %s AND event_id = %s AND outcome = 'received' "
        "   AND received_at < now() - (%s * interval '1 minute') "
        "RETURNING id",
        (provider, event.id, STALLED_EVENT_MINUTES),
    )
    conn.commit()
    if taken is None:
        return None
    log.warning(
        "billing event %s was left unfinished and is being retried", event.id
    )
    return taken["id"]


def _on_checkout_completed(
    conn: psycopg.Connection, event: stripe_api.Event, *, provider: str
) -> Outcome:
    session_id = event.obj.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise BillingError("checkout session event carries no session id")

    row = db.one(
        conn,
        "SELECT id, org_id, project_id, plan_code, state FROM checkout_sessions "
        " WHERE provider = %s AND provider_session_id = %s",
        (provider, session_id),
    )
    if row is None:
        # Not an error on Stripe's side and not necessarily on ours: a session
        # created by another integration against the same account looks exactly
        # like this. Refusing is the right answer either way -- there is no row
        # saying what it was for, and the payload is not allowed to say.
        raise BillingError("no checkout session was recorded for this id")
    if row["state"] == "completed":
        return Outcome("ignored", "checkout already completed", row["project_id"])

    db.execute(
        conn,
        "UPDATE checkout_sessions SET state = 'completed', updated_at = now() WHERE id = %s",
        (row["id"],),
    )
    conn.commit()

    provider_subscription_id = event.obj.get("subscription")
    if not isinstance(provider_subscription_id, str) or not provider_subscription_id:
        # A completed subscription-mode checkout normally names one. Without it
        # there is nothing to record against, and the subscription events that
        # follow will create the row themselves.
        return Outcome("applied", "checkout completed; no subscription named yet",
                       row["project_id"])

    existing = subscriptions.by_provider(
        conn, provider=provider, provider_subscription_id=provider_subscription_id
    )
    if existing is not None:
        return Outcome("ignored", "subscription already recorded", row["project_id"])

    customer = event.obj.get("customer")
    subscriptions.create(
        conn,
        project_id=row["project_id"],
        plan_code=row["plan_code"],
        state=_state_from_payment_status(event.obj.get("payment_status")),
        as_of=_moment(event.created),
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        provider_customer_id=customer if isinstance(customer, str) else None,
        checkout_session_id=row["id"],
    )
    return Outcome("applied", f"subscription opened on {row['plan_code']}", row["project_id"])


def _state_from_payment_status(payment_status: object) -> str:
    """The opening state of a subscription created from a completed checkout.

    `no_payment_required` is what Stripe sets for a trial, so it maps to
    `trialing`; `paid` means money moved, so `active`. Anything else -- an
    asynchronous method that has not settled -- opens `incomplete`, which
    entitles nothing until a subscription event says otherwise.

    A subscription event usually follows within seconds and corrects this. What
    it must not do is arrive at a state the transition map forbids, which is why
    a paid checkout never opens `trialing` and a trial never opens `active`.
    """
    if payment_status == "paid":
        return "active"
    if payment_status == "no_payment_required":
        return "trialing"
    return "incomplete"


def _on_subscription_event(
    conn: psycopg.Connection, event: stripe_api.Event, *, provider: str
) -> Outcome:
    provider_subscription_id = event.obj.get("id")
    if not isinstance(provider_subscription_id, str) or not provider_subscription_id:
        raise BillingError("subscription event carries no subscription id")

    if event.type == "customer.subscription.deleted":
        state = "canceled"
    else:
        status = event.obj.get("status")
        if not isinstance(status, str) or status not in stripe_api.STATUS_MAP:
            # Not defaulted. An unrecognised status is a question about whether
            # somebody has paid, and guessing at it either bills a customer for
            # nothing or gives away a paid plan.
            raise BillingError(f"unrecognised Stripe subscription status {status!r}")
        state = stripe_api.STATUS_MAP[status]

    price_id = stripe_api.price_id_of(event.obj)
    priced_plan = (
        plan_for_price(conn, price_id=price_id, livemode=event.livemode, provider=provider)
        if price_id
        else None
    )
    period_start, period_end = stripe_api.period_of(event.obj)
    moment = _moment(event.created)

    existing = subscriptions.by_provider(
        conn, provider=provider, provider_subscription_id=provider_subscription_id
    )
    if existing is not None:
        if existing.state == "canceled":
            return Outcome("ignored", "subscription is already canceled", existing.project_id)
        if price_id is not None and priced_plan is None:
            # The subscription moved to a price the platform does not sell.
            # Refusing keeps the customer on what they had rather than silently
            # dropping them to free, and it is visible in the event log.
            raise BillingError(f"price {price_id} is not mapped to any plan")
        subscriptions.record_state(
            conn,
            project_id=existing.project_id,
            state=state,
            as_of=moment,
            plan_code=priced_plan,
            period_start=_moment(period_start),
            period_end=_moment(period_end),
        )
        customer = event.obj.get("customer")
        if isinstance(customer, str) and customer:
            subscriptions.attach_customer(
                conn, subscription_id=existing.id, provider_customer_id=customer
            )
        return Outcome("applied", f"state {existing.state} -> {state}", existing.project_id)

    if state == "canceled":
        # Nothing was recorded and it is over. Recording it now would create a
        # dead row that entitles nothing and reconciles to the plan the project
        # is already on.
        return Outcome("ignored", "unknown subscription, already ended")

    row = _checkout_row_for(conn, event, provider=provider)
    if row is None:
        raise BillingError("no checkout session was recorded for this subscription")
    if priced_plan is None:
        raise BillingError(
            f"price {price_id!r} on this subscription is not mapped to any plan"
        )
    if priced_plan != row["plan_code"]:
        # The two independent answers disagree: what a manager chose, and what
        # Stripe says is being charged for. Either could be right, so neither is
        # acted on. ADR-041's rule is that the payload does not get to decide,
        # and "resolve in favour of our own row" would be deciding.
        raise BillingError(
            f"the price on this subscription maps to {priced_plan!r} but the "
            f"checkout recorded {row['plan_code']!r}"
        )

    customer = event.obj.get("customer")
    subscriptions.create(
        conn,
        project_id=row["project_id"],
        plan_code=row["plan_code"],
        state=state,
        as_of=moment,
        period_start=_moment(period_start),
        period_end=_moment(period_end),
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        provider_customer_id=customer if isinstance(customer, str) else None,
        checkout_session_id=row["id"],
    )
    if row["state"] == "open":
        db.execute(
            conn,
            "UPDATE checkout_sessions SET state = 'completed', updated_at = now() WHERE id = %s",
            (row["id"],),
        )
        conn.commit()
    return Outcome("applied", f"subscription opened on {row['plan_code']}", row["project_id"])


def _checkout_row_for(
    conn: psycopg.Connection, event: stripe_api.Event, *, provider: str
) -> dict | None:
    """The checkout this subscription came from, via metadata the platform set.

    The metadata was written by `start_checkout` and comes back inside a
    signed event, so it is as trustworthy as the event itself -- but it is used
    only as a **key**. What it looks up is a row the platform wrote, and that
    row is what says which project and which plan.
    """
    metadata = event.obj.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("checkout_session_id")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        checkout_id = uuid.UUID(raw)
    except ValueError:
        return None
    return db.one(
        conn,
        "SELECT id, org_id, project_id, plan_code, state FROM checkout_sessions "
        " WHERE id = %s AND provider = %s",
        (checkout_id, provider),
    )


def _finish(conn: psycopg.Connection, event_row: uuid.UUID, outcome: Outcome) -> Outcome:
    db.execute(
        conn,
        "UPDATE billing_events SET outcome = %s, note = %s, project_id = %s, "
        "       processed_at = now() WHERE id = %s",
        (outcome.outcome, outcome.note, outcome.project_id, event_row),
    )
    conn.commit()
    return outcome


# -- the end of a grace period ---------------------------------------------


@dataclass(frozen=True)
class GraceOutcome:
    project_ref: str
    #: `ended` -- the subscription was cancelled and the project will be
    #: reconciled to the default plan by the next reconciliation.
    #: `deferred` -- the provider could not be reached, so nothing was taken
    #: away. Tried again next pass.
    outcome: str
    note: str


def end_expired_grace(
    conn: psycopg.Connection,
    client: stripe_api.Client | None,
    *,
    grace_days: int,
    provider: str = stripe_api.PROVIDER,
) -> list[GraceOutcome]:
    """Cancel subscriptions whose failed payment has run out of tolerance.

    ADR-051: fourteen days -- configurable, and never a constant in application
    logic, which is why `grace_days` is a parameter with no default here.

    **The provider is cancelled first, and a provider that cannot be reached
    defers the whole thing.** Revoking the entitlement while leaving the
    subscription alive at Stripe would let a card retry succeed days later and
    charge somebody for a plan already taken away. Failing towards *not* taking
    the plan away is also the direction that costs the platform a few days of
    service rather than costing a customer money for nothing.

    **This changes no entitlement itself.** It records `canceled`, which puts
    the subscription on the reconciliation queue; the maintenance pass moves the
    project to the default plan, and the storage pass restricts it if it is over
    the free quota. Three separate steps, each already built, none of which
    deletes anything -- which is how acceptance criterion 4 is met by there
    being no code that could break it.
    """
    out: list[GraceOutcome] = []
    for row in subscriptions.in_expired_grace(conn, grace_days=grace_days):
        provider_id = row["provider_subscription_id"]
        if provider_id and row["provider"] == provider:
            if client is None:
                out.append(GraceOutcome(
                    row["project_ref"], "deferred",
                    "billing is not configured, so the provider cannot be cancelled",
                ))
                continue
            try:
                client.cancel_subscription(provider_id)
            except stripe_api.StripeError as exc:
                if not stripe_api.is_missing(exc):
                    out.append(GraceOutcome(
                        row["project_ref"], "deferred",
                        f"provider not cancelled ({exc}); nothing taken away",
                    ))
                    continue
                # Already gone at the provider, which is the state being asked
                # for. Carry on and record it locally.

        try:
            subscriptions.record_state(
                conn, project_id=row["project_id"], state="canceled", as_of=_now(),
            )
        except subscriptions.SubscriptionError as exc:
            # Most likely a webhook that got there first -- cancelling at the
            # provider makes it send `customer.subscription.deleted`, and both
            # paths are meant to be able to run.
            out.append(GraceOutcome(row["project_ref"], "ended", str(exc)))
            continue
        out.append(GraceOutcome(
            row["project_ref"], "ended",
            f"grace of {grace_days}d expired; {row['plan_code']} ends, data kept",
        ))
    return out


# -- reporting -------------------------------------------------------------


def events(conn: psycopg.Connection, *, limit: int = 50) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT e.event_id, e.event_type, e.livemode, e.event_at, e.received_at,
               e.outcome, e.note, pr.project_ref
          FROM billing_events e
          LEFT JOIN projects pr ON pr.id = e.project_id
         ORDER BY e.received_at DESC
         LIMIT %s
        """,
        (limit,),
    )


def unmapped_plans(conn: psycopg.Connection, *, livemode: bool) -> list[str]:
    """Paid plans the catalogue offers that no price maps to.

    A plan in this list cannot be bought: `start_checkout` refuses it. Worth a
    report because the failure is invisible until a customer tries.
    """
    default = models.default_plan(conn)
    rows = db.query(
        conn,
        """
        SELECT pl.code
          FROM plans pl
          LEFT JOIN billing_prices bp
                 ON bp.plan_code = pl.code AND bp.livemode = %s
         WHERE pl.is_active AND bp.id IS NULL
         ORDER BY pl.code
        """,
        (livemode,),
    )
    skip = default.code if default is not None else None
    return [row["code"] for row in rows if row["code"] != skip]


def _project(conn: psycopg.Connection, project_id: uuid.UUID) -> dict:
    row = db.one(
        conn,
        "SELECT pr.id, pr.org_id, pr.project_ref, pr.status, pr.deleted_at, "
        "       pl.code AS plan_code "
        "  FROM projects pr JOIN plans pl ON pl.id = pr.plan_id "
        " WHERE pr.id = %s",
        (project_id,),
    )
    if row is None:
        raise BillingError("project does not exist")
    if row["deleted_at"] is not None or row["status"] in ("DELETING", "DELETED"):
        raise BillingError("project is being deleted")
    return row


def _close_checkout(conn: psycopg.Connection, row_id: uuid.UUID, state: str) -> None:
    db.execute(
        conn,
        "UPDATE checkout_sessions SET state = %s, updated_at = now() WHERE id = %s",
        (state, row_id),
    )
    conn.commit()


def _audit(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    event_type: str,
    actor_user_id: uuid.UUID | None,
    detail: dict,
) -> None:
    """The customer-visible record.

    The same promise `subscriptions._audit` makes, and this is the module that
    was warned it would be tempted to break it: there is a Stripe payload in
    scope here. No amount, no currency, no customer id, no session id, no price
    id -- a plan code and a state, which is what a customer needs to answer
    "what happened to my project" without the audit trail becoming a second
    copy of a billing record.
    """
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, "
        "                          detail_json) VALUES (%s, %s, %s, %s, %s)",
        (project_id, "user" if actor_user_id else "system", actor_user_id, event_type,
         psycopg.types.json.Jsonb(detail)),
    )
