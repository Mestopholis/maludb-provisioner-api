# Execution Plan: Extension function grants (ADR-076)

Status: NOT STARTED — ADR-076 accepted 2026-09-12; grants slice 0 is next.
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
   when every function of that name in the exposed schemas is extension-owned.
3. The OpenAPI listing is counted first; a handful is accepted, a large number
   goes back to the owner.
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

## What is not known, and is why grants slice 0 comes first

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
- **A gateway OpenAPI filter** — only if grants slice 0's count sends decision 3
  back to the owner.
- **Letting customers set their own pre-request function.**

## Implementation steps

### Grants slice 0 — Measure before building

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

### Grants slice 1 — New tenants get the new posture

- Bootstrap 013: `maludb_platform.refuse_extension_rpc()` (owned by the
  platform, `SECURITY DEFINER` only if slice 0 shows it must be, `search_path`
  pinned, `EXECUTE` to the authenticator's request roles); the hardening function
  rewritten to `REVOKE … FROM PUBLIC` then `GRANT EXECUTE` to the six roles, in
  every schema; a repair pass for the functions already present.
- `render_config`: `db-pre-request = "maludb_platform.refuse_extension_rpc"`,
  and in-database precedence closed off per slice 0.
- `tenant_bootstrap.verify`: every extension function's `EXECUTE` grantees are
  exactly the six roles (no `PUBLIC`), the check function exists with the expected
  owner, and both event triggers fire.
- Provisioning already bootstraps before the worker starts, so a new tenant has
  no window; asserted, not assumed.
- Tests, each with a negative control (break it, watch it fail, restore):
  - every customer role runs a `vector` distance query, `similarity()`, `crypt()`,
    and inserts into a `uuid_generate_v4()` default;
  - `/rpc/gen_salt` as `anon` through PostgREST is refused with slice 0's status,
    and `gen_salt` did not execute;
  - a customer's own `public` function is still callable over `/rpc`;
  - an extension installed after bootstrap (`citext`, in `public` and in a
    customer schema) gets the same grants;
  - the tenant admin cannot remove or replace the check;
  - negative test R and `tests/test_direct_sql.py` restated for the new posture.
- `specs/tenant-role-model.md` updated.

### Grants slice 2 — Existing tenants

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

- [ ] Grants slice 0 findings recorded, with a reproducing script.
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
