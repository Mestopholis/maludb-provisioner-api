-- Phase 09 slice 4: the provider, attached to the facts slice 3 already held.
--
-- ADR-049 chose Stripe, and ADR-048 made that a small change on purpose: a
-- subscription already records what is paid for in MaluDB's own vocabulary, so
-- what arrives here is provider *identity* and the three ledgers a webhook
-- endpoint needs to be safe -- a price map, an event log, and a record of what
-- each checkout was for.
--
-- The last of those is the ADR-041 control in table form. What a customer buys
-- is decided when the platform creates the Checkout Session and written down
-- then; the webhook that arrives later looks it up rather than believing a plan
-- code out of a payload.

-- -- provider identity on the subscription ---------------------------------
--
-- Nullable, and they stay nullable: `cp-manage subscription create` records a
-- subscription nobody paid Stripe for -- a comped project, a migration from
-- another system, a test -- and that is a legitimate row rather than a broken
-- one. What must not happen is two subscriptions claiming the same Stripe
-- subscription, which the partial unique index below forbids.
ALTER TABLE subscriptions
    ADD COLUMN provider                 VARCHAR(20),
    ADD COLUMN provider_subscription_id TEXT,
    ADD COLUMN provider_customer_id     TEXT,
    -- What was in force at the last successful reconciliation, and therefore
    -- the queue: a row whose (state, plan_code) differs from these has a
    -- billing fact that has not reached a node yet. NULL means never
    -- reconciled.
    --
    -- **The pair rather than a timestamp**, and that was a bug before it was a
    -- design. `state_as_of` comes from the provider, whose timestamps are whole
    -- seconds -- and `checkout.session.completed` and
    -- `customer.subscription.updated` routinely arrive inside the same one. A
    -- queue keyed on it drops the second fact silently, which is the worst
    -- possible failure for a queue whose whole job is that nothing is missed.
    --
    -- These two are also exactly what `entitled_plan_code` reads, so the
    -- predicate asks the question that actually matters -- has the entitlement
    -- moved -- rather than a proxy for it.
    ADD COLUMN reconciled_state         VARCHAR(20),
    ADD COLUMN reconciled_plan_code     VARCHAR(50);

ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_provider_identity_check
        CHECK ((provider IS NULL) = (provider_subscription_id IS NULL));

-- One MaluDB subscription per provider subscription, forever -- including
-- across canceled rows, which is why this is not restricted the way the
-- one-live-per-project index is. A redelivered `checkout.session.completed`
-- for a subscription that was later canceled must not open a second row.
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_provider_subscription_key
    ON subscriptions(provider, provider_subscription_id)
    WHERE provider_subscription_id IS NOT NULL;

-- Existing rows are reconciled by definition: they were written before these
-- columns existed, by an operator who ran `subscription reconcile` or chose
-- not to. Backfilling means deploying this migration does not hand the
-- maintenance pass every subscription on the platform as work on its first run.
UPDATE subscriptions SET reconciled_state = state, reconciled_plan_code = plan_code;

-- -- the price map ----------------------------------------------------------
--
-- ADR-052: prices live in the provider and the platform stores the mapping.
-- No amount, no currency, and nothing here that could disagree with what a
-- customer is actually charged.
CREATE TABLE IF NOT EXISTS billing_prices (
    id          UUID PRIMARY KEY,
    provider    VARCHAR(20) NOT NULL,
    -- Test-mode and live-mode price ids are different strings for the same
    -- plan, and a deployment holds both only in the sense that a test database
    -- holds one and a production database the other. Keeping the flag means a
    -- live webhook can never resolve through a test-mode row.
    livemode    BOOLEAN NOT NULL,
    plan_code   VARCHAR(50) NOT NULL,
    price_id    TEXT NOT NULL,
    -- What the provider's product carried when this row was written, recorded
    -- for an operator's benefit rather than sent anywhere: the tax code lives
    -- on the Stripe *product* and Stripe is its authority. `billing price set`
    -- refuses a product whose code is not eligible for Managed Payments,
    -- because an ineligible product does not fail -- it silently falls back to
    -- MaluDB being the seller of record for that transaction (ADR-049).
    tax_code    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The direction that matters. A webhook resolves price id -> plan, so a
    -- price id mapping to two plans would make the plan a customer receives
    -- depend on row order. Unique here means that cannot be expressed.
    CONSTRAINT billing_prices_price_key UNIQUE (provider, livemode, price_id),
    -- And the reverse, so `start_checkout` has one answer for a plan.
    CONSTRAINT billing_prices_plan_key UNIQUE (provider, livemode, plan_code)
);

-- -- the event log ----------------------------------------------------------
--
-- Idempotency and replay refusal, as a table rather than as care. The unique
-- constraint is the control: the handler inserts the event id *first*, and a
-- duplicate insert is how it learns the event has already been seen. Two
-- concurrent deliveries of the same event -- which is a thing providers do --
-- cannot both pass a check-then-act, because only one insert survives.
CREATE TABLE IF NOT EXISTS billing_events (
    id          UUID PRIMARY KEY,
    provider    VARCHAR(20) NOT NULL,
    event_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    livemode    BOOLEAN NOT NULL,
    -- When the provider says the event happened, which is also what becomes
    -- the subscription's `state_as_of` and therefore its ordering guard.
    event_at    TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When the handler finished with it. NULL alongside outcome 'received' is
    -- an event that killed the handler mid-way.
    processed_at TIMESTAMPTZ,
    -- 'received' until the handler finishes; then one of the outcomes below.
    -- A row stuck at 'received' is an event that killed the handler, which is
    -- worth being able to find.
    outcome     VARCHAR(20) NOT NULL DEFAULT 'received',
    -- Nullable: an event may arrive for a subscription the platform does not
    -- know, and recording that is the point.
    project_id  UUID REFERENCES projects(id) ON DELETE SET NULL,
    -- One short line an operator can read. Never a payload, never an amount,
    -- never a customer identifier -- see `billing.py`.
    note        TEXT,

    CONSTRAINT billing_events_outcome_check
        CHECK (outcome IN ('received', 'applied', 'ignored', 'refused', 'failed')),
    CONSTRAINT billing_events_event_key UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS billing_events_recent_idx
    ON billing_events(received_at DESC);
CREATE INDEX IF NOT EXISTS billing_events_project_idx
    ON billing_events(project_id, received_at DESC)
    WHERE project_id IS NOT NULL;

-- -- what each checkout was for ---------------------------------------------
--
-- ADR-041 in a table: a value the customer influences cannot be the control.
-- The plan is chosen here, by an authenticated manager of the organization
-- that will be billed, and written down before the customer ever reaches the
-- provider. The webhook reads this row; it never reads a plan code out of a
-- payload, and the price id it does read is checked against this.
CREATE TABLE IF NOT EXISTS checkout_sessions (
    id                  UUID PRIMARY KEY,
    org_id              UUID NOT NULL,
    project_id          UUID NOT NULL,
    plan_code           VARCHAR(50) NOT NULL,
    provider            VARCHAR(20) NOT NULL,
    livemode            BOOLEAN NOT NULL,
    provider_session_id TEXT NOT NULL,
    -- Who asked for it. Not the payer -- the organization is that (ADR-020) --
    -- but the person whose session created it, so the audit trail can say.
    created_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    state               VARCHAR(20) NOT NULL DEFAULT 'open',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,

    CONSTRAINT checkout_sessions_state_check
        CHECK (state IN ('open', 'completed', 'expired')),
    CONSTRAINT checkout_sessions_session_key
        UNIQUE (provider, provider_session_id),
    -- The same composite foreign key `subscriptions` uses, for the same
    -- reason: two independent references would permit a row naming org A and
    -- org B's project, and that is a cross-tenant control rather than a typo.
    CONSTRAINT checkout_sessions_project_fkey
        FOREIGN KEY (project_id, org_id) REFERENCES projects(id, org_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS checkout_sessions_project_idx
    ON checkout_sessions(project_id, created_at DESC);
-- An organization may not have two checkouts running for the same project.
-- Without this, a customer who opens the page twice can pay twice, and the
-- second `checkout.session.completed` meets a project that already has a live
-- subscription -- which is refused, correctly, but after the money moved.
CREATE UNIQUE INDEX IF NOT EXISTS checkout_sessions_one_open_per_project
    ON checkout_sessions(project_id) WHERE state = 'open';

-- -- one checkout, one subscription -----------------------------------------
--
-- Added last because it references `checkout_sessions`, which is created above.
--
-- Found in slice 4's own security review. A subscription event resolves its
-- project through the checkout id the platform put in Stripe's metadata -- and
-- without this, an event replaying an *old* checkout id after the original
-- subscription was canceled would open a second subscription on the plan that
-- checkout bought, against a project with nothing live to refuse it. Reaching
-- it needs a valid signature, so it is not a hole an outsider walks through;
-- it is a paid plan granted by a fact rather than by a payment, which is the
-- class of thing worth making impossible rather than unlikely.
ALTER TABLE subscriptions
    ADD COLUMN checkout_session_id UUID
        REFERENCES checkout_sessions(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_checkout_key
    ON subscriptions(checkout_session_id)
    WHERE checkout_session_id IS NOT NULL;
