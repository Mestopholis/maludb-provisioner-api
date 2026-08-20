"""The Stripe protocol, in both directions and nowhere else (ADR-049).

Everything in this module is Stripe's vocabulary: form encoding, its idempotency
header, its webhook signature scheme, its subscription statuses, its tax codes.
Nothing above it knows any of that. `billing.py` speaks MaluDB's language and
calls in here to translate, which is what makes ADR-048's claim -- that a
provider swap is a change to one module -- something the file layout enforces
rather than something a comment asserts.

**Hand-rolled rather than the SDK, and the reasoning is not "fewer
dependencies".** What this needs from Stripe is four HTTP calls and one HMAC.
The HMAC is the part worth thinking about, and the repository already carries a
reviewed implementation of the same construction in `mail.py` -- an HMAC-SHA256
over `{timestamp}.{body}` with a constant-time compare and a freshness window.
Bringing in an SDK to avoid writing thirty lines that already exist twenty lines
away is the trade in the wrong direction, and the SDK's surface -- global
configuration, its own retry policy, its own HTTP client -- is larger than the
thing being avoided. The one place that argument would flip is a construction
this repository could get subtly wrong; `pyjwt` is here for exactly that reason.

**The API version is pinned.** An unpinned integration changes behaviour when
Stripe ships a version, at a moment nobody chose, in the component that moves
money. Managed Payments needs `2025-03-31.basil` or later.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

PROVIDER = "stripe"

#: Pinned deliberately -- see the module docstring. Raising it is a decision
#: with a changelog to read, not a dependency bump.
API_VERSION = "2025-03-31.basil"

DEFAULT_BASE_URL = "https://api.stripe.com"

#: Stripe is not a fast dependency and a checkout page is a person waiting. Long
#: enough to survive a slow round trip, short enough that a hung call does not
#: hold a request worker until something else times out.
DEFAULT_TIMEOUT = 15.0

#: How far out of date a webhook signature may be. Stripe's own libraries use
#: five minutes, and the number is doing real work: without it a captured
#: delivery replays forever, because the body and its signature never change.
SIGNATURE_TOLERANCE_SECONDS = 300

#: Stripe's subscription statuses, mapped onto MaluDB's (ADR-048). The mapping
#: is one-way and total: an unrecognised status is an error rather than a
#: default, because guessing here is guessing about whether somebody has paid.
#:
#: Two of these are worth saying out loud:
#:
#: - `unpaid` maps to `past_due` rather than to `canceled`. It is what Stripe
#:   moves a subscription to when retries are exhausted, and treating it as a
#:   cancellation would downgrade a customer at the moment ADR-051 says the
#:   grace period should be starting.
#: - `paused` maps to `past_due` for the same reason and with less certainty:
#:   it means a trial ended with no payment method. Keeping the plan and letting
#:   slice 5's grace period expire it is the direction that fails safely.
STATUS_MAP: dict[str, str] = {
    "incomplete": "incomplete",
    "incomplete_expired": "canceled",
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "paused": "past_due",
    "canceled": "canceled",
}

#: Product tax codes Stripe accepts for Managed Payments, restricted to the ones
#: a database platform could honestly claim. The full eligible list is much
#: longer and mostly about e-books and video games.
#:
#: **Why this list is a refusal rather than a warning.** An ineligible product
#: does not fail at checkout. The transaction falls out of Managed Payments and
#: MaluDB silently becomes the seller of record for it -- which is the entire
#: liability ADR-049 chose Managed Payments to avoid, arriving without an error
#: message, on one product, months before anybody reconciles a tax return.
ELIGIBLE_TAX_CODES: frozenset[str] = frozenset(
    {
        "txcd_10000000",  # Electronically supplied services -- the generic one
        "txcd_10010001",  # IaaS, personal use
        "txcd_10101000",  # IaaS, business use
        "txcd_10102000",  # PaaS, business use
        "txcd_10102001",  # PaaS, personal use
        "txcd_10103000",  # SaaS, personal use
        "txcd_10103001",  # SaaS, business use
        "txcd_10701100",  # Website hosting
    }
)


class StripeError(RuntimeError):
    """Anything Stripe refused or could not be asked.

    Deliberately one class. A caller's options are the same for a network
    failure and a 400 -- report it and do not pretend the call happened -- and
    the distinction that matters (`retryable`) is an attribute rather than a
    hierarchy.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str
    expires_at: int
    livemode: bool


@dataclass(frozen=True)
class Event:
    """A verified webhook event, reduced to what the platform acts on."""

    id: str
    type: str
    created: int
    livemode: bool
    #: The `data.object` of the event, untouched. Read through the accessors in
    #: `billing.py`, never trusted for anything that grants an entitlement.
    obj: dict[str, Any]


def form_encode(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's bracketed form encoding: `line_items[0][price]=price_x`.

    Returns pairs rather than a dict because Stripe's array syntax produces
    repeated structure that a dict would be fine with but a list makes obvious.
    Booleans are rendered lowercase, which Stripe requires and `str(True)`
    would get wrong.
    """
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            out.extend(form_encode(value, name))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(form_encode(item, f"{name}[{index}]"))
                else:
                    out.append((f"{name}[{index}]", str(item)))
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        else:
            out.append((name, str(value)))
    return out


def livemode_of(secret_key: str) -> bool:
    """Whether a key acts on real money, read from the key itself.

    Stripe's test keys are `sk_test_` / `rk_test_` and its live keys are not.
    Deriving it rather than configuring it separately removes a way to be wrong:
    a deployment cannot declare itself in test mode while holding a key that
    charges people, which is the mistake that would send real customers through
    a test-mode price map.

    A function as well as a property because the webhook route needs the answer
    without needing a client, and building one to read an attribute off it is
    the kind of line that later gets "simplified" into a bug.

    **No key is test mode, not live mode.** A naive "does it contain `_test_`"
    says *live* for an empty string, which is the wrong direction in the one
    case it decides anything: a deployment holding a webhook secret and no API
    key would then accept live events and resolve them through whatever price
    map it had. Absent configuration must fail towards refusing money, not
    towards taking it.
    """
    if not secret_key:
        return False
    return "_test_" not in secret_key[:12]


class Client:
    """A Stripe API client, holding the secret key for one deployment.

    Constructed per call site rather than kept as a module global, so a test can
    build one pointed at a local transport and no code path can accidentally
    reach the real API because a global was configured elsewhere.
    """

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not secret_key:
            raise StripeError("no Stripe secret key is configured")
        self._key = secret_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    @property
    def livemode(self) -> bool:
        return livemode_of(self._key)

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Stripe-Version": API_VERSION,
            "Authorization": f"Bearer {self._key}",
        }
        # Encoded here rather than handed to httpx as `data=`, because httpx
        # reads a *list* of pairs as raw content and Stripe's array syntax needs
        # repeated keys that a dict cannot express.
        content = urlencode(form_encode(data)) if data else None
        if content is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key:
            # Stripe replays the original response for 24 hours against the same
            # key. Without it, a retry after a timeout creates a *second*
            # checkout session -- and the customer can pay for both.
            headers["Idempotency-Key"] = idempotency_key

        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport, base_url=self._base_url
            ) as client:
                response = client.request(method, path, headers=headers, content=content)
        except httpx.HTTPError as exc:
            # The message, not the exception: an httpx error's string can carry
            # the request URL, and these URLs carry object ids.
            raise StripeError(f"Stripe could not be reached ({type(exc).__name__})",
                              retryable=True) from exc

        if response.status_code >= 400:
            raise StripeError(_error_message(response), retryable=response.status_code >= 500)
        try:
            return response.json()
        except ValueError as exc:
            raise StripeError("Stripe returned a body that is not JSON") from exc

    def create_checkout_session(
        self,
        *,
        price_id: str,
        success_url: str,
        cancel_url: str,
        client_reference_id: str,
        idempotency_key: str,
        managed_payments: bool = True,
        customer_email: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> CheckoutSession:
        """A hosted Checkout Session for one subscription.

        **Hosted Checkout, never Elements, and that is ADR-049 rather than a
        preference:** Managed Payments supports Checkout and Payment Links only,
        and a subscription cannot be created outside them. An Elements
        integration would work and would silently foreclose merchant-of-record
        status.

        `client_reference_id` carries the platform's own project id back on the
        completed event. It is a convenience for correlation and **not** a
        control: what binds a session to a project is the `checkout_sessions`
        row written before this call.
        """
        payload: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "managed_payments": {"enabled": managed_payments},
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": client_reference_id,
        }
        if customer_email:
            payload["customer_email"] = customer_email
        if metadata:
            payload["subscription_data"] = {"metadata": metadata}

        body = self._request(
            "POST", "/v1/checkout/sessions", data=payload, idempotency_key=idempotency_key
        )
        url = body.get("url")
        if not url:
            # A session with no URL cannot be redirected to, which makes it
            # useless rather than merely odd.
            raise StripeError("Stripe returned a checkout session with no URL")
        return CheckoutSession(
            id=str(body.get("id", "")),
            url=str(url),
            expires_at=int(body.get("expires_at") or 0),
            livemode=bool(body.get("livemode")),
        )

    def get_price(self, price_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/prices/{price_id}")

    def get_product(self, product_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/products/{product_id}")

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/subscriptions/{subscription_id}")


def _error_message(response: httpx.Response) -> str:
    """Stripe's own message for a failure, or the status code.

    Passed through to an operator, so it must not become a place a payload
    lands: only `error.message`, which Stripe writes for humans, and never the
    body.
    """
    try:
        payload = response.json()
    except ValueError:
        return f"Stripe answered {response.status_code}"
    message = (payload.get("error") or {}).get("message")
    if not isinstance(message, str) or not message:
        return f"Stripe answered {response.status_code}"
    return f"Stripe answered {response.status_code}: {message}"


# -- webhooks --------------------------------------------------------------


def verify_and_parse(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
    now: float | None = None,
) -> Event:
    """Verify a webhook signature, then parse. In that order, always.

    The order is the security property and the reason this is one function
    rather than two: a caller cannot parse first by accident, and there is no
    parsed object in existence for an unverified body.

    Stripe's scheme is a `Stripe-Signature` header of comma-separated pairs --
    `t=<unix>,v1=<hex>[,v1=<hex>]` -- where the signed content is
    `{t}.{raw body}`, HMAC-SHA256 keyed by the endpoint's signing secret and
    rendered hex. Several `v1` entries appear while a secret is being rotated,
    so any one matching is a pass.

    **The raw body matters.** The signature covers the bytes Stripe sent; a body
    that has been through `json.loads` and back is a different string and will
    not verify. That is why the route hands bytes to this function and reads
    nothing out of the request itself.

    The freshness window is not optional. Without it a captured delivery is
    valid forever, because nothing in it changes -- which is `mail.py`'s note
    about the same construction, and it is the same risk here with money on the
    other end.
    """
    if not secret:
        raise StripeError("no Stripe webhook signing secret is configured")

    timestamp, candidates = _parse_signature_header(signature_header)

    moment = time.time() if now is None else now
    if abs(moment - timestamp) > tolerance:
        raise StripeError("webhook signature timestamp is outside the accepted window")

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # compare_digest, not ==: a timing-variable comparison on a MAC is the
    # textbook mistake, and `mail.py` says the same thing about the same code.
    if not any(hmac.compare_digest(candidate, expected) for candidate in candidates):
        raise StripeError("webhook signature does not verify")

    try:
        body = json.loads(payload)
    except ValueError as exc:
        # Reachable only with a valid signature, so this is Stripe sending
        # something unparseable rather than an attacker sending anything.
        raise StripeError("webhook body is not JSON") from exc
    if not isinstance(body, dict):
        raise StripeError("webhook body is not an object")

    event_id = body.get("id")
    event_type = body.get("type")
    created = body.get("created")
    if not isinstance(event_id, str) or not event_id:
        raise StripeError("webhook body has no event id")
    if not isinstance(event_type, str) or not event_type:
        raise StripeError("webhook body has no event type")
    if not isinstance(created, int):
        raise StripeError("webhook body has no created timestamp")

    obj = ((body.get("data") or {}).get("object")) or {}
    if not isinstance(obj, dict):
        raise StripeError("webhook body has no data object")

    return Event(
        id=event_id,
        type=event_type,
        created=created,
        livemode=bool(body.get("livemode")),
        obj=obj,
    )


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    timestamp: int | None = None
    candidates: list[str] = []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value:
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise StripeError("webhook signature timestamp is not an integer") from exc
        elif key == "v1" and value:
            candidates.append(value)
    if timestamp is None:
        raise StripeError("webhook signature header has no timestamp")
    if not candidates:
        raise StripeError("webhook signature header has no v1 signature")
    return timestamp, candidates


def sign(payload: bytes, *, secret: str, timestamp: int) -> str:
    """Produce a `Stripe-Signature` header for `payload`.

    Here rather than in the tests because the tests are not the only consumer:
    a recorded fixture is only worth anything if the thing that signs it is the
    thing that verifies it. A separate implementation in a test file would drift
    from this one and the suite would keep passing while doing so.
    """
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


# -- reading Stripe objects -------------------------------------------------


def price_id_of(subscription: dict[str, Any]) -> str | None:
    """The single price a subscription is for, or None if it is not single.

    MaluDB sells one price per subscription (ADR-050: hard limits, so no metered
    line items). A subscription carrying more than one is not something this
    platform created, and resolving it to a plan by picking the first item would
    be a guess about what somebody is paying for.
    """
    items = ((subscription.get("items") or {}).get("data")) or []
    if not isinstance(items, list) or len(items) != 1:
        return None
    price = (items[0] or {}).get("price") or {}
    price_id = price.get("id")
    return price_id if isinstance(price_id, str) and price_id else None


def period_of(subscription: dict[str, Any]) -> tuple[int | None, int | None]:
    """The current billing period, from wherever this API version puts it.

    Stripe moved `current_period_start`/`_end` from the subscription onto its
    items. Both shapes are read because the pinned version is not the only one
    a recorded fixture or a replayed event may have been produced under, and a
    missing period is a display detail rather than a reason to refuse an event
    that says somebody paid.
    """
    start = subscription.get("current_period_start")
    end = subscription.get("current_period_end")
    if start is None or end is None:
        items = ((subscription.get("items") or {}).get("data")) or []
        if isinstance(items, list) and items:
            first = items[0] or {}
            start = first.get("current_period_start", start)
            end = first.get("current_period_end", end)
    return (
        start if isinstance(start, int) else None,
        end if isinstance(end, int) else None,
    )


def new_idempotency_key() -> str:
    return f"maludb-{uuid.uuid4()}"
