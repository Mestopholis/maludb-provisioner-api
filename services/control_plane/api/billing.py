"""Checkout, and the endpoint Stripe posts to (Phase 09 slice 4, ADR-049/053).

Two routes with almost nothing in common, deliberately kept together because
they are two halves of one exchange.

**`POST /v1/projects/{ref}/billing/checkout`** is an ordinary customer route:
a session authenticates it, a manager is required, and it returns a URL. It is
the point where the plan being bought is decided, by somebody the platform has
authenticated, and written down before the customer reaches Stripe.

**`POST /webhooks/stripe`** is not a customer route and is not in the OpenAPI
contract, but it *is* on the public listener, and that is a decision rather than
an oversight (ADR-053). Stripe posts from the internet. An endpoint it cannot
reach is an endpoint that does not work, and the alternative -- a proxy
exception routing one path to the internal listener -- is the kind of
undocumented deployment requirement that works until somebody rebuilds the load
balancer.

So the network position is not what protects it. **The signature is the
authentication**, the same sentence `hooks.py` writes about the same problem,
and it is verified before the body is parsed rather than after.

**What an attacker gains by reaching this endpoint without a valid signature:
nothing.** No path here reads a plan, a project, or an amount out of an
unverified body -- `verify_and_parse` refuses before a parsed object exists.
With a *valid* signature they are Stripe, and the remaining controls are the
ones in `billing.py`: the plan comes from a row the platform wrote, the event id
is claimed before it is acted on, and the provider's timestamp orders it.

**Nothing here reconciles.** ADR-038 keeps node superuser credentials out of the
internet-facing process, so this endpoint records the billing fact and the
maintenance pass -- which runs where those credentials live -- is what makes it
true on a node. The lag is seconds to a minute and it is the price of the split.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from services.control_plane import billing, db, models, ratelimit, stripe_api
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager
from services.control_plane.api.limit_dep import enforce as enforce_limit

log = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])

CHECKOUT_BUCKET = "billing-checkout"

#: Tighter than the console and looser than the credential route. Starting a
#: checkout costs a Stripe API call and creates a row, so it should not be free
#: to do in a loop -- but a customer clicking twice must not be refused.
CHECKOUT_LIMIT = ratelimit.Limit(6, 60)

#: The most a webhook body may be. Stripe's events are a few kilobytes and its
#: largest are well under this.
#:
#: **It is here because the signature cannot be checked until the body has been
#: read**, so an unauthenticated caller on a public endpoint decides how much
#: memory this process buffers before any control applies. Nothing else bounds
#: it -- there is no default body limit in the stack below this route.
MAX_WEBHOOK_BODY = 256 * 1024


async def _bounded_body(request: Request, limit: int = MAX_WEBHOOK_BODY) -> bytes | None:
    """Read the request body, or None if it is larger than `limit`.

    Streamed rather than `await request.body()`, and streamed rather than
    trusting `Content-Length`: a chunked request declares no length, so a check
    on the header alone is a check an attacker opts out of by not sending one.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class CheckoutIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=50)


class CheckoutOut(BaseModel):
    #: Where to send the customer. A URL on Stripe's domain, always.
    checkout_url: str
    plan_code: str
    expires_at: str


def _client(request: Request) -> stripe_api.Client:
    """The Stripe client for this application, or a 503 naming what is missing.

    `request.app.state.config`, not `config.load()` -- the mistake `database.py`
    records twice, where reading the process environment gives a test
    application a different answer from the one it was built with.
    """
    cfg = request.app.state.config
    if not cfg.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="billing is not configured on this deployment",
        )
    return stripe_api.Client(cfg.stripe_secret_key, base_url=cfg.stripe_api_base)


@router.post(
    "/v1/projects/{project_ref}/billing/checkout",
    response_model=CheckoutOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a checkout for a paid plan",
)
def start_checkout(
    project_ref: str, body: CheckoutIn, request: Request, principal: CurrentPrincipal
) -> CheckoutOut:
    """Open a hosted Checkout Session and return where to send the customer.

    **Manager, not member**, on `database.py`'s precedent and for a sharper
    reason: this commits the organization to a recurring charge. `viewer` exists
    so that seeing a project is not the same as being able to spend its owner's
    money.

    Resolution order matters and is the same one every project route uses: a
    non-member gets `404` before anything confirms the project exists, because a
    project ref is a customer's API subdomain and confirming one confirms a
    target.

    **This route grants nothing.** It returns a URL. The entitlement arrives
    later, from a webhook, through `plan_change`, on a node -- which is the
    separation ADR-048 exists for and the reason a customer who closes the
    Stripe page has changed nothing.
    """
    client = _client(request)
    cfg = request.app.state.config

    with db.connection() as conn:
        project = models.get_project_by_ref(conn, project_ref)
        if project is None or not principal.is_member_of(project.org_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
            )
        require_manager(principal, project.org_id)
        enforce_limit(
            request, bucket=CHECKOUT_BUCKET, limit=CHECKOUT_LIMIT, subject=str(project.id)
        )

        base = cfg.dashboard_url.rstrip("/")
        try:
            checkout = billing.start_checkout(
                conn,
                client,
                project_id=project.id,
                plan_code=body.plan_code,
                success_url=f"{base}/projects/{project_ref}/billing?checkout=complete",
                cancel_url=f"{base}/projects/{project_ref}/billing?checkout=cancelled",
                actor_user_id=principal.user.id,
                customer_email=principal.user.email,
            )
        except billing.BillingError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except stripe_api.StripeError as exc:
            # Not the customer's fault and not something they can act on, so it
            # is a 502 rather than a 4xx. The message is Stripe's own, which is
            # written for humans; nothing of the request is echoed.
            log.warning("checkout could not be started for %s: %s", project_ref, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="the payment provider could not be reached",
            ) from exc

    return CheckoutOut(
        checkout_url=checkout.url,
        plan_code=checkout.plan_code,
        expires_at=checkout.expires_at.isoformat(),
    )


@router.post(
    "/webhooks/stripe",
    include_in_schema=False,
    status_code=status.HTTP_200_OK,
)
async def stripe_webhook(
    request: Request,
    response: Response,
    stripe_signature: str = Header(default="", alias="stripe-signature"),
) -> dict:
    """Receive one Stripe event.

    **The raw body, never the parsed one.** The signature covers the bytes
    Stripe sent; a body that has been through a JSON round trip is a different
    string and would not verify. FastAPI would happily bind a model here, and
    doing so would be the bug.

    **What the status code means to Stripe**, which decides what happens next:

    - **200** — received. Includes every refusal that a retry cannot fix: an
      unmapped price, an unknown session, a project being deleted. These are
      recorded in `billing_events` with an outcome and a note, which is where an
      operator finds them. Answering 4xx instead would make Stripe redeliver the
      same failure for days and eventually disable the endpoint, taking the
      events that *would* have worked with it.
    - **400** — the signature did not verify, or the body is not an event.
      Deliberately terminal: there is no version of this Stripe should retry.
    - **503** — no signing secret is configured. Retryable, and true: the event
      was valid and the deployment could not check it, so Stripe should try
      again once somebody fixes the configuration.
    - **413** — the body is larger than any real event. Refused before it is
      buffered, because the signature cannot be verified until the body has
      been read, which means an unauthenticated caller would otherwise choose
      how much memory this process allocates.
    """
    cfg = request.app.state.config
    if not cfg.stripe_webhook_secret:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"error": "billing is not configured"}

    payload = await _bounded_body(request)
    if payload is None:
        log.warning("stripe webhook refused: body over %d bytes", MAX_WEBHOOK_BODY)
        response.status_code = status.HTTP_413_CONTENT_TOO_LARGE
        return {"error": "body too large"}

    try:
        event = stripe_api.verify_and_parse(
            payload=payload,
            signature_header=stripe_signature,
            secret=cfg.stripe_webhook_secret,
        )
    except stripe_api.StripeError as exc:
        # Coarse on purpose. A caller who cannot produce a valid signature
        # learns that the signature was wrong and nothing else -- not whether
        # the timestamp was stale, not whether the secret is the right one.
        log.warning("stripe webhook refused: %s", exc)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"error": "signature verification failed"}

    # Without a secret key there is nothing to derive the mode from, and a
    # deployment holding a webhook secret but no API key is misconfigured.
    # `livemode_of("")` is False, which refuses live events rather than acting
    # on them -- the safe direction, and the one that fails loudly in the event
    # log rather than quietly against the wrong price map.
    expected_livemode = stripe_api.livemode_of(cfg.stripe_secret_key or "")

    with db.connection() as conn:
        outcome = billing.handle_event(conn, event, expected_livemode=expected_livemode)

    if not outcome.ok:
        # Logged, because a refusal nobody looks at is a refusal nobody knows
        # about -- but still answered 200. The event id is Stripe's, not a
        # customer's, and the note is written by this codebase.
        log.warning(
            "stripe event %s (%s): %s -- %s",
            event.id, event.type, outcome.outcome, outcome.note,
        )
    return {"received": True}
