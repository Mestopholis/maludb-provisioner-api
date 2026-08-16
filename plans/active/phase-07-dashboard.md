# Execution Plan: Phase 07 — Customer Dashboard

Status: **NOT STARTED** — drafted 2026-08-16, for the repository owner's review.
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-07-slice-*`, one per slice
Related task: `tasks/PHASE-07-DASHBOARD.md`
Dependencies: Phase 06 complete (merged 2026-08-16, PR #46). **ADR-037 is
Proposed**, and slice 0 either ratifies it or replaces it — the split is the
first thing built, so leaving it Proposed while implementing it would make the
implementation the decision, which is the mistake slice 1 of Phase 06 recorded.

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

## Decisions needed before slice 1

1. **ADR-037**, Proposed: two applications, internal by default. Slice 0 ratifies
   or replaces it.
2. **Who provisions a project a customer just asked for?** The public
   application must not hold node admin credentials, so it cannot call
   `jobs.provision` itself. Options: the internal application runs provisioning
   on a request from the public one; a worker process consumes
   `provisioning_jobs`; or creation stays operator-driven and the dashboard only
   requests it. **Recommendation: a worker.** `provisioning_jobs` already
   records attempts and error codes, `jobs.provision` is already resumable, and
   a queue is the shape that survives a node being briefly unreachable — which a
   synchronous HTTP call is not.
3. **Platform MFA: in scope or deferred?** `tasks/PHASE-07-DASHBOARD.md` lists
   MFA enrolment. Nothing exists for it, and it is a self-contained piece.
   **Recommendation: defer**, record it in `docs/OPEN-QUESTIONS.md`, and do not
   let it hold the rest of a phase that is otherwise the difference between
   having customers and not.
4. **CAPTCHA on signup from day one, or velocity limits first?** A consequence
   of the public-launch decision; slice 5 needs the answer, not slice 0.

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
  request — with the node work done by whatever decision 2 chooses, not in the
  request.
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

- Signup velocity limits per source, and CAPTCHA if decision 4 says so.
- Account-farming defences, given one user may hold several organizations.
- Audit visibility over `audit_events`, scoped to what a customer may see.

## Non-goals

- The dashboard itself (ADR-025 — separate repository).
- Billing, payment and plan changes that move money (Phase 09).
- Storage (Phase 10), Supabase migration tooling (Phase 08).
- Platform MFA, if decision 3 defers it.
- Mining and spam *detection*, which needs telemetry this phase does not build;
  the abuse work here is preventative.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-07-DASHBOARD.md`.
- [ ] A security review per slice.
- [ ] The public application demonstrably cannot reach node admin credentials.
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

## Progress log

- 2026-08-16 — Drafted, not started. Four decisions outstanding, listed above.
