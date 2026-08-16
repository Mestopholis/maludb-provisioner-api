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
