# Billing and Plans

## Philosophy

Plans should be understandable and predictable.

The architecture should be resource/feature driven, while exact public prices and quotas remain configurable.

## Accepted distinctions

Free:

- API-only;
- constrained resources;
- sleeping API workers;
- no public direct PostgreSQL connection.

Paid may add:

- direct/pool PostgreSQL access;
- higher API/query limits;
- larger storage;
- always-warm workers;
- backups;
- PITR;
- Realtime capacity;
- support/SLA features;
- placement on production-oriented node pools.

## Important rule

Plan behavior must be data/configuration-driven.

Do not scatter checks like:

```text
if plan == "free"
```

through unrelated services when a centralized entitlement/limits model can be used.

## Two facts, not one (ADR-048)

A project's plan is two separate facts and the platform keeps them in two
separate places, on purpose.

| | Where | What it means | Who writes it |
|---|---|---|---|
| **Entitlement** | `projects.plan_id` | what the platform *enforces* — resolved by `entitlements.for_project`, counted against by the gateway, written to a node by `plan_apply` | `plan_change.change_plan`, and nothing else |
| **Subscription** | `subscriptions` | what is *paid for* — plan, state, and the moment that state became true | `subscriptions`, which never touches the other column |

The flow between them runs one way. `subscriptions.reconcile` computes the plan
a subscription entitles and hands it to `plan_change`; billing state proposes,
`plan_apply` disposes. Nothing that knows a billing provider exists can reach a
node, which is what makes swapping providers a change to one module.

The two can disagree, and that is a state with a name rather than a bug:

- **`unbilled`** — the project is on a plan no live subscription pays for.
  `cp-manage project set-plan` moves a project and takes no money, so this is
  every paid project until somebody records a subscription for it.
- **`diverged`** — a subscription entitles a plan the project is not on. An
  upgrade that was recorded and never applied, or a cancellation that was
  recorded and never enforced.

`cp-manage subscription drift` lists both. It reports and does not correct:
moving a project between plans is a change that should have somebody's name on
it, and `cp-manage subscription reconcile --ref <ref>` is that name.

### Subscription states

MaluDB's own, not a provider's. A provider's states are mapped onto these, so
that a provider's vocabulary never reaches the code that grants entitlements.

| State | Entitles |
|---|---|
| `incomplete` | the free tier — begun and never paid for |
| `trialing` | the subscription's plan |
| `active` | the subscription's plan |
| `past_due` | the subscription's plan |
| `canceled` | the free tier; terminal, and a returning customer gets a new row |

`past_due` keeping its plan is now a decision as well as a default: a failed
payment is not by itself a downgrade, and **ADR-051** settles how long that
lasts. Fourteen days — configurable, never a constant in application logic — of
entirely unchanged service. Then the subscription becomes `canceled`,
`reconcile` hands the default plan to `plan_change`, and the project meets
ADR-040's storage restriction: `INSERT` and `UPDATE` revoked, `SELECT`,
`DELETE` and `TRUNCATE` kept. Writes stop; reads do not; the database,
`project_ref`, API keys and rows all survive. **Nothing deletes customer data,
at any stage** — that is the acceptance criterion whose failure cannot be
undone, and it is met by there being no code that deletes.

Provider states are mapped onto the table above and never the reverse. Stripe
(ADR-049) happens to use the names `trialing` and `past_due` too; the mapping
must not rely on that, because the coincidence would not survive a provider
change. `stripe_api.STATUS_MAP` is the whole of it, and it is total: an
unrecognised status is refused rather than defaulted, because a default there is
a guess about whether somebody has paid.

## Taking the money (slice 4)

Three modules, and the layering is the design rather than a filing convention.
`stripe_api` holds Stripe's protocol -- form encoding, signatures, statuses --
and nothing above it knows any of that. `billing` decides what an event *means*.
`subscriptions` holds what has been paid for and cannot reach a node.

### Buying a plan

1. A **manager** of the organization calls
   `POST /v1/projects/{ref}/billing/checkout`. Manager rather than member,
   because this commits the organization to a recurring charge.
2. The platform writes a `checkout_sessions` row -- **which plan, which
   project, which organization** -- and only then creates a hosted Stripe
   Checkout Session. The row is written first so that the one-open-per-project
   index refuses a second concurrent checkout before any money can move.
3. The customer pays on Stripe's page.
4. Stripe posts events to `POST /webhooks/stripe`. The platform records what
   was paid for. **It grants nothing.**
5. `maintenance.reconcile_subscriptions` applies it, through `plan_change`, on
   a node. Seconds to a minute later.

Step 4 and step 5 being separate is ADR-053: the webhook endpoint has to be
reachable from the internet, and ADR-038 keeps node credentials out of the
process that answers it.

### What is trusted, and what is not

**The plan comes from the `checkout_sessions` row, never from the payload.**
That row was written by an authenticated manager before the customer reached
Stripe, which is ADR-041 -- a value the customer influences cannot be the
control. The price id on the incoming subscription is resolved through the
platform's own map and required to *agree* with that row; a disagreement is
refused rather than resolved, because resolving in favour of either would be
deciding.

Four controls, none of them relied on alone:

| Control | What it stops |
|---|---|
| Signature verified before parsing | Anything not from Stripe. No unverified body is ever a parsed object. |
| Event id inserted before the event is acted on | Duplicates and replays, including two concurrent deliveries of one event. A row left at `received` for five minutes can be taken over, so a handler that died does not swallow the event. |
| `state_as_of` from the provider's timestamp | A stale `canceled` arriving after the `active` that superseded it. |
| The plan comes from a row the platform wrote | A well-formed event asking for a plan nobody bought. One checkout opens at most one subscription. |
| The body is capped before it is read | An unauthenticated caller choosing how much memory the process buffers, since the signature cannot be checked until the body has been read. |

### Prices

ADR-052: no amount and no currency is stored. `billing_prices` maps
`plan_code` -> Stripe price id, per mode, and both directions are unique --
a price id resolving to two plans would make what a customer receives depend on
row order.

`cp-manage billing price set` checks the Stripe **product's** tax code before
writing the mapping, and refuses one that is not eligible for Managed Payments.
That check is not bureaucracy: an ineligible product does not fail at checkout,
it silently drops that transaction out of Managed Payments and makes MaluDB the
seller of record for it -- acquiring the indirect-tax liability ADR-049 chose
Managed Payments to avoid, with no error, discoverable months later.

## When a payment fails (slice 5)

ADR-051, in three stages, the third of which never arrives.

1. **Fourteen days of entirely unchanged service.** The subscription is
   `past_due`, it keeps its plan, and every entitlement is what it was. Cards
   expire and banks decline for reasons unrelated to intent; restricting on the
   first failure punishes the wrong thing. The period is
   `MALUDB_BILLING_GRACE_DAYS`, because a grace period is a plan limit and the
   development rules forbid hard-coding those.
2. **Then the subscription ends and the project reverts to the free tier.** The
   provider is cancelled *first* — leaving it alive would let a card retry
   succeed days later and charge somebody for a plan already taken away — and a
   provider that cannot be reached defers the whole thing, so the platform
   loses a few days of service rather than a customer losing money for nothing.
   Then `reconcile` moves the project, `plan_apply` revokes direct access, and
   the storage pass restricts writes if the data is now over the free quota
   (ADR-040: `INSERT` and `UPDATE` revoked, `SELECT`, `DELETE` and `TRUNCATE`
   kept).
3. **Nothing is ever deleted.** Not at the end of grace, not later, not as a
   storage-reclamation pass. The database, its rows, the `project_ref` and the
   API keys all survive, and the customer can read everything and delete their
   way back under the free quota under their own power.

**The clock is `state_since`, not `state_as_of`** (ADR-054). Stripe re-sends
`past_due` on every dunning retry with a newer timestamp, so a period measured
from when the fact was last asserted restarts on every retry and never expires.
The two columns are equal until a state is confirmed twice, which is exactly why
the difference is easy to miss.

`cp-manage billing status` lists projects in grace and when each one runs out,
while there is still something to be done about it. `cp-manage subscription
show` says the same for one project.

### Where to look when something is wrong

- `cp-manage billing status` -- can this deployment take money, and what is
  waiting to be applied.
- `cp-manage billing events` -- what Stripe delivered and what became of each.
  `refused` names something the platform declined and says why; `failed` is a
  bug; a row stuck at `received` is an event that killed the handler.
- `cp-manage subscription drift` -- projects whose plan disagrees with what is
  being paid for them. Still reported and not corrected.

Billing attaches to the organization (ADR-020) and the plan attaches to the
project, so a subscription carries both: the org is who pays, the project is
what the plan applies to. The pair is a composite foreign key, so a
subscription cannot name one organization and another organization's project.

## Upgrade

Normal free-to-paid upgrade changes entitlements/limits and keeps the tenant database in place.

A later background move to another node/pool may be performed for operational reasons, but it is not required to complete payment/upgrade.
