# Execution Plan: Phase 04 — Auth and RLS

Status: NOT STARTED — one dependency question below decides whether slice 4 is buildable
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-04-slice-*`, one per slice
Related task: `tasks/PHASE-04-AUTH.md`
Dependencies: Phase 03 complete (merged 2026-08-15). Slice 4 depends on
`malumail` implementing R1–R7 of `docs/EMAIL.md`, which is outside this repository.

## Objective

An end user signs up and signs in through the official client, and their JWT
drives RLS on the Data API — completing the claim Phase 03 could only make for
anonymous callers.

```javascript
await client.auth.signUp({ email, password })
await client.auth.signInWithPassword({ email, password })
const { data } = await client.from('notes').select('*')   // now filtered by auth.uid()
```

## Two things already in the tenant that GoTrue will collide with

Both are documented Phase 00 findings; neither is handled by the code Phase 02
and 03 shipped. They are stated first because they are the difference between
"start a GoTrue worker" and "start a GoTrue worker that migrates successfully".

### Bootstrap 002 pre-creates the auth helpers, and GoTrue's migration will refuse them

Phase 00 finding 5: with `auth.uid()` already present and owned by the platform
role, GoTrue's migration fails with `ERROR: must be owner of function uid`.

Bootstrap `002` creates `auth.uid()`, `auth.jwt()`, `auth.role()` and
`auth.email()` as the platform superuser, and it does so for a good reason —
GoTrue's own version reads the legacy `request.jwt.claim.sub`, which returns
NULL against PostgREST 14 and fails every policy closed. Slice 3 of Phase 02
made that an asserted property.

So the two requirements are in genuine tension and one of them has to move.
Finding 5 names the way out: **create the helpers owned by the project's auth
role** rather than the platform role, so GoTrue's migration may replace them,
and re-assert the modern definitions afterwards. That makes the ordering
explicit — bootstrap establishes a correct version, GoTrue migrates over it, and
verification confirms the surviving definitions still read
`request.jwt.claims`. `tenant_bootstrap.verify()` already tests exactly that
behaviour and can be reused unchanged as the post-migration gate.

### The auth role has no `search_path`, so GoTrue bookkeeping lands in `public`

Phase 00 finding 4 and ADR-018 both require
`ALTER ROLE mldb_<ref>_auth IN DATABASE <db> SET search_path = auth, public`.
Grepping the tree, nothing sets it. Without it GoTrue's first migration creates
`schema_migrations` in `public`, where PostgREST exposes it as a table on the
customer's Data API.

This is a Phase 02 miss rather than new work, and it is cheap to fix — but it
must land *before* the first GoTrue worker starts, because afterwards the table
already exists in the wrong schema and moving it is a data migration on a live
tenant rather than a configuration change.

## Preconditions

- [x] Phase 03 complete — gateway, API keys, PostgREST workers, proof milestone.
- [x] `project_credentials.jwt_signing` already exists per project, provisioned
      in Phase 03 slice 2. GoTrue must **reuse** it; minting a second one gives a
      project whose own Auth tokens its own Data API rejects.
- [x] `/auth/v1` is already routed and answers a deliberate 404. Implementing
      Auth is adding a routing entry and a worker, not inventing routing.
- [x] The gateway already forwards an end-user JWT untouched, with tests.
- [x] `project_email_settings` exists, with SMTP credentials modelled as Class B
      encrypted (`docs/SECRETS.md`).
- [ ] **`malumail` implements R1–R7 of `docs/EMAIL.md`.** Outside this
      repository. Slice 4 cannot be verified without it — see below.

## Decisions needed before slice 1

### 1. JWT signing stays HS256 per project for this phase

`docs/AUTH.md` targets asymmetric signing with JWKS so that rotation and
downstream verification do not depend on sharing one secret. That remains the
right destination and is **not** this phase.

Recommendation: keep the per-project HS256 secret. It is already provisioned,
PostgREST is already configured with it, and the Phase 03 gateway already mints
`service_role` tokens with it. Changing the algorithm mid-phase would touch all
three at once, in the phase that is also introducing end-user identity.

`docs/AUTH.md` also says the key-storage and rotation design "must receive a
dedicated security review before production". Asymmetric signing plus that
review is a phase of its own, and pretending otherwise would deliver neither
well. What Phase 04 owes is the **documented rotation design** its own
acceptance criteria already ask for — written against HS256, with the migration
path to JWKS stated.

### 2. Unconfirmed-user retention

ADR-019 requires a policy: unconfirmed users hold the `UNIQUE` constraint on
`auth.users.email` and can block the legitimate owner of an address indefinitely.

Recommendation: expire unconfirmed users after a configured interval, defaulting
to 24 hours, enforced by the control plane rather than by GoTrue — the same
place plan limits live, and configuration-driven per `AGENTS.md`.

## Slices

Sequential, with a security review between each.

### Slice 1 — Tenant auth reconciliation and the GoTrue worker

The two collisions above, plus worker lifecycle.

- Bootstrap `007`: transfer ownership of the `auth` helper functions to the
  project's auth role, and set the auth role's `search_path`. A new file
  because `002` is immutable once applied, and because existing tenants need
  the change too.
- GoTrue worker lifecycle reusing the Phase 03 `workers.py` machinery —
  systemd template unit per ADR-027, port allocation, readiness rather than
  port-open.
- **Demand-driven start.** ADR-022 measured the Auth worker as the single
  largest per-project allocation at 17.6 MB PSS and requires it not be started
  for projects that do not use Auth. Worker accounting must therefore count
  Auth workers separately, not fold them into "warm".
- Config generated from the existing `jwt_signing` credential and the project's
  auth-role DSN, read back through the key ring.
- Verified by GoTrue migrating cleanly against a tenant provisioned by Phase 02
  code, with `schema_migrations` in `auth` and `verify()` still passing.

### Slice 2 — Signup, sign-in, session lifecycle

- `/auth/v1` added to the gateway routing table; worker started on demand.
- Signup, password sign-in, refresh, get-user, signout through the official
  client, against the real gateway — the Phase 03 slice 4 pattern, which caught
  a path-prefix defect no amount of stubbed testing would have.
- **Cross-project negative test**: a user of project A cannot authenticate to
  project B, and A's token is refused by B's Data API. The tokens are signed
  with per-project secrets, so this should hold structurally; it gets an
  explicit test because "should hold structurally" is how the Phase 02 findings
  described themselves before they were found.
- Email confirmation disabled for this slice only, with the switch to
  confirmed-by-default happening in slice 4. ADR-019 is explicit that
  `MAILER_AUTOCONFIRM=true` is not a production default; this is a temporary
  test posture, and slice 4 is what removes it.

### Slice 3 — RLS with real end-user identity

Completes what Phase 03 could only claim for anonymous callers.

- Two signed-in end users see only their own rows through the official client.
- `auth.uid()` resolves from a GoTrue-issued token, end to end, rather than a
  hand-set `request.jwt.claims`.
- `specs/compatibility-matrix.yaml` `rls` moves from partial to covering the
  signed-in path, and the auth features move off `planned` — only for what the
  suite actually exercises.

### Slice 4 — Email through the relay *(gated on `malumail`)*

Everything ADR-019 and `docs/EMAIL.md` require: per-project SMTP credentials,
confirmation on by default, password reset, quota enforced at the relay,
immediate revocation on suspend, bounce and complaint feedback, and a
suppression list consulted before every send.

This slice carries seven of the phase's fourteen acceptance criteria and
**cannot be verified against a stub**. A local SMTP sink can prove GoTrue is
configured correctly and that a confirmation link round-trips, but not that
quota is enforced at the relay, that suspension revokes sending, or that
bounces arrive — those are properties of `malumail`, not of this repository.

If the relay is not ready, the honest options are to build slices 1–3 and stop,
or to build 1–3 plus the local-sink half of 4 and mark the relay-dependent
criteria explicitly unmet. What is not acceptable is calling the phase complete
with those criteria checked off against a stub.

## Non-goals

- OAuth, magic links, MFA, enterprise SSO — already `deferred` in the matrix.
- Asymmetric signing and JWKS — decision 1; a phase of its own with a dedicated
  security review.
- Supabase user migration. `docs/AUTH.md` is explicit that seamless migration
  must not be promised until tested, and password-hash portability alone is a
  substantial piece of work.
- Realtime and Storage.
- Custom sender domains with DKIM. `project_email_settings` models them; only
  `platform_default` is in scope here.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-04-AUTH.md`, or an explicit
      statement of which are unmet and why.
- [ ] A security review per slice.
- [ ] Compatibility matrix promoted only from tests through the real gateway
      with the official client.
- [ ] CI runs the Auth suite with no skips, as slice 0 established for
      `maludb_core`.

## Risks

- **Cross-project token acceptance.** The failure mode is one project's user
  reading another's data. Per-project signing secrets make it structurally
  unlikely; slice 2 tests it rather than reasoning about it.
- **The GoTrue migration collision** is the most likely thing to make slice 1
  take longer than expected, because it is a three-way constraint between
  upstream's migration, ADR-018's ownership rules, and the claim-key behaviour
  Phase 02 asserted.
- **Email is an external dependency** and the largest single risk to finishing
  the phase. It is also the one this repository cannot resolve on its own.
- **Auth worker density.** ADR-022 measured Auth as the largest per-project
  cost; starting one per project regardless of use would roughly double warm
  memory for no benefit.
- **Unconfirmed-user squatting** on the `auth.users.email` unique constraint,
  which is why decision 2 exists rather than being left to a later cleanup.

## Decision log

- 2026-08-15 — Plan created. Two decisions recommended (HS256 for this phase;
  24-hour unconfirmed-user expiry). One external dependency raised: whether
  `malumail` implements R1–R7.

## Progress log

- 2026-08-15 — Plan created, four slices. Not started.
