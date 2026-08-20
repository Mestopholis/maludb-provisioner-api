-- Phase 09 slice 3: what has been paid for, kept apart from what is enforced.
--
-- ADR-048. `projects.plan_id` is the *entitlement*: what the platform enforces,
-- what `entitlements.for_project` resolves, and what slice 0's `plan_apply`
-- writes to a node. A subscription is a different fact -- what a customer is
-- entitled to *because somebody is paying* -- and this table never writes the
-- other one. Billing state proposes; `plan_change` disposes.
--
-- That separation is acceptance criterion 3, and it is also what makes a
-- provider swap survivable: nothing downstream of here knows a provider exists.

-- The composite-foreign-key target. `projects.id` is already the primary key,
-- so this adds no real index and no real constraint on `projects` -- what it
-- adds is something `subscriptions` can point a *pair* at, below.
ALTER TABLE projects ADD CONSTRAINT projects_id_org_key UNIQUE (id, org_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id          UUID PRIMARY KEY,

    -- Who pays, and what they are paying for.
    --
    -- `docs/ACCOUNTS.md` puts billing on the organization (ADR-020) and
    -- `projects.plan_id` puts the plan on the project, so a subscription needs
    -- both: the org holds the payment relationship and the `billing` role that
    -- may manage it, the project is what the plan applies to.
    --
    -- The pair is a *composite* foreign key rather than two separate ones, and
    -- that is the point of the UNIQUE above. Two independent references would
    -- let a row name org A and a project belonging to org B -- which is a
    -- cross-tenant control, not a typo: it would let one organization move
    -- another organization's project between plans. The database refuses it
    -- here rather than leaving it to whichever caller remembers to check.
    org_id      UUID NOT NULL,
    project_id  UUID NOT NULL,

    -- The plan this subscription entitles, by code rather than by foreign key,
    -- for `upgrade_requests`' and `plan_changes`' reason: a plan may be retired
    -- from the catalogue while a subscription naming it is still what somebody
    -- is being charged for, and losing that to a catalogue edit would lose the
    -- answer to "what did we sell them".
    --
    -- Note what this is not: it is not the project's current plan. The two
    -- agreeing is the *reconciled* state, and them disagreeing is exactly what
    -- `cp-manage subscription drift` exists to report.
    plan_code   VARCHAR(50) NOT NULL,

    -- MaluDB's own states, not a provider's. Every provider considered has a
    -- set close to this one and none has exactly it, so a provider's states are
    -- mapped onto these in slice 4 rather than stored raw -- otherwise the
    -- provider's vocabulary reaches `plan_change`, and swapping providers means
    -- rewriting everything that reads this column.
    --
    --   incomplete  a subscription that has never successfully been paid for.
    --               Entitles nothing. The starting state for a checkout that
    --               was begun and not finished.
    --   trialing    entitled, unpaid, by arrangement.
    --   active      entitled and paid.
    --   past_due    a payment failed and the subscription still entitles its
    --               plan. **This is a default, not a decision.** How long that
    --               lasts and what happens at the end of it is the third open
    --               question under `## Billing`, and slice 5's business. What
    --               slice 3 fixes is only that a failed payment does not
    --               silently become a downgrade the moment it arrives.
    --   canceled    entitles nothing, terminal. A customer who comes back gets
    --               a new row, which is why the uniqueness below excludes this
    --               state.
    state       VARCHAR(20) NOT NULL,

    -- The ordering guard, and the reason it is a column rather than slice 4's
    -- problem: providers retry and deliver out of order, so a `canceled` can
    -- arrive after the `active` that superseded it and downgrade a paying
    -- customer. Ordering by arrival is what makes that possible.
    --
    -- So every transition carries the moment the *provider* says the fact was
    -- true, and one older than what is recorded here is refused as stale. A
    -- timestamp rather than a sequence number because it is the only ordering
    -- key all three candidate providers actually expose. An operator acting
    -- through `cp-manage` supplies now(), which is honest: they are the source.
    state_as_of TIMESTAMPTZ NOT NULL,

    -- The billing period, for slice 6's usage display. Nullable because an
    -- operator-created subscription has no period until a provider gives it
    -- one, and a made-up period would be a number shown to a customer that
    -- nothing measured.
    period_start TIMESTAMPTZ,
    period_end   TIMESTAMPTZ,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT subscriptions_state_check
        CHECK (state IN ('incomplete', 'trialing', 'active', 'past_due', 'canceled')),
    CONSTRAINT subscriptions_period_order_check
        CHECK (period_start IS NULL OR period_end IS NULL OR period_end > period_start),
    CONSTRAINT subscriptions_project_fkey
        FOREIGN KEY (project_id, org_id) REFERENCES projects(id, org_id) ON DELETE CASCADE
);

-- No provider columns, deliberately.
--
-- Which provider, and whether it is a merchant of record, is the first open
-- question under `## Billing` and is unanswered. A nullable `provider_*` column
-- nobody writes is a guess at the answer's shape that would have to be right,
-- and `ALTER TABLE ... ADD COLUMN` in slice 4 costs nothing. Slice 3 is the
-- part of billing that does not depend on who takes the money; it should not
-- contain a column that names them.

-- One live subscription per project, on `upgrade_requests`' pattern. A canceled
-- one stays as the record of what was sold, and a customer who re-subscribes
-- gets a new row rather than having their history overwritten.
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_live_per_project
    ON subscriptions(project_id) WHERE state <> 'canceled';

-- The org's own view: what is this organization paying for.
CREATE INDEX IF NOT EXISTS subscriptions_org_idx
    ON subscriptions(org_id, created_at DESC);
