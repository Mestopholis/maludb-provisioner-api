# Execution Plan: Phase 04 — Auth and RLS

Status: BLOCKED — slices 1–3 complete; slice 4 needs ADR-029 ratified first
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-04-slice-*`, one per slice
Related task: `tasks/PHASE-04-AUTH.md`
Dependencies: Phase 03 complete (merged 2026-08-15). Slice 4 depends on MaluMail,
a separate product: transactional and marketing email over a REST API **or** an
SMTP relay, with templates, analytics and deliverability handling.

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
- [ ] **MaluMail's API contract for the control-plane surface.** The sending
      side is settled (SMTP, below); what slice 4 still needs is how the control
      plane provisions per-project credentials, reads quota, receives bounces,
      and consults suppression. See slice 4.

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

### Slice 4 — Email through MaluMail *(blocked on ADR-029)*

The MaluMail contract arrived and does not match what ADR-019 assumed, so this
slice is **not buildable as planned**. `AGENTS.md` requires stopping and
proposing an ADR rather than deviating quietly; that is **ADR-029**, and this
slice waits on it being ratified.

What changed, in short: MaluMail exposes a REST API and no SMTP submission, and
GoTrue 2.195.0 turns out to have a Send Email Hook that ADR-019 stated it did
not. Both were verified rather than inferred — the hook was demonstrated
end to end with no SMTP configured at all. So the transport ADR-019 specified is
unavailable on one side and unnecessary on the other.

Assuming ADR-029 is accepted, the slice becomes:

- A platform HTTP endpoint per project, configured into the Auth worker as
  `GOTRUE_HOOK_SEND_EMAIL_URI`, with a per-project secret. Signature verified
  as Standard Webhooks (`webhook-id`, `webhook-signature`, `webhook-timestamp`).
- Message composition, which is now ours: the hook receives an
  `email_action_type` and a token, not a rendered message, so building the
  confirmation link and the body moves into the platform.
- Per-project quota checked **before** the call, not discovered from a `429`.
  MaluMail limits per account and counts one unit per accepted recipient, so
  with one platform key a single noisy project can exhaust every project's
  allowance. This is the sharpest consequence of ADR-029 and the reason quota
  cannot be left to the relay as ADR-019 intended.
- Suppression consulted locally and reconciled from `GET /v1/suppressions` on a
  schedule, since MaluMail has no webhooks. `email_suppressions` already exists
  from migration 0003.
- `200` from `/v1/send` treated as final and never retried — there is no
  idempotency key, and GoTrue retries a failing hook, so a naive implementation
  double-sends. Only `429`, `502` and transient `5xx` are retryable.
- `project_email_settings` revised: its SMTP credential columns model something
  that will not exist, and a per-project hook secret replaces them.

Still genuinely blocked on MaluMail regardless of ADR-029:

- **Custom sender domains.** Verification is a portal step with no API, so
  `sender_mode = 'custom_domain'` cannot be provisioned programmatically.
- **Per-project quota at the relay.** Keys are per account and portal-created,
  so the platform holds one key and enforces per-project limits itself.
- **Immediate revocation on suspend.** Nothing to revoke per project; suspension
  must be enforced in the hook.

Those three are acceptance criteria in `tasks/PHASE-04-AUTH.md` phrased as
properties of the relay. They will need rewording to describe where enforcement
actually lives, or MaluMail will need to grow the API surface they assume.

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
- **Email spans two systems.** The sending path is well understood; the
  control-plane path depends on a MaluMail API this plan has not seen. That is
  the largest remaining unknown in the phase, and the one this repository cannot
  resolve on its own.
- **Auth worker density.** ADR-022 measured Auth as the largest per-project
  cost; starting one per project regardless of use would roughly double warm
  memory for no benefit.
- **Unconfirmed-user squatting** on the `auth.users.email` unique constraint,
  which is why decision 2 exists rather than being left to a later cleanup.

## Decision log

- 2026-08-15 — Plan created. Two decisions recommended (HS256 for this phase;
  24-hour unconfirmed-user expiry). One external dependency raised.
- 2026-08-15 — MaluMail confirmed to offer a REST API alongside the SMTP relay,
  which ADR-019 did not contemplate. Slice 4 becomes a two-surface integration:
  GoTrue sends over SMTP, the control plane uses the API for credentials, quota,
  bounces and suppression. Needs an ADR amending ADR-019, and MaluMail's API
  contract for those four operations.

## Progress log

- 2026-08-15 — Plan created, four slices. Not started.
- 2026-08-15 — Slice 1: tenant auth reconciliation and the GoTrue worker.

  The plan named two collisions; there were **three**, and the first is worse
  than Phase 00 recorded. Reproduced against stock GoTrue 2.195.0 before
  writing the fix:

  1. The auth role had no privilege on the `auth` schema at all -- not even
     USAGE -- so GoTrue could not create its tables. The plan did not name this
     one; it surfaced from reading the actual grants.
  2. Phase 00 predicted GoTrue's bookkeeping would land in `public`. On
     PostgreSQL 15+ it does not: `public` no longer grants CREATE to PUBLIC, so
     the migration fails outright with `permission denied for schema public`.
     The observed failure is a worker that cannot start, not a silent Data API
     leak -- better, but it means Auth was never going to work at all without
     this.
  3. The helper functions were owned by the platform role, so GoTrue's
     `create or replace function auth.uid()` would have raised
     `must be owner of function uid` -- the documented finding 5.

  Bootstrap `007` resolves all three by handing the schema and the four helpers
  to the project's auth role and setting its `search_path`. Verified: 70
  migrations apply, `public` is empty afterwards, `schema_migrations` is in
  `auth`, and `verify()` still passes -- GoTrue's replacement `auth.uid()`
  coalesces both claim keys, and `anon`'s EXECUTE grant survives the replace.

  `auth_workers.py` reuses the Phase 03 supervisor and port allocation rather
  than copying them; `SystemdSupervisor` and `allocate_port` are now
  parameterised by unit template and port column. Auth is off by default
  (`auth_enabled`), which is ADR-022's demand-driven requirement made
  structural rather than remembered.
- 2026-08-15 — Slice 2: signup, sign-in and session lifecycle.

  `/auth/v1` is routed to a per-project GoTrue worker. The gateway's single
  hard-coded REST prefix became a `Surface` table, each entry carrying its own
  port, lifecycle state and activity clock -- ADR-022 requires the two workers
  to sleep and wake independently, and one shared "warm" flag could not express
  that.

  Auth is 404 on a project that has not enabled it, checked **after**
  authentication so it cannot be used to survey which projects run Auth. The
  Auth supervisor is deliberately not defaulted to the PostgREST one: they
  drive different unit templates, and silently reusing the wrong one fails as a
  worker that will not come up rather than as a wiring mistake.

  Seven cases added to the official-client suite, run through the real gateway
  against a real GoTrue: signup, sign-in, the claims RLS needs, get-user,
  refresh, signout, and a wrong password refused. 21 compatibility cases in
  total.

  One assertion was mine being wrong rather than a defect: an HS256 token is a
  pure function of its claims, so refreshing inside the same second returns a
  byte-identical access token. The suite now checks the refreshed session
  *works* instead of that it changed, and the matrix records why.

  Cross-project isolation is tested two ways: two projects never share a
  signing secret -- demonstrated by showing A's token fails to verify under B's
  key, not just that the strings differ -- and a user created in one project's
  `auth.users` does not appear in another's. The second exists because a shared
  user table keyed by email is the usual way a multi-tenant Auth service is got
  wrong, and nothing in the configuration would announce it.
- 2026-08-15 — Slice 3: RLS with real end-user identity.

  Two separately signed-in clients, each seeing only its own rows, with
  `auth.uid()` resolved from a GoTrue-issued token that travelled through the
  gateway rather than from a hand-set `request.jwt.claims`. That is what Phase
  03 could not claim, and it closes the matrix entry.

  **All three cases passed first time, which is why they were then mutated.**
  Two of them failed correctly under `USING (true)`. The third did not fail
  under `WITH CHECK (true)` -- it asserted that inserting a row owned by
  another user errors, and `.insert().select()` errors either way because the
  SELECT policy hides the row being returned. The test was passing vacuously
  while a row really was being planted in the other user's account.

  Rewritten to check from the victim's side: the planting attempt is made, then
  the other user selects and must still see exactly their own row. That version
  fails under the mutation, with the message that names the actual harm.

  The matrix records the reasoning, because the vacuous form is the one someone
  would naturally write again.
- 2026-08-15 — Slice 4 stopped before implementation. The MaluMail contract shows
  a REST-only API with no SMTP submission, no programmatic key provisioning and
  no delivery webhooks — so ADR-019's R1, R2 and R6 cannot be met as written.
  Checking the other side of that assumption showed GoTrue 2.195.0 *does* have a
  Send Email Hook, contradicting ADR-019 directly; demonstrated end to end with
  no SMTP configured. ADR-029 proposed. Per AGENTS.md, implementation stops
  until it is ratified rather than deviating from a recorded decision.
