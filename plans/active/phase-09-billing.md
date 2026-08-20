# Execution Plan: Phase 09 — Billing, and making a plan change mean something

Status: **IN PROGRESS** — 2026-08-20. Slices 0 to 4 are complete. Slices 5 and
6 are unblocked: the four `## Billing` open questions were answered 2026-08-20
and recorded as ADR-049 to ADR-052.

Slice 3 came out of that blocked set earlier, under ADR-048, and the reasoning
is in the slice-3 entry below: subscription state is the part of billing that
does not depend on who takes the money, which is exactly what the slice's own
description asked for and what the blanket header got wrong.

Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-09-slice-*`, one per slice
Related task: `tasks/PHASE-09-BILLING.md`
Dependencies: Phase 08 complete (merged 2026-08-19, PRs #56–#67). The
per-slice security review is now a merge gate rather than a checklist item —
see `AGENTS.md` — so this is the first phase where an unreviewed slice cannot
merge. That is a change in what the Verification section below is worth.

## Objective

Turn a free project into a paid project without moving its database, and make
the resulting entitlements true everywhere they are supposed to be true.

The second half is the work. The first half — `UPDATE projects SET plan_id` —
already almost exists and would be a lie if shipped alone.

## What is already true, measured before planning

### A plan change takes effect in one half of the platform and not the other

Every entitlement is resolved through `entitlements.for_project`, which reads
the plan row. Where that resolution happens **per request**, changing a plan is
instant. Where it happened **once, at provisioning**, changing a plan does
nothing at all and nothing says so.

| Entitlement | Enforced | A plan change reaches it |
|---|---|---|
| `api_requests_per_window`, `concurrent_api_requests` | gateway, per request | immediately |
| `database_storage_bytes` | `storage.py`, maintenance pass | immediately |
| `emails_per_day` | `mail.py`, per send | immediately |
| `sql_console_*` | `api/sql.py`, per request | immediately |
| `max_projects` | `api/projects.py`, per create | immediately |
| `realtime_connections` | gateway + `maintenance._realtime_past_its_plan` | immediately |
| `statement_timeout_ms`, `work_mem_mb`, `temp_file_limit_mb`, `lock_timeout_ms`, `idle_in_transaction_timeout_ms` | role GUCs on the node | **never** |
| `direct_database_access` | `rolcanlogin` on `mldb_<ref>_admin` | **never** |
| `database_connections` | role `CONNECTION LIMIT` | **never** |
| `postgrest_pool_size` | worker configuration | only when a worker restarts |

`jobs.py` applies the bottom four during provisioning and nothing re-applies
them. `maintenance.run_all` sleeps workers, measures storage, retries failed
provisioning and checks replication slots; it does not touch plan settings.

This is not a hypothesis. `cp-manage project direct-sql` exists *because* of it
and says so in its own help text: "an upgrade taking effect before the next
provisioning run". The gap has a manual workaround shipped in the CLI, which is
the strongest possible evidence that somebody already hit it.

**Realtime is the single exception, and it is the pattern to copy.**
`maintenance._realtime_past_its_plan` finds projects whose plan no longer allows
Realtime and turns it off. One entitlement out of ten reconciles. Slice 0 is
that idea applied to the rest, rather than a new invention.

### Nothing can change a project's plan today, and that is deliberate

`api/projects.py` refuses any plan but the default at creation. That refusal is
Phase 07's ADR-041-adjacent finding: a customer could put a project on any plan
they named. `upgrade_requests` (migration 0015) records *intent* — a customer
pressed a button — and its own comment says why it stops there:

> Deliberately **not** a change to `projects.plan_id`. An upgrade that took
> effect here would grant paid entitlements to a project nobody has billed.

So the seam this phase fills is already cut, documented, and queued. There is a
`REQUESTED -> CONTACTED -> CLOSED` operator queue with rows waiting in it.

### `projects.status` already reserves `UPGRADING`

Migration 0017's check constraint lists it. Nothing sets it. A plan change that
touches the node is a multi-step operation that can fail halfway, and the state
to park it in already exists.

### The database credential a paid customer needs already exists and is unreachable

`project_credentials` holds an envelope-encrypted, recoverable `db_admin`
secret per project (ADR-023). Provisioning generates it whether or not the plan
entitles the project to use it — `provisioning.py` says so — and
`set_direct_sql_access` decides only whether the role may log in.

No route returns it. Grepping every router finds no path by which a customer
receives a database credential of any kind. "Paid direct DB access" is
therefore a delivery problem, not a provisioning one, and it is the phase's
sharpest security surface: a long-lived secret, handed to a customer, stored in
their application configuration, and reachable from the internet.

### Free-to-paid does not need to move anything

ADR-006 decouples node movement from purchase, and nothing in provisioning ties
capability to placement — `node_pool` exists and defaults to `shared` for every
project. Acceptance criterion 1 is satisfiable by construction rather than by
work: the correct implementation of "retain database identity" is to not write
any code that moves a database.

## Scope

- Reconciling a running project with its plan, idempotently.
- A plan change as an authorized, audited, resumable operation.
- Delivering and revoking paid direct database access.
- Subscription state, kept distinct from entitlement state.
- Billing-provider integration, after the provider is chosen.
- Failed-payment handling that restricts rather than destroys.
- Usage display against the billing period.

## Non-goals

- **Moving a project to another node or pool.** ADR-006 already says purchase
  does not require it. A background move is Phase 11's business.
- **Storage products** (Phase 10) and **PITR/backups** (Phase 11), even though
  both appear in `docs/BILLING-AND-PLANS.md` as things paid plans "may add".
  A plan may name an entitlement this platform does not implement yet; what it
  must not do is sell one.
- **Invoicing, tax, dunning emails as MaluDB surfaces.** Whatever the provider
  does is what happens, at least at launch.
- **Storing card data in any form.** Provider-hosted checkout only. This is a
  non-goal in the sense that a design proposing otherwise should be rejected
  without discussion.
- **A price list in the public API.** `plans.router` is authenticated today and
  its comment already says it is "an entitlement catalogue, not a price list".

## Preconditions

- ~~The four `## Billing` open questions answered, as ADRs, before slice 4.~~
  **Met 2026-08-20 — ADR-049 to ADR-052.** (Was "before slice 3"; ADR-048
  narrowed it first. See the slice-3 progress entry.)
- A **Stripe test-mode** account for slices 4 onward, with test-mode price ids
  for each paid `plan_code`. No test in this repository may reach Stripe, and
  none may need a network at all: recorded fixtures for webhook payloads,
  signed with a test secret.
- A tax advisor's confirmation of the Stripe product tax code before *live*
  mode — `txcd_10102000` (PaaS, business use) or `txcd_10103001` (SaaS,
  business use). ADR-049 deliberately does not decide it. It does not block
  slice 4, which stores whichever string it is told.

## Decisions that were needed before slice 4 — all answered 2026-08-20

Recorded as ADR-049 to ADR-052, and kept in `docs/OPEN-QUESTIONS.md` with their
reasoning. Summarised here with what each one changes, because "payment
provider?" understated them.

1. **Which provider, and is it a merchant of record?** **Stripe, with
   merchant-of-record status as configuration rather than code (ADR-049).**
   Stripe Managed Payments is Stripe's own MoR and the same API as plain
   Stripe, enabled per account and per Checkout Session — so the tax posture is
   a deployment decision and slice 4 is built once. It does constrain slice 4:
   hosted Checkout only, never Elements, and a subscription cannot be created
   outside Checkout. The original framing, kept because it is still the axis
   that matters: Stripe is a payment
   processor: the platform is the seller and owes VAT/sales-tax registration
   and remittance in every jurisdiction it sells into. Paddle and Lemon Squeezy
   are merchants of record: they are the seller, and that obligation is theirs.
   For a two-person team selling internationally that is the deciding axis, and
   it is a business decision rather than a technical one. It also changes the
   integration: an MoR gives fewer primitives and takes a larger cut.
2. **Overage or hard limits?** **Hard limits (ADR-050)** — entitlements are
   ceilings refused at the point of use, and no usage quantity is ever reported
   to a provider. Managed Payments could not bill overage anyway: no invoice
   items on a subscription, no one-off invoices outside the billing period.
   The original framing: this decides whether the platform needs a
   metering pipeline at all. Hard limits reuse everything Phase 05 built and
   add nothing. Overage means per-project usage aggregated per billing period
   and reported to the provider, which is a new subsystem with its own
   correctness problem — a double-reported unit is a customer's money.
3. **What does a failed payment do, and for how long?** **Fourteen days of
   unchanged service, then ADR-040's storage restriction, and never deletion
   (ADR-051).** The grace period is configuration, not a constant. The original
   framing: criterion 4 says data
   is not destroyed. ADR-040's storage restriction is the mechanism already
   built: revoke `INSERT`/`UPDATE`, keep `SELECT`/`DELETE`/`TRUNCATE`, so a
   customer can still read and still shrink. What needs deciding is the grace
   period, and what happens to a project holding 40 GB when its plan reverts to
   a tier whose quota is 24 MB. Indefinite free storage of paid-sized data is a
   cost; deleting it is the thing criterion 4 forbids.
4. **Prices in the repository, or only in the provider?** **Only in the
   provider (ADR-052)** — the platform stores `plan_code` -> Stripe price id
   plus the ADR-049 product tax code, and no amount or currency. As
   recommended, and for the reason given: Two sources of truth for a number a customer is charged
   is the kind of drift that becomes a refund. `specs/plans-and-limits.yaml`
   stays what it is — an entitlement catalogue.

One further decision, technical rather than commercial, needed before slice 2 —
**answered 2026-08-19 and shipped as ADR-047**; kept here for the reasoning:

5. **Does a paid customer receive the `mldb_<ref>_admin` password, or a role of
   their own?** Recommendation: **a role of their own.** The admin role is what
   the platform's own mediated SQL enters (ADR-039), what maintenance uses, and
   what `specs/tenant-role-model.md` bounds. Handing out its password makes
   rotation a platform outage, makes revocation indistinguishable from
   breaking the console, and gives a credential in a customer's `.env` the same
   identity the platform uses on their behalf. A separate `mldb_<ref>_client`,
   member of the same grants, is revocable and rotatable on its own. This
   changes `specs/tenant-role-model.md` and therefore needs an ADR rather than
   an implementation choice.

## Implementation steps

### Slice 0 — reconcile a project with its plan *(unblocked)*

The whole of the "never" column above, made true. A `plan apply` operation that
takes a project and re-asserts every node-side entitlement: role GUCs, LOGIN,
connection limit, worker pool size, Realtime, storage state. Idempotent, safely
retryable, and safe to run against a project whose plan has not changed —
because the maintenance pass will run it that way.

Deliberately no billing, no route, no plan change: this slice makes the
*existing* silent divergence visible and fixable, and is worth merging even if
every later slice were abandoned. `cp-manage project plan-apply` and a
`plan-drift` report naming projects whose node disagrees with their plan.

### Slice 1 — changing a plan, as an operation rather than an UPDATE

`UPGRADING` status, an audited transition, slice 0's apply as the second half,
and the database identity assertion criterion 1 asks for: same `database_name`,
same `project_ref`, same node, same API keys. Internal/operator route plus
`cp-manage`; still nothing a customer can call, because nobody has paid yet.
Closes an `upgrade_requests` row when it fulfils one.

### Slice 2 — paid direct database access *(needs decision 5)*

The delivery route for a credential, its rotation, and its revocation on
downgrade. Negative tests are the deliverable: a free project has no route to
one; a downgraded project's credential stops working; the credential cannot
reach another tenant; revocation does not break the mediated SQL console.

### Slice 3 — subscription state, kept apart from entitlement state *(done)*

A `subscriptions` table and a state machine — `trialing`, `active`,
`past_due`, `canceled`, `incomplete` — that is **provider-shaped but not
provider-specific**, and never writes entitlements directly. Billing state
proposes; slice 0's apply disposes. That separation is criterion 3, and it is
also what makes a provider swap survivable.

### Slice 4 — the provider *(done)*

Stripe. **Hosted Checkout**, webhooks, and the mapping table. Elements is out,
and not as a style preference: ADR-049 turns on Checkout being the only
integration Managed Payments supports, so an Elements build would silently
foreclose merchant-of-record status. A subscription is created by Checkout and
never by the API.

The mapping table is `plan_code` -> price id + product tax code, per environment
— test-mode and live-mode ids are different strings for the same plan.

The security surface: signature verification before any parsing, idempotency by
Stripe event id, replay refusal, ordering enforced by ADR-048's `state_as_of`,
and no trust whatsoever in amounts or plan codes arriving in a webhook body —
ADR-041's lesson, which is that a value the customer influences cannot be the
control. The plan a subscription entitles is resolved through the mapping from
the price id Stripe reports, never read from the payload as a plan code.

A refund Stripe issues on its own initiative — which Managed Payments permits
within 60 days — is a webhook to handle, not an impossibility.

### Slice 5 — failed payment *(unblocked — ADR-051)*

Fourteen days of unchanged service — configurable, never a constant — then
`canceled`, reconciliation to the default plan, and restriction through the
ADR-040 mechanism. No deletion at any point. An explicit test that the rows,
the database, the `project_ref` and the API keys all survive the whole
transition. The test is the point: criterion 4 is the one acceptance criterion
whose failure is unrecoverable.

### Slice 6 — usage against the billing period *(unblocked — ADR-050)*

Extend `/v1/projects/{ref}/usage` with the period boundaries from the
subscription. **No metered quantity**, because hard limits mean nothing is
metered and no number in this response is ever sent to Stripe. Usage against
the plan's ceiling is what it already computes.

## Verification

- [ ] Unit/integration tests.
- [ ] Tenant-isolation checks — a direct credential is a new way into a tenant
      database and belongs in the negative suite beside the executor role.
- [ ] Documentation/spec updates: `docs/BILLING-AND-PLANS.md`,
      `specs/tenant-role-model.md` if decision 5 adds a role,
      `specs/plans-and-limits.yaml`, `docs/RESOURCE-GOVERNANCE.md`.
- [ ] Any new architecture decision recorded before the slice that implements it.
- [ ] **A security review before merge on every slice.** This is now enforced by
      CI rather than promised here: `scripts/require-security-review.sh` refuses
      a change with no `Security-Review:` trailer, and the job is required on
      `main`. Phase 07 and Phase 08 both carry this box unticked. This phase has
      no mechanism by which it can.
- [ ] No test reaches a live billing provider, and none needs a network.

## Risks

- **A webhook is an unauthenticated public endpoint until its signature is
  checked.** Verify before parsing, not after. Treat the body as hostile and
  the plan code inside it as advisory: the subscription's identity comes from
  the platform's own mapping, never from the payload.
- **Replay and out-of-order delivery.** Providers retry, and a `canceled`
  arriving after a re-`active` would downgrade a paying customer. Idempotency
  by event id, and ordering by the provider's own sequence rather than by
  arrival.
- **Entitlement escalation.** The Phase 07 finding was a customer putting a
  project on any plan they named. Every route added here is a chance to
  reintroduce it, and slice 1's route is the obvious one.
- **A downgrade that destroys data.** Criterion 4. The mechanism to lean on
  exists and is tested; the risk is a new path that does not use it.
- **Reconciliation that silently half-applies.** Slice 0 exists because that is
  the current state. A `plan apply` that fails partway must leave the project in
  `UPGRADING` and say so, not report success.
- **Money in a test suite.** Test-mode keys are still keys. Recorded fixtures,
  no live calls, and the CI secret scan already refuses committed key material.

## Decision log

- 2026-08-19 — Drafted. Slices 0 to 2 deliberately precede any billing decision:
  the reconciliation gap is real today, is independent of who takes the money,
  and is what acceptance criterion 3 is actually asking for.

## Progress log

- 2026-08-20 — **Slice 4 complete: the provider, and the process boundary it
  ran into.** Stripe. Migration 0021, `stripe_api.py`, `billing.py`,
  `api/billing.py`, a maintenance pass, four `cp-manage billing` commands, and
  69 tests. ADR-053.

  **The design was decided by two existing ADRs pulling against each other, not
  by preference.** Stripe posts webhooks from the internet, so the endpoint has
  to be on the public listener — the `hooks.py` precedent does not transfer,
  because GoTrue calls from a node and Stripe does not. But applying a plan
  needs a node superuser credential, and ADR-038 keeps those out of the process
  bound to the internet.

  So the webhook records and never applies, and `maintenance.reconcile_subscriptions`
  applies it where the credentials already live. A customer's plan arrives a
  pass after their payment — seconds to a minute — and that lag is the honest
  price of the split. Every way of removing it puts a node credential in the
  process that answers the internet.

  The tempting implementation would have been one function: receive event,
  write `projects.plan_id`, apply. It is short, obviously correct on the happy
  path, and violates an ADR that is about provisioning rather than about
  billing — which is exactly why it would have got through review.

  **A bug the tests found, and it is the one worth recording.** The
  reconciliation queue was first keyed on `state_as_of`: pending when the
  provider's timestamp differed from the last reconciled one. Stripe's event
  timestamps are **whole seconds**, and `checkout.session.completed` and
  `customer.subscription.updated` routinely arrive inside the same second — so
  the second fact could be marked done by applying the first. Silent loss, in
  the one structure whose entire purpose is that nothing is missed.

  The queue now compares `(state, plan_code)` against what was last applied,
  which is exactly what `entitled_plan_code` reads. It asks the real question —
  has the entitlement moved — rather than a proxy for it, and it does not depend
  on how precise anybody's clock is. `test_marking_it_reconciled_takes_it_off_the_queue`
  fires both events in the same second deliberately.

  **What is trusted and what is not.** The plan comes from a `checkout_sessions`
  row written by an authenticated manager before the customer ever reached
  Stripe (ADR-041). The price id on the incoming subscription is resolved
  through the platform's own map and required to *agree* with that row;
  disagreement is refused rather than resolved, because resolving in favour of
  either would be deciding. `test_the_plan_comes_from_the_recorded_row_and_not_from_the_payload`
  sends an event whose metadata asks for a more expensive plan and asserts the
  customer gets what they bought.

  **The tax-code check was not in the slice as planned and is the sharpest thing
  in it.** ADR-049 flagged the product tax code as something a tax advisor
  decides. Reading Stripe's own eligibility page while implementing turned up
  the failure mode: an ineligible product **does not error**. The transaction
  drops out of Managed Payments and MaluDB silently becomes the seller of record
  for it — which is the whole liability ADR-049 chose Managed Payments to avoid,
  arriving with no error message and discoverable by reconciling a tax return
  months later. `cp-manage billing price set` now refuses an ineligible product,
  with `--unverified` as a named escape hatch that says what it costs.

  **Hand-rolled rather than the SDK**, and the reasoning is not dependency
  count: what this needs is four HTTP calls and one HMAC, and the HMAC is the
  same construction `mail.py` already carries reviewed — HMAC-SHA256 over
  `{timestamp}.{body}`, constant-time compare, freshness window. `stripe_api.sign`
  lives beside the verifier rather than in the tests, so a fixture cannot drift
  from the code that checks it.

  **Answering 200 to refusals is deliberate.** A retry cannot fix an unknown
  session or an unmapped price, and days of redelivery ends with Stripe
  disabling the endpoint and taking the events that *would* have worked. So
  every event is written to `billing_events` before it is acted on, and
  `cp-manage billing events` is where a refusal is found.

  **Security review: 3 findings, all fixed, each with a test that fails without
  the fix.**

  1. **The webhook buffered an unbounded body before it could check anything.**
     The signature cannot be verified until the body has been read, so on an
     unauthenticated public endpoint the caller was choosing how much memory
     this process allocated — and nothing else in the stack bounds a request
     body. Now capped at 256 KB, read from the stream rather than trusted from
     `Content-Length`, because a chunked request declares no length at all and a
     header check is one the caller opts out of by omitting it.
  2. **One checkout row could open more than one subscription.** A subscription
     event resolves its project through the checkout id the platform put in
     Stripe's metadata. Once the first subscription was canceled the project had
     nothing live to refuse a second, so an event replaying the old checkout id
     would open a new subscription on the plan that checkout bought — a paid
     plan granted by a replayed fact rather than by a payment. Reaching it needs
     a valid signature, so it was not a hole an outsider could walk through; it
     is the class of thing worth making impossible rather than unlikely, and a
     nullable `checkout_session_id` with a unique index is where that is cheap.
  3. **A crashed handler swallowed its own event permanently.** The event id is
     claimed *before* the event is acted on, which is what makes duplicates and
     concurrent deliveries safe — and it is also what makes a handler that dies
     in between unrecoverable: the row sits at `received`, and Stripe's
     redelivery, the one thing that would fix it, is turned away as a
     duplicate. A ledger built to guarantee nothing is missed had a way to miss
     something. A row stalled for five minutes can now be taken over by a later
     delivery, through a conditional UPDATE exactly one caller wins — a lease,
     not a retry, so a merely slow handler still cannot have its work done twice
     underneath it.

  Two near-misses caught during the review rather than by it, both now pinned
  by an assertion:

  - The lease's interval started life as an f-string. It is a module constant
    and interpolating it was safe — which is precisely how the habit survives
    long enough to be applied to something that is not. Ruff's `S608` flagged
    it; it is a bound parameter now.
  - Pulling `livemode` out of `stripe_api.Client` into a function turned
    `"_test_" not in key` loose on an empty string, which answers **live**. The
    original code had guarded that with an explicit `else False`, so this was a
    refactor introducing a bug rather than a bug being found — and the one case
    it decides anything is a deployment holding a webhook secret and no API
    key, which would then have accepted live events. Absent configuration must
    fail towards refusing money, never towards taking it.
    `test_absent_configuration_fails_towards_refusing_money` is the assertion
    that caught it and now keeps it.

  Also checked and clean: no generated SQL identifiers anywhere in the slice;
  every statement parameterised; `org_id` read from the project rather than
  accepted, on slice 3's precedent; the composite foreign key repeated on
  `checkout_sessions`; the audit trail asserted to carry no amount, currency,
  customer, session or price id; Stripe errors passed through as
  `error.message` only, never a body, and network failures as an exception type
  name because an httpx error's string carries the request URL and those URLs
  carry object ids.

  **Deliberately not rate-limited: the webhook.** Stripe delivers in bursts and
  retries, and a limiter keyed on source would let a flood starve real
  deliveries — turning a denial-of-service into lost payments. What bounds an
  unauthenticated flood instead is that it does no database work at all: the
  body cap and the signature check both run before a connection is taken.
  `test_the_webhook_refuses_an_unsigned_body` asserts the ledger stays empty.

  **Not done here, and named so it is not assumed:** what happens when a payment
  keeps failing is slice 5. `past_due` still keeps its plan indefinitely — the
  fourteen-day grace ADR-051 decided is not implemented yet, and nothing in this
  slice expires anything.


- 2026-08-20 — **The four billing decisions answered; slices 4 to 6 unblocked.**
  Recorded as ADR-049 to ADR-052. No code in this change: `docs/DECISIONS.md`,
  `docs/OPEN-QUESTIONS.md` and this plan.

  **The provider question was framed against a landscape that had moved.** It
  asked processor *or* merchant of record, treating those as different vendors —
  Stripe against Paddle or Lemon Squeezy — with the integration following from
  the commercial choice. Checked against Stripe's own documentation rather than
  the comparison articles, which are almost all written by competing MoRs:
  **Stripe Managed Payments is Stripe's own merchant-of-record offering.** Stripe
  becomes the legal seller, invoicing as *Sold through Link, LLC*, and registers,
  files and remits indirect tax in 80-plus countries including the US, the EU 27
  and the UK. Same `Customer`, same Billing `Subscription`, same `Price`, same
  Checkout Session, same webhook signature scheme. Enabled per account and per
  Checkout Session.

  So the axis that decides who owes tax turned out not to decide the
  integration at all, which is the best available outcome for a plan whose
  slice 4 was blocked on it: the posture becomes deployment configuration, and
  ADR-048's boundary — a subscription records what is paid for and never writes
  an entitlement — already kept it out of everything else.

  **It does constrain slice 4, and that is why the constraint is in the ADR
  rather than left for the implementer.** Managed Payments supports only hosted
  Checkout and Payment Links; not Elements, not advanced integrations, and a
  subscription cannot be created outside Checkout. An Elements build would work
  perfectly and foreclose the MoR option silently — the cost appearing only
  the day somebody tried to turn it on. That is precisely the class of decision
  `docs/DECISIONS.md` exists to catch before it is made by default.

  **Two answers reinforce each other rather than merely coexisting.** Hard
  limits (ADR-050) were chosen on their own merits — a metering pipeline is a
  subsystem whose correctness is somebody's money. It then turns out Managed
  Payments *cannot* bill overage: no invoice items on a subscription, no
  one-off invoices outside the billing period. So "just add metered billing"
  would not be an incremental feature later; it would be a decision to leave
  merchant-of-record status. Worth recording, because the temptation will not
  announce itself as an architectural change.

  **What the failed-payment answer does not settle.** ADR-051 gives fourteen
  days of unchanged service, then ADR-040's storage restriction, and no
  deletion ever — which satisfies criterion 4 by there being no code that
  deletes. It accepts indefinite retention of restricted projects as the cost.
  Whether a project restricted for a year is ever reclaimed after delivered
  notice is left open in `docs/OPEN-QUESTIONS.md` rather than answered by
  silence, and it needs a notice mechanism the platform does not have.

  **Still needed before live mode, and not before slice 4:** a tax advisor's
  confirmation of the product tax code — `txcd_10102000` (PaaS, business use)
  or `txcd_10103001` (SaaS, business use). ADR-049 declines to decide it; the
  mapping table stores whichever string it is told. Getting it wrong is a
  mispriced tax, not a broken integration.

- 2026-08-19 — **Slice 3 complete: two facts about a plan, and the one that was
  never recorded.** ADR-048, migration 0020, `subscriptions.py`, `cp-manage
  subscription create | set-state | show | reconcile | drift`, and two
  allowlisted audit events.

  **The block was wrong and measuring it is what showed that.** This plan's
  header put slices 3 onward behind the four `## Billing` questions, while the
  per-slice annotations marked only slices 4 and 5 — and slice 3's own
  description asked for something "provider-shaped but not provider-specific".
  Checked one question at a time: which provider changes the *mapping* in slice
  4, not the states here; overage changes metering in slice 6; failed-payment
  grace changes what `past_due` *does*, which slice 3 does not decide; prices
  change slice 4's mapping table. None of them reaches this code. So the header
  was a blanket rather than a finding, and ADR-048 narrows it to slices 4-6
  rather than slice 3 quietly proceeding against a written gate.

  **The property under test is a negative one, which is why most of the suite
  asserts an absence.** Recording a payment, a failed payment, a cancellation
  and an upgrade all leave `projects.plan_id` where they found it. A suite that
  only checked that reconciliation works would pass equally against a webhook
  handler writing entitlements directly — the design this one exists to rule
  out — so the first two tests assert that nothing happened.

  **The cross-tenant control is a composite foreign key, not a check.** A
  subscription names an org (who pays, ADR-020) and a project (what the plan
  applies to, `projects.plan_id`), and two independent references would permit a
  row pairing org A with org B's project — which is not a typo but a control:
  it would let one organization move another's project between plans. A
  `UNIQUE (id, org_id)` on `projects` makes the pair referenceable, so no
  future caller can get it wrong. The module reads `org_id` from the project
  rather than accepting it, which is the same property one layer up.

  **`state_as_of` is here rather than in slice 4, and that was the one design
  call worth arguing about.** The ordering guard looked like webhook plumbing.
  It is not: it is a property of the record, and putting it in the parser would
  mean every future writer — a backfill, an operator, a second provider —
  re-implements it or skips it. A `canceled` arriving after the `active` that
  superseded it downgrades a paying customer, and that is the risk this plan's
  own Risks section names. Strictly-older is refused; equal is accepted,
  because a redelivery of the current truth is idempotent and exact duplicates
  are slice 4's event-id idempotency, which is a different control. A timestamp
  rather than a sequence, because it is the only ordering key all three
  candidate providers expose.

  **No provider columns, deliberately, and this is the discipline the slice is
  about.** A nullable `provider_subscription_id` would have cost nothing to add
  and would have been a guess at the first open question's answer. `ALTER TABLE
  ... ADD COLUMN` in slice 4 costs nothing either, and slice 3 now contains no
  line that changes if the answer comes back Paddle instead of Stripe.

  **`subscription drift` reports a divergence that has always existed.** Every
  paid project on the platform is `unbilled` the day this ships, because
  `project set-plan` moves a project and takes no money — which was correct
  while there was nowhere to record that money had been taken, and is a queue to
  work now that there is. It reports and does not correct, on `plans drift`'s
  precedent plus a stronger reason: moving a project between plans unattended is
  a change that should have somebody's name on it.

  **No HTTP route, on slice 1's precedent and for slice 1's reason.** The only
  consumer a subscription-writing route will ever have is slice 4's webhook
  handler; building one now means designing its authentication twice.

- 2026-08-19 — **Slice 2 complete: paid direct access, delivered through a role
  of its own.** ADR-047, migration 0019, `mldb_<ref>_client`, `GET` and
  `POST .../database/connection[/rotate]`, `cp-manage project backfill-client`
  and `rotate-client-credential`, negative tests S to Y.

  Decision 5 answered by the repository owner: a role of its own. Three
  measurements decided the shape of it, and two changed the design.

  **`SET role = admin` on the client role is load-bearing.** Without it, a
  table a customer creates over their direct connection is owned by the client
  role — and `ALTER DEFAULT PRIVILEGES` only affects objects created by the role
  it names, which is exactly how Phase 08 produced a table the customer's own
  data API could not read. Measured: with the setting, `session_user` is the
  client role, `current_user` is the admin role, and objects are owned by the
  admin role, so a direct connection and the SQL console are indistinguishable
  at the object level.

  **Self-service rotation needs no node credential.** ADR-038 keeps those out of
  the public application, so rotation connects *as the client role* and changes
  its own password. Measured, all four properties: `SET ROLE NONE` returns to
  the client role where `RESET ROLE` returns to admin (the role setting is the
  session default); an ordinary role may change its own password; the same
  session is refused `42501` for the admin role's; and the old password stops
  working immediately. So the capability the route needs is one the customer
  already has, and nothing was widened to provide it.

  **The connection host must not be the node's.** The route first returned
  `nodes.hostname`, which is wrong twice: a node hostname names which node a
  customer shares — `docs/CONTROL-PLANE.md` already treats one as something the
  audit trail must not publish — and it breaks the moment ADR-006's background
  move to another node happens, which the customer's application would discover
  as an outage rather than be told. It is `<ref>.<database_domain>` now,
  following ADR-008's shape for the API URL, with `MALUDB_DATABASE_DOMAIN`
  defaulting under the gateway domain. The control plane's own rotation
  connection still uses `internal_host`, because that one is not the customer's.

  **A predicate bug the suite found in one run.** `_client_done` was written as
  "the role exists", and a leftover role from an earlier run made the step a
  no-op that left the project with no credential at all — a login nobody has the
  password for. It checks both halves now, which is what `_executor_done`
  already did one ADR earlier.

  `mldb_<ref>_admin` is `NOLOGIN` on every tier from here, and
  `set_direct_sql_access` forces it on every call, so a project provisioned
  before this shows up in `cp-manage plans drift` rather than staying quietly
  reachable.

- 2026-08-19 — **Slice 1 complete: a plan change as an operation, and the
  status that would have made it an outage.** `plan_change`, migration 0018,
  `cp-manage project set-plan` and `plan-history`, and a new allowlisted audit
  event.

  **The plan said to use the `UPGRADING` status. Measuring it first is what
  stopped that.** Migration 0017 reserves `UPGRADING` and nothing sets it, so it
  looked like the obvious in-flight marker — until three separate gates turned
  out to serve only `("PROVISIONED", "ACTIVE")`: the gateway's
  `SERVING_STATUSES`, `api/tenant_access.py` for the SQL and schema routes, and
  `workers.py` for starting a worker. Parking a project there would take its
  data API, its console and its workers offline for the duration of a purchase,
  and leave them offline if the change failed partway. An upgrade is the moment
  a customer least wants an outage, and ADR-006 makes keeping the database in
  place the whole point of it.

  So the marker is a `plan_changes` row instead and `projects.status` is not
  touched at all — asserted by a test, along with the status still being one the
  gateway serves. `UPGRADING` stays reserved and unused *deliberately* now, with
  the reason in the migration for whoever reaches for it next.

  **The node is written before the plan row.** A failed apply then leaves the
  project entirely on its old plan. The other order fails in the direction that
  matters: a downgrade that updated the row first and then failed would leave
  `direct_database_access` live on a node while the plan says the project no
  longer has it, indefinitely and silently.

  **Identity is asserted rather than assumed.** Acceptance criterion 1 is
  satisfied by construction — ADR-006 decoupled node movement from purchase, so
  the correct implementation writes no code that moves a database — but "we did
  not write that code" is a claim about the present. `plan_change.identity`
  reads the ref, the database name, the node and the live API key ids before
  and after, and refuses if they differ. API keys are in there because a change
  that rotated one would take a customer's application down just as effectively
  as moving the database, and less visibly.

  **No HTTP route, deliberately.** The plan asked for an internal one. The only
  consumer a plan-change route will ever have is slice 4's webhook handler, and
  building it now means designing its authentication twice — so `cp-manage
  project set-plan` is the only caller until there is a second one. Narrowed on
  purpose rather than forgotten.

  Mutual exclusion is a unique index on `plan_changes(project_id) WHERE state =
  'RUNNING'` rather than a lock, because the thing being excluded is a second
  *process* starting while the first is partway through writing to a node.

- 2026-08-19 — **Slice 0 complete: reconciliation, and the control it found
  applied to nobody.** `plan_apply`, `cp-manage project plan-apply`,
  `cp-manage plans drift`, and drift reporting in the maintenance pass.

  **The finding, measured before any of it was written.** `apply_plan_settings`
  wrote a plan's GUCs to `mldb_<ref>_authenticator` and `mldb_<ref>_auth` — the
  roles *the platform* logs in as, for PostgREST and GoTrue — and to neither
  `mldb_<ref>_admin` nor `mldb_<ref>_executor`, which are the roles a
  *customer's* session logs in as. A direct connection on a paid tenant
  reported `temp_file_limit = -1`, `work_mem = 4MB` and
  `max_parallel_workers_per_gather = 2`: the cluster's defaults against a plan
  saying 256 MB and no parallel workers.

  ADR-017 found only two of these six bind against a client that does not want
  them, and named `temp_file_limit` as one. So the single per-session control a
  tenant cannot switch off was applied to nobody who could switch it off, and
  both paid direct SQL and — since ADR-039 gave every tier a console — the
  executor role could fill a shared node's disk with temp files. ADR-017's
  "free tier is unaffected in practice: it has no direct SQL" was true when
  written and was superseded by ADR-039 without anybody re-reading it.

  Asserted through a real connection rather than off `pg_db_role_setting`,
  because a role setting that is present and not applied is the exact failure
  ADR-017 describes and the catalogue cannot tell the two apart. Verified to
  fail against the old role list: `assert '-1' != '-1'`.

  **An ordering bug the fix produced, and what it changed.** Adding the executor
  to the role list broke provisioning at `DATABASE_CREATING`: the executor is
  created a stage *later*, because it needs `CONNECT` on a database that does
  not exist yet. `apply_plan_settings` now writes to the roles that exist and
  the executor stage applies them again — and a role absent for any other
  reason surfaces as drift rather than as silence, which is the difference
  between tolerating an ordering constraint and skipping a control.

  **Why the maintenance pass reports and does not correct.** `cp-manage project
  direct-sql --disable` is an operator's incident control, and that project's
  plan still says it is entitled. A reconciler on a timer would undo it within
  the hour — a control cancelling a control. So the pass names what diverged and
  which way it points: `withheld` is a plan change that never reached the node,
  which before this slice was every plan change; `excess` is a project getting
  more than its plan grants, which is either that incident measure or a
  privilege nobody is paying for.

- 2026-08-19 — Drafted, not started. Slices 0-2 unblocked; slices 3-6 blocked on
  the four `## Billing` open questions plus decision 5 for slice 2.
