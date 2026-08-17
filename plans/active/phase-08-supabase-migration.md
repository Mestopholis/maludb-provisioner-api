# Execution Plan: Phase 08 — Supabase Migration, and the SQL surface it migrates into

Status: **DRAFTED, NOT STARTED** — 2026-08-17. Slice 0 is unblocked; slices 5
onward remain blocked on three open questions.

Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-08-slice-*`, one per slice
Related task: `tasks/PHASE-08-SUPABASE-MIGRATION.md`
Dependencies: Phase 07 complete (merged 2026-08-16, PR #54). **ADR-039 Accepted,
ratified 2026-08-17**, before any slice writes code — ADR-037 and ADR-038 were
handled the same way, because a decision left Proposed while its implementation
is written makes the implementation the decision.

## Objective

Make an existing Supabase project analyzable and migratable to MaluDB for a
defined compatibility subset — and, first, give a MaluDB project somewhere for
that schema to land.

## What is already true, measured before planning

### A free project cannot create a table

The control plane's routers are `health`, `auth`, `organizations`, `plans`,
`projects`, `api_keys`, `usage`, `audit` and `hooks`. There is no SQL or DDL
route. Free resolves `direct_database_access: false`, which leaves
`mldb_<ref>_admin` `NOLOGIN` with a stored password it can never use, and
PostgREST performs no DDL — it exposes objects that already exist.

This is why the SQL surface leads the phase rather than trailing it. The
migration story ends with "now apply this schema to your new project", and
there is currently no way to apply anything to any project on any tier.

### The containment work is done; this adds a caller, not a privilege

`tests/test_direct_sql.py` already pins what `mldb_<ref>_admin` cannot do: reach
another tenant, `CREATE EXTENSION`, grant extension functions to `anon` and undo
ADR-018, own or drop `public` or touch `maludb_platform`, or hold `SUPERUSER`,
`CREATEDB`, `CREATEROLE`, `BYPASSRLS` or `REPLICATION`. Phase 02 and the
direct-SQL slice paid for that. What this phase adds is a way to reach the role,
not a change to what the role may do.

### Per-statement limits in the plan catalogue are not enforcement

ADR-017: five of six are `context = user` and a tenant already defeated one.
`specs/plans-and-limits.yaml` gives free `statement_timeout_ms: 8000`,
`lock_timeout_ms: 3000`, `work_mem_mb: 4`,
`max_parallel_workers_per_gather: 0` and `database_connections: 10`. Only the
last, and `temp_file_limit_mb`, actually bind. So the numbers are reusable as
*targets* while the enforcement has to come from the platform holding the
connection.

### PostgREST already learns about customer DDL

`services/control_plane/bootstrap/006_schema_reload.sql` installs
`ddl_command_end` and `sql_drop` event triggers issuing
`NOTIFY pgrst, 'reload schema'`. Its comment names "the dashboard SQL editor"
among the callers it was written for. Nothing to build; something to assert.

### Storage restriction does not bind the role the console will run as

`storage.py` sets `RESTRICTED_ROLES = ("anon", "authenticated")` and revokes
`INSERT`/`UPDATE` from those two only, leaving reads, deletes and truncates so a
customer can shrink out of it. The tenant admin role is deliberately unrestricted
— which is sound today, because paid direct SQL already bypasses it (and
`docs/RESOURCE-GOVERNANCE.md` says so) while free has no path to that role at
all, making the gateway sufficient for free.

Slice 1 removes that asymmetry. Without a control-plane check, a storage-restricted
free project writes its way back over quota through the console. This is the
audit's most substantive finding and it is slice 1 scope, not a later cleanup.

### No customer on any tier can install an extension, and Supabase's free tier can

`tests/test_direct_sql.py` and negative test H both assert that
`mldb_<ref>_admin` gets `permission denied` for `CREATE EXTENSION`. Supabase runs
its SQL editor as `postgres` and uses `supautils` to let that role install from a
60-plus allowlist (`supautils.privileged_extensions`), on the free plan.

**ADR-010's text does not require the stricter reading.** It says customers
cannot install *arbitrary* extensions on a shared node — not that they can
install none. The current implementation is stricter than the decision, in the
same shape as the ADR-005 finding: the ADR is narrower than the paraphrase the
code was written against.

This is Phase 08 scope rather than a later nicety, because migrated Supabase
schemas routinely open with `create extension if not exists "uuid-ossp"` or
`pgcrypto`, and "supported extensions" is already a scope line in the task. The
mechanism is also half-built: `bootstrap/005_extension_hardening_trigger.sql`
fires on `CREATE EXTENSION`/`ALTER EXTENSION` and revokes the new functions from
`anon`, deliberately not exception-handled so a failed revoke aborts the install.
That is the hard half of what `supautils` does. What is missing is the allowlist
itself and a `SECURITY DEFINER` installer that checks it.

Needs a decision before slice 5 (see slice 4): allowlisted self-service install,
or scanner-reports-blocker plus an operator-applied install. Either is a
defensible answer; shipping neither means migration fails on line one.

### Rate limiting exists and is control-plane-side

`services/control_plane/ratelimit.py` (`LocalLimiter`, `Limit`, `Decision`) came
with Phase 07 slice 0 and fronts auth routes today. ADR-030 records that its
state is per-process and that the multiplication is written down. Reuse it
rather than adding a second limiter.

## Scope

- Platform-mediated SQL execution for every tier, per ADR-039.
- Read-only schema introspection for the dashboard.
- Role impersonation for RLS debugging.
- Compatibility scanner.
- Schema/data migration, RLS/functions/triggers/indexes, supported extensions.
- Initial Auth migration where proven.
- Validation report and cutover runbook.

## Non-goals

- **Direct PostgreSQL endpoint architecture for paid users.** Open question,
  Phase 09. ADR-039 deliberately does not answer it.
- **A SQL editor UI.** ADR-025 puts the frontend in its own repository. This
  phase builds the API that a Monaco editor calls.
- **Typed schema-mutation endpoints** in the shape of `postgres-meta`'s
  create-table/add-column API. The frontend can compose SQL; introspection is
  what it cannot compose. Revisit if the frontend repository asks.
- **Storage and Edge Function migration.** Phase 10 and later.

## Preconditions

- ~~ADR-039 Accepted~~ — done 2026-08-17, with the `AGENTS.md` and
  `docs/RESOURCE-GOVERNANCE.md` corrections it requires, in the plan PR.
- The three `## Migration` open questions answered (blocks slice 5).

## Implementation steps

### Slice 0 — the decisions, and nothing customer-visible

ADR-039 is ratified, and the documentation it contradicted was corrected in the
same PR rather than left for this slice — `AGENTS.md`'s invariant bullet and
`docs/RESOURCE-GOVERNANCE.md`'s free-tier principles and storage-quota note. An
accepted ADR contradicting `AGENTS.md` is the worst place in the repository to
leave an inconsistency, since that file is loaded into every agent's context as
canonical instruction.

What remains here: add `sql_console`, `sql_console_row_limit` and
`sql_console_concurrent` to `specs/plans-and-limits.yaml` and
`entitlements.DEFAULTS`, a pointer on ADR-005, and the executor role to
`specs/tenant-role-model.md`. `plans sync` writes identity only, so the numbers
land through `entitlements.DEFAULTS` and a deployment's overrides survive.

### Slice 1 — execute

Migration: a per-project executor login role, member of `mldb_<ref>_admin` and
nothing else, owning nothing, with a small `CONNECTION LIMIT`; its credential
stored ADR-023 Class B. Provisioning creates it; a `cp-manage` path backfills
existing projects, because a re-provision is not an acceptable upgrade path for
a project with data in it.

`POST /v1/projects/{ref}/sql` — public router under ADR-037, authorized by
organization membership, entitlement-gated on `sql_console`. Connect as the
executor role, `SET ROLE` to the tenant admin role, run the statement, cap rows
server-side at `sql_console_row_limit`, and enforce the tier's statement timeout
by cancelling from a second connection rather than by setting a GUC the
submitted SQL can raise. `ratelimit.LocalLimiter` in front. One audit event per
statement.

**Writes are refused while the project is storage-restricted.** A control-plane
check on `projects.storage_restricted_at`, because `RESTRICTED_ROLES` does not
cover the role this runs as. Extending `RESTRICTED_ROLES` to the admin role is
the more thorough fix and would close the acknowledged paid hole too, but it
changes Phase 05 behaviour for live paid projects and is a decision of its own —
raise it, do not fold it in here.

Tests that matter more than the happy path:

- The executor role is a member of `mldb_<ref>_admin` and of nothing else, and
  is never granted *to* a shared role — ADR-016's one-directional rule, whose
  violation makes every tenant's `authenticated` a member of it.
- `RESET ROLE;` in submitted SQL returns to the executor role, which can re-enter
  the admin role. That is the intended ceiling, so the assertion is that it
  reaches *no more* than the admin role — not that the reset is blocked.
- `SET statement_timeout = 0; SELECT pg_sleep(...)` is still cancelled.
- A row cap that the submitted SQL cannot raise by appending its own `LIMIT`.
- Cross-project: a member of org A cannot execute against a project of org B.
- The executor role cannot reach another tenant's database — the ADR-014
  `CONNECT` lockdown, re-asserted for a new role.
- DDL through this route reloads the tenant's PostgREST schema cache
  (bootstrap 006), so a table created in the dashboard is immediately visible
  to the project's Data API.
- Running SQL against a slept free project does **not** start its workers.
  ADR-022 holds that free-tier economics rest entirely on sleep, and a console
  that woke a project would quietly undo that.

### Slice 2 — introspect

Read-only catalog endpoints the dashboard needs to render anything: tables,
columns, indexes, constraints, RLS policies, functions, enabled extensions,
roles. Same authorization and entitlement gate; no write path. Cheap and
low-risk, and it is the half a frontend cannot compose out of raw SQL without
reimplementing `information_schema` knowledge in TypeScript.

### Slice 3 — impersonate

Execute as `anon` or `authenticated` with a supplied JWT claim set, which is how
a customer debugs an RLS policy that returns an empty result rather than
`42501` — the failure mode Phase 00 finding 7 and ADR-018 keep producing.
Impersonation must be a nested `SET ROLE` that cannot be escaped back to the
admin role within the request.

### Slice 4 — answer the open questions

`docs/OPEN-QUESTIONS.md` `## Migration` asks: migration CLI vs dashboard first,
required Supabase features for initial launch, and downtime expectations. All
three shape the scanner's output format and the runbook. Phase 07 opened by
answering four decisions before slice 0 was unblocked; same pattern.

A fourth, found by this plan's audit and not yet in that file: **may a customer
install an allowlisted extension themselves?** ADR-010's text permits it and the
implementation currently forbids it. Whichever way it goes needs an ADR, because
the answer changes what the scanner reports and whether a migration can complete
unattended.

### Slices 5-8 — the migration work

Deliberately sketched rather than detailed, because slice 4 changes their shape.

5. **Compatibility scanner.** Reads a source Supabase project and reports
   blockers against `specs/compatibility-matrix.yaml` and the ADR-010 extension
   allowlist. Read-only against the source, which is an acceptance criterion.
6. **Schema and data migration.** Applies through the slice 1 substrate. RLS,
   functions, triggers, indexes.
7. **Auth migration where proven** — users and identities; what is not proven is
   documented as not supported rather than attempted.
8. **Validation report and cutover runbook.** The post-migration official-client
   compatibility suite is the acceptance criterion, so it runs against a
   migrated project, not a hand-built one.

## Verification

- [ ] Unit/integration tests
- [ ] Compatibility tests where applicable — post-migration official Supabase
      client suite against a migrated project
- [ ] Tenant-isolation checks — new executor role added to the negative suite
- [ ] Documentation/spec updates
- [ ] **A security review before merge on every slice, not after.** Phase 07
      asked for this, four slices merged without it, and the catch-up pass found
      three issues in shipped code — including a customer able to grant
      themselves `direct_database_access`. This phase hands customers arbitrary
      SQL execution; slices 1 and 3 in particular are not mergeable on a green
      suite alone.

## Risks

- **Executor role over-privilege.** `RESET ROLE` is reachable from submitted SQL
  and returns to a role that can re-enter the admin role, which is the intended
  ceiling. The risk is therefore not the reset but the executor role drifting
  above that ceiling or gaining a second membership. Asserted, not reviewed.
- **Storage enforcement bypass.** See slice 1. Free is the tier where the quota
  carries the economics, per ADR-022's 24 MB disk floor.
- **Usage under-reporting.** Console statements are not gateway requests, so
  they miss `api_requests_per_window` and `/v1/projects/{ref}/usage`. Either
  surface the console's counters there or accept a real data path the usage view
  cannot see — a Phase 05 telemetry gap this phase creates.
- **Rate-limit multiplication.** `LocalLimiter` state is per process (ADR-030),
  so with N control-plane processes the effective limit is N times configured.
  Write it down for this limit as ADR-030 did for the gateway's.
- **The public application's reach widens.** It gains per-project executor
  credentials, against ADR-038's direction of travel. Bounded — one tenant at
  that tenant's admin level, versus every tenant on the node for
  `nodes.admin_dsn` — and stated in ADR-039 rather than left to be found.
- **Connection exhaustion.** ADR-022 makes connections the binding constraint at
  roughly 24 warm projects per node. Console connections are backends. Mitigated
  by `CONNECTION LIMIT` on the executor role — genuinely enforcing per ADR-017 —
  short-lived connections, and `sql_console_concurrent`.
- **A long statement on a shared node.** The out-of-band cancel is the control.
  If it fails, ADR-009's other layers are what remain; monitoring and the
  `docs/RESOURCE-GOVERNANCE.md` escalation path apply unchanged.
- **Free-tier abuse.** Arbitrary SQL on a free project is a compute surface —
  `pg_sleep` loops, deliberate cartesian products. The timeout and row cap bound
  a statement; the rate limiter bounds the rate; neither bounds a customer
  creating projects to farm them, which `max_projects: 2` and Phase 07's abuse
  controls already address.
- **A customer breaking their own Data API.** They can now drop a table their
  application reads. That is theirs to do — the platform's obligation is that
  the failure is legible, which bootstrap 006 covers by reloading the cache
  rather than leaving PostgREST advertising a vanished endpoint.
- **Executor-role backfill.** Existing projects predate the role. A migration
  plus a `cp-manage` path, run and verified before the route is public;
  provisioning a role into a live tenant must be idempotent per `AGENTS.md`.
- **Scanner scope creep.** "Compatibility scanner" is unbounded without slice 4.
  Do not start it before those three questions are answered.

## Decision log

- 2026-08-17 — SQL surface leads the phase rather than trailing it. The
  migration story needs an execution substrate, and there is currently none on
  any tier.
- 2026-08-17 — Free and paid both get mediated SQL; the paid line stays
  credentials and a reachable port. ADR-039, Proposed.
- 2026-08-17 — No third-party admin tool. Adminer and pgAdmin rejected in
  ADR-039 with reasons; a customer running one against their own paid endpoint
  is unaffected.
- 2026-08-17 — Typed schema-mutation endpoints are a non-goal for this phase.
  Introspection is what a frontend cannot compose; mutation it can.

- 2026-08-17 — ADR-039 ratified. Audit against Phases 00-07 found three
  contradictions: two false bullets in `docs/RESOURCE-GOVERNANCE.md`, the storage
  restriction not binding the console's role, and ADR-016's one-directional
  membership rule constraining the executor role. All three are now slice scope
  rather than review findings.
- 2026-08-17 — Storage enforcement for the console is a control-plane check, not
  an extension of `RESTRICTED_ROLES`. Extending it would change Phase 05
  behaviour for live paid projects and deserves its own decision.
- 2026-08-17 — Checked the phase's constraints against Supabase's free tier
  rather than assuming they matched. Statement timeouts and write-refusal over
  quota both match — Supabase enters read-only mode at 500 MB and MaluDB is the
  gentler of the two, since `storage.py` leaves `DELETE`/`TRUNCATE` available
  where Supabase requires read-only mode be disabled first. Tenant escape is not
  a Supabase concept, as each of their projects is its own instance; ADR-002 and
  ADR-013 make it mandatory here. Extension installation is the one real
  divergence and is now a blocking question for slice 4.

## Progress log

- 2026-08-17 — **Slice 1 complete.** `POST /v1/projects/{ref}/sql` runs a
  customer's statement as `mldb_<ref>_admin`, entered by `SET ROLE` from a new
  `mldb_<ref>_executor`. Migration 0017 adds `nodes.db_port` and the
  `EXECUTOR_CREATING` state; `cp-manage project backfill-executor` covers
  projects provisioned before the role existed. Negative tests K to N pass.

  **Two bugs the node-less run could not have found, and one of them was the
  slice.** `SET statement_timeout = %s` is a syntax error: `SET` is a utility
  statement and takes no bind parameter, so the session setup raised `42601`
  before any statement ran — which would have meant *no timeout and no
  read-only enforcement*, the two controls this slice exists for. It is
  `set_config()` now, parameterised properly rather than composed. The second
  was `RESET ROLE`'s test failing on `dict_row` indexing, which is only a test
  bug but was hiding behind the first.

  Both were invisible without `MALUDB_NODE_ADMIN_DSN`, which is the banner's
  whole point. A disposable superuser was created to run them and dropped
  afterwards.

  **The read-only session was wrong, and the probe that settled it found a
  second thing.** Restriction was first held by putting the console's session in
  a read-only transaction. Asked to extend `RESTRICTED_ROLES` instead, two
  probes were run before implementing either — and both came back badly. A table
  owner can `GRANT INSERT ON t TO current_user` after the revoke and write
  immediately, so the requested change is a default rather than enforcement. And
  `SET default_transaction_read_only = off` is accepted inside a read-only
  session, so the mechanism already written into slice 1 did not hold either,
  and its comment said it did.

  ADR-040 records both. The restriction moved into grants on
  `mldb_<ref>_admin`, covering the API, the console and paid direct SQL by one
  mechanism; the read-only session and its false comment were removed; and the
  re-grant is asserted in the suite so the limitation cannot quietly become
  folklore. `DELETE` and `TRUNCATE` survive, which restores the shrink path a
  read-only session had taken away — a free project over quota can clean up
  again, which Supabase's own read-only mode does not allow without a toggle.

  Second time in two slices that a session-level GUC was mistaken for a control.
  The first was ADR-017, which is in this repository precisely because someone
  measured instead of assuming.

- 2026-08-17 — **Slice 0 complete.** `sql_console`, `sql_console_row_limit`,
  `sql_console_concurrent` and `sql_console_timeout_ms` resolve on every tier;
  ADR-005 carries a clarification pointer; `specs/tenant-role-model.md` gains
  `mldb_<ref>_executor`, the `RESET ROLE` reasoning, and negative tests K to N
  which gate slice 1's merge.

  **A fourth key, not in the plan.** The plan said to enforce "the tier's
  statement timeout", and following that literally produces a production console
  with no ceiling: `statement_timeout_ms` is `UNLIMITED` on that tier on
  purpose, because a long analytical query is a legitimate workload for a direct
  connection. It is not a legitimate workload for a browser waiting on an HTTP
  response while the platform holds a connection open. `sql_console_timeout_ms`
  is separate, real on every tier, and a configured zero falls back to the
  default rather than meaning no limit — the only place in `entitlements.py`
  where zero is not taken at face value, because it is the only one where zero
  fails open onto a shared node.

  `sql_console` defaults to true everywhere, which makes the flag look
  decorative. It is the switch for containing one abusive project without
  inventing a tier to move it to.

- 2026-08-17 — Drafted, not started. Slice 0 unblocked; slices 5-8 blocked on
  the three `## Migration` open questions.
