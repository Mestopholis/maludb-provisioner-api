# Execution Plan: Phase 05 — Resource Governance

Status: NOT STARTED — two decisions below need the owner's call before slice 2
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-05-slice-*`, one per slice
Related task: `tasks/PHASE-05-RESOURCE-GOVERNANCE.md`
Dependencies: Phase 04 complete (merged 2026-08-15).

## Objective

Stop one project degrading the node it shares with others. ADR-009 makes this
explicitly layered — gateway limits, PostgreSQL settings, and node capacity
management — and this phase is where the first layer actually exists.

## What is already true

Worth stating, because four of the eleven acceptance criteria are already met by
earlier phases and the plan should not pretend otherwise:

- **Free projects cannot bypass API limits through direct SQL**, because they
  have none (ADR-005, enforced since Phase 02 and asserted by negative test J).
- **Auth workers are not started for projects that do not use Auth** —
  `auth_enabled`, Phase 04 slice 1.
- **Wake waits for readiness rather than port-open**, so the first request after
  a wake serves rather than answering `503 PGRST002` — Phase 03 slice 2.
- **Warm and total project counts are tracked separately** on every node.

The gap in that last one is the phase in miniature: `capacity_of` computes
`current_warm_projects` and `max_warm_projects`, and `rejection_reason()` never
looks at them. The measurement exists and enforces nothing, which is the state
most of this phase is in.

## What is genuinely missing

- **No entitlement resolver.** Limits are read three different ways today: the
  email quota reads `plans.config_json` directly, provisioning takes
  `plan_settings` as an argument from its caller, and the PostgREST pool size is
  a module constant. `specs/plans-and-limits.yaml` lists the intended keys and
  every value is `null`. Nothing can enforce a plan consistently until one place
  answers "what is this project entitled to".
- **No gateway rate or concurrency limit at all.** ADR-009's first layer is
  absent, which makes every other layer the only layer.
- **No storage accounting.** Nothing measures a project's database size, so the
  quota keys in the spec describe an intention rather than a control.
- **Nothing sleeps.** `idle_workers` and `idle_auth_workers` exist and are
  tested; no scheduler calls them. Free-tier economics rest entirely on sleep
  (ADR-022), so this is the difference between a measured design and a working
  one.

## Decisions needed before slice 2

### 1. Where rate-limit state lives — `docs/OPEN-QUESTIONS.md`, "Redis/distributed cache or gateway-local cache first?"

| Option | For | Against |
|---|---|---|
| **Gateway-local, in process** | No new infrastructure, no network hop on the hot path, and ADR-026 already put the gateway's added latency under a microscope | With more than one gateway the effective limit is `N ×` the configured one, and a restart forgets every counter |
| **Redis** | One true counter regardless of gateway count; the obvious destination | New operational dependency for a platform that currently has none, plus a round trip on every request in the path ADR-026 measured at +6.3 ms |

**Recommendation: gateway-local first, with the multiplication written down.**
There is one gateway today. A local counter is honest about what it enforces,
costs nothing on the hot path, and the interface can be a small protocol so
swapping in Redis is a class rather than a rewrite. What must not happen is
shipping a local counter and describing it as a platform-wide limit.

### 2. What "write restriction" means when a project exceeds storage

The scope calls for "quota warnings/write restriction" without saying what
restriction is. The options differ sharply in blast radius:

- **Revoke `INSERT`/`UPDATE` from the API roles**, leaving reads working. Visible
  to the application as a permission error on writes only.
- **`ALTER DATABASE ... SET default_transaction_read_only`**, which also stops
  the tenant's own migrations and anything using direct SQL.
- **Refuse writes at the gateway**, which leaves paid direct SQL unaffected and
  therefore does not actually bound growth.

**Recommendation: revoke write privileges from `anon` and `authenticated`,
keeping reads.** It bounds growth where growth happens, leaves the customer able
to read and export their data, and is reversible by a grant. It is also
customer-visible and irreversible-feeling in the moment, so it needs a warning
threshold crossed first and an audit event — both in scope below.

## Slices

Sequential, with a security review between each.

### Slice 1 — Plan entitlements

One resolver, `entitlements.for_project(conn, project_id)`, returning a typed
view of what a plan allows. Then wire the three existing consumers to it: the
email quota, the PostgREST pool size, and the statement/resource settings
provisioning applies.

No new enforcement — deliberately. This slice makes the later ones possible and
is verifiable on its own: a project on a plan with a given limit resolves to
that limit, an absent key falls back to a documented default rather than to
"unlimited", and a malformed `config_json` fails closed the way `nodes.py`
already does for operator-supplied JSON.

`specs/plans-and-limits.yaml` gets real values for the first time, which is a
product decision as much as a technical one — the plan file says exact public
values are unapproved, so these land as defaults that configuration overrides.

### Slice 2 — Gateway rate and concurrency limits

ADR-009's first layer.

- Per-project request rate, from the entitlement.
- Per-project concurrent in-flight requests, which is the one that protects the
  database: a slow query holds a PostgREST pool slot, and the pool is 3.
- `429` with `Retry-After`, distinguishable from the `429` MaluMail returns for
  email quota — a client that cannot tell them apart cannot act on either.
- **ADR-026 requires a measurement.** The gateway's added latency was recorded
  at +6.3 ms; this slice must re-run `scripts/bench-gateway.py` and record what
  the limiter costs, because the decision to keep Python in the data path was
  made on that number.

### Slice 3 — Storage accounting and quota enforcement

- `pg_database_size()` per project on a schedule, recorded against the project.
- Quotas are **net of the ~23 MB `maludb_core` baseline** (ADR-015): a 100 MB
  free quota that counts the extension is a 77 MB quota described as 100.
- Warning threshold, then write restriction per decision 2, with an audit event
  for both and a documented path back.

### Slice 4 — Sleep, wake and capacity enforcement

- A scheduler that actually calls `idle_workers` and `idle_auth_workers`. Free
  worker sleep is what makes the free tier viable (ADR-022) and nothing invokes
  it today.
- `rejection_reason()` learns about warm capacity, closing the ADR-022 criterion
  that is currently tracked and unenforced.
- **Connection headroom asserted**, not assumed: `warm × backends_per_project`
  against `max_connections` minus reserved. ADR-022 found connections, not
  memory, are the binding constraint at roughly 24 warm projects per node, and
  a node that quietly exceeds that fails at connection time for tenants that did
  nothing wrong.

## Non-goals

- **A transaction-mode pooler.** ADR-022 requires one before roughly 25 warm
  projects per node, and this phase makes that ceiling visible and enforced
  rather than removing it. Raising the ceiling is its own work.
- Billing or usage reporting from the counters this phase creates — Phase 09.
- Native MaluDB resource governance, which ADR-009 lists as eventual.
- Per-endpoint or per-user rate limits. Per project is the unit that protects
  the node.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-05-RESOURCE-GOVERNANCE.md`.
- [ ] A security review per slice.
- [ ] The rate limiter's cost measured, not estimated (ADR-026).
- [ ] Limits demonstrated to be configuration-driven by changing a plan and
      observing the change, rather than by reading the code.

## Risks

- **A limiter is a denial-of-service surface pointed at your own customers.** An
  off-by-one or a wrong default rejects legitimate traffic, and the failure mode
  is silence from a working application. Defaults should be generous and every
  rejection should say which limit was hit.
- **Write restriction is customer-visible and feels irreversible.** It needs a
  warning first, an audit trail, and a documented way back.
- **Python in the data path**, again. The limiter runs per request; ADR-026 made
  keeping Python there conditional on measurement, so this slice owes one.
- **Enforcing warm capacity will make placement fail on nodes that currently
  accept projects.** That is the point, but it changes behaviour for anything
  already provisioned near the line, so it wants a report before it wants
  enforcement.

## Decision log

- 2026-08-15 — Plan created. Two decisions surfaced rather than taken: where
  rate-limit state lives, and what write restriction means.

## Progress log

- 2026-08-15 — Plan created, four slices. Not started.
