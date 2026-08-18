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
- ~~The three `## Migration` open questions answered~~ — done 2026-08-17, four
  of them, as ADR-042 to ADR-045. Slices 5-8 are unblocked.

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

**Shipped as one endpoint rather than several** — `GET
/v1/projects/{ref}/database/schema`, with `?schema=` to narrow it. See the
progress log: a route per catalogue costs a dashboard page eight tenant
connections and eight rate-limit tokens, and the snapshot is worth more when it
is internally consistent. Not "low-risk" as written, either: `pg_roles` is
cluster-scoped and passing it through discloses every other tenant on the node.

### Slice 3 — impersonate

Execute as `anon` or `authenticated` with a supplied JWT claim set, which is how
a customer debugs an RLS policy that returns an empty result rather than
`42501` — the failure mode Phase 00 finding 7 and ADR-018 keep producing.
Impersonation must be a nested `SET ROLE` that cannot be escaped back to the
admin role within the request.

**A nested `SET ROLE` cannot deliver that, and the plan should have known.**
Slice 1 established that `RESET ROLE` is reachable from submitted text and lands
on the connecting role — so nesting `anon` inside the admin role leaves the
escape one statement away. Shipped as a different *login role* instead:
`mldb_<ref>_authenticator`, a member of the three shared names and of nothing
else. Same requirement, met structurally. See the progress log.

### Slice 4 — answer the open questions

**Done 2026-08-17. ADR-042, ADR-043, ADR-044, ADR-045.**

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

Sketched before slice 4 because it changed their shape. Now shaped by it:
**everything below is a CLI the customer runs** (ADR-042), reading their source
with their credentials from their machine and writing to the destination through
the slice 1-3 API with no privileged path of its own.

5. **Compatibility scanner.** `maludb-migrate scan`. **Done 2026-08-17.** Reads a source Supabase
   project read-only — an acceptance criterion — and reports against
   `specs/compatibility-matrix.yaml` and `specs/extension-allowlist.yaml`. Two
   severities and the distinction is load-bearing (ADR-043): a **blocker** means
   the migration will not complete correctly and must not be attempted; a
   **warning** is something to know about. A blocker names the phase that will
   carry the surface, so the answer is a date rather than a refusal. Outputs the
   measured data size and the expected freeze window with it (ADR-044).
6. **Schema and data migration.** Split in two: **6a, the ADR-045 installer,
   done 2026-08-18**; 6b is `maludb-migrate apply`. Applies through the slice 1
   substrate. RLS, functions, triggers, indexes. **Carries the ADR-045 installer**, which is the
   first thing this slice needs and did not exist: a `SECURITY DEFINER` function
   owned by the platform role, checking `specs/extension-allowlist.yaml`, with
   its own negatives — an extension off the list still refused, which is
   negative test H generalised rather than replaced.
7. **Auth migration where proven** — users and identities, email and password
   only (ADR-043). Password hashes where GoTrue's format allows; where it does
   not, the honest outcome is a reset for those users, reported by the scanner
   in advance rather than discovered at cutover. OAuth/magic link/MFA/SSO are
   blockers, because a user row whose only authenticator is a provider
   configuration migrates into an account nobody can sign in to.
8. **Validation report and cutover runbook.** The post-migration official-client
   compatibility suite is the acceptance criterion, so it runs against a
   migrated project, not a hand-built one. **Measures the freeze window** that
   ADR-044 commits to publishing, and compares row counts — the platform cannot
   enforce a freeze on somebody else's platform, so the check for "writes
   continued" is arithmetic after the fact, and any table that moved is named.

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

- 2026-08-18 — **Slice 6a complete: the extension installer.** Split from the
  migration itself, because "schema and data migration" plus a new platform
  privilege is two reviewable things and `AGENTS.md` prefers small slices. 6b is
  `maludb-migrate apply`.

  **ADR-045's stated mechanism could not work, and building it is what showed
  that.** The ADR said "a `SECURITY DEFINER` installer that checks the
  allowlist" — a function a customer calls *instead of* writing
  `CREATE EXTENSION`. But the motivation was the literal line a migrated schema
  opens with, `create extension if not exists "uuid-ossp"`, and an installer
  only helps if something rewrites that line first. A migration that edits the
  customer's own SQL before applying it is a different and worse product. The
  ADR is amended rather than quietly departed from.

  What shipped puts the check where the DDL already is: `GRANT CREATE ON
  DATABASE` to the tenant admin, which PostgreSQL 13+ turns into "may install a
  `trusted` extension and nothing else" — measured, `citext` installed and
  `postgres_fdw` was refused by PostgreSQL itself — plus an event trigger that
  narrows `trusted` to `specs/extension-allowlist.yaml` and aborts at
  `ddl_command_end`, which rolls the install back. The grant also gives
  `CREATE SCHEMA`, which 6b needs anyway: a migrated project brings its own.

  **Two measurements changed the design, and both had produced a control that
  looked like it was working.**

  - `current_user` inside a `SECURITY DEFINER` function is the *owner*, not the
    caller. The superuser exemption — there so provisioning can install
    `maludb_core` — was written against it and was therefore unconditionally
    true, so the trigger refused nothing and a non-allowlisted extension
    installed cleanly. It is `session_user` now.
  - `object_identity` from `pg_event_trigger_ddl_commands()` is *quoted* where
    the name needs it, so `uuid-ossp` arrives as `"uuid-ossp"` and never matches
    the allowlist. That refused precisely the extension the ADR exists for. The
    trigger joins on `objid` to `pg_extension.extname` instead.

  Neither would have been caught by a node-less suite, and neither was in the
  plan. Both were found by running the ADR's own example line.

  **The list is data in each tenant, not baked into bootstrap SQL.** Bootstrap
  files are immutable once applied, so an embedded list would have frozen at
  whatever each project was provisioned with — a fleet where what a customer may
  install depends on the month they signed up. `maludb_platform.allowed_extensions`
  is synced from the spec at provisioning and by a new `cp-manage extensions
  sync`, which **removes** as well as adds: taking an extension off the list is
  how a security decision gets reversed, and a tenant that never hears about it
  keeps the old permission.

  `tenant_bootstrap.verify` now checks the new event trigger the way it already
  checked the ADR-018 one — a superuser-run migration is how either would go
  missing, and without this one the admin's `CREATE ON DATABASE` reverts to
  meaning "any trusted extension".

  Negative test Q in `specs/tenant-role-model.md`, which is test H generalised
  rather than replaced: H said the admin cannot `CREATE EXTENSION` at all, and
  ADR-045 deliberately changed that, so the property worth pinning moved.

  **The security review found three more, and the pattern is now familiar: two
  of them were controls that looked like they were working.**

  - `CREATE EXTENSION x CASCADE` reports only *x* to an event trigger, so an
    allowlisted entry with an unlisted dependency would drag it in past the
    check, install script running as the bootstrap superuser. Not reachable
    against today's list — no entry declares `requires`, and PostgreSQL's
    `trusted` check does cover cascaded dependencies — but the file is designed
    to grow. The trigger walks the `pg_depend` closure now, and criterion 5 says
    an entry's dependencies must be listed too, asserted against the node's own
    `pg_available_extensions` rather than against a list someone typed.
  - **ADR-018's hardening only ever looked at `public`**, which was sufficient
    while no tenant role could install anything. `GRANT CREATE ON DATABASE` ends
    that: measured, a customer created a schema, installed an allowlisted
    extension into it, granted `anon` USAGE, and every function became
    `anon`-executable with no revoke applied. `specs/tenant-role-model.md` lists
    that among the things the admin must never be able to do and the existing
    test only tried the direct grant in `public` — so the invariant had become
    true by accident. Bootstrap 011 drops the schema filter. Negative test R.
  - `pg_event_trigger.evtenabled` has **four** values. `ENABLE REPLICA` fires
    only for `session_replication_role = 'replica'`, so a tenant in that state
    installed a non-allowlisted extension while `verify` reported it healthy.
    All three trigger checks — including the two that predate this slice —
    required `<> 'D'` and now require `'O'` or `'A'`.

  And a data error worth naming: the allowlist recorded `vector` as
  `trusted: true` when the node reports otherwise. Nothing reads that field
  programmatically, but it is the one a future reviewer would trust when
  applying criterion 1.

- 2026-08-17 — **Slice 5 complete.** `maludb-migrate scan`, in
  `services/migrate/`: reads a Supabase project read-only, judges it against
  `specs/compatibility-matrix.yaml` and `specs/extension-allowlist.yaml`, and
  reports blockers and warnings with an exit code a deployment script can gate
  on. Twelve rules, twenty-six tests, five of them against a database built to
  look like a Supabase project.

  **Split so the interesting cases are testable.** Reading is `source.py`;
  judgement is `rules.py`, a pure function over facts. That is what makes a
  project with Vault secrets, an OAuth-only user base and a `plpython3u`
  function testable without anybody owning one shaped like that.

  **The credential is the customer's and stays theirs** (ADR-042). Read from
  `MALUDB_SOURCE_DSN` by preference because an argument is visible in `ps` and
  in shell history; never written to the report, an error or a log line; and a
  connection failure reports its `sqlstate` alone, because psycopg's connection
  errors echo the conninfo. Asserted rather than intended.

  **Read-only is real here, and for the reason it was not in slice 1.** Every
  statement is a constant in the module and nothing takes submitted SQL, so
  `READ ONLY REPEATABLE READ` is a backstop rather than the claim ADR-040
  demolished. The test monkeypatches a write into the scan and asserts `25006`
  against the customer's own database shape.

  **Three findings exist to stop a clean report being misread.** A scan that
  could not read part of the project is a *blocker*, not a shrug — what it could
  not see may be what would have blocked the migration. Edge Functions and
  Realtime broadcast/presence are reported as undetectable, always, because
  absence of evidence is not evidence of absence and "no findings" must not read
  as "everything I have migrates".

  **The freeze window is not invented.** ADR-044 commits to a measured one and
  slice 8 measures it, so an unmeasured rate prints "not measured yet" rather
  than a plausible figure somebody would schedule an outage around.
  `--throughput-mb-per-s` is there for whoever has measured.

  **Two bugs found by running it rather than reading it.** `reltuples` is `-1`
  for a table the planner has never analysed — every table in a freshly restored
  database — and flooring that to zero reported "0 rows" for a project full of
  data, to a customer sizing a maintenance window. Counted separately and named
  now. And each probe needed its own savepoint: the first missing Supabase
  schema aborted the transaction, so a project without Vault would have reported
  finding nothing at all.

  Also fixed: `pyyaml` was a **dev** dependency while `cp-manage plans sync`
  imports it at runtime. AGENTS.md calls that step mandatory, so a production
  install without the `dev` extra failed the documented bring-up at step 5. The
  scanner is its second runtime consumer.

  **The security review found three more, and every one of them pointed the
  same way: a false *clean* verdict.** That is the dangerous direction for a
  tool whose whole job is telling a customer their cutover is safe.

  - **Row-level security is not an error.** A session that is neither the owner
    nor `BYPASSRLS` reads an RLS-protected table as zero rows, silently —
    measured. Supabase enables RLS on `storage.objects` by design and owns it as
    `supabase_storage_admin`, so a customer doing the responsible thing (a
    purpose-made read-only role rather than their project owner) was told they
    had no stored objects, and would have cut over during a write freeze and
    found the files gone. The probes now check whether RLS applies to this
    session and report *unreadable* instead of a count, which routes into the
    existing blocker.
  - **The exposure rule checked ordinary tables only.** A view without
    `security_invoker=true` runs as its owner, so RLS underneath does not apply
    to the caller — measured: a role that read 0 rows from the table read every
    row through a view over it. RLS cannot be enabled on a materialized view at
    all. And a table *with* RLS whose policy is `USING (true)` for `anon` reads
    as protected and is not. All three are reported now; the old rule found the
    smallest of them.
  - **Every item in the report is a name from the source database**, printed
    straight to a terminal — including free text like a bucket's name. A
    crafted one repaints the report a customer reads to decide their cutover is
    safe. Control characters are stripped at render time and items are bounded;
    the JSON keeps the byte-accurate name, since it is not painting a screen.

  Plus a crash the tests could not catch: `sum(bigint)` is `numeric`, which
  becomes a `Decimal` that `json.dumps` refuses — so `--format json`, the
  documented runbook interface, failed for exactly the projects that have
  storage. The unit tests missed it because a hand-written fake row holds a
  plain int, so the regression test is a node one.

- 2026-08-17 — **Slice 4 complete.** Four decisions, four ADRs, no customer-
  visible code. Slices 5-8 are unblocked and their entries above are rewritten
  to the shape the answers give them.

  **ADR-042 — a CLI, not the dashboard.** The deciding argument turned out not
  to be developer experience. A scanner has to read the *source* Supabase
  project, which means somebody holds the customer's Supabase credential; a
  dashboard-driven one means the control plane accepting, storing and using a
  third party's production secret, with a blast radius on somebody else's
  platform and a revocation path this project does not own. `docs/SECRETS.md`
  has no class for that and should not gain one to save a `--dsn` flag. Two
  supporting reasons: ADR-025 puts the frontend in another repository, so
  "dashboard first" blocks this phase on work that is not here; and a long,
  restartable, output-heavy operation against two databases is a shape a
  terminal fits and a request/response API does not.

  **ADR-043 — scope is the compatibility matrix, read back.** Database,
  email/password Auth, Postgres Changes; everything else a blocker naming its
  phase. Not a judgement about what customers want: those are the surfaces with
  a `supported` status earned by the official-client suite, and `AGENTS.md`
  forbids claiming compatibility the tests do not support. A migration that
  silently carried Storage or an OAuth identity would be making exactly that
  claim in the one place a customer cannot check it — their own cutover. The
  matrix stays the authority, so promoting a surface grows the scope without
  amending the ADR.

  **ADR-044 — a measured write freeze.** The existing doc already staged
  zero-downtime as later; what was missing was a number. "Expect some downtime"
  is not something a customer can schedule around, and a figure nobody measured
  would be worse than none, so slice 8 measures the window against data volume
  and the runbook carries the result. The uncomfortable half is written down
  rather than glossed: **the platform cannot enforce the freeze**, because the
  source is Supabase. A migration where writes continued produces a destination
  quietly missing rows — the worst failure this phase can have — so validation
  compares row counts and names any table that moved.

  **ADR-045 — self-service allowlisted extensions**, plus
  `specs/extension-allowlist.yaml`. The alternative makes every migration a
  support ticket, in the middle of the write freeze ADR-044 had just committed
  to. What made it cheap rather than brave is that the security half already
  exists: bootstrap 005 has revoked new functions from `anon` on every
  `CREATE EXTENSION` since Phase 00, without exception handling, so a failed
  revoke aborts the install.

  The allowlist's **criteria** are the durable part and the contents are not:
  `trusted` or a written review; no path outside its own database; no
  cluster-wide state; no preload slot or background worker.
  `tests/test_extension_allowlist.py` pins them, including that nothing is in
  both halves and that the classes which must never be admitted are named —
  `pg_stat_statements` among them, because its view is populated for the whole
  cluster and on a shared node that is a window onto other tenants. PostGIS is
  absent for a capacity reason rather than a security one, and says so.

  The installer is deliberately **not** built here; it is slice 6's first
  requirement and lands with its own negatives.

- 2026-08-17 — **Slice 3 complete.** `POST /v1/projects/{ref}/sql` takes an
  optional `role` (`anon` | `authenticated` | `service_role`) and `claims`, runs
  the statement as that role with `request.jwt.claims` set, and answers with
  `ran_as`. Negative test P is added to `specs/tenant-role-model.md`.

  **The plan's own instruction was unimplementable, and slice 1 is why.** It
  said impersonation "must be a nested `SET ROLE` that cannot be escaped back to
  the admin role within the request" — but slice 1 established that `RESET ROLE`
  is reachable from submitted text and returns the session to the *connecting*
  role. Nest `anon` inside the admin role and the escape is one statement long.

  So the role that connects is the thing that had to change:
  `mldb_<ref>_authenticator` rather than the executor. It is a member of the
  three shared names and of nothing else, so a reset lands somewhere with
  nothing above the requested role — the requirement, met by construction rather
  than by hoping the submitted text does not try. It also needed no new role, no
  new credential and no backfill: every project has had one since Phase 02.

  And it is the *right* role on the merits, not only the convenient one. It is
  what PostgREST connects as, doing exactly what PostgREST does with it, so a
  policy debugged here is debugged as the application will meet it. The cost is
  written down rather than discovered: console impersonation spends connections
  from the authenticator's limit, which PostgREST shares.

  **`specs/tenant-role-model.md` said the opposite, and contradicted its own
  test.** It anticipated granting the three shared names *to* the executor "which
  is what impersonation needs" — one paragraph below test K, which asserts the
  executor's memberships are exactly `{mldb_<ref>_admin}`. Both could not hold.
  The section is corrected rather than quietly replaced.

  Test P asserts a property of a role that has existed since Phase 02 and had
  never been checked: the authenticator is not a member of the admin role. The
  grant that would break it is one line nobody has a reason to write.

  Impersonation is a lower ceiling for a *request*, not a sandbox for the
  customer, who reaches the admin role by sending the next request without a
  role. Said in the module docstring so nobody mistakes it for a permission
  system later. The claims are not verified and are not a credential — there is
  nothing to verify against, and what bounds the statement is the role.

  The audit trail records `requested_role` and the claim *keys*, never the claim
  values: a claim set is where the customer's own end users' ids and emails
  live. Both keys are on the Phase 07 audit allowlist, or the route would have
  silently dropped them.

  **The security review before merge earned its place, and found a hole this
  slice had opened and then failed to close.** Phase 05 exempts `service_role`
  from the storage revoke because it "is reachable only from the project's own
  backend", whose route is the gateway — which refuses writes at quota.
  Impersonation is a second route the gateway never sees, so a restricted
  project could ask to be `service_role` and write its way further over quota.

  That much was caught while building, and the first fix refused the *request*
  to impersonate `service_role` while restricted. **The review then showed the
  fix was bypassable in one line**: `SET ROLE` is authorized against the session
  user, which on an impersonating connection is the authenticator — a member of
  all three shared names. So `role: "anon"` plus `SET ROLE service_role;` in the
  statement walks around it. Measured, then asserted.

  ADR-041 is the real fix and goes where ADR-040 already said it should: in
  grants. `storage.RESTRICTED_ROLES` gains `service_role`, the request-level
  check is deleted rather than kept as a second layer that would teach the next
  reader that the requested role means something, and the general rule is
  written into the module: **on this surface the role in a request selects a
  credential and nothing else.** `tests/test_storage.py` had a test asserting
  the old exemption; ADR-041 is what authorises rewriting it, and it now asserts
  the cleanup path the exemption actually existed for.

  The response field was renamed `ran_as` → `requested_role` for the same
  reason. A statement can change its own role, so the platform never observed
  what it ran as, and a field that said it did would put a false claim in a
  customer-visible audit trail.

  **And a flake in the storage suite turned out to be a real defect in the
  tests, not luck.** Two full runs across slices 2 and 3 each failed one
  quota-enforcement test that passed in isolation. The cause: usage is measured
  net of a per-project baseline recorded at provisioning, so a test that sets a
  one-byte quota is really asking the fixture's own table to exceed a
  *difference* of a few kilobytes — and `pg_database_size` is not monotonic
  between two readings. In a ten-minute run, autovacuum reclaims more than the
  test adds, the billable figure floors to zero, and a project with a one-byte
  quota is cheerfully `ok`. The tests now zero the baseline as well, so the
  whole 23 MB is billable and the comparison is decisive rather than a race.
  Fixed here because it is a test that reports a broken quota as a passing one
  whenever it is slow enough.

  **The review of the fix then found the fix was inert, and why.**
  `storage.evaluate` applied the revoke only when a project *changed* state, so
  widening `RESTRICTED_ROLES` would never have reached a project already sitting
  in `restricted` — a no-op for exactly the population that needed it. The
  larger finding is older: ADR-040 accepts its own residual risk on the stated
  grounds that "the maintenance pass re-measures and re-applies, so a customer
  who re-grants is in a loop rather than through a door", and with the revoke
  inside the transition branch **there was no loop** — one re-grant held until
  the project dropped below quota. A mitigation two ADRs lean on did not exist.

  The revoke now runs on every pass where the state is `restricted`; the audit
  event and the timestamp stay on the transition, which is what the idempotence
  was actually protecting (`test_re_measuring_..._does_not_re_audit` still
  passes). `test_a_re_grant_is_taken_away_again_by_the_next_pass` asserts the
  loop, so ADR-040's mitigation is a fact the suite holds rather than a sentence
  in a decision record — and no backfill is needed here or for the next change
  to the restricted set. ADR-041 records that, plus two consequences it does not
  fix: the admin role can re-arm the impersonation path by granting to
  `service_role`, and `release` re-grants rather than restores.

  `cp-manage project storage` was printing that writes are revoked from "anon
  and authenticated" and that "service_role is untouched" — both clauses false
  after ADR-041, in text an operator reads during a quota incident.

- 2026-08-17 — **Slice 2 complete.** `GET /v1/projects/{ref}/database/schema`
  answers schemas, tables with their columns, indexes, constraints and policies,
  functions, installed extensions and roles, in one read-only `REPEATABLE READ`
  snapshot taken as `mldb_<ref>_admin`. Negative test O is added to
  `specs/tenant-role-model.md`.

  **The finding was in a catalogue, not in the code.** `pg_roles` is
  cluster-scoped, and the ADR-014 `CONNECT` lockdown does not touch it: role
  rows are readable from inside any database on the node. A naive
  `SELECT ... FROM pg_roles` would therefore have answered one customer with
  every other tenant's `mldb_<ref>_*` roles — and a ref is the customer's API
  subdomain (ADR-008), so that is a list of the node's other customers. Roles
  are answered from an allowlist built from the project instead, and the test
  provisions two tenants to prove the second is absent from the first's answer.
  `pg_available_extensions` is node-wide for the same reason and is deliberately
  not reported at all: it would advertise a capability no tier has until slice 4
  decides otherwise.

  **The read-only transaction is real here, and ADR-040 is why that needs
  saying.** Slice 1 learned that a read-only session is not a control against
  submitted SQL, because `SET default_transaction_read_only = off` walks out of
  it. Nothing on this path can issue a `SET` — every statement is a constant in
  `introspection.py` and the only caller-supplied values are schema names, bound
  as parameters — so `READ ONLY` is a genuine backstop rather than a claim. The
  test proves it by monkeypatching a write into the transaction and asserting
  `25006` rather than by reading the code that says so.

  **One endpoint, not nine.** `postgres-meta` splits this across a route per
  catalogue, which would make one dashboard page eight tenant connections and
  eight rate-limit tokens on a plan whose whole console budget is one statement
  per window. The plan said "endpoints"; this is the deviation and the reason.
  Its bucket is separate from the console's, so browsing a schema cannot spend
  the ability to run a statement — asserted from the outside.

  **Two things found while building it, both older than this slice.** The
  ADR-038 import-graph test checked five of the nine public routers and the four
  it skipped included `sql` — the one public route that opens a tenant
  connection. It now covers all of them, and passes. And `project_factory` never
  dropped `mldb_<ref>_executor`, so every run since slice 1 left another one on
  the cluster.

  The gates — membership, entitlement, readiness, rate limit — moved to
  `api/tenant_access.py`, since slice 3 would have been their third copy.

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
