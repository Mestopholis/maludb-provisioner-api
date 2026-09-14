# Execution Plan: public launch — Developer, Builder, Professional

Status: NOT STARTED — written 2026-09-14 from the sales tier review
(`docs/sales/tier-readiness.html`) and the launch-timing discussion. Nothing here is
built yet.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: one per engineering slice, named `launch/<slice>`
Related task: `tasks/DEPLOYMENT.md`, `tasks/PHASE-09-BILLING.md`, `tasks/PHASE-07-DASHBOARD.md`
Dependencies: `plans/active/deployment-topology.md` (its two-machine walk is this plan's
first human step); `plans/active/memory-spaces.md` is **not** a dependency — see Non-goals.

**Slices are numbered "launch slice N". Human-owned steps are "launch step H-N".**

## Objective

Take real money for three self-serve tiers on a deployment somebody other than its
authors installed, with every claim on the pricing page true on the day it is published:

| Tier | Plan code (unchanged) | Proposed price | Sold on |
|---|---|---|---|
| Developer | `free` | $0 | own PostgreSQL database, Data API, Auth, Storage, SQL console, vector search, schema graph |
| Builder | `starter` | $49/mo | production backend: direct connections, 7-day point-in-time recovery, Realtime, always-warm API |
| Professional | `production` (retuned) | $149/mo | production capacity for teams, longer recovery window, more projects |

Plan **codes do not change**. Display names, prices and limits do. Existing rows in
`projects.plan_id`, `billing_prices` and `subscriptions` reference the codes.

## Scope

- Plan catalogue: display names, limits retuned to the prices, Stripe price mapping.
- Dashboard: buy a plan, see usage against limits, understand a limit that was hit.
- The free-tier abuse gaps already recorded as open.
- The human steps that engineering cannot do: deployment, Stripe, legal, operations.
- A launch checklist that gates the announcement.

## Non-goals

- **Memory spaces (ADR-079).** Professional launches on capacity, recovery and team
  features. Memory ships to every tier when `plans/active/memory-spaces.md` completes and
  is announced then. The pricing page must not mention it before.
- **Hybrid search.** Builder's pitch drops "hybrid search" until it is built.
- **Dedicated and Enterprise tiers.** Sales-assisted, later; they need a per-customer
  node pool (ADR-065's revisit condition), a hardware cost model, and contract billing
  outside Stripe Checkout (ADR-049, ADR-050).
- **Multi-node.** `docs/DEPLOYMENT.md` states one node is a single point of failure and
  multi-node is unsupported; launch accepts that and says so (launch step H-7).

## Preconditions

- Phases 01–12 merged; `main` green.
- maludb_core pinned at 0.105.x with the vector owner fence merged (#146).

## Implementation steps

### Launch slice 1 — The catalogue says what sales will sell

- Display names Developer / Builder / Professional in `specs/plans-and-limits.yaml`
  (`plans sync` writes the name) and in the dashboard's public view
  (`frontend/app.js` `PUBLIC_PLANS`), prices filled in — the public view currently shows
  `—` for both paid plans.
- **Builder** (`starter`): `max_projects` 20 → a small number the owner confirms (the
  tier is "one production app"; a few leaves room for staging).
- **Professional** (`production`): retuned to $149. The current defaults — 100 GB
  database, 250 GB objects, 1 TiB egress a month, 30-day point-in-time recovery — cost
  well over $500 at a comparable provider and, under ADR-050's hard limits, are what the
  heaviest $149 customer can consume. Proposed for the owner to confirm: 25–50 GB
  database, 14-day recovery window, lower egress. Keeping `node.backup-policy`'s rule in
  mind: the longest window any offered plan sells sets what a node must retain.
- `tests/test_public_pricing.py` keeps the public page equal to `entitlements.DEFAULTS`;
  update both together.
- Every number stays configuration (`AGENTS.md`); this slice changes defaults and the
  published projection, not application logic.

### Launch slice 2 — A customer can buy a plan from the dashboard

The APIs exist (`POST /v1/projects/{ref}/billing/checkout`, `GET /v1/projects/{ref}/usage`,
Phase 09); `frontend/` has no billing UI.

- Project page: current plan, usage against each published ceiling, billing period and
  grace state from `usage.billing` (ADR-051: `grace_ends_at` is the earliest the
  restriction can arrive, and must be worded that way).
- Upgrade button for managers (the route answers 403 to members — say why, don't hide the
  button silently), redirecting to hosted Checkout; the return page explains that the plan
  applies within a minute (ADR-053: the maintenance pass applies it).
- A limit that was hit is a conversion surface (ADR-050 consequences): 429/413 responses the
  dashboard shows name the limit and link to upgrade.
- No amount or currency rendered from the platform (ADR-052); prices on the public page are
  the published list, receipts are Stripe's.

### Launch slice 3 — Close the recorded free-tier abuse gaps (built 2026-09-14)

**As built:** the project-cap race is closed with a per-organization advisory lock held
from the count to the commit (a forced-race test fails without it). **No
organizations-per-user cap:** organizations are only ever created at signup, one per
account, so each one already costs a signup and a challenge — the gap was not an
amplifier. `cp-manage abuse report` is the detection report, from control-plane data;
CPU and live connections are node-side and not in it.


From `docs/OPEN-QUESTIONS.md` "What controls a self-serve free tier?":

- **The project cap race** (`api/projects.py`): serialise the count and insert per
  organization. The first attempt deadlocked against the test suite's `TRUNCATE`; find
  the lock that does not, with a test that races two creates.
- **Organizations per user**: no cap today, so the per-organization project cap is per
  account times unlimited organizations. Add an entitlement-style limit.
- **Detection**: a report of free projects by CPU, connections and egress against their
  ceilings, so the review in launch step H-6 has something to review.

### Launch slice 4 — The launch checklist runs as a command where it can (built 2026-09-14)

**As built:** `deploy preflight` gains three checks — the maintenance pass has finished a
run in the last fifteen minutes (runs are now recorded in `maintenance_runs`, migration
0040); the signup challenge is required, configured and fails closed (fatal in
production); and the dashboard address is not the default once billing is on. Price
mappings per mode were already checked. **No timer unit shipped:** which host runs the
maintenance pass is an open topology question (`docs/OPEN-QUESTIONS.md`), so preflight
checks that it runs, not where.


- Extend `cp-manage deploy preflight` with what launch adds: every offered paid plan has a
  price mapping in the mode the deployment runs (live vs test), Turnstile is configured and
  failing closed, signups are not open before a node is placeable, the maintenance pass is
  scheduled (a stale last run is a failure, since it is what applies purchases).
- What a command cannot see stays a checklist in `docs/DEPLOYMENT.md` §5.

## Human-owned steps

| Step | What | Why it is on the critical path |
|---|---|---|
| H-1 | **The two-machine deployment walk** (`plans/active/deployment-topology.md`): fresh control plane and node from `docs/DEPLOYMENT.md` alone; DNS, TLS, the internal listener unreachable from outside | It is that plan's acceptance test and nothing here launches without it |
| H-2 | **Off-site backup repository** for the node and the control-plane dump, KEK stored separately (ADR-064, ADR-070) | A backup in the same failure domain is not a backup |
| H-3 | **Node operator steps**: `cp-manage node pin set` for `vector` and `maludb_core`, `node extension-check`, `extension grants` (ADR-076), `extension upgrade` so existing tenants get fenced vector wrappers | A node without pins places no projects |
| H-4 | **Stripe**: live account with Managed Payments enabled, products created with eligible tax codes, prices, `billing price set` per plan, webhook endpoint and secret | `billing price set` refuses an ineligible product; a missing mapping makes the plan unbuyable (409) |
| H-5 | **Legal**: terms of service, privacy policy, acceptable-use policy | The AUP is unresolved in `docs/OPEN-QUESTIONS.md` and is not an engineering decision; public signup is decided (2026-08-16) |
| H-6 | **Abuse review owner**: who reads launch slice 3's report, how often, and what they do (suspend is an explicit state transition) | A public free tier on shared nodes attracts mining and spam |
| H-7 | **Support and status**: a support address, where incidents are announced, and the published position that one node is a single point of failure | Customers need a place to ask, and the SPOF is a known position rather than a surprise |
| H-8 | **Confirm the numbers**: Builder project cap, Professional retune, and the pricing page copy against `docs/sales/tier-readiness.html` | Sales' prices and the page must agree on launch day |

## Sequencing

1. H-1, H-2, H-4 and H-5 start now; they are the long poles and none depends on code.
2. Launch slices 1 and 3 in parallel; slice 1 waits on H-8's numbers for its final values.
3. Launch slice 2 after slice 1 (it renders the names and prices).
4. Launch slice 4 last, against the deployment from H-1.
5. H-3 on the real node, then preflight, then the §5 checklist, then open signups.

Engineering is estimated at about a week of slices; the launch date is set by H-1, H-4 and
H-5.

## Verification

- [ ] `cp-manage deploy preflight` exits 0 on the launch deployment.
- [ ] `docs/DEPLOYMENT.md` §5 checklist completed and recorded.
- [ ] A real signup on the real site reaches an ACTIVE project.
- [ ] A real card buys Builder in live mode, the plan applies, the receipt is Stripe's.
- [ ] A failed payment walks ADR-051's grace period in test mode end to end.
- [ ] The pricing page matches `entitlements.DEFAULTS` (`tests/test_public_pricing.py`) and
      sales' tier sheet, and mentions nothing unbuilt (memory spaces, hybrid search).
- [ ] Two concurrent project creates cannot exceed the cap (launch slice 3 test).

## Risks

- **Professional under-priced for its limits.** Hard limits mean the heaviest customer
  consumes the whole ceiling for $149. Mitigation: H-8 retune before publishing; limits are
  configuration and can be lowered for new projects without a release.
- **Selling past one node.** Capacity is one node until multi-node is supported.
  Mitigation: `cp-manage node list` capacity watched daily after launch; signups closed
  (`MALUDB_SIGNUPS_OPEN`) before placement starts refusing.
- **Purchases not applied.** The webhook records, the maintenance pass applies (ADR-053);
  an unscheduled pass means customers pay and nothing changes. Mitigation: launch slice 4
  fails preflight on a stale pass.
- **Memory announced early.** Sales positioning leads with memory; the page must not until
  memory spaces ship. Mitigation: verification item above.

## Decision log

- 2026-09-14 — Launch Developer, Builder and Professional together; Professional sold on
  capacity, recovery and team features; memory spaces and hybrid search excluded from launch
  copy; Dedicated and Enterprise later and sales-assisted. From the owner's review of sales'
  proposed tiers.

## Progress log

- 2026-09-14 — Launch slice 3: project-cap race closed; organizations-per-user found not to
  be a gap; `cp-manage abuse report` added.
- 2026-09-14 — Plan written. No code.
