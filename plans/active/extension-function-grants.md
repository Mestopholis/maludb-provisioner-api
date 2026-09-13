# Execution Plan: Extension function grants (ADR-076)

Status: IN PROGRESS — grants slices 0–2 done 2026-09-13; grants slice 3 (compatibility evidence and docs) is next.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/adr-076-extension-grants`, then one branch per slice
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md` (unblocks vector search)
Dependencies: pinning slice 0 merged (#120), whose finding 7 this answers.
Independent of the pinning plan's later slices; both touch
`extension_upgrade.py`'s neighbourhood and whichever lands second rebases.

**Slices are numbered "grants slice N"**, so they are not confused with the
data-model graph's slices or the pinning plan's.

## Objective

Make one sentence true that is currently false:

> A Supabase application that queries pgvector by distance, inserts into a table
> with a `uuid_generate_v4()` default, or calls `crypt()` in a trigger works on
> this platform as every customer role — and `anon` still cannot call
> `gen_salt`, or any other extension function, over the Data API.

## What ADR-076 decided, so this plan does not relitigate it

1. `anon`, `authenticated`, `service_role` and the tenant's `admin`, `client` and
   `executor` hold `EXECUTE` on every extension-owned function, in every schema.
2. A platform-owned PostgREST `db-pre-request` function refuses `/rpc/<name>`
   when ~~every~~ **any** function of that name in the exposed schemas is
   extension-owned (amended after grants slice 0, finding 5).
3. ~~The OpenAPI listing is counted first.~~ **Closed: the 19 listed extension
   paths are accepted** (grants slice 0, finding 2).
4. Explicit grants to those six roles, `PUBLIC` still revoked; the event trigger
   grants that set instead of revoking.
5. Existing tenants through a canary-then-batches run, with the pre-request check
   live before the grants land on each.

## What is already true

- `bootstrap/003_extension_hardening.sql` revoked `EXECUTE` in `public`;
  `005_extension_hardening_trigger.sql` made it an event trigger
  (`maludb_harden_extensions`, not exception-handled); `011_harden_every_schema.sql`
  removed the `public` filter. All three are immutable.
- `tenant_bootstrap.verify` asserts no extension function in `public` is
  executable by `anon` or `authenticated`, and that both event triggers fire.
- `workers.render_config` writes PostgREST's file with `db-config = true`; the
  data-model graph already relies on in-database config on the authenticator
  (ADR-074), measured to take effect on `NOTIFY pgrst, 'reload config'` in under
  half a second.
- `cp-manage project bootstrap --ref` applies pending bootstrap files to one
  project; there is no fleet-wide bootstrap run.
- Tests asserting today's posture: `tests/test_tenant_bootstrap.py`
  (`test_no_extension_function_is_executable_by_api_roles`,
  `test_anon_cannot_call_gen_salt_specifically`, and the install-after-bootstrap
  pair), `tests/test_direct_sql.py` (the admin cannot grant `gen_salt` to
  `anon`), `tests/test_extension_install.py` (negative test R). The compatibility
  matrix row `extension_functions_as_rpc`.

## What was not known, and is why grants slice 0 came first

All answered by grants slice 0; see `specs/extension-grants-model.md`. In short:
`request.path` is available; the check refuses before the body runs, as a clean
403, for ~0.5 ms; 19 names are listed and 14 were reachable; the customer cannot
remove the check; "every" left a bypass, so the rule is "any"; and in-database
configuration overrides the file, which verification must guard.

- **Whether PostgREST 14.17 gives a pre-request function what it needs.**
  `request.path` (and its exact form for `/rpc/gen_salt`), whether pre-request
  runs for RPC before the function is resolved or called, and what status and
  body a raised error produces — a clean 4xx naming nothing internal, or a 500.
- **What PostgREST lists and can call.** With `anon` holding `EXECUTE`, how many
  of the 373 appear in the OpenAPI description, and how many are actually
  callable over `/rpc` (unnamed arguments restrict it). Decision 3 waits on the
  first number.
- **Whether a customer can remove the check.** The tenant admin must not be able
  to set `pgrst.db_pre_request` in-database, on the authenticator or any role
  PostgREST reads, nor replace the function. And which wins if the file and the
  in-database setting disagree.
- **Overloads.** A customer function sharing a name with an extension function
  stays callable; confirm an extension overload of that name is not then reachable
  through it with different arguments, or record that it is.
- **Where the check runs cheapest**: the lookup's cost per RPC request on a
  tenant with the full extension set.

## Scope

- The pre-request function, the flipped hardening function and trigger, in a new
  bootstrap file; `db-pre-request` in `render_config`.
- `tenant_bootstrap.verify` asserting the exact grant set and the configured
  check.
- The tests above rewritten to the new posture, each with its negative control.
- A fleet run for existing tenants.
- Official-client compatibility evidence; matrix rows; `specs/tenant-role-model.md`;
  customer-facing notes on the occupied pre-request hook.

## Non-goals

- **Vector search as a feature.** Unblocked here, decided separately.
- **Relocating extensions** into an `extensions` schema — unavailable while
  `maludb_core` hard-codes `public` (ADR-018).
- **A gateway OpenAPI filter.** Decided against after grants slice 0: the 19
  listed paths are accepted.
- **Letting customers set their own pre-request function.**

## Implementation steps

### Grants slice 0 — Measure before building (done 2026-09-13)

A spike (`scripts/spike-extension-grants.py`, findings in
`specs/extension-grants-model.md`) against a bootstrapped tenant and a real
PostgREST 14.17, with grants restored by hand on that tenant only:

- Pre-request: `request.path` for `/rpc/gen_salt`, for `/rpc/<customer fn>`, and
  for a table path; refused with a custom SQLSTATE — the resulting status and
  body; whether the extension function ever executes (a side-effecting probe).
- OpenAPI: extension paths listed for `anon`, `authenticated`, `service_role`.
- `/rpc` callability of each of the 373 as `anon`, before the check.
- The tenant admin attempting `ALTER ROLE <authenticator> IN DATABASE … SET
  pgrst.db_pre_request`, `CREATE OR REPLACE` of the check, and file-versus-
  in-database precedence.
- A customer `public.similarity(text)` beside pg_trgm's: which calls succeed.
- Latency of RPC requests with and without the check.

**Stop and report** if PostgREST cannot refuse before the call executes, if a
refusal can only be a 500, or if the tenant admin can remove the check: decision 2
is then infeasible as written.

### Grants slice 1 — New tenants get the new posture (done 2026-09-13)

**As built**, where it differs from the steps below: the check lives in its own
`maludb_guard` schema, not `maludb_platform`, so the request roles' `USAGE`
does not reach the bootstrap ledger; it is two bootstrap files, 013 (the check)
and 014 (the grants), with `apply` holding 014 back unless the caller says the
check is live; the worker names the check only once 013 is recorded
(`workers.pre_request_for`); `maludb_core` is excluded from the grant (ADR-076,
amended); and the check percent-decodes the path, after the security review
reached `gen_salt` as `/rpc/gen%5Fsalt`.

- Bootstrap 013: `maludb_platform.refuse_extension_rpc()` — owned by the
  platform, `SECURITY INVOKER` (slice 0 ran it as the request role and it read
  `pg_proc`/`pg_depend` as such), `search_path` pinned, `USAGE` on
  `maludb_platform` and `EXECUTE` to `anon`, `authenticated`, `service_role`,
  refusing with `PT403` when **any** function of the name is extension-owned; the hardening function
  rewritten to `REVOKE … FROM PUBLIC` then `GRANT EXECUTE` to the six roles, in
  every schema; a repair pass for the functions already present.
- `render_config`: `db-pre-request = "maludb_platform.refuse_extension_rpc"`.
  In-database configuration overrides the file (slice 0, finding 7), so nothing
  may write `pgrst.db_pre_request` on the authenticator, and `verify` asserts it
  is absent.
- `tenant_bootstrap.verify`: every extension function's `EXECUTE` grantees are
  exactly the six roles (no `PUBLIC`), the check function exists with the expected
  owner, no in-database `pgrst.db_pre_request` is set for the authenticator, and
  both event triggers fire.
- **Granting `USAGE` on `maludb_platform` to the request roles is new** — slice 0
  granted it so the check could run. Everything else in that schema must stay
  unexecutable by them; a test lists what those roles can reach there.
- Provisioning already bootstraps before the worker starts, so a new tenant has
  no window; asserted, not assumed.
- Tests, each with a negative control (break it, watch it fail, restore):
  - every customer role runs a `vector` distance query, `similarity()`, `crypt()`,
    and inserts into a `uuid_generate_v4()` default;
  - `/rpc/gen_salt` as `anon` through PostgREST is refused with slice 0's status,
    and `gen_salt` did not execute;
  - a customer's own `public` function is still callable over `/rpc`, and one
    sharing an extension function's name is refused (slice 0, finding 5);
  - an in-database `pgrst.db_pre_request` fails `verify`;
  - an extension installed after bootstrap (`citext`, in `public` and in a
    customer schema) gets the same grants;
  - the tenant admin cannot remove or replace the check;
  - negative test R and `tests/test_direct_sql.py` restated for the new posture.
- `specs/tenant-role-model.md` updated.

### Grants slice 2 — Existing tenants (done 2026-09-13)

**As built**, where it differs from the steps below: the run cannot write a
worker's file or probe its port — workers are node-local, the run is on the
control plane — so the check goes live through an in-database
`pgrst.db_pre_request` and a reload notification, and the evidence is
`pg_stat_activity`: no PostgREST connected, or one with its listener (ADR-076,
grants slice 2 note). `cp-manage extension grants --node`; attempts in
`extension_grant_upgrades` (migration 0036); `tenant_bootstrap.apply_held` applies
014 and verifies in one transaction.

- `cp-manage tenant grants-upgrade --node <n> [--batch-size N]`, reusing
  `extension_upgrade`'s node lock, refused node states and canary/batch shape.
- Per tenant: write its PostgREST config with `db-pre-request` and reload a
  running worker, **confirm the check refuses `/rpc/gen_salt`** (a sleeping worker
  reads the file at its next start), then apply bootstrap 013 in one transaction
  with `verify`, then record. The first failure stops the run with that tenant
  rolled back.
- A table recording attempts, in the class of `extension_upgrades` (the next free
  migration number when the slice lands).
- `projects.bootstrap_version` updated only for tenants that verified.

### Grants slice 3 — Compatibility evidence and docs

- Official client (`tests/compat/`): a `match_documents` RPC ordering by
  `<=>` called as `anon` and as a signed-in user; `insert` into a table with a
  `uuid_generate_v4()` default as a signed-in user; `rpc('gen_salt')` refused.
- The OpenAPI outcome from slice 0 and decision 3, asserted.
- `specs/compatibility-matrix.yaml`: `extension_functions_as_rpc` re-evidenced;
  a supported row for extension functions from SQL; the occupied pre-request hook
  as an intentional incompatibility.
- `docs/OPEN-QUESTIONS.md` closed; `docs/MIGRATION` notes if a migrated schema's
  behaviour changes; plan to `plans/completed/`.

## Verification

- [x] Grants slice 0 findings recorded, with a reproducing script.
- [ ] Every customer role uses `vector`, `pg_trgm`, `pgcrypto` and `uuid-ossp`
      functions from SQL, defaults and triggers.
- [ ] `anon` cannot reach any extension function over `/rpc`; the function does
      not execute; negative control shows the check is what refuses.
- [ ] A customer function in `public` remains callable over `/rpc`.
- [ ] The tenant admin cannot remove the check.
- [ ] The fleet run upgrades a canary, stops at a failing tenant with it rolled
      back, and no tenant is ever observed with grants and no check.
- [ ] Existing suites pass, compatibility included.

## Risks

- **A window with grants and no check** reopens ADR-018's finding. Mitigated by
  ordering (check first, confirmed, then grants) and by a test that samples the
  window during the fleet run.
- **A pre-request bug refuses all RPC**, including customers' own functions: a
  Data API outage per tenant. The canary exists for this; the check is
  exercised against a customer function before the run proceeds.
- **The hook is PostgREST's only one.** A later platform need for pre-request
  (rate limiting, auditing) must compose into the same function.
- **A sleeping worker's config** is read at its next start; a tenant whose worker
  never restarts after the file change and before the grants is exactly the
  window above, which is why the run confirms the refusal rather than assuming it.

## Decision log

- 2026-09-12 — ADR-076 accepted, five questions decided one at a time by the
  repository owner. A gateway OpenAPI filter was offered with a wrong reason (that
  the gateway already rewrites responses) and withdrawn before the owner relied on
  it; decision 3 is measure-then-decide.

## Progress log

- 2026-09-12 — Plan written. No code.
- 2026-09-13 — **Grants slice 2 built.** Measured first: an in-database
  `pgrst.db_pre_request` is live on a running worker 0.18 s after a reload
  notification and 1.1 s after a lost listener reconnects. Owner decision: that
  mechanism, with the listener as evidence, over a node-local run. Tests on real
  tenants, each with its control: a worker without a listener stops the run and a
  listening one is granted; grants failing verification roll back, and removing
  verify from the transaction makes that test fail; a real PostgREST from a
  pre-check file refuses `gen_salt` after the run, and answers it when the run
  skips the setting.
- 2026-09-13 — **Grants slice 1 built.** Bootstraps 013 and 014, the hold in
  `tenant_bootstrap.apply`, `verify` for both states, `db-pre-request` in the
  worker config from 013 on. Owner decision: `maludb_core` excluded from the
  grant. Security review found the check compared the undecoded path —
  `/rpc/gen%5Fsalt` answered a salt — fixed and asserted through PostgREST.
  Negative controls: the compat suite without the check fails its RPC case; the
  worker test's unguarded PostgREST answers `gen_salt`; removing the hold fails
  the holding test.
- 2026-09-13 — **Grants slice 0 measured** against a provisioned tenant and
  PostgREST 14.17 (`specs/extension-grants-model.md`,
  `scripts/spike-extension-grants.py`). No stop condition met. Two owner
  decisions on the findings: the check refuses a name if **any** function of it is
  extension-owned, closing a measured bypass; the 19 listed OpenAPI paths are
  accepted. New for slice 1: in-database config overrides the file, so its absence
  is verified; and `USAGE` on `maludb_platform` for request roles is audited.
