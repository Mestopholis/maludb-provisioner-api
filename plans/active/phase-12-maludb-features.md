# Execution Plan: Phase 12 — MaluDB-Native Features, led by the data-model graph

Status: **BLOCKED on an owner decision** — slice 0 complete 2026-09-12, and it
found that ADR-074 decision 3 (platform wrappers over the live facades) cannot
work as written. `specs/maludb-datamodel-model.md` sets out four options. Slice 1,
the upgrade procedure, does not depend on the choice and can proceed.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/phase-12-maludb-features`, then one branch per slice
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md`
Dependencies: Phases 01–11 merged and closed. ADR-072 point 2 merged (#109),
which matters here: slice 4 adds a check to the gateway, and the gateway it adds
it to is the narrowed one.

## Objective

Make one sentence true that is currently false:

> A developer on any plan can turn on MaluDB's data-model graph for their
> project, refresh it, and ask it to describe a table — from their own server,
> through the official Supabase client — and a project that never turns it on
> is indistinguishable from a project on a platform without it.

The second half is not decoration. `AGENTS.md` requires MaluDB features to
*extend* the Supabase-compatible surface without silently altering it, and the
way that goes wrong here is quiet: a schema that appears in every project's API,
or a set of functions `anon` can reach.

## What ADR-074 decided, so this plan does not relitigate it

1. The data-model graph leads — it needs one memory schema per project and
   nothing else.
2. Opt-in per project; the platform runs `enable_memory_schema` into a fixed,
   platform-owned schema.
3. Reached through PostgREST RPC via **platform-owned wrappers** in a dedicated
   schema, `EXECUTE` to `service_role` only; the gateway answers for projects
   that have not opted in.
4. Every plan, refresh frequency limited per plan through `entitlements`.
5. Extension upgrades are operator-run: a canary, then batches, stop at the
   first failure.

It also **amends ADR-015**, whose "no 'MaluDB-enabled' project flag" sentence
decision 2 contradicts. The extension stays in every tenant database; the
surface on top of it is opt-in.

## What is already true

Worth stating, so it is not rebuilt:

- `maludb_core` 0.104.0 is in every tenant database (ADR-015), and
  `maludb_datamodel_refresh` / `maludb_datamodel_describe` ship in it.
- `enable_memory_schema(name)` exists and was measured: 165 objects (74
  relations, 84 functions), 0.55 s, ~1 MB on a 23 MB baseline.
- Provisioning records `projects.extension_versions` and `bootstrap_version`
  (migration 0005). The per-project version *record* the upgrade procedure needs
  exists; the procedure does not.
- Realtime is the opt-in precedent: `projects.realtime_enabled` (migration 0012),
  `realtime.enable()`, a `realtime.enabled` audit event, and entitlement fields
  resolved per plan.
- ADR-018's event trigger re-applies `EXECUTE` revokes on extension-owned
  functions after every `CREATE`/`ALTER EXTENSION`.

## What is not true, and is the reason slice 0 comes first

- PostgREST's `db-schemas` is a **single global setting** today
  (`workers.py`, `exposed_schema = "public"`). A per-project, conditional second
  schema is new.
- ~~**ADR-018's trigger does not reach the facades**, which carry `PUBLIC`'s
  default `EXECUTE`.~~ **Wrong** — slice 0: `enable_memory_schema` writes explicit
  ACLs for MaluDB's own roles only, and no customer role reaches them, even
  transitively.
- ~~**Nobody knows what `describe` discloses.**~~ Slice 0: **the full structure of
  a table the caller has no privilege on**, limited only by schema visibility.
- ~~**Nobody knows what `_memory_schema_assert_manageable` checks.**~~ Slice 0:
  `CREATE` on the memory schema for **`session_user`** — which is why a wrapper
  on PostgREST's path cannot pass it, and why this plan is blocked.

## Scope

- The data-model graph: enable, refresh, describe.
- A tenant-fleet extension upgrade procedure — the task file's unmet
  prerequisite, and a precondition of shipping anything customers call.
- The gateway's opt-in check and the per-plan refresh limit.
- A black-box compatibility test through the official client, and a
  MaluDB-extension section in `specs/compatibility-matrix.yaml`.

## Non-goals

Stated so they are not drifted into:

- **The memory pipeline, vector search, and the SVPOR knowledge graph.** Later
  surfaces. The memory pipeline additionally needs the project-to-account
  tenancy ADR, which ADR-074 deliberately defers.
- **`anon` or `authenticated` access to the graph.** `service_role` only until
  slice 0's disclosure measurement says otherwise, and widening it is a decision,
  not a slice.
- **`/maludb/v1` endpoints and an SDK.** ADR-074 keeps the door open; this plan
  does not walk through it.
- **Code-mining edges** (the extension's DM-3 work that stitches repository
  namespaces into the data-model graph). The data-model graph here is the
  database's own structure.
- **Dependency version pinning, `auth_token_*`, and `maludb-restd`.** Deferred by
  ADR-074.
- **Fixing `maludb_core`.** A defect found in the extension is raised upstream,
  the way ADR-018's relocation defect was, not patched in this repository.

## Implementation steps

### Slice 0 — Measure before building

No product code, no schema, no route. A spike, in the class of
`scripts/bench-backup.py`, with findings written to
`specs/maludb-datamodel-model.md`. Each question below decides something later
slices would otherwise have to guess.

1. **What `_memory_schema_assert_manageable` requires**, and whether a wrapper
   owned by the platform role can call the facades. If it cannot, slice 3's
   design changes and this plan is corrected before slice 1 starts.
2. **What `describe` discloses.** As a role with no privilege on a relation, call
   `describe` on it. Record exactly what comes back. This is the measurement
   ADR-074 names as the only thing that could justify granting `authenticated`.
3. **Who can reach the facades.** After enabling a memory schema in a
   bootstrapped tenant, enumerate `EXECUTE` and `USAGE` for `PUBLIC`, `anon`,
   `authenticated`, `service_role` and the project's own login roles. The
   assertion to establish or disprove: no customer-controlled role can call a
   facade directly. A finding here is a security fix in slice 2, not a note.
4. **Whether enabling writes anything into `public`**, the one schema PostgREST
   always exposes. Storage migration 0011 created a function unqualified; the
   same class of accident is checked for rather than assumed away.
5. **What refresh costs** on a schema large enough to matter — hundreds of tables
   with foreign keys, views and routines — in time and CPU, so slice 4's per-plan
   limit is a number with a reason.
6. **What adding a schema to `db-schemas` costs** a running PostgREST: a config
   reload, or a restart. A restart is a brief outage on the request that turns
   the feature on, and slice 3 has to say so if it is one.
7. **What an extension upgrade does to an enabled schema.** Does `ALTER EXTENSION
   maludb_core UPDATE` rebuild the facades, or must `enable_memory_schema` run
   again — and is it idempotent if it does? Measured against a real older
   version, not read from the update scripts.

**Exit:** every question answered with a measurement, the plan corrected where
an answer contradicts it, and `docs/OPEN-QUESTIONS.md` updated in place.

**✅ Complete 2026-09-12.** Answers, in the numbering above — details and
reproduction in `specs/maludb-datamodel-model.md`:

1. **Fails.** The guard checks `session_user`; through PostgREST that is always
   the authenticator. ADR-074 decision 3 is infeasible as written.
2. `describe` **ignores the caller's privileges**; only schema visibility limits
   it. `service_role`-only is the entire control.
3. **No customer role reaches the facades**, transitively or otherwise. They run
   as the node superuser.
4. Enabling adds, removes and re-grants **nothing in `public`**.
5. **~1.2 s at the floor** (ADR-018 leaves `maludb_core`'s 373 functions in
   `public`), **~2.7 s for 300 tables**; replace-style, with ~1.3 MB of vacuumable
   churn — and so WAL — per refresh.
6. **No restart**: `NOTIFY pgrst, 'reload config'` then `'reload schema'`, 0.46 s
   and 0.63 s, 0 errors on concurrent reads, and the same in reverse.
7. `ALTER EXTENSION UPDATE` **does not rebuild facades**; re-running
   `enable_memory_schema` does, idempotently, keeping the graph. It drops and
   recreates its own views as it goes.

### Slice 1 — The fleet extension upgrade procedure

First, because it is the task file's own prerequisite and because nothing built
after it should ship until an upgrade can reach it safely.

`cp-manage extension upgrade --node <name> [--to <version>] [--batch-size N]`:

- **Refuse** a node that is `draining`, has a tenant `MOVING`, or has a restore in
  progress — the same states other passes already respect.
- **Canary first**: one tenant, upgraded, verified, before any other is touched.
- **Verification is a property check, not a clean exit.** After each tenant:
  the recorded version matches `maludb_core_version()`; ADR-018's revoke still
  holds on `public` (a fleet-wide `ALTER EXTENSION` adding a function is exactly
  the case that ADR names); and, for a project with the surface enabled, the
  facades still answer and the wrappers still resolve.
- **Stop at the first failure.** The failed tenant stays on its previous version,
  its project records why, and no further tenant on the node is touched.
- **Record per project**: `extension_versions` updated only for tenants that
  verified.
- Reports what it did and what it left, in the style of `node rebuild`.

**Slice 0 settled the facade question: it is part of this.** `ALTER EXTENSION`
leaves every enabled schema on its old facades, so for each enabled project the
procedure re-runs `enable_memory_schema` after the extension update and records
the version it returns. Nothing reports a schema's facade version otherwise; a
stale schema looks current until something missing is called.

### Slice 2 — Enablement

- Migration: `projects.maludb_datamodel_enabled`, and the enablement timestamp.
- Entitlements: `maludb_datamodel` (bool, **true on every plan**) and
  `datamodel_refreshes_per_hour` (int, per plan), with defaults in
  `entitlements.DEFAULTS` and overridable in `plans.config_json`.
- `maludb.enable(project)`: `enable_memory_schema` into the fixed schema, and
  record the `enabled_version` it returns. **No extra revokes** — slice 0 found
  the facades already closed to every customer role. **Idempotent and safely
  retryable**, per `AGENTS.md` — a second call on an enabled project changes
  nothing, and a call that failed halfway can be re-run.
- A customer route and `cp-manage project maludb enable`, both behind the
  entitlement; an audit event `maludb.datamodel.enabled`.
- **No disable in this slice.** Dropping a memory schema drops a customer's
  graph; that is a destructive operation with its own state checks, and is
  planned, not improvised.

### Slice 3 — Wrappers and exposure

> **BLOCKED on the owner's choice among options A–D in
> `specs/maludb-datamodel-model.md`.** As written below this slice cannot work:
> the facades' `session_user` guard refuses every call made on PostgREST's path.
> It is left intact for the record, and is rewritten once the decision is made.

- A platform-owned schema, `maludb`, holding wrapper functions with stable names:
  `datamodel_refresh()` and `datamodel_describe(relation text)`.
- Each wrapper pins `search_path` with `pg_temp` last and schema-qualifies what it
  calls — the measured lesson of ADR-072's `gateway_node_id()`.
- `EXECUTE` revoked from `PUBLIC`, granted to `service_role` only.
- PostgREST `db-schemas` becomes `public` plus `maludb` **for enabled projects
  only**, and `public` alone for everyone else. **A reload, not a restart** (slice
  0): rewrite the worker's config, then `NOTIFY pgrst, 'reload config'`.
- Any wrapper is a **string-body** SQL function, never `BEGIN ATOMIC`: re-running
  `enable_memory_schema` on upgrade drops and recreates its objects, and a tracked
  dependency would be cascade-dropped along with them.
- `NOTIFY pgrst, 'reload schema'` after enabling, per ADR-018.

### Slice 4 — The gateway's opt-in check, and the refresh limit

- A request for the `maludb` schema — `Accept-Profile: maludb` or
  `Content-Profile: maludb` — on a project without the surface enabled is
  answered by the gateway with a clear error naming the cause, and never
  reaches PostgREST.
- `datamodel_refreshes_per_hour` enforced per project. Where it is enforced is
  decided with the delivery option; the size comes from slice 0: each refresh is
  ~1.2 s of floor plus schema-dependent time, and ~1.3 MB of churn for a 300-table
  schema. **The limit is about CPU and WAL, not storage** — refresh replaces rows
  rather than accumulating them.

### Slice 5 — Compatibility, documentation, release

- A black-box test through the official client: `supabase.schema('maludb')
  .rpc('datamodel_describe', ...)` against a real enabled project, plus the
  negative cases — a non-enabled project refused by the gateway, `anon` refused,
  and `authenticated` refused.
- `specs/compatibility-matrix.yaml` gains a MaluDB-extension section. **No
  Supabase-compatible row changes**, and a test asserts `public`'s exposed
  surface is unchanged by enabling.
- `docs/MALUDB-FEATURES.md` for customers: what the graph contains, how to call
  it, what it costs against the plan's refresh limit.
- `tasks/PHASE-12-MALUDB-FEATURES.md` acceptance criteria closed, and this plan
  moved to `plans/completed/`.

## Verification

- [ ] Slice 0 findings in `specs/maludb-datamodel-model.md`, each a measurement
      with what it does not cover.
- [ ] `tests/test_extension_upgrade.py` — a real upgrade across real versions on
      a real cluster: a canary that verifies, a batch that stops at a failing
      tenant and leaves it on its old version, and ADR-018's revoke re-asserted
      after the upgrade rather than assumed.
- [ ] Enablement is idempotent and retryable, tested by enabling twice and by
      re-running after an injected mid-enable failure.
- [ ] **Tenant isolation**: `describe` refuses a schema other than the tenant's
      own, `maludb_core` or `public`; and no customer-controlled role can call a
      facade directly.
- [ ] **Exposure**: `anon` and `authenticated` cannot call a wrapper; a
      non-enabled project's API is byte-for-byte the surface it had before
      Phase 12; enabling adds nothing to `public`.
- [ ] Black-box compatibility test through the official client, including the
      negative cases.
- [ ] `ruff`, full suite, OpenAPI drift, migrations idempotent.
- [ ] **A `Security-Review:` trailer on every slice.** Slices 2 and 3 are not
      mergeable on a green suite alone: they publish `SECURITY DEFINER` functions
      through a public API.

## Risks

- **`describe` discloses structure beyond the caller's privileges — confirmed.**
  Contained only by `service_role`-only access. Any option that would let
  `authenticated` near it has to filter by the caller's own privileges, because
  no grant can.
- **Superuser-owned code behind a public API.** The facades are `SECURITY
  DEFINER` and owned by the extension's installer, the node superuser. Every
  delivery option is judged partly on how much of that it puts within reach of a
  request.
- ~~**ADR-018 reopened through a door it never covered.**~~ Did not materialise:
  slice 0 found `enable_memory_schema` sets its own restrictive ACLs. A test still
  holds that, because an upstream release could change it.
- **Upstream facade churn breaks the wrappers on upgrade.** The wrappers are the
  contract precisely so this lands on the platform rather than on customer code,
  and slice 1's canary verifies wrappers before a batch proceeds.
- ~~**Turning the feature on restarts PostgREST.**~~ Did not materialise: a
  config reload, 0.46 s, no dropped requests.
- **Refresh on a huge schema is a shared-node CPU problem.** Contained by the
  per-plan limit and by `service_role`-only access, so end users cannot trigger
  it. Sized from slice 0's measurement rather than guessed.
- **`maludb_core` has a defect that blocks a wrapper.** Raised upstream, the plan
  corrected, and the affected capability left out rather than patched around.
- **The ADR-015 amendment is read as licence to make other things opt-in.** It is
  not. The extension stays unconditional; only the customer-facing surface built
  on it is opt-in, and a later surface that wants the same treatment says so in
  its own decision.

## Decision log

- 2026-09-12 — **ADR-074 accepted**, the owner deciding five questions one at a
  time: the data-model graph leads; opt-in, platform-enabled; RPC via
  platform-owned wrappers with a gateway opt-in check; every plan with per-plan
  refresh limits; operator-run canary-then-batch upgrades.
- 2026-09-12 — The delivery question was reopened by the owner mid-decision, on
  the grounds that `/maludb/v1` handles opt-in more cleanly. That was right, and
  the answer changed the recommendation rather than overriding the point: the
  gateway gets the opt-in check, so RPC inherits the clean behaviour. What
  decided against `/maludb/v1` was a fact, not a preference — the gateway has
  never opened a tenant database connection, so an endpoint needs either that
  capability on the internet-facing process ADR-072 had just narrowed, or
  routing through the mediated SQL path.
- 2026-09-12 — **ADR-015 amended rather than contradicted.** Writing ADR-074
  found that ADR-015 rules out a "MaluDB-enabled" project flag in as many words.
- 2026-09-12 — Deferred by ADR-074, so not in this plan: the project-to-account
  tenancy mapping, dependency pinning, `auth_token_*`, `maludb-restd`.

## Progress log

- 2026-09-12 — **Slice 0 complete, and it stops the plan at slice 3.** The
  data-model facades guard themselves with `CREATE` on the memory schema checked
  against `session_user`, which a `SECURITY DEFINER` wrapper cannot change and
  PostgREST always sets to the authenticator. ADR-074's delivery design was built
  on an assumption about the extension nobody had tested, which is what slice 0
  was ordered first to find cheaply. Per `AGENTS.md` the line is stopped and the
  conflict documented rather than worked around; options A–D are in the spec.
- 2026-09-12 — Two findings went the other way. ADR-074 feared the facades carried
  `PUBLIC`'s default `EXECUTE`; they do not, and no customer role reaches them. It
  feared enabling needed a PostgREST restart; it needs a `NOTIFY`. ADR-074 carries
  a findings section correcting both, rather than being quietly edited.
- 2026-09-12 — A measurement trap worth keeping: twenty refreshes inside a `DO`
  block grew the database 26.6 MB linearly, which reads as retained history. It is
  dead rows that cannot be vacuumed inside one transaction. Committed and
  vacuumed, size stayed flat and row counts did not move across five more
  refreshes.

- 2026-09-12 — Plan written on `plan/phase-12-maludb-features` alongside
  ADR-074. No code. Slice 0 is next and needs none: a bootstrapped tenant, the
  extension's older versions for question 7, and a schema large enough to make
  refresh cost something.
