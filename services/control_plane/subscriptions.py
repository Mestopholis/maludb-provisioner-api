"""What has been paid for, kept apart from what is enforced (Phase 09 slice 3).

ADR-048. There are two facts about a project's plan and the platform has only
ever had one of them. `projects.plan_id` is the **entitlement**: what
`entitlements.for_project` resolves, what the gateway counts against, and what
slice 0's `plan_apply` writes to a node. A **subscription** is the other fact --
what a customer is entitled to *because somebody is paying* -- and until now
there was nowhere to put it, so slice 1's `set-plan` had to stand in for both.

**Billing state proposes; `plan_change` disposes.** Nothing in this module
writes `projects.plan_id`. It computes the plan a subscription entitles, and
`reconcile` hands that to `plan_change.change_plan`, which is the one operation
that moves a project between plans and the only one that touches a node. That
is acceptance criterion 3, and it is also what makes a provider swap survivable:
everything downstream of `reconcile` is unaware that billing exists.

**A provider is identity here and nothing else** (slice 4, ADR-049). The
columns added in migration 0021 record *which* Stripe subscription a row
corresponds to, so a webhook can find it again. The **states stay MaluDB's own**
and Stripe's are mapped onto them in `stripe_api.STATUS_MAP`, so a provider's
vocabulary still never reaches `plan_change` -- which is the property that made
answering the provider question a change to two modules rather than to the
platform. Nothing in this file imports `stripe_api`, and that is the test.

**No HTTP route here.** Slice 4 added one, and it is in `api/billing.py` where
its authentication -- a signature, not a session -- lives next to it. The rule
this module keeps is narrower and unchanged: nothing that arrives from outside
reaches these functions without something in between deciding what it means.

**What is not decided here.** `past_due` keeps its plan. That is a default
rather than an answer: how long a failed payment is tolerated and what happens
at the end of it is the third `## Billing` question and slice 5's business. What
slice 3 settles is only that a failed payment does not silently become a
downgrade the moment it arrives -- which, given acceptance criterion 4, is the
direction to fail in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from services.control_plane import db, models, plan_change

CREATED = "project.subscription.created"
STATE_CHANGED = "project.subscription.state_changed"

#: Every state a subscription can be in. Migration 0020 documents each one and
#: its check constraint is the authority; this tuple is for argument validation
#: and for the CLI's help text.
STATES: tuple[str, ...] = ("incomplete", "trialing", "active", "past_due", "canceled")

#: The states that entitle the subscription's plan. The other two entitle the
#: default plan, which is to say nothing beyond what any project gets free.
#:
#: `past_due` is in here on purpose and is the whole of slice 3's opinion about
#: failed payment: a payment failing is not, by itself, a downgrade.
ENTITLING: frozenset[str] = frozenset({"trialing", "active", "past_due"})

#: Legal transitions. Same-state is legal everywhere it appears, because a
#: provider redelivering the current truth -- a renewal, a period change -- is
#: the ordinary case and not an error.
#:
#: `canceled` is terminal and has no outward edges. A customer who comes back
#: gets a new subscription row rather than a resurrected one, which is why
#: migration 0020's uniqueness excludes canceled rows: the old row stays as the
#: record of what was sold, and reviving it would overwrite that history.
TRANSITIONS: dict[str, frozenset[str]] = {
    "incomplete": frozenset({"incomplete", "trialing", "active", "canceled"}),
    "trialing": frozenset({"trialing", "active", "past_due", "canceled"}),
    "active": frozenset({"active", "past_due", "canceled"}),
    "past_due": frozenset({"past_due", "active", "canceled"}),
    "canceled": frozenset(),
}


class SubscriptionError(RuntimeError):
    """A refusal that is safe to show an operator."""


@dataclass(frozen=True)
class Subscription:
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    plan_code: str
    state: str
    state_as_of: datetime
    period_start: datetime | None
    period_end: datetime | None
    #: Slice 4. None for a subscription nobody paid a provider for -- a comped
    #: project, a migration from another system -- which is a legitimate row
    #: rather than a broken one.
    provider: str | None = None
    provider_subscription_id: str | None = None

    @property
    def entitles(self) -> bool:
        return self.state in ENTITLING


@dataclass(frozen=True)
class Divergence:
    """A project whose enforced plan is not the plan being paid for."""

    project_ref: str
    #: `unbilled` -- the project is on a plan no live subscription pays for.
    #: `diverged` -- a live subscription entitles a plan the project is not on.
    #: Told apart because the operator's next move differs: an unbilled project
    #: needs somebody to decide whether to sell it or downgrade it, where a
    #: diverged one needs `subscription reconcile` and nothing else.
    direction: str
    plan_code: str
    entitled_plan_code: str
    state: str | None

    def __str__(self) -> str:
        billing = f"subscription {self.state} -> {self.entitled_plan_code}" \
            if self.state else "no subscription"
        return f"on {self.plan_code}, {billing}"


@dataclass
class Reconciliation:
    project_ref: str
    plan_code: str
    entitled_plan_code: str
    #: None when the project was already on its entitled plan, which is the
    #: ordinary result and the one a repeated run must produce.
    change: plan_change.Change | None = None

    @property
    def changed(self) -> bool:
        return self.change is not None and not self.change.unchanged


def _now() -> datetime:
    return datetime.now(UTC)


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
        raise SubscriptionError("project does not exist")
    return row


def _require_active_plan(conn: psycopg.Connection, plan_code: str) -> str:
    """Refuse a plan the catalogue does not offer, at write time.

    `plan_change` validates this too, so the strict need is small -- but a
    subscription naming a plan that does not exist is a row that can never
    reconcile, reported as drift forever, and discovered by an operator rather
    than by whoever wrote it. Failing at the write is the cheaper end.
    """
    plan = models.plan_by_code(conn, plan_code)
    if plan is None:
        # `plan_by_code` filters on is_active, so a retired plan and a
        # misspelled one answer the same, as they do in `plan_change`.
        raise SubscriptionError(f"no active plan with code {plan_code!r}")
    return plan.code


def _default_plan_code(conn: psycopg.Connection) -> str:
    plan = models.default_plan(conn)
    if plan is None:
        raise SubscriptionError(
            "this deployment has no plan called 'free'; run `cp-manage plans sync`"
        )
    return plan.code


def _row_to_subscription(row: dict) -> Subscription:
    return Subscription(
        id=row["id"], org_id=row["org_id"], project_id=row["project_id"],
        plan_code=row["plan_code"], state=row["state"], state_as_of=row["state_as_of"],
        period_start=row["period_start"], period_end=row["period_end"],
        provider=row.get("provider"),
        provider_subscription_id=row.get("provider_subscription_id"),
    )


def for_project(conn: psycopg.Connection, project_id: uuid.UUID) -> Subscription | None:
    """The project's live subscription, if it has one.

    Live means "not canceled". A project may have any number of canceled rows
    behind it and at most one that is not, which migration 0020's partial unique
    index enforces rather than trusts this query to imply.
    """
    row = db.one(
        conn,
        "SELECT id, org_id, project_id, plan_code, state, state_as_of, "
        "       period_start, period_end, provider, provider_subscription_id "
        "  FROM subscriptions WHERE project_id = %s AND state <> 'canceled'",
        (project_id,),
    )
    return _row_to_subscription(row) if row else None


def history(conn: psycopg.Connection, project_id: uuid.UUID) -> list[dict]:
    """Every subscription this project has had, most recent first."""
    return db.query(
        conn,
        "SELECT id, plan_code, state, state_as_of, period_start, period_end, "
        "       created_at, updated_at "
        "  FROM subscriptions WHERE project_id = %s ORDER BY created_at DESC",
        (project_id,),
    )


def create(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    plan_code: str,
    state: str = "active",
    as_of: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    provider: str | None = None,
    provider_subscription_id: str | None = None,
    provider_customer_id: str | None = None,
    checkout_session_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Subscription:
    """Record that something is being paid for. Changes no entitlement.

    The project's organization is **read from the project** rather than taken as
    an argument. That is the cross-tenant control: a caller that could name the
    org could pay for one organization's project out of another's subscription,
    and later move it between plans. Migration 0020's composite foreign key
    catches the same mistake at the database, which is defence in depth rather
    than a second opinion -- neither is load-bearing alone.
    """
    if state not in STATES:
        raise SubscriptionError(f"{state!r} is not a subscription state")
    if state == "canceled":
        # Creating one already dead records nothing and would occupy the row a
        # real subscription needs. Almost certainly a mistake at the CLI.
        raise SubscriptionError("a subscription cannot be created canceled")

    project = _project(conn, project_id)
    if project["deleted_at"] is not None or project["status"] in ("DELETING", "DELETED"):
        raise SubscriptionError("project is being deleted")
    code = _require_active_plan(conn, plan_code)

    if for_project(conn, project_id) is not None:
        raise SubscriptionError(
            "this project already has a live subscription; "
            "change its state instead, or cancel it first"
        )

    subscription_id = uuid.uuid4()
    moment = as_of or _now()
    try:
        db.execute(
            conn,
            "INSERT INTO subscriptions (id, org_id, project_id, plan_code, state, state_as_of, "
            "                           period_start, period_end, provider, "
            "                           provider_subscription_id, provider_customer_id, "
            "                           checkout_session_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (subscription_id, project["org_id"], project_id, code, state, moment,
             period_start, period_end, provider, provider_subscription_id,
             provider_customer_id, checkout_session_id),
        )
        _audit(conn, project_id, CREATED, actor_user_id,
               {"plan": code, "state": state})
        conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        # The check above is the message; migration 0020's partial unique index
        # is the control. Two callers reaching here together -- an operator and
        # slice 4's webhook handler, which is the pair this will actually see --
        # would both pass a check-then-insert, and the second would create a
        # project with two live subscriptions entitling different plans.
        conn.rollback()
        if provider_subscription_id and _claimed_elsewhere(
            conn, provider, provider_subscription_id
        ):
            # Migration 0021's second unique index. Distinguished because the
            # operator's next move is different: this is a redelivery or a
            # duplicate handler, not a project that is already sold.
            raise SubscriptionError(
                f"{provider} subscription {provider_subscription_id} is already "
                "recorded against a project"
            ) from exc
        if checkout_session_id and db.one(
            conn,
            "SELECT 1 AS hit FROM subscriptions WHERE checkout_session_id = %s",
            (checkout_session_id,),
        ) is not None:
            # Migration 0021's `one checkout, one subscription` index. A second
            # subscription claiming a checkout that already produced one is a
            # paid plan being granted by a replayed fact rather than by a
            # payment -- see the migration's note.
            raise SubscriptionError(
                "that checkout has already opened a subscription"
            ) from exc
        raise SubscriptionError(
            "this project already has a live subscription; "
            "change its state instead, or cancel it first"
        ) from exc

    return Subscription(
        id=subscription_id, org_id=project["org_id"], project_id=project_id,
        plan_code=code, state=state, state_as_of=moment,
        period_start=period_start, period_end=period_end,
        provider=provider, provider_subscription_id=provider_subscription_id,
    )


def record_state(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    state: str,
    as_of: datetime | None = None,
    plan_code: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Subscription:
    """Assert the subscription's current truth, as of a moment. Entitles nothing.

    Named for what a provider actually delivers. A webhook does not carry a
    delta -- it carries the whole current state of a subscription, so state,
    plan and period arrive together and a renewal, an upgrade and a failed
    payment are all the same call with different contents.

    **`as_of` is the ordering guard, and it is why the write is conditional
    rather than a plain UPDATE.** Providers retry and deliver out of order, so a
    `canceled` can arrive after the `active` that superseded it. Ordered by
    arrival, that downgrades a paying customer -- the risk the phase plan names.

    A fact older than the one on the row is refused as stale, and the comparison
    is made twice on purpose: once here, for the message an operator reads, and
    once in the UPDATE's `WHERE`, which is the one that holds when two writers
    arrive together. Equal is allowed, because a redelivery of the current truth
    is idempotent and exact duplicates are slice 4's event-id idempotency, which
    is a different control.
    """
    if state not in STATES:
        raise SubscriptionError(f"{state!r} is not a subscription state")

    current = for_project(conn, project_id)
    if current is None:
        raise SubscriptionError(
            "this project has no live subscription; create one first"
        )
    if state not in TRANSITIONS[current.state]:
        if current.state == "canceled":  # unreachable via `for_project`, kept honest
            raise SubscriptionError(
                "a canceled subscription is terminal; create a new one"
            )
        raise SubscriptionError(
            f"a subscription cannot go from {current.state} to {state}"
        )

    moment = as_of or _now()
    if moment < current.state_as_of:
        raise SubscriptionError(
            f"stale: this subscription already reflects a later fact "
            f"({current.state_as_of.isoformat()}); refusing to apply one from "
            f"{moment.isoformat()}"
        )

    code = _require_active_plan(conn, plan_code) if plan_code else current.plan_code

    # The guard is repeated in the WHERE clause, and that is not belt-and-braces.
    # The check above ran against a row read earlier in this transaction; two
    # deliveries arriving together would both pass it and the later commit would
    # win regardless of which fact is newer -- which is precisely the
    # out-of-order downgrade the column exists to prevent. Concurrency is the
    # normal case for a webhook endpoint, so the comparison belongs where the
    # database can serialise it.
    written = db.execute(
        conn,
        "UPDATE subscriptions SET state = %s, plan_code = %s, state_as_of = %s, "
        "       period_start = COALESCE(%s, period_start), "
        "       period_end = COALESCE(%s, period_end), updated_at = now() "
        " WHERE id = %s AND state_as_of <= %s",
        (state, code, moment, period_start, period_end, current.id, moment),
    )
    if written == 0:
        conn.rollback()
        raise SubscriptionError(
            "stale: this subscription was updated with a later fact while this "
            "one was being applied; refusing to overwrite it"
        )
    if state != current.state or code != current.plan_code:
        _audit(conn, project_id, STATE_CHANGED, actor_user_id, {
            "from_state": current.state, "to_state": state,
            "from_plan": current.plan_code, "to_plan": code,
        })
    conn.commit()

    updated = for_project(conn, project_id)
    if updated is None:
        # Only reachable by writing state='canceled', which drops the row out of
        # the live view. Return what was written rather than None.
        return Subscription(
            id=current.id, org_id=current.org_id, project_id=project_id,
            plan_code=code, state=state, state_as_of=moment,
            period_start=period_start or current.period_start,
            period_end=period_end or current.period_end,
        )
    return updated


def _claimed_elsewhere(
    conn: psycopg.Connection, provider: str | None, provider_subscription_id: str
) -> bool:
    return db.one(
        conn,
        "SELECT 1 AS hit FROM subscriptions "
        " WHERE provider = %s AND provider_subscription_id = %s",
        (provider, provider_subscription_id),
    ) is not None


def by_provider(
    conn: psycopg.Connection, *, provider: str, provider_subscription_id: str
) -> Subscription | None:
    """Find a subscription by the provider's own id, canceled ones included.

    Deliberately not filtered to live rows, unlike `for_project`. A provider
    sends events about subscriptions it has already ended -- a final invoice, a
    redelivery -- and a lookup that could not see a canceled row would answer
    "unknown subscription" and, worse, leave the way open for a second row
    claiming the same provider id.
    """
    row = db.one(
        conn,
        "SELECT id, org_id, project_id, plan_code, state, state_as_of, "
        "       period_start, period_end, provider, provider_subscription_id "
        "  FROM subscriptions WHERE provider = %s AND provider_subscription_id = %s",
        (provider, provider_subscription_id),
    )
    return _row_to_subscription(row) if row else None


def attach_customer(
    conn: psycopg.Connection, *, subscription_id: uuid.UUID, provider_customer_id: str
) -> None:
    """Record the provider's customer id if it was not known at creation.

    Set-once: an `UPDATE ... WHERE provider_customer_id IS NULL`, so a later
    event carrying a different customer cannot silently move a subscription's
    billing identity. A later event naming a different customer changes nothing
    and is not an error -- the column is a convenience for support, not a
    control -- so it is left alone rather than resolved in favour of whichever
    event arrived last.
    """
    db.execute(
        conn,
        "UPDATE subscriptions SET provider_customer_id = %s, updated_at = now() "
        " WHERE id = %s AND provider_customer_id IS NULL",
        (provider_customer_id, subscription_id),
    )
    conn.commit()


def pending_reconciliation(conn: psycopg.Connection) -> list[dict]:
    """Subscriptions holding a billing fact that has not reached a node yet.

    The queue, and it is a predicate over columns that already move rather than
    a flag somebody has to remember to set: a row whose `(state, plan_code)`
    differs from what was last applied has changed since. Nothing has to enqueue
    anything, so nothing can forget to.

    **The pair rather than `state_as_of`**, which is what this was first written
    against and which is wrong for a reason worth keeping written down. Stripe's
    event timestamps are whole seconds, and `checkout.session.completed` and
    `customer.subscription.updated` routinely arrive inside the same one -- so a
    queue keyed on the timestamp can mark the second fact done by applying the
    first. Comparing the two values that `entitled_plan_code` actually reads
    asks the real question, and it is exact rather than nearly always right.

    Ordered oldest-first so a backlog drains in the order it accumulated, which
    matters when two subscriptions for the same organization are waiting and one
    of them is a cancellation.
    """
    return db.query(
        conn,
        """
        SELECT s.id, s.project_id, s.state, s.plan_code, s.state_as_of, pr.project_ref
          FROM subscriptions s
          JOIN projects pr ON pr.id = s.project_id
         WHERE (s.reconciled_state, s.reconciled_plan_code)
               IS DISTINCT FROM (s.state, s.plan_code)
           AND pr.deleted_at IS NULL
           AND pr.status NOT IN ('DELETING', 'DELETED')
         ORDER BY s.state_as_of
        """,
    )


def mark_reconciled(
    conn: psycopg.Connection, *, subscription_id: uuid.UUID, state: str, plan_code: str
) -> None:
    """Record what was in force when this subscription reached the node.

    Takes the values that were actually applied rather than re-reading the
    current ones. An event arriving *while* reconciliation runs therefore leaves
    the row pending, because what is on it no longer matches what was applied --
    which is the same property the timestamp version was reaching for, obtained
    without depending on how precise a provider's clock is.
    """
    db.execute(
        conn,
        "UPDATE subscriptions SET reconciled_state = %s, reconciled_plan_code = %s, "
        "       updated_at = now() WHERE id = %s",
        (state, plan_code, subscription_id),
    )
    conn.commit()


def entitled_plan_code(conn: psycopg.Connection, project_id: uuid.UUID) -> str:
    """The plan this project's *billing* says it should be on.

    Not what it is on. The two agreeing is the reconciled state, and the whole
    value of keeping them apart is that this function can be evaluated without
    touching a node, a role, or a project row.

    A project with no live subscription, or one in a state that entitles
    nothing, resolves to the default plan -- which is the correct answer rather
    than a fallback: every project is entitled to the free tier by existing.
    """
    subscription = for_project(conn, project_id)
    if subscription is None or not subscription.entitles:
        return _default_plan_code(conn)
    return subscription.plan_code


def reconcile(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    requested_by: uuid.UUID | None = None,
) -> Reconciliation:
    """Make the enforced plan match the paid-for plan, through `plan_change`.

    The seam. Everything above this line is billing and knows nothing about
    nodes; everything below it is `plan_change`, which knows nothing about
    billing. Nothing here writes `projects.plan_id`, opens a node connection of
    its own, or decides what a plan grants.

    Idempotent by construction: a project already on its entitled plan produces
    no change at all, because `change_plan` returns `unchanged` and this returns
    it without a `plan_changes` row. That matters because the whole point of a
    reconciler is that running it twice is uneventful.
    """
    project = _project(conn, project_id)
    # Read before the change, so an event arriving during it stays pending. See
    # `mark_reconciled`.
    live = for_project(conn, project_id)
    entitled = entitled_plan_code(conn, project_id)
    if project["plan_code"] == entitled:
        # Already correct, which is the ordinary result -- but the subscription
        # still has to stop being pending, or the queue never drains and the
        # maintenance pass rediscovers it every run. "Nothing to do" is a
        # completed reconciliation, not a skipped one.
        _settle(conn, project_id, live)
        return Reconciliation(
            project_ref=project["project_ref"], plan_code=project["plan_code"],
            entitled_plan_code=entitled,
        )

    change = plan_change.change_plan(
        conn, admin_conn, project_id=project_id, to_plan_code=entitled,
        requested_by=requested_by,
    )
    _settle(conn, project_id, live)
    return Reconciliation(
        project_ref=project["project_ref"], plan_code=project["plan_code"],
        entitled_plan_code=entitled, change=change,
    )


def _settle(
    conn: psycopg.Connection, project_id: uuid.UUID, live: Subscription | None
) -> None:
    """Mark whatever subscription this reconciliation was about as applied.

    `live` is the row read *before* the plan change, because that is the fact
    that has now reached the node. A canceled subscription has no live row, so
    the most recent one for the project is used instead -- reconciling a
    cancellation is exactly the case where `for_project` returns nothing and the
    work still has to be recorded as done.
    """
    subscription = live
    if subscription is None:
        row = db.one(
            conn,
            "SELECT id, state, plan_code FROM subscriptions WHERE project_id = %s "
            " ORDER BY state_as_of DESC LIMIT 1",
            (project_id,),
        )
        if row is None:
            return
        mark_reconciled(
            conn, subscription_id=row["id"], state=row["state"], plan_code=row["plan_code"]
        )
        return
    mark_reconciled(
        conn, subscription_id=subscription.id, state=subscription.state,
        plan_code=subscription.plan_code,
    )


def drift(conn: psycopg.Connection) -> list[Divergence]:
    """Which projects' plans disagree with what is being paid for them.

    The fleet view, and it reports rather than corrects -- slice 0's precedent,
    for slice 0's reason plus a stronger one. A reconciler on a timer would move
    projects between plans unattended, which is the class of change that should
    have somebody's name on it. `subscription reconcile` is that name.

    `unbilled` is the finding this report exists for. Every project on the
    platform is one today: `cp-manage project set-plan` moves a project to a
    paid plan and takes no money, which was correct while there was nowhere to
    record that money had been taken. Now there is, and a paid project with no
    subscription is a question rather than a state.
    """
    default = _default_plan_code(conn)
    rows = db.query(
        conn,
        """
        SELECT pr.project_ref, pl.code AS plan_code, s.plan_code AS sub_plan, s.state
          FROM projects pr
          JOIN plans pl ON pl.id = pr.plan_id
          LEFT JOIN subscriptions s
                 ON s.project_id = pr.id AND s.state <> 'canceled'
         WHERE pr.deleted_at IS NULL
           AND pr.status NOT IN ('DELETING', 'DELETED')
         ORDER BY pr.project_ref
        """,
    )

    out: list[Divergence] = []
    for row in rows:
        entitled = row["sub_plan"] if row["state"] in ENTITLING else default
        if row["plan_code"] == entitled:
            continue
        out.append(
            Divergence(
                project_ref=row["project_ref"],
                direction="unbilled" if row["state"] is None else "diverged",
                plan_code=row["plan_code"],
                entitled_plan_code=entitled,
                state=row["state"],
            )
        )
    return out


def _audit(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    event_type: str,
    actor_user_id: uuid.UUID | None,
    detail: dict,
) -> None:
    """The subscription's own history.

    A table of its own was considered and rejected: `plan_changes` is a table
    because a plan change is a resumable *operation* with a half-done state,
    where a subscription transition is a fact that either was recorded or was
    not. `audit_events` already holds facts, is already shown to customers
    through the ADR-047 allowlist, and already outlives the row it describes.

    Never a provider's payload, an amount, or a customer identifier -- which
    costs nothing to promise now, and is the promise slice 4 will be tempted to
    break when it has a webhook body in its hand.
    """
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, "
        "                          detail_json) VALUES (%s, %s, %s, %s, %s)",
        (project_id, "user" if actor_user_id else "system", actor_user_id, event_type,
         psycopg.types.json.Jsonb(detail)),
    )
