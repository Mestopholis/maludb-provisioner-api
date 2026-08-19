# Execution Plan: Phase 09 — Billing, and making a plan change mean something

Status: **DRAFTED, NOT STARTED** — 2026-08-19. Slices 0 to 2 are unblocked.
Slices 3 onward are blocked on the four open questions under `## Billing` in
`docs/OPEN-QUESTIONS.md`, which need answering as ADRs before any code that
touches money is written.

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

- The four `## Billing` open questions answered, as ADRs, before slice 3.
- A provider test-mode account for slices 4 onward. No test in this repository
  may reach a live provider, and none may need a network at all: recorded
  fixtures for webhook payloads, signed with a test secret.

## Decisions needed before slice 3

Recorded in `docs/OPEN-QUESTIONS.md` under `## Billing`. Summarised here with
what each one changes, because "payment provider?" understates them.

1. **Which provider, and is it a merchant of record?** Stripe is a payment
   processor: the platform is the seller and owes VAT/sales-tax registration
   and remittance in every jurisdiction it sells into. Paddle and Lemon Squeezy
   are merchants of record: they are the seller, and that obligation is theirs.
   For a two-person team selling internationally that is the deciding axis, and
   it is a business decision rather than a technical one. It also changes the
   integration: an MoR gives fewer primitives and takes a larger cut.
2. **Overage or hard limits?** This decides whether the platform needs a
   metering pipeline at all. Hard limits reuse everything Phase 05 built and
   add nothing. Overage means per-project usage aggregated per billing period
   and reported to the provider, which is a new subsystem with its own
   correctness problem — a double-reported unit is a customer's money.
3. **What does a failed payment do, and for how long?** Criterion 4 says data
   is not destroyed. ADR-040's storage restriction is the mechanism already
   built: revoke `INSERT`/`UPDATE`, keep `SELECT`/`DELETE`/`TRUNCATE`, so a
   customer can still read and still shrink. What needs deciding is the grace
   period, and what happens to a project holding 40 GB when its plan reverts to
   a tier whose quota is 24 MB. Indefinite free storage of paid-sized data is a
   cost; deleting it is the thing criterion 4 forbids.
4. **Prices in the repository, or only in the provider?** Recommended: only in
   the provider, with the platform storing the mapping `plan_code` ->
   provider price id. Two sources of truth for a number a customer is charged
   is the kind of drift that becomes a refund. `specs/plans-and-limits.yaml`
   stays what it is — an entitlement catalogue.

One further decision, technical rather than commercial, needed before slice 2:

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

### Slice 3 — subscription state, kept apart from entitlement state

A `subscriptions` table and a state machine — `trialing`, `active`,
`past_due`, `canceled`, `incomplete` — that is **provider-shaped but not
provider-specific**, and never writes entitlements directly. Billing state
proposes; slice 0's apply disposes. That separation is criterion 3, and it is
also what makes a provider swap survivable.

### Slice 4 — the provider *(blocked on decisions 1 and 4)*

Checkout, webhooks, and the mapping table. The security surface: signature
verification before any parsing, idempotency by provider event id, replay
refusal, and no trust whatsoever in amounts or plan codes arriving in a webhook
body — ADR-041's lesson, which is that a value the customer influences cannot
be the control.

### Slice 5 — failed payment *(blocked on decision 3)*

Grace, then restriction through the ADR-040 mechanism, and an explicit test
that data survives. The test is the point: criterion 4 is the one acceptance
criterion whose failure is unrecoverable.

### Slice 6 — usage against the billing period

Extend `/v1/projects/{ref}/usage` with the period and, if decision 2 says
overage, the metered quantity. Whatever is displayed must be the same number
the provider is told, computed once.

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
