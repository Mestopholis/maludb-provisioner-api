# Execution Plan: Phase 07 — Customer Dashboard

Status: **NOT STARTED** — drafted 2026-08-16, for the repository owner's review.
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-07-slice-*`, one per slice
Related task: `tasks/PHASE-07-DASHBOARD.md`
Dependencies: Phase 06 complete (merged 2026-08-16, PR #46). **ADR-037 and
ADR-038 are Accepted**, ratified 2026-08-16 before slice 0 builds either — a
decision left Proposed while its code is written makes the implementation the
decision, which is the lesson Phase 06 slice 1 recorded.

## Objective

The minimum production API for account and project operations, reachable from
the internet, with self-serve signup open at launch.

**This phase builds no interface.** ADR-025 puts the web frontend in its own
repository; what belongs here is the API that frontend consumes, and the
acceptance criterion that matters most is the one that says so — the dashboard
uses control-plane APIs rather than direct privileged database operations. Every
slice below is therefore an API slice, and "the dashboard can do X" means "there
is an endpoint for X and a test that drives it".

## What is already true, measured before planning

Eight facts about the repository as it stands. Three of them change the shape of
the work and one of them is a security finding.

### Nothing creates a project

`grep -rn "INSERT INTO projects" services/` returns nothing. Project rows exist
only in tests, and `cp-manage` has `project retry`, `cleanup`, `email` and
`storage` but no `create`. `jobs.provision()` takes a `project_id` that must
already exist.

So "create project" is not exposing an existing function over HTTP. It is new
code on the provisioning path: reference allocation, placement reservation
(`nodes.reserve_placement`, which exists), the row, and then the provisioning
run. `AGENTS.md` requires provisioning operations to be idempotent or safely
retryable, and this one will be reachable by anyone who can sign up.

### The API process can already decrypt every node's superuser credential

`nodes.admin_dsn()` unwraps a per-node admin DSN from `nodes.admin_ciphertext`
using the KEK, and the control-plane process holds the KEK — it needs it for
project credentials. No route calls `admin_dsn` today, so the exposure is
currently theoretical.

Phase 07 is what makes it concrete: it adds routes, and ADR-037 adds a listener
bound to the internet. A public process that can decrypt node superuser
credentials is the sharpest concentration of privilege in the platform, and
`docs/ARCHITECTURE.md` already says the equivalent about the gateway ("do not
place database superuser credentials in the gateway"). The same sentence should
be true of the public control-plane application, and slice 0 is where it becomes
enforced rather than merely true by accident.

### The control plane has no rate limiting of any kind

`services/gateway/limits.py` fronts tenant traffic. Nothing throttles
`/v1/auth/signup` or `/v1/auth/signin`. With signup public at launch (decided
2026-08-16), this is the first thing a public listener needs, and it is a
credential-stuffing surface independently of free-tier policy.

### Five smaller ones, each of which is scope

- **No password reset exists for platform users.** `identity.py` authenticates,
  creates sessions and PATs, and manages invitations and roles; it cannot reset
  a password. The task's scope lists it.
- **Sessions and PATs can be created and revoked, but not listed.** Revocation
  without a list is a control a customer cannot exercise: they cannot revoke
  what they cannot see.
- **API key material is already the right shape.** Publishable keys are sealed
  and re-openable (`key_ring.open`), secret keys are hashed only. That is
  exactly the one-time/reveal/reset policy the acceptance criterion asks for, so
  slice 2 is an endpoint over an existing model rather than a change to it.
- **`audit_events` exists** (migration 0003). Audit visibility is a read model,
  not new capture.
- **Everything privileged is CLI.** Provisioning, node registration,
  `realtime-check`, placement and Realtime enablement are `cp-manage`. Keeping
  them there is a choice this phase should make deliberately rather than drift
  out of.

## Decisions taken before slice 1

All four were answered by the repository owner on 2026-08-16, before any code.

1. **ADR-037 accepted**: two applications, internal by default, separate
   listeners, with the public route set asserted by a test.
2. **Provisioning runs in a worker (ADR-038).** The public application allocates
   the reference, reserves placement and records the request; a worker holding
   the node admin credentials runs `jobs.provision`. `provisioning_jobs` already
   records attempts and `jobs.provision` is already resumable, so the queue is
   reusing Phase 02's machinery rather than inventing a second one — and the
   internet-facing process has no path to a node's superuser at all.
3. **Platform MFA is deferred** and recorded in `docs/OPEN-QUESTIONS.md`. It is
   self-contained, nothing depends on it, and it is the piece most likely to
   expand a phase that is otherwise the difference between having customers and
   not. What is deferred is the choice of factors and whether owners may require
   it of members.
4. **CAPTCHA on signup from day one**, rather than added when abuse appears.
   Slice 5 chooses the provider and, more importantly, its failure mode: what
   happens to signup when the challenge service is unreachable. Failing closed
   stops signups; failing open removes the control precisely when someone is
   most likely to be attacking it.

## Slices

Sequential, with a security review between each — the Phase 06 rule, and it
found something in its own slice's code every time.

### Slice 0 — The split, and the first throttle

The security foundation, before any new route exists to be classified wrongly.

- Two applications per ADR-037, built from the same routers, internal by
  default, with a test asserting the public application's route set is exactly
  the classified one.
- **The public application cannot decrypt node admin credentials.** Asserted,
  not assumed: the finding above is that it currently can, and a test that fails
  when someone wires `admin_dsn` into a public route is the only version of this
  that stays true.
- Rate limiting on the control plane, starting with the unauthenticated and
  credential-checking routes. Phase 05's limiter is a gateway component against
  a different threat model; whether it is reused or a control-plane one is
  written is a slice decision, but the limit is configuration-driven either way
  (`AGENTS.md`).
- `specs/control-plane-api.yaml` gains the public/internal distinction, and
  `scripts/export-openapi.py` exports the public document as the contract.

### Slice 1 — Project creation and status

- Reference allocation, placement reservation, the row, and the provisioning
  request enqueued — the node work belongs to the worker (ADR-038), never to the
  request that asked for it.
- The provisioner worker itself, in the shape ADR-027 already uses for the other
  workers, holding the node admin credentials the public application must not.
- A status endpoint a dashboard can poll, reading `provisioning_jobs` rather
  than inventing a second source of truth.
- Idempotent and safely retryable, including a double-submitted create.
- **No privileged database credentials in any response** — the first acceptance
  criterion, and the reason `ProjectOut` already omits `node_id` and
  `database_name`.

### Slice 2 — Keys and connection details

- List, reveal (publishable) and reset; secret keys shown once at creation and
  never again, which the model already enforces.
- The project's API URL, which is derived from the ref and the gateway domain
  rather than stored.
- The second acceptance criterion, and a test that a free project's response
  carries nothing that would let it connect to PostgreSQL directly.

### Slice 3 — Usage and limits

- Storage from Phase 05 slice 3's accounting, connection and request limits from
  `entitlements`, Realtime slots from Phase 06, email quota from Phase 04.
- An upgrade entry point that records intent. **Not billing** — Phase 09 owns
  payment, and this must not grow a second entitlement path.

### Slice 4 — The account-management gaps

- Password reset for platform users, which does not exist.
- Session and PAT listing, so revocation is exercisable.
- Ownership transfer, which `identity.guard_owner_tier` already has the
  invariants for.

### Slice 5 — What a public free tier needs

- Signup velocity limits per source, and CAPTCHA, which is required from day
  one. Its failure mode is the decision worth arguing about, not its provider.
- Account-farming defences, given one user may hold several organizations.
- Audit visibility over `audit_events`, scoped to what a customer may see.

## Non-goals

- The dashboard itself (ADR-025 — separate repository).
- Billing, payment and plan changes that move money (Phase 09).
- Storage (Phase 10), Supabase migration tooling (Phase 08).
- Platform MFA, deferred (`docs/OPEN-QUESTIONS.md`).
- Mining and spam *detection*, which needs telemetry this phase does not build;
  the abuse work here is preventative.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-07-DASHBOARD.md`.
- [ ] A security review per slice.
- [ ] The public application demonstrably cannot reach node admin credentials —
      ADR-038's whole point, and a test rather than a review comment.
- [ ] A project created through the API and provisioned end to end, on a real
      node, by the same tests that cover `cp-manage`'s path.
- [ ] Rate limits shown to be configuration-driven by changing one and observing
      the change, rather than by reading the code.

## Risks

- **This is the first internet-facing control-plane surface.** Every mistake in
  it is remotely reachable by definition, and the phase adds routes faster than
  any phase so far.
- **A public process holding the KEK.** It can decrypt node admin credentials
  today; slice 0 must close that or the rest of the phase is built on it.
- **Project creation is new code on the provisioning path**, reachable by anyone
  who can sign up, and provisioning is the operation `AGENTS.md` singles out for
  idempotency.
- **The abuse surface arrives with the launch that needs it.** Signup is public
  from day one and the control plane has never been throttled.
- **Scope**: the task file lists fourteen capabilities. Slices 4 and 5 are where
  that pressure lands, and MFA is the piece most likely to expand.

## Decision log

- 2026-08-16 — Plan drafted. Signup is public at launch (repository owner), so
  free-tier abuse controls are in this phase rather than deferred. ADR-037
  proposed for the public/internal split; `/v1/plans` stays authenticated
  because it is an entitlement catalogue rather than a price list, and Phase 09
  is where prices exist.
- 2026-08-16 — All four outstanding decisions answered by the repository owner:
  ADR-037 accepted, provisioning moves to a worker (**ADR-038**, written and
  accepted the same day), platform MFA deferred, CAPTCHA required from day one.
  Nothing is left blocking slice 0.

## Progress log

- 2026-08-16 — **Slice 5 complete, and reviewed before commit rather than
  after.** Three controls: a challenge on signup (`captcha.py`, provider behind
  a protocol, Turnstile implemented), a per-organization projects cap
  (`max_projects`, an entitlement like any other), and an audit trail a customer
  can read (`api/audit.py`, allowlisted event types *and* allowlisted
  `detail_json` keys).

  Each control fails in the direction that costs the platform rather than its
  customers' safety. A challenge service that cannot be reached blocks signups
  instead of waving them through, and `MALUDB_CAPTCHA_FAIL_OPEN=1` inverts that
  in configuration so the choice is made by somebody who means it. A deployment
  that requires a challenge and configured no provider refuses rather than
  accepting everybody through the development verifier. An event type nobody has
  classified is invisible rather than published — `detail_json` is free-form and
  written by several subsystems, so returning the row and redacting what looks
  sensitive is the wrong way round.

  **The security review found a privilege escalation that shipped in slice 1.**
  `plan_code` on project creation was an entitlement the caller granted
  themselves: `plan_by_code` accepted any active plan, nothing checked whether
  the organization was entitled to it, and `GET /v1/plans` hands every
  authenticated user the codes. Naming `production` gave an unbilled project a
  hundred projects instead of two, production resource settings, and
  `direct_database_access: True` — which is `AGENTS.md`'s "free projects are
  API-only" invariant and a named item in its own review rules. Self-service
  creation now accepts only the default plan and answers a forbidden code
  exactly as it answers an unknown one; upgrades go through the queue slice 3
  built, which is operator-mediated on purpose.

  One thing attempted and withdrawn: a `SELECT ... FOR UPDATE` on the
  organization row to make the cap hold under concurrency. It deadlocked
  against the suite's own `TRUNCATE` — 5 failures and 17 errors — and was
  removed rather than shipped half-understood. The cap is therefore a soft limit
  against somebody deliberately racing it, documented as such at the call site.
  A lock whose failure mode is unclear is worse than a gap that is written down.

- 2026-08-16 — **Slice 3 complete.** `GET /v1/projects/{ref}/usage` and the
  upgrade-request routes; migration 0015 adds `upgrade_requests`. Two decisions
  taken by the repository owner before any code, because both were product
  choices rather than implementation ones.

  **Usage reports what the platform records and names what it does not.**
  Storage carries the figure, the time it was measured and Phase 05's
  `storage_state`; email is counted from `email_events`. Requests and
  connections report their limit with `metered: false` and `used: null`,
  because ADR-030 keeps the gateway's limiters in-process and nothing
  accumulates a counter — and reading live connections would mean querying the
  node, which ADR-038 keeps out of this process. A zero would be a claim about
  usage the platform cannot make.

  The `null`-is-not-`0` distinction runs through the response shape and is the
  thing most likely to be "simplified" later: a project measured five minutes
  ago and one never measured at all are different situations, and reporting
  both as zero tells a customer their database is empty on the strength of
  nobody having looked.

  **An upgrade request is a row in a queue and grants nothing.** Phase 09 owns
  payment; a route here that moved the project onto the paid plan would grant
  paid capacity to a project nobody had billed. Pressing twice returns the open
  request; a closed one lets the customer ask again, which is why the unique
  index is partial. The operator's note is never selected into a customer
  response.

  One thing writing it corrected: `projects.plan_id` is NOT NULL, so "a project
  with no plan" is not a state that exists. The real robustness case is a plan
  *code* this deployment's `entitlements.DEFAULTS` does not recognise — a rename
  or an older release — where `resolve` falls back to free rather than raising.
  A usage endpoint that raised on it would break the dashboard for exactly the
  customers least able to explain why.

- 2026-08-16 — **Slice 2 complete.** `services/control_plane/api/api_keys.py`
  lists, creates and revokes; `ProjectOut` gains `api_url`, derived from the ref
  and the gateway domain rather than stored — it is a fact about how this
  deployment routes rather than about the project, and a stored one would let a
  project keep a URL the platform no longer serves.

  The acceptance criterion was already true and this slice had to keep it true:
  a secret key is stored as a verifier, so there is no reveal route because
  there is no value anywhere to return. The test asserts that at the storage
  layer as well as the response layer, since "the route chooses not to" is a
  weaker claim than "there is nothing to send". Listing returns the publishable
  key inline rather than behind a reveal call: a dashboard needs it on every
  page load, and ceremony around a value that is public by design suggests it is
  a secret.

  Reset is create-then-revoke, two calls on purpose. A rotate endpoint revoking
  at the moment it mints would break every running client between two
  deployments, and a customer who wants that can do it in that order anyway.

  One relationship the tests found nothing was asserting: a key can be created
  while a project is still provisioning, and it does **not** authenticate until
  the project is serving — `api_keys.authenticate` refuses any key whose project
  is not PROVISIONED or ACTIVE. Good behaviour, and it lives in a different
  module from the routes that now depend on it, so it is asserted rather than
  assumed.

  Full suite 582 passed / 33 skipped.

- 2026-08-16 — **Slice 1 complete.** `POST /v1/organizations/{org_id}/projects`
  allocates a reference, reserves placement in the same transaction and records
  the request; `services/control_plane/provisioner.py` claims the row with
  `FOR UPDATE SKIP LOCKED` and runs `jobs.provision` with the node credential
  the public application must not hold. Migration 0014 adds the idempotency
  key, who asked and when. 202 rather than 201, because the project named in
  the response does not exist on a node yet.

  Three things the tests found before a customer could have:

  **Nothing seeds the `plans` catalogue.** Entitlements resolve their limits by
  plan *code* with their own defaults, so `plans` is a catalogue an operator
  populates rather than the source of the numbers — and a deployment that never
  populates it could not create a project, while answering the caller `404
  unknown plan` for a plan they never named. It is a 503 naming the platform
  now, and the catalogue is an operator prerequisite worth documenting.

  **A test that lied.** The two-worker test opened its first connection as
  `db.connection().__enter__()` without holding the context manager, which let
  it be garbage-collected — returning the connection to the pool and releasing
  the lock the test existed to assert. Both workers were then the same
  connection, which sees its own lock as its own. Two real connections now,
  because two workers are two processes.

  **The wrong terminal state.** Provisioning ends at `PROVISIONED`; `ACTIVE` is
  what Phase 03 records once a project's workers serve. Asserting `ACTIVE` was
  asserting that this slice does a later slice's work.

  And one correction to slice 0's own guard: it counted function-local imports
  as module-level, so `realtime.py` breaking an import cycle inside two of its
  functions made the guard demand a refactor of code no public route can call.
  The closure is module-level now, while the forbidden-call scan still walks
  every scope — and both violation shapes were re-checked by writing them,
  including a function-local import of the provisioner.

- 2026-08-16 — **Slice 0 complete.** `create_public_app` and `create_app` are
  built from one `_build`, the classification lives in `PUBLIC_ROUTERS`, and
  three tests guard it — each verified by breaking it, because a control that
  has only ever been seen passing has not been tested. The first version of the
  route-set assertion found *nothing at all* (FastAPI 0.141 keeps an included
  router as a wrapper rather than flattening it) and an empty set satisfies
  "serves no unclassified route" perfectly. The second version derived its
  expectation from `PUBLIC_ROUTERS`, so moving a router by mistake moved the
  expectation with it; the paths are written out now.
  ADR-038's invariant is a static import-graph test rather than a runtime one:
  what matters is what the public application *can* reach, and a test watching
  which functions today's handlers call would pass the day someone imports the
  provisioner and fail the day they call it, which is a slice too late.
  `services/control_plane/ratelimit.py` is new. One design error, caught by a
  test rather than by review: the account bucket originally spent a token on
  every signin *attempt*, which rations the person it protects — several
  devices, or a session short enough to sign in daily, and a legitimate user
  locks themselves out. It counts failures now, checked before the password is
  verified and charged only when it was wrong, while the source bucket counts
  attempts and is released on success.
  Full suite 558 passed / 33 skipped, contract in sync.

- 2026-08-16 — **A flaky test found while running the suite, and not fixed
  here.** `tests/test_storage.py::test_re_measuring_an_already_restricted_project_does_not_re_audit`
  is order-dependent: it passes alone, and in a full run it fails in *both*
  directions — `assert 2 == 1` with this slice's changes, `assert 0 == 1` with
  them stashed and only the provisioning suites ahead of it. So it predates
  slice 0 and is not a regression from it. Two wrong counts in opposite
  directions means the test depends on state it does not control, and one of
  those directions — an evaluate that audits *twice* — is the exact property it
  exists to assert, so it deserves its own investigation rather than a
  quietened assertion. Left failing rather than folded into an unrelated slice.

- 2026-08-16 — Drafted, not started. The four decisions it opened with are
  answered; slice 0 is unblocked and is the split plus the first throttle.
