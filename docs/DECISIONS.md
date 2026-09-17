# Architecture Decisions

This file records accepted project-level decisions. New durable decisions should be appended rather than hidden in implementation details.

## ADR-001 — Product wedge is Supabase compatibility

Status: Accepted

MaluDB Platform will initially compete as a Supabase-compatible production alternative. MaluDB-native functionality is added after/alongside compatibility and must not break supported Supabase behavior.

## ADR-002 — Tenant unit is a database, not infrastructure

Status: Accepted

Each project gets its own PostgreSQL/MaluDB database and constrained roles on an already-running shared MaluDB cluster.

Project creation does not provision a VM or container.

## ADR-003 — Proxmox VMs are pre-provisioned MaluDB nodes

Status: Accepted

Platform administrators provision/manage MaluDB VMs separately. The control plane schedules projects onto these existing nodes.

## ADR-004 — Platform retains database ownership

Status: Accepted

Customers do not technically own the PostgreSQL database and do not receive superuser privileges. They receive constrained roles that provide only the product-supported capabilities.

## ADR-005 — Free tier is API-only

Status: Accepted — **clarified by ADR-039, 2026-08-17.** The rule below is unchanged; the title overstates it and should be read as "free tier receives no connection credentials".

Free projects do not receive public direct PostgreSQL connection credentials. This prevents bypass of API-layer rate/concurrency/quota controls.

ADR-039 draws the line where this text draws it rather than where the title does: credentials and a reachable port stay paid, while SQL the platform executes on a project's behalf is available to every tier. The stated reason survives the clarification — a mediated statement does not bypass the platform's controls, because the platform holds the connection and can cancel it, which ADR-017 established a direct connection cannot be made to respect.

## ADR-006 — Paid upgrade normally retains the database

Status: Accepted

Free-to-paid upgrade changes entitlements/limits without requiring a database migration. Operational movement to another node/pool may occur later but is decoupled from purchase.

## ADR-007 — Per-project PostgREST/Auth processes are acceptable for MVP

Status: Accepted

For the initial implementation, each active project may have its own PostgREST and Auth configuration/process.

Free project API workers may sleep while inactive. The platform can revisit process density after measuring real usage.

## ADR-008 — Project URL plus project-scoped API key

Status: Accepted

Public APIs use a stable project-specific hostname. The gateway must verify that the submitted API key belongs to the project referenced by the hostname.

## ADR-009 — Resource governance is layered

Status: Accepted

Use gateway throttling/concurrency, small DB pools, PostgreSQL/MaluDB settings, storage quotas, node scheduling, and eventually native MaluDB resource governance.

## ADR-010 — Extensions are allowlisted

Status: Accepted

Customers cannot install arbitrary PostgreSQL extensions on a shared node.

## ADR-011 — Repository is agent-neutral

Status: Accepted

Codex and Claude Code must work from the same canonical docs/specs/tasks/plans. `CLAUDE.md` imports the shared `AGENTS.md`; agent-specific files must not contain competing architecture.

## ADR-012 — MaluDB is PostgreSQL 17 plus the `maludb_core` extension

Status: Accepted

Verified 2026-08-15 against the running development install. MaluDB is not a PostgreSQL fork or a wire-compatible reimplementation. It is stock PostgreSQL 17 (PGDG) with a C extension, `maludb_core`, installed per database.

Consequences:

- All PostgreSQL-dependent designs in this repository are valid with standard semantics: `CREATE DATABASE`, cluster-scoped roles, RLS, `ALTER ROLE ... IN DATABASE`, statement/lock timeouts, logical replication, WAL archiving, `pg_database_size()`.
- The extension is per-database and requires superuser to install, so the platform installs it and customers cannot. This reinforces ADR-010.
- The extension registers no background workers and needs no `shared_preload_libraries` entry, so per-tenant installation does not consume worker slots.
- Extension upgrades are a per-tenant-database fleet operation, not a single cluster operation.

Details and measurements are in `docs/MALUDB.md`.

## ADR-013 — Platform database-per-tenant is the authoritative tenancy boundary

Status: Accepted (ratified 2026-08-15)

MaluDB has its own tenancy model: accounts and schemas inside a single database, bound by the `maludb_core.current_account_id` GUC, with `malu$object_grant` for cross-tenant grants and `MALU_ALL_*` views for cross-tenant reads. The platform (ADR-002) uses one database per customer project.

Proposed resolution: the platform's database boundary is the security boundary. MaluDB account/schema tenancy is treated as an intra-project organizing feature available *within* one customer's database, never as a mechanism for separating two customer projects. Cross-tenant MaluDB features (`malu$object_grant`, `MALU_ALL_*`) therefore operate only among schemas belonging to a single customer.

Ratified. Consequences for implementation:

- The database boundary is the only tenancy boundary the platform relies on for isolation. No security control may depend on `current_account_id`, `malu$object_grant`, or `MALU_ALL_*` scoping to separate two customer projects.
- MaluDB account/schema tenancy remains available *inside* one customer's database as a product feature for that customer's own organization of data.
- A project therefore maps to a database, not to a MaluDB account and not to a schema. Phase 02 generates roles accordingly; see `specs/tenant-role-model.md`.

## ADR-014 — Tenant databases require explicit privilege lockdown at provisioning

Status: Accepted

Verified 2026-08-15: PostgreSQL grants `CONNECT` to `PUBLIC` on every new database, so by default any cluster role can connect to any tenant database on a shared node. A login role with no grants connected to an unrelated tenant database and read its catalog.

Every tenant database provisioning run must therefore:

- `REVOKE CONNECT ON DATABASE <tenant_db> FROM PUBLIC` and grant `CONNECT` only to that project's roles;
- never grant the `maludb` role, which is a superuser on the current install, to any customer role;
- never grant a `BYPASSRLS` role (`maludb_memory_admin`, `maludb_memory_auditor`, `maludb_llm_admin`, `maludb_llm_auditor`, `maludb_modeld`, `maludb_mc2dbd`) to any customer role;
- scope per-role resource settings with `ALTER ROLE ... IN DATABASE ...`, since role-level `SET` is cluster-wide, and apply them to the **login** role — see ADR-017.

These are blocking negative tests for Phase 02, not review guidance. The concrete SQL is in `specs/tenant-role-model.md`.

## ADR-015 — `maludb_core` is installed in every tenant database

Status: Accepted. **Amended by ADR-074** (2026-09-12): the extension is still
installed in every tenant database with no conditional path, but the
customer-facing feature surface built on it is opt-in per project, so the
"no 'MaluDB-enabled' project flag" sentence below no longer holds.

Every tenant database gets `CREATE EXTENSION maludb_core CASCADE` during provisioning. MaluDB capability is a property of the platform, not an add-on a project opts into, so there is no conditional path and no "MaluDB-enabled" project flag.

Consequences:

- Every project carries a ~23 MB storage floor before storing any customer data. Free-tier storage quotas must be defined net of this baseline, or a 100 MB quota is 23% consumed at creation.
- Node capacity planning must budget ~23 MB × tenant count of pure baseline, plus per-database catalog overhead.
- Extension upgrades are a whole-fleet operation on every node: no tenant database can be skipped. This requires a runbook covering ordering, batching, failure isolation, and per-tenant version tracking.
- Provisioning must record the installed `maludb_core` version and its dependency versions per project, because `CREATE EXTENSION` installs whatever the node's OS packages currently provide and drift is already observable.
- Phase 12 can assume MaluDB functions are present in every tenant database.

## ADR-016 — Supabase role names are shared cluster-wide; every project gets its own authenticator

Status: Accepted

Depends on ADR-013.

Migrated Supabase RLS policies name roles literally (`TO authenticated`, `TO anon`, `TO service_role`) and call `auth.uid()`. PostgreSQL roles are cluster-scoped, so a shared node can hold exactly one role of each name. Renaming them per tenant would break every migrated policy, which defeats the migration wedge in ADR-001.

Decision: create `anon`, `authenticated`, and `service_role` once per node as `NOLOGIN`, privilege-free **names**. Each project gets its own globally unique `mldb_<ref>_authenticator` login role, which is the only role that connects and which `SET ROLE`s to the shared names per request.

This is safe because object privileges are per-database, so a grant to `authenticated` in one tenant does not exist in another — verified. Combined with the ADR-014 `CONNECT` lockdown, a session can never span two tenants.

One exception is load-bearing: **role membership is cluster-global**. Granting any per-tenant role *to* a shared role makes every tenant's `authenticated` a member of it — verified. Grants involving shared roles are therefore one-directional: shared roles may be granted to a tenant authenticator, never the reverse.

Full specification, provisioning SQL, and required negative tests: `specs/tenant-role-model.md`.

## ADR-017 — PostgreSQL role and database settings are defaults, not enforcement

Status: Accepted

Verified 2026-08-15. Two findings change how layer 3 of `docs/RESOURCE-GOVERNANCE.md` must be read.

**Role settings apply at login, to the login role.** `ALTER ROLE authenticated IN DATABASE d SET statement_timeout` has no effect on a PostgREST session, because `authenticated` is entered via `SET ROLE` rather than login. It fails open and raises no error. Settings must target the authenticator/login role, where they do apply and do survive the subsequent `SET ROLE`.

**Five of the six controls are tenant-overridable.** By `pg_settings.context`, `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `work_mem`, and `max_parallel_workers_per_gather` are all `context = user`: any session may raise or disable them. A tenant with direct SQL ran `SET statement_timeout = 0` successfully. Only `temp_file_limit` (`context = superuser`) and the `CONNECTION LIMIT` role attribute are genuinely enforcing.

Consequences:

- Database-layer settings are a good-citizen default for well-behaved clients, not a control that binds a hostile one. Do not describe them as enforcement.
- Free tier is unaffected in practice: it has no direct SQL, and PostgREST is platform-configured (ADR-005).
- **Paid direct SQL has no effective per-statement resource ceiling from this layer.** Enforcement for paid tiers must come from `CONNECTION LIMIT`, `temp_file_limit`, pooler-level controls, node capacity management, and monitoring with the escalation path in `docs/RESOURCE-GOVERNANCE.md` — not from role GUCs.
- This strengthens rather than weakens ADR-009: layering is required precisely because no single layer, including this one, is sufficient.

**Amended 2026-08-19, during Phase 09 slice 0: the two settings that bind were applied to roles nobody logs in as.**

This ADR names `temp_file_limit` and `CONNECTION LIMIT` as the only two of these controls that hold against a client that does not want them, and tells paid tiers to rely on them. `provisioning.apply_plan_settings` wrote the plan's GUCs to `mldb_<ref>_authenticator` and `mldb_<ref>_auth` — the roles **the platform** logs in as, for PostgREST and GoTrue — and to neither `mldb_<ref>_admin` nor `mldb_<ref>_executor`, which are the roles a **customer's own session** logs in as.

Measured on a provisioned paid tenant, through a real direct connection rather than off the catalogue:

```
statement_timeout = 0        temp_file_limit = -1       work_mem = 4MB
max_parallel_workers_per_gather = 2   idle_in_transaction_session_timeout = 0
```

Those are the cluster's defaults against a plan that says 256 MB of temp files and no parallel workers. `CONNECTION LIMIT` was applied correctly; `temp_file_limit` — the other half of this ADR's own advice — was applied to nobody who could exceed it. Both paid direct SQL and, since ADR-039, the every-tier SQL console could write temp files until a shared node's disk filled.

The free-tier bullet above is also superseded rather than merely dated. "Free tier is unaffected in practice: it has no direct SQL" was true when written and stopped being true with ADR-039, which gave every tier a mediated SQL surface running as `mldb_<ref>_executor` — a login role that carried no plan settings at all. Nothing re-read this ADR when that decision was taken.

`provisioning.settings_roles` now names every login role, provisioning applies the settings again once the executor exists (it is created a stage after the database, so the first application cannot see it), and `plan_apply` re-asserts them on demand for projects provisioned before this. `tests/test_plan_apply.py` asserts the limit through a real connection, because a role setting that is present and not applied is exactly the failure this ADR exists to describe and `pg_db_role_setting` cannot tell the two apart.

## ADR-018 — Tenant bootstrap must harden the exposed schema

Status: Accepted. **Partly superseded by ADR-076** (2026-09-12): the `EXECUTE` revoke below removed extension functions from every customer role, not only from the RPC surface, so customer roles now hold `EXECUTE` and a PostgREST pre-request check keeps the functions off the Data API. The finding stays closed.

Established by the Phase 00 spike (`tasks/PHASE-00-FEASIBILITY.md`), executed 2026-08-15 against stock PostgREST 14.17 and Supabase Auth 2.195.0.

`CREATE EXTENSION maludb_core CASCADE` installs 373 functions into `public`. PostgREST exposes the callable subset as RPC endpoints, and `anon` was able to invoke them — `/rpc/gen_salt`, `/rpc/armor`, `/rpc/pgp_key_id` and others were reachable on the public Data API.

Supabase's usual mitigation — installing extensions into a dedicated `extensions` schema — is **not available**: `maludb_core` hard-codes `public.gen_random_bytes`, and relocating its dependencies fails the install. That is an upstream defect to be raised against `maludb-core`, not worked around in the extension.

Every tenant bootstrap must therefore:

- revoke `EXECUTE` from `PUBLIC`, `anon`, and `authenticated` on every extension-owned function in the exposed schema, after installing `maludb_core` and before the project becomes reachable;
- **keep that revoke true as the extension set changes.** A one-time revoke covers only the functions that exist when it runs. Anything installed afterwards lands in `public` with PostgreSQL's default `EXECUTE` grant to `PUBLIC` and is immediately RPC-callable by `anon`. ADR-015 makes this routine rather than exceptional: `maludb_core` is in every tenant database and ships 146 update scripts, so a fleet-wide `ALTER EXTENSION ... UPDATE` adding a function would re-open this finding everywhere at once, silently. The revoke must therefore be re-applied automatically after any `CREATE EXTENSION` or `ALTER EXTENSION` — enforced in the database by an event trigger, not by a runbook step — and its presence is itself a verified bootstrap property (confirmed 2026-08-15: without it, installing `tablefunc` into a bootstrapped tenant made 11 functions `anon`-callable);
- set `search_path = auth, public` on the project's auth role, so Supabase Auth's `schema_migrations` bookkeeping does not land in the exposed schema;
- create identity columns as `GENERATED BY DEFAULT AS IDENTITY`, since `GENERATED ALWAYS` breaks the official client's `.upsert()`;
- choose and document the `anon` grant posture, because "no grant" surfaces to applications as `42501 permission denied` rather than an empty result set;
- issue `NOTIFY pgrst, 'reload schema'` after any tenant DDL, since PostgREST otherwise returns `PGRST205` for new objects.

The verified SQL for each is in `tasks/PHASE-00-FEASIBILITY.md` and `scripts/spike-provision-tenant.sh`. Bootstrap hardening is a blocking Phase 02 deliverable and a required item in the Phase 03 security review.

## ADR-019 — Transactional email uses the MaluDB relay over SMTP, with per-project credentials

Status: Accepted

Transactional email is a hard dependency of Supabase-compatible Auth: signup confirmation, password reset, and magic link have no non-email alternative. Verified 2026-08-15 — with no SMTP configured, GoTrue 2.195.0 fails **silently**, returning HTTP 200 and stamping `confirmation_sent_at` while sending nothing and logging nothing. With SMTP configured, failures are loud (HTTP 500, logged) and transactional (the user row is rolled back).

Decision: the platform sends through the MaluDB-operated relay (`malumail`), which must expose authenticated SMTP submission. GoTrue speaks SMTP and has no HTTP send-email hook in this build; an SMTP frontend keeps stock upstream GoTrue, as required by the compatibility rule preferring upstream software.

Each project receives **its own SMTP credentials**, which is natural because each active project already has its own Auth process (ADR-007). This makes the relay the authoritative enforcement point for per-project attribution, quota, and immediate revocation on suspend or delete — enforcement that cannot be bypassed by worker configuration.

Consequences:

- Email volume is a metered, plan-configured entitlement like API and storage limits, enforced at the relay.
- Free and paid sending must use separate IP pools. Operating our own relay means owning IP reputation outright, with no shared-pool cushion and no vendor to escalate a blocklisting to; pool separation and per-project quotas are what make that acceptable.
- The relay must report bounces and complaints back to the control plane, and a global suppression list must be consulted before every send.
- Email confirmation is **on** by default. `GOTRUE_MAILER_AUTOCONFIRM=true` accepts addresses without proving control of them and permits account squatting; it is not a production default. An email-free tier is possible via `ALLOW_UNVERIFIED_EMAIL_SIGN_INS` but only as an explicit per-project entitlement.
- Unconfirmed users require a retention policy, since they hold the `UNIQUE` constraint on `auth.users.email` and can block the legitimate owner of an address.

Requirements on the relay, product decisions, and abuse controls are in `docs/EMAIL.md`.

## ADR-020 — Projects are owned by organizations, from the first row

Status: Accepted

`docs/CONTROL-PLANE.md` previously listed organizations as "later". Deferring them is the more expensive option: billing, plan entitlements, and team access all attach to the owning entity, so introducing an organization layer after launch means migrating the ownership of every project and every subscription.

Decision: model organizations from day one. `projects.account_id` becomes `projects.org_id` with a real foreign key to `organizations`. Every user receives a personal organization automatically at signup, so a solo developer never encounters organizational concepts in the product, while the ownership edge is an organization from the first row written.

Roles are organization-scoped and deliberately few: `owner`, `admin`, `developer`, `billing`, `viewer`. Project-scoped roles are a non-goal for now; the membership table is shaped so a `project_members` table can be added later without migrating existing rows. An organization must always retain at least one `owner`.

Details in `docs/ACCOUNTS.md`; tables in `specs/control-plane-schema.sql`.

## ADR-021 — Control-plane identity is separate from tenant Auth

Status: Accepted (ratified 2026-08-15)

Running MaluDB's own accounts on a MaluDB project with GoTrue would dogfood the product and make a good story. It would also create a circular dependency: signing in requires a project, creating that project requires the control plane, and the control plane requires signing in. A platform incident affecting tenant Auth would simultaneously lock operators out of the tooling needed to resolve it.

Proposed: platform user identity — users, organizations, sessions, personal access tokens, MFA factors — lives in the control-plane database, independent of tenant infrastructure. This is the fifth credential concept, alongside the four `docs/ARCHITECTURE.md` already warns against conflating; it must not share a session, token format, or table with tenant end-user identity.

Sessions are server-side records rather than stateless JWTs, because an account controlling production databases needs revocation to take effect immediately rather than at token expiry.

Ratified. Consequences for implementation:

- Phase 01 builds platform identity directly: `users`, `organizations`, `org_members`, `user_sessions`, `personal_access_tokens`, `user_mfa_factors` in the control-plane database, per `specs/control-plane-schema.sql`. It does not configure a tenant Auth service for this purpose.
- No control-plane authentication path may depend on a tenant database, a tenant Auth worker, or a provisioned project being healthy. Operators must be able to sign in during a tenant-plane incident.
- Password hashing, session issuance, and PAT verification are the control plane's own responsibility, following the algorithm split in ADR-023.

Revisit dogfooding once the platform is operationally mature and a break-glass path exists that does not depend on tenant Auth. Until then, "MaluDB runs on MaluDB" is not a claim the platform makes.

## ADR-022 — Connections, not memory, bound warm density; a pooler is required

Status: Accepted

Measured 2026-08-15 with three concurrent projects, each running its own PostgREST 14.17 and Auth 2.195.0. See `docs/CAPACITY.md`. This is the measurement ADR-007 deferred.

Per warm project: 31.8 MB PSS of workers (14.2 PostgREST + 17.6 Auth), 4 PostgreSQL backends at a pool size of 3, and 24 MB of disk. Backends cost ~4 MB PSS idle and ~15 MB once they have served queries, because relcache and catcache are per-database and therefore not shared between tenants — the intrinsic memory cost of ADR-002.

At default `max_connections = 100`, this caps a cluster at **roughly 24 warm projects**, while memory on the same host would have allowed about 40. Connections run out first.

Decisions:

- A **transaction-mode pooler is required** before roughly 25 warm projects per node at default settings. It is not an optional later optimization. Because poolers key server pools by (user, database), database-per-tenant gains no cross-tenant multiplexing; the benefit is that idle-but-warm tenants stop holding backends. Pool capacity is sized from *concurrently active* projects, not total projects.
- The node scheduler must track **warm** project count separately from total project count. They have entirely different cost profiles and only warm count consumes connections.
- Worker startup is **per-service and demand-driven**. The Auth worker is the single largest per-project allocation and must not be started for projects that do not use Auth.
- Wake orchestration must wait for **readiness, not for the port to open**. PostgREST answers `503 PGRST002` until its schema cache loads, so a request routed on port-open alone fails.

ADR-007 stands. At 32 MB of worker PSS per project, the per-project process model is not the binding constraint, and the measured cost does not justify building a custom multi-tenant PostgREST or Auth. Revisit if warm density targets exceed a few hundred per node, or if cold start grows beyond about a second for representative schemas.

Free-tier economics rest entirely on sleep: a slept project costs zero RAM and zero connections, leaving free density bounded by the 24 MB disk floor. Measured cold start is sub-second — 320 ms to a serving PostgREST, 175–268 ms to a responding Auth worker — which makes an aggressive sleep policy clearly correct.

## ADR-023 — Secrets are classified by recoverability; hashing and encryption are not interchangeable

Status: Accepted

`docs/SECURITY.md` previously gave one rule — "prefer storing non-reversible verification data" — which is correct for about half the secrets in this system and unworkable for the other half. Storing a tenant database password as a hash makes the platform unable to configure the worker that needs it.

Secrets are classified by whether the platform must reproduce the plaintext:

- **Class A, verifiers** — hashed. Project API keys, personal access tokens, session tokens, invitation tokens, user passwords.
- **Class B, recoverable** — envelope encrypted. Tenant database passwords, per-project JWT signing keys, SMTP passwords, MFA seeds.
- **Class C, ephemeral** — not stored.

Within Class A the algorithm differs by **entropy, not importance**. User passwords use Argon2id, because they are human-chosen and dictionary-attackable. High-entropy machine-generated tokens use HMAC-SHA-256 with a server-side pepper: there is no feasible search space to slow down, and project API keys are verified on every request, where a memory-hard function would be a self-inflicted denial of service. Each carries a non-secret indexed prefix so a hashed value can still be looked up in one fetch.

Class B uses a KEK/DEK hierarchy with AEAD. Associated data must bind each ciphertext to its table, column, and owning identifier, so a ciphertext moved between rows fails to decrypt. Key version is stored per value so rotation is incremental. The KEK is never persisted in the control-plane database — a dump of it must be useless alone — and the control plane must fail closed, refusing to start, if the KEK source is unavailable.

This adds `encryption_keys` and `project_credentials` to `specs/control-plane-schema.sql`. The latter closes a real hole: per-project database passwords and JWT signing keys previously had nowhere to live at all, and the Phase 00 spike wrote them to a file in `/tmp`.

**MaluDB's in-database secret store is not used for platform secrets.** It would create a bootstrap circularity — the control plane needs a database credential to reach the database that would hold that credential — and it would put platform secrets inside a tenant-adjacent store, blurring the boundary ADR-013 draws. It remains a tenant-facing product feature. This resolves the open question raised in `docs/MALUDB.md`.

Where the KEK itself lives is unresolved and tracked in `docs/OPEN-QUESTIONS.md`. Full model, rotation procedures, and required tests: `docs/SECRETS.md`.

## ADR-024 — Control-plane stack is Python 3.12 with FastAPI and psycopg3

Status: Accepted

This decision covers the **control plane only**. The gateway is a different workload and is deliberately not decided here.

### Stack

Python 3.12, FastAPI, uvicorn, psycopg3. No ORM: raw SQL through psycopg3 helpers, matching the house style already established in `maludb-python-api-server`, which uses `psycopg` with `dict_row` and thin `db_query` / `db_exec` / `db_one` wrappers and no ORM anywhere.

Rationale:

- **Operational familiarity**, which matters most on a low-traffic, correctness-critical service. The team already runs ~13,000 lines of FastAPI + psycopg3 with tests and ruff, and that codebase was itself a port from PHP — the trajectory is settled.
- **psycopg3** is best-in-class for PostgreSQL, and the control plane is database-heavy.
- **Crypto for ADR-023 is covered by audited libraries**: `cryptography` provides AES-GCM and ChaCha20-Poly1305; `argon2-cffi` provides the memory-hard hash for passwords.
- Worker supervision in later phases is expected to be systemd rather than in-process, so the language's process-management strength is not a deciding factor.

Go was the strongest purely technical fit — pgx, stdlib AEAD, a single static binary on a Proxmox VM — but it is not installed and there is no Go experience here. On a security-critical control plane, unfamiliarity is a defect risk, not merely slower delivery. Revisit if the gateway is later written in-house, where one language across both would beat two.

### The gateway is not decided here

Control plane and gateway have opposite profiles: the control plane is low-traffic orchestration, the gateway sits on every tenant API request with a sub-millisecond budget. Choosing one stack for both would compromise one of them.

The gateway decision belongs to Phase 03, where Envoy with an `ext_authz` callout should be the leading candidate — it satisfies ADR-008's project/key matching without writing a proxy, and `docs/SOURCES.md` already references Supabase's self-hosted Envoy configuration.

### Authority: generated artefacts win

FastAPI generates OpenAPI from route signatures, and migrations will supersede the hand-written schema. Both create two sources of truth unless settled now.

- **API contract**: code-first. FastAPI is authoritative. CI regenerates `specs/control-plane-api.yaml` and fails if it differs from the committed file, so the spec stays reviewable in pull requests without being hand-maintained.
- **Database schema**: migrations become authoritative. `specs/control-plane-schema.sql` becomes the human-readable reference. Migrations are plain versioned `.sql` files applied by a minimal runner — the schema spec is already SQL and executes cleanly, so no ORM or migration DSL is introduced to restate it.

### Interactive docs must not be public

FastAPI serves `/docs`, `/redoc`, and `/openapi.json` unauthenticated by default. For the control plane that publishes a map of the admin surface, including provisioning endpoints from Phase 02 onward, which `docs/SECURITY.md` forbids exposing.

Documentation routes are configuration-driven: enabled in development, and in production either disabled or behind the same authentication as the API. This is covered by the existing Phase 01 criterion that configuration supports multiple environments.

## ADR-025 — This repository is backend-only; the web frontend lives in its own repository

Status: Accepted

The public website, self-serve signup, and customer dashboard do not belong in this repository.

Reasoning, in order of weight:

- **Trust boundary.** The control plane is the highest-trust component in the system: it holds provisioning credentials, the KEK, and every tenant's database passwords. A public marketing site is the lowest-trust. The code review rules in `AGENTS.md` — cross-tenant access, SQL injection through generated identifiers, privilege escalation — are correct for everything here and wrong for a pricing-page copy change. Sharing a repository means either applying that scrutiny to marketing changes or relaxing it for both.
- **Existing scope.** `services/README.md` already enumerates this repository's components as control-plane API, provisioning worker, gateway/router, worker manager, metrics collector, and billing adapter. All backend.
- **Cadence.** Marketing content changes constantly; the control plane should not. A shared pipeline runs provisioning tests on every copy edit.
- **Language.** A mixed Python/TypeScript monorepo needs tooling investment that brings no benefit at current team size.

The frontend is a **client** of this repository, and the contract already exists: ADR-024 makes `specs/control-plane-api.yaml` authoritative and CI-enforced against the FastAPI application, so the web repository generates a typed client from it rather than sharing source.

Marketing, signup, and dashboard should share **one** web repository. Splitting them further is over-engineering at current team size; marketing can be separated later if it moves to a CMS or a different owner.

This decision is about repository topology, not deployment topology. Which control-plane endpoints are internet-reachable is a separate and still-unresolved question, recorded in `docs/OPEN-QUESTIONS.md`.

## ADR-026 — The gateway is a Python ASGI proxy for the MVP, with a measured throughput number

Status: Accepted

Decided 2026-08-15, opening Phase 03. Resolves "API gateway implementation choice?" in `docs/OPEN-QUESTIONS.md`.

Every tenant data request passes through the gateway, which makes it simultaneously the security boundary and the throughput ceiling. Three candidates were weighed: a Python ASGI proxy in the ADR-024 stack, a Caddy/nginx front end calling the control plane to authorize and resolve an upstream, and Envoy driven by a control-plane xDS service.

The gateway is chosen as a **Starlette/httpx ASGI proxy in the existing Python service**.

The deciding factor is where the cross-tenant control lives. ADR-008 requires the gateway to verify that a submitted API key belongs to the project named by the hostname; a mismatch there is a cross-tenant read available to anyone on the internet holding any valid key. That check, its cache, and its invalidation belong in one place with one test harness. Splitting them across a proxy configuration and an authorization callback creates exactly the seam where such a defect hides, and Phase 02's reviews found repeatedly that the bugs lived in the joins between components rather than inside them.

The cost is accepted knowingly: Python sits in the data path for every byte of tenant traffic. This is an MVP decision, not a permanent one, and it is falsifiable — Phase 03 slice 3 must record a measured throughput and latency number, in the manner ADR-022 established for warm density. Replacing the transport later does not change the control plane, because the routing and authorization logic is a library the proxy calls rather than the proxy itself.

**Measured 2026-08-15**, Phase 03 slice 3, on the development host with `scripts/bench-gateway.py`:

- **+6.3 ms added latency per request at p50** (8.08 ms direct to the upstream, 14.35 ms through the gateway), measured sequentially so nothing queues and the difference is the gateway's own cost.
- Under 20 concurrent requests: 56 rps through the gateway against 133 rps direct. Both figures are bounded by the thread-per-request Python stub standing in for PostgREST, so that pair is a floor rather than a throughput measurement.

Measuring changed the implementation, which is the point of requiring it. The first version made three or four database round trips per request — a project lookup, a key resolution, an AES-GCM decrypt of the project's JWT secret, and an activity write — and re-read a signing key that never changes on every request. Those are now cached in the gateway process with short bounded TTLs, and the JWT secret is only fetched on the path that actually mints a token. p95 under concurrency fell from 1366 ms to 398 ms on that change alone.

6 ms is acceptable for the MVP and is not obviously acceptable at scale. Re-run the script before deciding it is fine; the decision was made falsifiable so it could be revisited on evidence, not so the evidence could be filed and forgotten.

Consequences:

- Slice 3 lands a measurement, not just a passing test suite.
- Internal worker endpoints must be unreachable from the internet independently of the gateway, since the gateway is no longer a hardened C proxy (`docs/API-GATEWAY.md`).
- TLS termination and the wildcard-certificate strategy remain open, and may still be handled by something in front. That is a transport decision and does not reopen this one.

### The socket measurement, because the request one does not carry over

**Measured 2026-08-16**, Phase 06 slice 3, with `scripts/bench-gateway-sockets.py`. A WebSocket is held rather than served and gone, so the question changes from "what latency does a request pay" to "how many connections can one gateway process hold": 200 concurrent Realtime sockets, proxied through the gateway to an echo upstream.

- **≈204 kB of RSS per socket**, covering *both* ends — the benchmark client and the gateway share the process — so the gateway's own share is smaller and this is an upper bound. At that bound, ten thousand concurrent subscribers is roughly 2 GB in one gateway process.
- **8.8 ms to complete a handshake** at p50, 11.9 ms at p95. Paid once per connection rather than once per message, which is why it matters far less than the request path's +6 ms.
- **1.6 ms round trip for a frame** at p50 on an established socket.
- RSS did not fall when all 200 closed (105.7 MB against 105.6 MB while held). That is the allocator not returning pages to the OS rather than a leak — the limiter's own counter goes back to zero, which `tests/test_gateway_realtime.py` asserts — but it means a gateway sized for a peak stays that size.

Two things the measurement changed, both found by running it rather than by reading the code. Passing `Host` as an extra header on the upstream connection appended a **second** Host rather than replacing the library's, leaving the header that identifies the tenant ambiguous. And an ordinary client-initiated disconnect raised out of the close path, logging a traceback for every normal session end.

What this does **not** measure is what a Realtime *server* process costs. The upstream here is an echo server. ADR-022 still has no Realtime density term, and no capacity figure may assume one — see `docs/REALTIME.md`.

## ADR-027 — Per-project API workers are systemd template units

Status: Accepted

Decided 2026-08-15. Resolves "systemd template units vs another supervisor?" in `docs/OPEN-QUESTIONS.md`.

ADR-007 permits a PostgREST process per active project, and ADR-022 measured the cost as acceptable. Those processes need supervision: restart on failure, log capture, resource limits, and a lifecycle that survives a control-plane restart.

Workers run as **systemd template units**, `maludb-postgrest@<project-ref>.service`, started and stopped by the control plane through systemd rather than spawned as child processes.

The alternative — the control plane spawning and tracking subprocesses — puts process supervision inside a web application. A control-plane restart or crash would then orphan every tenant's worker, leaving processes that nothing owns and that no operator can find by conventional means. Systemd already solves restart policy, log routing, and cgroup limits, and an operator who has never read this codebase can still inspect and restart a tenant's worker with standard tools.

Consequences:

- The control plane needs a narrow, audited privilege to manage exactly these units, not general root.
- ADR-022's requirement stands and is unaffected: wake must wait for PostgREST readiness, because `systemctl start` returning says nothing about whether the schema cache has loaded. A unit that is `active` still answers `503 PGRST002` for a moment.
- Node provisioning must install the template unit, which makes it part of node preparation rather than tenant provisioning.

## ADR-028 — API keys carry a MaluDB-specific prefix

Status: Accepted

Decided 2026-08-15. Resolves "exact MaluDB key format?" in `docs/OPEN-QUESTIONS.md`.

Project API keys are `mldb_publishable_<random>` and `mldb_secret_<random>`, mirroring the *shape* of Supabase's modern `sb_publishable_` / `sb_secret_` keys but not the prefix.

**Corrected 2026-08-15, during implementation.** This ADR was first written with an `mdb_` prefix, which was wrong: every generated identifier in the system already uses `mldb_` — tenant databases (`mldb_<ref>`), tenant roles (`mldb_<ref>_authenticator`), and the personal access tokens from Phase 01 (`mldb_pat_...`). A second, nearly identical prefix for one credential type would be arbitrary inconsistency, and the near-miss between `mdb_` and `mldb_` is the kind that survives review and then confuses a grep. The reasoning below is unaffected; only the literal string changed.

This costs nothing in compatibility. The official client treats the key as an opaque bearer token in a header and never parses it, so ADR-001's compatibility wedge is unaffected. The distinct prefix buys two things that matter operationally: a leaked key is attributable to MaluDB at a glance rather than being mistaken for a Supabase key, and secret-scanning rules — ours, GitHub's, and customers' — can match on it.

Storage follows ADR-023: keys are high-entropy, so they are Class A hashed with the pepper, not encrypted. The plaintext is returned exactly once at creation and is unrecoverable afterwards. `api_keys.key_identifier` is the indexed lookup handle, so validating a key is one indexed read rather than a comparison against every key on the platform.

Record the prefix divergence in `specs/compatibility-matrix.yaml` as intentional, per the `AGENTS.md` rule on documenting deliberate incompatibilities.

Storage splits by class, which is the substantive half of this decision. A **secret** key is server-side only and never read back, so it is Class A: an HMAC verifier and nothing else, and a database leak yields no working key. A **publishable** key is public by design, lives in the customer's client bundle, and a dashboard must be able to display it again indefinitely — it is recoverable *by requirement*, so ADR-023 makes it Class B: envelope encrypted as well as verifiable. Encrypting a value that is already public reads as redundant; the point is that the platform must be able to hand it back, and ADR-023 holds that a value with that requirement is never stored hashed-only. Migration 0007 makes the split a `CHECK` constraint rather than a convention, because a secret key carrying recoverable ciphertext would not be visible in review of the code that wrote it.

## ADR-029 — Auth email is sent through GoTrue's Send Email Hook to the MaluMail REST API, superseding the SMTP mechanism in ADR-019

Status: Proposed — supersedes the transport mechanism of ADR-019, not its intent

ADR-019 decided that transactional email goes through MaluDB's own relay. That stands. The mechanism it chose does not, and it rests on a claim that is false for the version it cites.

### Two findings

**GoTrue 2.195.0 has a Send Email Hook.** ADR-019 states it "has no HTTP send-email hook in this build" and reasons from there that the relay must expose authenticated SMTP submission. Verified 2026-08-15 against the same binary, with no SMTP configured at all:

```
GOTRUE_HOOK_SEND_EMAIL_ENABLED=true
GOTRUE_HOOK_SEND_EMAIL_URI=http://127.0.0.1:29999/send-email
GOTRUE_HOOK_SEND_EMAIL_SECRETS="v1,whsec_<base64>"
```

A signup returned `200`, GoTrue logged `Noop mail client being used` and `Hook ran successfully`, and the endpoint received a signed POST carrying `user`, `metadata`, and an `email_data` object with `email_action_type`, `token`, `token_hash`, `redirect_to` and `site_url`. Signature headers follow Standard Webhooks (`webhook-id`, `webhook-signature`, `webhook-timestamp`).

**MaluMail exposes REST only.** Confirmed 2026-08-15 against the published documentation at `malumail.com/docs`, which lists four endpoints and no others: `POST /v1/send`, `GET /v1/suppressions`, `POST /v1/suppressions`, `DELETE /v1/suppressions`. There is no SMTP submission endpoint, API keys are created by a human in a portal rather than programmatically, there are no delivery webhooks, no usage or quota endpoint, no templates API, and no sub-account concept. Sending domains are verified in the portal with no API.

ADR-019's requirements therefore stand as follows: **R1** (authenticated SMTP submission) and **R2** (per-project credentials) cannot be met at all; **R3** (per-project quota at the relay) has no mechanism, since limits are per account and there is nothing to read them from; **R6**'s bounce feedback exists only as a suppression list to be polled; **R7** (per-project observability) has no endpoint. **R4** (DKIM for a verified domain) and **R5** (separate IP pools) are properties of the service and unaffected.

Taken together: the transport ADR-019 specified is unavailable on one side and unnecessary on the other.

### Decision

Auth email flows **GoTrue → a platform HTTP hook → MaluMail `POST /v1/send`**.

The hook endpoint is platform code, one route per project, configured into each project's Auth worker along with a per-project hook secret. Stock upstream GoTrue is preserved, which was ADR-019's reason for choosing SMTP in the first place — the compatibility rule that prefers upstream software is satisfied better by a supported extension point than by an SMTP frontend built to accommodate its absence.

### Two sender modes, because there are two different email streams

Ratified by the owner 2026-08-15. `project_email_settings.sender_mode` already
models this; the point here is which stream uses which, and why one account is
not enough on its own.

The streams are genuinely different and were being conflated:

1. **MaluDB emailing its own customers** — control-plane signup confirmation,
   organisation invitations, billing, dashboard password reset. Governed by
   ADR-021, not by tenant Auth. One MaluMail account, ours. A customer never
   touches MaluMail for any of it.
2. **A customer's application emailing its own end users** — the confirmation
   and reset messages GoTrue sends. Those recipients are the customer's users,
   who have no relationship with MaluDB.

**`platform_default`** — the platform's single MaluMail account, used for all of
stream 1 and for stream 2 on development and free projects. It works with no
customer onboarding at all, which is what makes a new project usable for signup
the moment it is created. It carries a deliberately low **per-project** rate
limit, enforced by the platform before the call, because one account's allowance
is shared by every project using this mode.

**`custom_domain`** — a customer going to production supplies their own MaluMail
API key and verified sending domain. Their branding, their sending reputation,
their rate limits, enforced by MaluMail against their own account.

Sending stream 2 from the platform account indefinitely would be wrong for three
reasons, which is why the second mode exists rather than being a later nicety:
a confirmation message from an unrecognised domain for an app the recipient does
know reads as phishing; every tenant would share one sending reputation, so one
tenant's complaints degrade delivery for all of them — the risk ADR-019 already
named in choosing to own the relay outright; and one project's signup surge would
consume the shared rate limit.

This is also what Supabase does. Its built-in sender is capped at two messages
per hour, carries no delivery SLA, and is documented as being for "exploring and
getting started" — with custom SMTP required for anything real. Following the
same shape keeps ADR-001's compatibility posture and sets expectations customers
already have.

The per-project limit on `platform_default` is a plan entitlement, not a
constant: `specs/plans-and-limits.yaml` already carries `emails_per_day`,
`emails_per_month` and `email_custom_sending_domain`, and `AGENTS.md` forbids
hard-coding plan limits in application logic.

### Consequences

- **The platform composes the message.** With the hook enabled GoTrue no longer renders or sends anything; it hands over a token and an action type. Building the confirmation URL and the message body becomes ours. This is more work than SMTP, and it is also the only way to control branding — MaluMail's templates are not reachable from `/v1/send`, which takes only `subject`, `text` and `html`.
- **On `custom_domain`, the platform holds a credential that sends mail as the customer's domain.** Class B under ADR-023, because the hook must reproduce it to call the API. Worth naming as its own risk: from a customer's point of view this is arguably more sensitive than their database password, since a leak lets someone send mail that passes SPF and DKIM as them. Never logged, never returned over the API, and revocable by the customer in their own portal without our involvement.
- **Email is metered by MaluDB only on `platform_default`.** There the platform enforces the plan entitlement before calling, because the allowance is shared. On `custom_domain` the customer's own MaluMail plan governs, we neither meter nor bill it, and a `429` reflects their limits rather than ours. ADR-019 described email as "a metered, plan-configured entitlement like API and storage limits"; that remains true for the mode where it can be, and stops being true for the mode where the account is not ours.
- **Bounce feedback is polled, not pushed.** MaluMail has no webhooks; bounces and complaints are added to the suppression list automatically. The platform reads `GET /v1/suppressions` with the project's own key into `email_suppressions`, which migration 0003 already provides. Suppression is also enforced server-side, so a suppressed address appears in `rejected` on a `200` rather than failing the request — partial success must be inspected, never assumed.
- **`/v1/send` has no idempotency key.** GoTrue retries a failing hook, so a naive implementation double-sends. The hook must treat a `200` from MaluMail as final and must not retry it; only `429`, `502` and transient `5xx` are retryable.
- **`project_email_settings` needs revising.** Its `smtp_username` and `smtp_ciphertext` columns model SMTP credentials that will not exist. They become the customer's MaluMail API key plus the per-project hook secret — both still Class B, for the same reason the SMTP password was.
- **Suspension is enforced by us, not by the relay.** The key belongs to the customer, so the platform cannot revoke it. Since auth mail only leaves through our hook, refusing to send there is complete for anything the platform originates — but it is worth being clear that a suspended customer can still use their own MaluMail account directly, because it is theirs.

### What does not change

Email confirmation is still on by default; `MAILER_AUTOCONFIRM=true` still is not a production posture. Email is still a metered, plan-configured entitlement. Unconfirmed users still need a retention policy. Separate IP pools for free and paid are still required of MaluMail. Those are ADR-019's product decisions and they survive intact.

## ADR-030 — Rate-limit state is per gateway process, and the multiplication is written down

Status: Accepted

Decided 2026-08-16, opening Phase 05 slice 2. Resolves "Redis/distributed cache or gateway-local cache first?" in `docs/OPEN-QUESTIONS.md` for the rate limiter specifically; the key cache made the same call independently in Phase 03.

Per-project rate and concurrency counters live in the gateway process. There is no shared store.

The alternative is Redis, which is where this ends up. It is not where it starts, for two reasons. There is one gateway today, so a local counter *is* the platform limit rather than an approximation of it. And ADR-026 accepted Python in the data path on the condition that its cost be measured; adding a network round trip to every request, to a service the platform does not otherwise run, spends that budget before there is a second gateway to justify it.

**The consequence, stated plainly rather than left to be discovered: with N gateways the effective limit is N times the configured one.** A project on 300 requests per minute served by three gateways can make 900. That is a property of this implementation, not a rounding error, and it means the limit must not be described to customers as a platform-wide guarantee until the state is shared.

Two things follow, and both are in the code rather than in an intention:

- `Limiter` is a protocol with a narrow surface — `acquire` and `release`. Replacing `LocalLimiter` with a Redis-backed one is a class, not a rewrite of the request path.
- The counter is swept, so state does not accumulate one entry per project ref ever seen.

Revisit when a second gateway is deployed, which is also when the key cache's staleness window stops being the only cross-gateway consistency question worth asking.

### The measurement ADR-026 requires

Re-run after the limiter landed, because ADR-026 made keeping Python in the data
path conditional on a number rather than on a judgement:

> **+5.77 ms added latency per request at p50** (7.95 → 13.72 ms), measured
> sequentially so nothing queues and the difference is the gateway's own cost.

The figure recorded when the gateway was built, before any limiter existed, was
+6.3 ms. The limiter is therefore free at this scale within run-to-run noise —
which is what an in-process dictionary behind a lock should cost, and is the
strongest argument for ADR-030's choice not to put a network round trip there.

The first run of that benchmark after the limiter landed served **327 of 500**
requests and reported a latency that was mostly rejections: the bench project
was on the free default of 300 requests a minute. The script now provisions its
own generous allowance. Worth recording because a customer running a load test
will hit exactly that, and the number they see will be the cost of being refused
rather than the cost of being served.

### What the limiter deliberately does not do

- **It does not limit unauthenticated requests.** Limiting before authentication would let anyone spend a project's allowance by sending its hostname, turning the limiter into the denial-of-service tool it exists to prevent. The cost is that an unauthenticated flood still pays for a project lookup and a key check per request; bounding *that* is platform-level protection and belongs in front of the gateway, not inside it.
- **It does not fail closed on a missing limit.** A plan with no rate configured resolves to a positive default in `entitlements`, so reaching the limiter with a zero limit means something is already wrong. It allows the request rather than locking the project out on a configuration error — the layers below (pool size, statement timeout, node capacity) still apply, and ADR-009 exists precisely so no single layer has to be sufficient.

## ADR-031 — The `REPLICATION` attribute is contained at the node, not the tenant

Status: Accepted — **topology superseded by ADR-034**; the security analysis stands

Accepted 2026-08-16, opening Phase 06 slice 1, which encodes it as a node precondition rather than an intention. Proposed 2026-08-16 from the Phase 06 slice 0 spike. Evidence and reproduction in `specs/realtime-replication-model.md`.

**Read this with ADR-034.** Everything below about the `REPLICATION` attribute, the cluster-wide exposure through physical replication, and the `pg_hba.conf` reject that contains it remains correct and remains a node prerequisite. The conclusion it drew about *how many Realtime servers a node runs* does not: slice 4 found that upstream's replication slot names assume one tenant per PostgreSQL cluster, which database-per-tenant does not provide. The reasoning that failed is identified in ADR-034 — "upstream is itself multi-tenant" is true across clusters, not across databases within one.

Realtime requires logical decoding, logical decoding requires the `REPLICATION` role attribute, and there is no lesser privilege that buys it — PostgreSQL refuses both the SQL and protocol paths with *"Only roles with the REPLICATION attribute may use replication slots."* So the platform must issue that attribute to a customer-serving role, and cannot design around it.

Measured consequence: a non-superuser holding only `REPLICATION`, with `CONNECT` on one tenant database and explicitly denied another, took a 484 MB physical base backup containing **every database on the cluster**. The ADR-014 `CONNECT` lockdown does not constrain physical replication, because physical replication names no database and never reaches the check. Logical replication, by contrast, *is* bound by `CONNECT` — the same role's logical connection to the denied database was refused.

The containment is `pg_hba.conf`. Its `replication` keyword matches physical connections only, so `host replication all <cidr> reject` blocks base backups and physical streaming while leaving logical decoding working and `CONNECT`-scoped. That line becomes a **node prerequisite**, asserted at node registration alongside `wal_level`, in the same way ADR-014's lockdown is mandatory rather than advisory.

This decides the topology question, though not the way the plan expected. The cluster-wide exposure is a property of the attribute, not of sharing: a Realtime instance per project inherits it identically, so per-project isolation buys nothing against it. It is closed at the node or not at all. With it closed, the remaining difference is blast radius on credential compromise — N tenants for a shared server, one for a per-project server — weighed against a per-project BEAM VM whose cost ADR-022 does not cover and which this spike could not measure, because Docker is absent from the development host.

Therefore: **one shared Realtime per node, conditional on the `pg_hba` reject being present**, matching upstream's own multi-tenant model as `AGENTS.md`'s compatibility rule prefers. Its credential store holds replicator credentials for many tenants and must meet ADR-023 in full; that concentration is the same category the control plane already occupies. If per-project Realtime is later measured and turns out to be cheap, the blast-radius argument favours it and this should be reopened.

Two properties of the replicator role are recorded here because they are easy to lose. It reads every table in its own database past grants and row-level security — decoding reads WAL, which is written before any policy is consulted — so **all RLS enforcement for Postgres Changes happens in the Realtime server rather than in PostgreSQL**, and the compatibility suite has to prove it does. And the attribute must never land on `mldb_<ref>_admin` or `mldb_<ref>_authenticator`, both customer-reachable on paid plans; `REPLICATION` on either would hand a customer a readable copy of every tenant on the node.

### How the containment is asserted, since a node precondition nobody checks is a comment

Slice 1 checks the `pg_hba.conf` reject two ways, because the static and the empirical answer fail differently.

A probe does the empirical half: libpq's `replication=true` opens a physical replication connection and nothing else, so it reaches exactly the rule under test. A node is ready only when that connection is refused *by an hba reject specifically* — a refusal for any other reason (node down, wrong credential) is recorded as unknown, and unknown is not ready. The failure mode of the opposite choice is a node marked prepared because the check could not run.

The probe is necessary and **not sufficient**, which the security review of slice 1 caught in slice 1's own code. It runs as one role, the platform's own, and `pg_hba.conf` matches on the user as well as the address. So

```
host replication postgres 127.0.0.1/32 reject
host replication all      127.0.0.1/32 trust
```

answers the probe correctly — that role genuinely is rejected — while admitting every tenant replicator on the node underneath it. Verified against a live cluster: the probe returned "rejected" on exactly that file.

`pg_hba_file_rules` is therefore parsed as well, and the two must agree. First-match is modelled per (type, address, netmask), and a reject shadows the rules below it only when it names `all` users. CIDR containment between groups is not modelled, so a permissive rule shadowed by a broader earlier reject is still reported — the conservative direction, and the fix is deleting a line that was already dead. Parsing also lets the failure name the offending line, which is what an operator needs.

`tests/test_realtime_node.py` runs the same assertion through `pg_basebackup`, which is what an attacker would actually reach for, against a cluster built by `scripts/realtime-test-cluster.sh`. That script also builds a deliberately unprotected cluster on request, so the check has been shown capable of returning *unsafe* — a control that has never failed has not been tested. On a stock Debian cluster it returns `host replication all 127.0.0.1 trust`.

## ADR-032 — WAL retention is bounded, and an invalidated slot is a reportable incident

Status: Accepted

Accepted 2026-08-16, opening Phase 06 slice 1. Proposed 2026-08-16 from the Phase 06 slice 0 spike. Resolves the second decision in `plans/completed/phase-06-realtime.md`.

At the `max_slot_wal_keep_size = -1` default, one idle logical slot grew `pg_wal` from 17 MB to 225 MB during a single 200,000-row insert, pinning 206 MB that a `CHECKPOINT` did not release. Nothing self-limits: the consumer need not be malicious, only absent — a crashed worker, a partition, or a project Phase 05 put to sleep on purpose. A full disk stops writes for **every tenant on the node**, making one project's inactivity a cross-tenant outage.

`max_slot_wal_keep_size` is therefore mandatory node configuration, recorded per node, and it is the *only* backstop: a role holding `REPLICATION` for legitimate reasons can create a WAL-reserving physical slot through ordinary SQL even with ADR-031's `pg_hba` reject in place, and PostgreSQL has no per-role slot quota.

Bounded, the failure inverts cleanly — the slot is invalidated (`wal_status=lost`) and `pg_wal` plateaus. Note that it plateaus rather than shrinks; PostgreSQL recycles segments for reuse, so the bound caps growth and does not reclaim, and node capacity must budget the bound as space that will be occupied.

The cost is a project that stops receiving changes with nothing in the connection to say so. An invalidated slot is therefore treated as a **project-visible incident**: detected by the Phase 05 maintenance pass, recorded as an audit event, and surfaced to the customer. Recovery re-creates the slot, which resumes from the present and does not replay the gap — the report must say so, or a customer will assume a backfill that never happened.

Silence is the one unacceptable outcome. ADR-009's defence in depth is about containing failures, and a contained failure nobody is told about is indistinguishable from data loss.

### What slice 1 built, and the one thing it deliberately does not do

`maintenance.check_replication_slots` compares every prepared node's real slots against the projects that should hold them, and writes an audit event on the *transition* rather than on the state — the pass runs under a timer, and a row per run would bury the one that mattered. Three outcomes are distinguished, because they need different responses: `lost` is PostgreSQL doing what this ADR asked, `missing` is drift nobody asked for, and an unaccounted slot is the physical-slot path above, which no project would ever point the pass at.

Every event carries `replayed_on_recovery: false`. Stated rather than implied, because the whole failure mode here is a customer who believes something happened that did not.

Slice 1 does **not** re-create an invalidated slot. Recovery skips the gap, so the platform silently repairing it would convert a reportable incident back into a silent one — the exact outcome this ADR exists to prevent. Re-creation belongs with per-project enablement in slice 2, where there is a customer-visible surface to say what was lost.

The capacity consequence is enforced separately: committed slots are counted from *enablement*, not from what the node currently reports, so a project whose slot is lost keeps its claim on one. Counting live slots would hand a stalled project's slot to somebody else and then be unable to give it back.

## ADR-033 — Realtime runs as a pinned container image under Podman, supervised by systemd

Status: Accepted

Decided 2026-08-16, opening Phase 06 slice 4. Introduces a container runtime to the platform, which is a deployment-model change and therefore an ADR rather than an implementation detail.

Upstream `supabase/realtime` is distributed as a **container image only** — no release binaries — and Phase 06 slices 0 through 3 worked around its absence: slice 0 measured the PostgreSQL layer underneath the server, slice 3 proxied to a stub. Slice 4 cannot, because its whole point is that the official client talks to the real thing. The alternative, building the Elixir release from source, means the platform owns a build upstream does not publish and tracks its dependencies indefinitely, to avoid one package install. `AGENTS.md` prefers upstream's own artefact.

**Podman rather than Docker**, for one reason that matters on a host holding every tenant's database: membership of the `docker` group is root-equivalent, because the daemon runs as root and will bind-mount anything it is asked to. Podman is rootless and daemonless, runs the same OCI image, and `podman generate systemd` produces an ordinary systemd unit — so ADR-027's rule that workers are systemd-supervised survives rather than being contradicted. Verified rootless with `crun` and subuid/subgid maps on the development host.

**Pinned to v2.110.0**, and the reason is hardware rather than caution. Upstream's latest image (v2.128.0) starts the BEAM successfully and then dies with SIGILL inside `liblumis_nif-v0.7.0`, a precompiled Rust NIF built for a CPU baseline these nodes do not meet: `QEMU Virtual CPU version 2.5+`, with SSE4.2 and `popcnt` but **no AVX, AVX2, BMI2 or FMA**. The dependency is recent — absent from `mix.exs` at v2.110.0, present at v2.128.0 — and v2.110.0 boots and serves cleanly.

Production nodes are expected to share that CPU profile, so this is a platform constraint and not a development-host quirk. It is recorded in `specs/realtime-server-model.md` beside the pin, so the version has a reason attached rather than becoming folklore, and it is pinned in **every** environment rather than only locally: the repository already pins `maludb_core`, PostgREST and GoTrue for the same reason, and a CI that tested a version no developer could run would be proving something about software nobody uses.

Revisit when nodes have AVX2, at which point the pin can move and the constraint can be deleted rather than worked around.

## ADR-034 — Realtime is one instance per project, superseding ADR-031's topology

Status: Accepted

Decided 2026-08-16, Phase 06 slice 4. **Supersedes the topology half of ADR-031.** ADR-031's security analysis stands entirely and is not reopened: the `REPLICATION` attribute still reads the whole cluster through physical replication, the `pg_hba.conf` reject is still the containment, and it is still a node prerequisite. What changes is the conclusion drawn about how many Realtime servers a node runs.

ADR-031 chose one shared Realtime per node, reasoning that upstream is itself multi-tenant and that `AGENTS.md` prefers upstream's arrangement. The first premise is true and does not help. **Upstream is multi-tenant across clusters, not across databases within a cluster.** On Supabase every project is its own database server, so a fixed replication slot name is unambiguous. ADR-002 puts many tenants in one cluster, and PostgreSQL replication slot names are cluster-unique.

Measured, with two tenants on one shared server (`specs/realtime-server-model.md`):

```
ReplicationSlotBeingUsed: replication slot "supabase_realtime_replication_slot_"
is active for PID 1222378
```

The slot name derives from `SLOT_NAME_SUFFIX`, a **server-level environment variable**, so one server serves exactly one tenant per cluster for Postgres Changes. The failure is the shape this phase keeps having to design against: the client reports `SUBSCRIBED` and then receives nothing, while the server retries in a loop. Indistinguishable, from the application's side, from a table nobody is writing to.

Therefore: **one Realtime instance per project**, each with its own `SLOT_NAME_SUFFIX`, HTTP port and gen_rpc port. Verified end to end — two instances, two tenants, each client receiving its own events and only its own. This returns Realtime to ADR-007's per-project worker model, which ADR-027's systemd machinery already covers, so it is a topology the platform can already supervise.

The costs are real and are the reason this is recorded rather than assumed:

- **~146 MB per instance** (cgroup accounting; ~235 MB RSS including shared pages), against the 31.8 MB ADR-022 measured for an entire warm project. Realtime is roughly 4.5× everything else a project costs, and is by a distance the most expensive capability a project can enable. This is ADR-022's missing Realtime density term, outstanding since slice 0.
- **Two replication slots per tenant**, not one: `supabase_realtime_replication_slot_<suffix>` on wal2json for Postgres Changes, and `supabase_realtime_messages_replication_slot_<suffix>` on pgoutput for broadcast. At PostgreSQL's default `max_replication_slots = 10` with two held back for the platform, that is **4 Realtime projects per node**.
- Both are created and owned by the server. The platform must not create a slot of its own — Phase 06 slice 2 did, and it was observed filling a cluster's slot budget with slots nothing ever read.

The blast-radius argument ADR-031 weighed now falls the other way for free: a per-project server holds one tenant's credential rather than N, which is the outcome ADR-031 said it would prefer if per-project ever turned out to be affordable. It is not cheap, but it is the only arrangement that works.

Revisit only if upstream makes the slot name a per-tenant setting. Forking to add that was considered and rejected: `AGENTS.md` prefers upstream behaviour, and an Elixir fork of the component that reads every tenant's WAL is not a maintenance commitment worth making for density.

## ADR-035 — A Realtime container reaches PostgreSQL and nothing else on its node

Status: Accepted

Decided 2026-08-16, Phase 06 slice 5, from a measurement taken while building the per-project workers ADR-034 called for. It constrains the runtime ADR-033 introduced.

A Realtime instance is a container, and rootless Podman offers exactly two ways for one to reach a service on its host. Turning on `slirp4netns`'s `allow_host_loopback` was the obvious way to let it reach the node's PostgreSQL, and it was measured to do considerably more than that: from inside the container, `127.0.0.1:5432` answered — **a different cluster, carrying tenants this project has nothing to do with** — and by the same route so would every loopback-bound worker on the node. A tenant's PostgREST serves anonymous requests through `db-anon-role` to anything that can open its port, so a compromised Realtime container would hold the anon-visible data of every project on the node, past the gateway and past ADR-028's keys entirely. That is the cross-tenant failure `AGENTS.md` puts first in its review rules, reached without a single privilege escalation.

Measured containment: with the flag **off**, the container reaches no loopback service on the node (`curl` exit 7) and still reaches a non-loopback address on it (exit 0). Therefore:

- **The node has a Realtime data address** — a private address on an interface of its own, which PostgreSQL also listens on. `MALUDB_REALTIME_DB_HOST` names it, a worker refuses to start without one, and `render_env` refuses a loopback value rather than substituting something that cannot work.
- **ADR-031's `pg_hba.conf` reject must name that address too.** The containment is per-address; opening a second address without a reject re-opens exactly the physical-replication hole ADR-031 closed. `scripts/realtime-test-cluster.sh` builds both, and `cp-manage node realtime-check` reports a permissive rule on any address.
- **Capabilities are dropped except `SETUID` and `SETGID`.** The image's entrypoint runs its migration step as `nobody` through sudo, so a bare `--cap-drop ALL` fails at `setresuid` reporting `no valid sudoers sources found`, which reads like a broken image rather than a capability the platform removed. What contains this process is the user namespace and the network namespace, not the capability set; `no-new-privileges` is left off for the same reason.

The metadata database is **per project**, and that is part of the same containment rather than tidiness. `CLUSTER_STRATEGIES` defaults to `POSTGRES` in a production release and discovers peers *through the metadata database*, so instances sharing one would form a single distributed Erlang cluster spanning every tenant on the node — gen_rpc between the processes that read each tenant's WAL. Each instance gets its own database, its own login role, and `CLUSTER_STRATEGIES=NONE` besides. The cost is one small platform-owned database per Realtime project, against a slot ceiling of four such projects per node.

Revisit if a node ever needs Realtime to reach something other than PostgreSQL, which would be a decision in itself.

## ADR-036 — The gateway translates the project key inside a channel frame, and a slept instance holds the socket

Status: Accepted — **the 1013 half was superseded on 2026-08-16; see the amendment below**

Decided 2026-08-16, Phase 06 slice 5. Both halves were found by the compatibility suite driving `@supabase/supabase-js`, and by nothing else — which is the argument `AGENTS.md`'s compatibility rule makes for testing with the official client rather than a reimplementation.

**The key inside the frame.** The client sends its key twice: in the query string, which the gateway already replaces with a minted JWT, and again as `access_token` in the payload of every `phx_join`. On Supabase both are the same value and both are a JWT — the anon key *is* a signed token there. ADR-028 made MaluDB's keys opaque, so the copy inside the frame reaches upstream unmodified and the server answers `MalformedJWT: The token provided is not a valid JWT`. The socket connects, every channel fails, and the client reports a channel error that says nothing about keys.

So the gateway parses that one frame and replaces that one field. It is a real departure from slice 3's rule that frames are forwarded verbatim, and it is kept as narrow as the defect requires: only frames that parse as JSON are touched, only the exact string the caller authenticated with is replaced — an end-user JWT passes through, which matters because rewriting one would turn every RLS policy reading `auth.uid()` into one that matches nothing — and both Phoenix serialisers are handled, since the official client defaults to the array form an object-only implementation would silently miss. A fresh token is minted per frame, which also fixes an expiry a long-lived socket would otherwise outlive.

**1013 rather than a held connection.** Waking a Realtime instance costs about nine seconds — a BEAM boot and a migration run, against PostgREST's 320 ms — and the official client abandons a connection after ten. Holding the socket therefore fails the same connection anyway, ten seconds later, having occupied the gateway in the meantime. So a project whose instance is asleep is closed with 1013 ("try again later") and the wake starts in the background, deduplicated per project. phoenix.js reconnects on a close with its own backoff, so the customer's next attempt lands on a ready instance; verified end to end with the official client.

The cost is that a customer's first connection after an idle hour fails once before succeeding. That is the honest shape of the trade, and it is the reason `maintenance.REALTIME_IDLE_MINUTES` is an hour rather than the fifteen minutes the other workers use. Revisit if instances ever become cheap enough to keep warm, at which point neither half of this is needed for Realtime — though the key translation would still be needed for any surface where a client puts a key inside a message.

### Amendment, 2026-08-16 — the socket is held, because "the client reconnects" is not a property the platform owns

The paragraph above rests on one assumption: that the official client keeps retrying for at least as long as a wake takes. It does not, and how long it tries depends on the runtime it is running in rather than on anything about MaluDB.

Measured against `supabase/realtime` v2.110.0 with a 9.7s wake, `@supabase/supabase-js` 2.112.3, the same gateway and the same project:

| Client runtime | Socket attempts | Outcome |
|---|---|---|
| Node 24.18 | 4 (`STOPPED`×3, `STARTING`×1) | connects on the fourth, receives changes |
| Node 22.23 | 2 | gives up, reports `CHANNEL_ERROR: transport failure` |

Node 22 is a supported runtime and the one CI pins, so this was not an exotic configuration — it was the majority case failing while the development host happened to pass. A customer on it, whose project had been idle an hour, would get a dead socket and an error naming nothing.

**So the gateway accepts the socket and holds it while the instance boots**, rather than closing 1013 and depending on a reconnect it cannot require. The client's `phx_join` waits in the receive queue and is delivered to upstream once it answers; nothing is read from the socket in the meantime, so the gateway never answers a frame on the instance's behalf. `WAKE_HOLD_SECONDS` bounds the wait at 45 seconds, after which the connection is closed 1013 as before — a client that does retry gets another chance, and one that does not has at least been told rather than left holding a silent socket.

Two things this trades away, both deliberately. A held socket occupies a connection from the project's `realtime_connections` entitlement while it waits, which is correct — it is a connection the project is using — and it is counted before the wait and released in a `finally`. And the subprotocol must be echoed before upstream has negotiated one, so the gateway answers the client's own first preference and logs a warning if upstream later disagrees; the official client requests none, which is why this is a warning rather than a design problem.

What generalises past Realtime: a platform behaviour whose correctness depends on a client library's retry policy is not a behaviour the platform controls. The original decision was verified end to end with the official client and was still wrong, because it was verified on one runtime — "tested with the official client" and "tested with the official client everywhere it runs" are different claims, and only the second one supports a compatibility guarantee.

## ADR-037 — The control plane serves two applications, and a route is internal until it is mounted publicly

Status: Accepted — drafted and ratified 2026-08-16 by the repository owner, before Phase 07 slice 0 builds it. Answers the first half of "Public API surface and self-serve signup" in `docs/OPEN-QUESTIONS.md`.

`docs/SECURITY.md` requires that node administrative interfaces and worker ports not be internet reachable, but nothing has ever said which *control-plane* routes are which. `specs/control-plane-api.yaml` places every path under `/v1` behind identical security schemes, so the document that describes the API cannot distinguish a signup form from an operator action.

**The reason to decide now rather than during Phase 07 is that the boundary is currently trivial and is about to stop being.** The control plane's whole HTTP surface is six routers, and everything genuinely privileged — provisioning runs, node registration, `realtime-check`, placement, plan application, Realtime enable/disable — is `cp-manage`, a CLI with no route at all. Phase 07 is the phase that changes that: its acceptance criterion is that the dashboard uses control-plane APIs rather than direct privileged database operations, which means project creation, API-key management and usage all arrive as HTTP. Classifying six routers today is a decision; classifying twenty-odd routes after a browser is already calling them is an audit.

**Decision: two ASGI applications, built from the same routers, bound to separate listeners.** `create_public_app()` mounts only the routers named in an explicit `PUBLIC_ROUTERS` list; `create_internal_app()` mounts everything. The default is therefore internal — a router added without a decision is unreachable from the internet rather than reachable by omission — and a test asserts the public application's route set matches the classification exactly, so adding a router without classifying it fails the suite rather than shipping.

| Router | Classification | Why |
|---|---|---|
| `auth` — signup, signin, me, signout, personal access tokens | **public** | Signup must be reachable, and every other route here is a platform user acting on their own account |
| `organizations` — list, members, invitations, role changes | **public** | The dashboard's core; authorised per caller, scoped to organisations they belong to |
| `projects` — list, get | **public** | The dashboard's core. Already omits `node_id` and `database_name` |
| `plans` — list | **public**, authenticated | What the dashboard's upgrade flow reads. Not anonymous — see below |
| `health` — `/healthz`, `/readyz` | **both** | Each listener needs its own liveness answer, and neither returns data |
| `hooks` — `/internal/hooks/email/{ref}` | **internal** | Called by a project's Auth worker on a node, never by a customer. Already `include_in_schema=False` |

Phase 07's new routes classify by the same rule: project creation, API-key management, usage and ownership transfer are public; anything that provisions, places, or administers a node is internal, and stays CLI unless there is a reason it cannot be.

**Reachable and unauthenticated are different axes, and conflating them is how an entitlement catalogue becomes a public one.** This ADR classifies *reachability*. Whether a reachable route also drops its authentication is a second decision per route, and today exactly one is anonymous: `POST /v1/auth/signup`. `/v1/plans` was the case that made the distinction worth writing down. It reads as "the pricing endpoint", and it is not: it requires a principal, it carries no price field — nothing in the schema does, until Phase 09 — and what it returns is `plans.config_json`'s `limits` verbatim, which includes `work_mem_mb`, `temp_file_limit_mb`, `postgrest_pool_size` and the statement, lock and idle-transaction timeouts. Published anonymously that tells anyone precisely where every threshold sits, which is a gift to whoever is designing a workload to sit just beneath them, and it commits the platform in public to numbers `specs/plans-and-limits.yaml` calls starting values rather than approved pricing. So `/v1/plans` stays authenticated and serves the upgrade flow; a public pricing view is a curated projection with prices in it, and belongs with Phase 09 rather than being this endpoint with its authentication removed.

**Network position is defence in depth, not the control.** Every route keeps the authentication it has, and the email hook already states the principle for its own case: the signature is what authenticates the caller, and being unreachable from the internet is a second line rather than the first. A split that let a route relax its own checks because "it is internal" would be worse than no split — it would concentrate trust in a network boundary that a single misconfigured listener removes.

Two alternatives were considered and both fail in the same direction. A **gateway-level path allowlist** puts the decision in a list that must enumerate everything to be excluded: a typo, a new prefix, or a forgotten route fails *open*, and the thing that decides what the internet can reach is exactly where a failure must not do that. A **public backend-for-frontend** proxying a narrow subset is a second place to implement authentication and re-derives the same classification anyway, for one more deployable and one more hop.

Consequences. Deployment grows a second listener, which `docs/CONTROL-PLANE.md` and `deploy/` must describe; in development one process can serve both on different ports. The OpenAPI contract becomes the *public* document — the customer-facing API is what compatibility and drift checks are for — with the internal document generated beside it rather than published. And `specs/control-plane-api.yaml` needs its security schemes split to match, since a single scheme across both surfaces is the thing this ADR exists to stop.

## ADR-038 — Provisioning runs in a worker, and the internet-facing application never holds node admin credentials

Status: Accepted

Decided 2026-08-16 by the repository owner, opening Phase 07. It follows ADR-037's split and is the reason that split is worth more than route hygiene.

Phase 07 makes project creation a customer action, and provisioning a project means creating databases and roles on a node — which needs that node's superuser DSN. The control plane already stores one per node, encrypted on the `nodes` row and unwrapped by `nodes.admin_dsn()` with the KEK, and **the control-plane process already holds the KEK** because it needs it for project credentials. Nothing calls `admin_dsn` from a route today, so the reach is currently theoretical; a phase that adds both routes and an internet-facing listener is exactly what turns it into a real one. `docs/ARCHITECTURE.md` already says the equivalent of the gateway — "do not place database superuser credentials in the gateway" — and the public control-plane application deserves the same sentence.

**So creating a project is enqueued, not executed.** The public application allocates the reference, reserves placement under the node row lock (`nodes.reserve_placement`, which Phase 06 already made refuse a node out of Realtime slots), writes the row and records the request. A separate **provisioner worker** holds the node admin credentials and runs `jobs.provision`. The public application therefore has no code path to a node's superuser, which slice 0 asserts with a test rather than leaves to review.

The queue is not merely a way to move the credential. `provisioning_jobs` already records attempts and error codes, and `jobs.provision` is already resumable — Phase 02 built both, and `cp-manage project retry` already drives them. A synchronous HTTP call would have to invent a worse version of each, and would fail a customer's request for a node that is briefly unreachable rather than retrying it. What the customer gets instead is a project whose status they poll, which is what every comparable platform does and what the dashboard needs anyway.

**What is actually enforced, as opposed to intended.** A Phase 07 security review made the distinction and it belongs here rather than in a review comment nobody will find. The public application still *loads the KEK* — `api_keys.reveal_publishable` needs it to hand back a publishable key — and it imports `services.control_plane.nodes` for `reserve_placement`, which is the module `admin_dsn` lives in. So the property a test can assert is "no module reachable from a public router calls `admin_dsn` or `provision`", which is narrower than "the internet-facing process cannot obtain a node DSN". A bug that reached `admin_dsn` through reflection, or a future route that called it deliberately, would be caught by the test; a memory-disclosure bug in the public process would not be stopped by any of this. Closing that gap properly means the public application not holding the KEK at all, which in turn means publishable keys being served from somewhere else — a real change, not a comment, and not one this ADR makes. Recorded so that nobody reads the paragraph above as a stronger guarantee than the code provides.

Consequences. There is a new process to deploy and supervise, in the shape ADR-027 already uses. Creation answers with a project in a pending state rather than a finished one, so the API is asynchronous where a naive dashboard might expect otherwise — the status endpoint reads `provisioning_jobs` rather than inventing a second source of truth. And the boundary needs enforcing rather than documenting: the public application must not import a path to `nodes.admin_dsn`, and a test that fails when someone wires one in is the only version of this that survives contact with a future slice.

## ADR-039 — Platform-mediated SQL is available to every tier; the paid line is credentials and a reachable port

Status: Accepted — drafted and ratified 2026-08-17 by the repository owner, before Phase 08 builds against it. ADR-037 and ADR-038 were handled the same way, because a decision left Proposed while its implementation is written makes the implementation the decision.

Clarifies ADR-005 rather than superseding it. Does not answer "direct DB endpoint architecture for paid users?" in `docs/OPEN-QUESTIONS.md`, which stays open and stays Phase 09.

**A free project cannot create a table.** Measured 2026-08-16, closing Phase 07. The control plane's routers are health, auth, organizations, plans, projects, api_keys, usage, audit and hooks — there is no SQL or DDL route anywhere. Free resolves `direct_database_access: false`, which leaves `mldb_<ref>_admin` `NOLOGIN` with a stored password it can never use. PostgREST performs no DDL; it exposes objects that already exist. So the free tier ships a database its owner has no way to put a schema into, and `docs/ROADMAP.md` has said "SQL/schema tooling as allowed" since Phase 7 without anything implementing it.

**ADR-005's text and the AGENTS.md invariant do not say the same thing.** ADR-005 says free projects "do not receive public direct PostgreSQL **connection credentials**", and gives the reason: it prevents bypass of API-layer rate, concurrency and quota controls. `AGENTS.md` paraphrases that as "Free projects are API-only. Direct PostgreSQL access is a paid capability", which is broader — it reads as a rule about SQL rather than about credentials. The gap between those two sentences is where this decision sits, and it is a documentation inconsistency rather than a reversal.

Decision: **the platform executes SQL on a project's behalf for every tier, and continues to withhold connection credentials and a reachable port from free.** The capability is gated by a new `sql_console` entitlement, separate from `direct_database_access`, so the two can move independently and Phase 09 does not have to untangle them. Free receives the console with the numbers `specs/plans-and-limits.yaml` already carries for it; paid receives looser numbers and, when Phase 09 answers the endpoint question, the port as well.

**Mediated execution is more governable than the direct access already sold, not less.** This is the load-bearing argument and it is ADR-017's finding read forward. Five of the six per-statement controls — `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `work_mem`, `max_parallel_workers_per_gather` — are `context = user`, and a tenant with direct SQL ran `SET statement_timeout = 0` successfully during that verification. Only `temp_file_limit` and the `CONNECTION LIMIT` attribute genuinely bind. A direct connection therefore has no effective per-statement ceiling, which ADR-017 states outright. A mediated endpoint does, because the platform holds the connection and can cancel from a second one regardless of what the submitted SQL sets. The intuition that free plus SQL equals quota bypass is backwards: this is the tightest SQL surface the platform can offer, and the one it is already committed to offering is the loose one.

**It runs as the tenant admin role, entered by `SET ROLE`, from an executor role that owns nothing.** `SET ROLE` into a `NOLOGIN` role works — it is how a tenant authenticator reaches `authenticated` today — so free needs no `LOGIN` grant and ADR-005's credential rule stays literally intact. The connecting role is a new per-project login role whose only membership is `mldb_<ref>_admin`, owning no object and holding no privilege of its own, carrying a small `CONNECTION LIMIT` — one of the only two controls ADR-017 found enforcing — with its credential stored ADR-023 Class B, envelope encrypted with row binding, because the platform must use it repeatedly.

Two precisions, because the obvious summary of that paragraph is wrong in a way a security review would have to rediscover. Customer SQL is arbitrary text and may contain `RESET ROLE;`, which returns the session to the connecting role — and since that role is a member of `mldb_<ref>_admin`, it can simply `SET ROLE` back. **A reset is therefore not contained, and is not meant to be:** the tenant admin role is the customer's intended ceiling in their own database, and they reach it either way. What the executor role buys is not containment below that ceiling but three other things — the stored credential is not the admin role's password, connections are accounted and capped separately, and free never receives a usable login. The invariant worth testing is that the executor role is never *more* privileged than `mldb_<ref>_admin` and is a member of nothing else.

And ADR-016's load-bearing exception applies directly: role membership is cluster-global, so grants involving the shared `anon`/`authenticated`/`service_role` names are one-directional. Those may be granted **to** the executor role, which is what impersonation needs; the executor role must never be granted **to** them, which would make every tenant's `authenticated` a member of it.

Note that ADR-017's *first* finding applies as well: settings on the connecting role do not survive into the `SET ROLE`, exactly as they fail to for PostgREST's authenticator. The session must set them explicitly, and they remain defaults rather than enforcement, which is why the out-of-band cancel is the control and the GUCs are courtesy.

**Alternatives considered.**

*Deploy Adminer or pgAdmin.* Rejected. Adminer's login form takes a server address; arbitrary-host connect is its feature, and on a shared cluster that is a pivot to the control-plane database, another tenant, or anything else the host can route to — a shape it has CVE history for. Neutering it means pinning the server and injecting credentials, which means owning a fork. It authenticates by PostgreSQL credential, while platform identity is control-plane accounts plus organization membership (ADR-020, ADR-021), so the session mapping is the same work done indirectly. It adds PHP to a stack ADR-024 fixed as Python 3.12, on a host that reaches tenant databases, and offers no hook for row caps, statement cancellation, audit, or the connection accounting ADR-022 makes load-bearing. A customer running Adminer themselves against their own paid endpoint is unaffected by this and remains supported.

*Paid only.* Rejected. It leaves the free tier unable to create a table, which is not a reduced tier but a broken one. It also diverges from the compatibility target in the direction that costs most: Supabase ships a SQL editor in Studio — executing through `postgres-meta`, one of its core open-source services, described as "a RESTful API for managing your Postgres. Fetch tables, add roles, and run queries" — and additionally gives free projects pooled connection strings through Supavisor. Supabase's paid line is IPv4 reachability and dedicated pooling, not SQL. A migrating customer arriving on MaluDB's free tier would find they cannot do in the dashboard what they did on the tier they left.

*Give free projects direct credentials instead.* Rejected on ADR-017: it is the surface with no per-statement ceiling, and it would make free the tier hardest to defend.

**What this contradicts in earlier phases, found by audit rather than in review.**

*`docs/RESOURCE-GOVERNANCE.md`, "Free-tier principles" (Phase 05).* Two of its four accepted bullets stop being true: "API-only external access", and "Direct SQL cannot bypass gateway controls **because it is not exposed**". The second is the more important one, because it is a *reason* rather than a rule — free tier was safe because there was no path, and after this ADR the reason becomes that the mediated path enforces its own limits. A document still giving the old reason would justify a future relaxation that the old reason no longer supports.

*Storage restriction is not enforced against the role this console runs as.* `services/control_plane/storage.py` sets `RESTRICTED_ROLES = ("anon", "authenticated")` and revokes `INSERT`/`UPDATE` from those two, leaving reads, deletes and truncates so a customer can shrink their way out. The tenant admin role is deliberately not restricted. Today that is sound: paid direct SQL bypasses it and `docs/RESOURCE-GOVERNANCE.md` says so plainly — "Because direct DB access exists on paid tiers, paid storage enforcement cannot rely only on the API gateway" — while free cannot reach the admin role at all, so for free the gateway *is* sufficient. **This ADR removes that asymmetry**, and a restricted free project could otherwise write its way back over quota through the console. Free is the tier where the quota carries the economics, since ADR-022 bounds free density by the 24 MB disk floor.

The fix belongs in Phase 08 slice 1 and is a control-plane check: the console refuses write statements while `projects.storage_restricted_at IS NOT NULL`. Extending `RESTRICTED_ROLES` to the admin role is the more thorough alternative — it would close the acknowledged paid hole as well — but it changes Phase 05 behaviour for existing paid projects, so it is a separate decision rather than a side effect of this one.

**Consequences.**

- `AGENTS.md`'s "Free projects are API-only" bullet must be rewritten to state the credential-and-port line, or the invariant list contradicts this file. ADR-005 gains a pointer here and keeps its text. `docs/RESOURCE-GOVERNANCE.md`'s free-tier principles are rewritten per the audit above.
- The public application gains reach to per-project executor credentials, which widens what ADR-038 narrowed. The widening is bounded and worth stating rather than discovering: an executor credential compromises one tenant at that tenant's admin level, where `nodes.admin_dsn` compromises every tenant on the node. ADR-038's import-graph test is unaffected and still applies.
- Rate limiting reuses `ratelimit.LocalLimiter`, so ADR-030's consequence applies unchanged and must be restated for this limit: with N control-plane processes the effective limit is N times the configured one.
- Console statements are not gateway requests, so they do not count against `api_requests_per_window` and do not appear in what `/v1/projects/{ref}/usage` reports. Either the console's own counters are surfaced there or the usage view under-reports a real data path — a Phase 05 telemetry gap this ADR creates.
- A customer may create a `SECURITY DEFINER` function owned by their admin role and grant it to `anon`, giving anonymous callers admin-level reach **inside their own database**. That is their prerogative and matches Supabase; it is not a cross-tenant escalation, and it is already reachable through paid direct SQL. Recorded so a review does not report it as new.
- `specs/plans-and-limits.yaml` and `entitlements.DEFAULTS` gain `sql_console`, `sql_console_row_limit` and `sql_console_concurrent`. Per `AGENTS.md` the numbers stay in configuration; the code reads them.
- A new per-project credential exists, so provisioning creates one more role and `tests/test_direct_sql.py`'s negatives extend to it. Existing projects need it backfilled, which is a migration plus a `cp-manage` path, not a re-provision.
- The execution route is public under ADR-037 — the dashboard calls it — and must never reach `nodes.admin_dsn`. ADR-038's import-graph test already fails if someone wires that in.
- Every statement is an audit event. The audit router exists and is allowlisted event by event, so this is a new event type rather than new machinery.
- Nothing new is needed for PostgREST to notice customer DDL. Bootstrap `006_schema_reload.sql` already installs `ddl_command_end` and `sql_drop` event triggers issuing `NOTIFY pgrst, 'reload schema'`, and its comment names "the dashboard SQL editor" as a motivating case — written during Phase 00 against exactly this eventuality.
- `specs/compatibility-matrix.yaml` is unaffected. It tracks the tenant client-library surface, and this is a platform feature. The free-tier divergence from Supabase belongs here, in this ADR, and is recorded above.

## ADR-040 — Storage restriction applies to the project's admin role, and is a default rather than enforcement

Status: Accepted — decided 2026-08-17 by the repository owner, during Phase 08 slice 1. Amends the enforcement surface of `docs/RESOURCE-GOVERNANCE.md`'s storage quotas; supersedes nothing.

Phase 05 revokes `INSERT` and `UPDATE` from `anon` and `authenticated` when a project passes its storage quota, leaving reads, deletes and truncates so a customer can shrink out. The project's own admin role was deliberately not included, and that was sound while it was true that free could not reach it: paid direct SQL bypassed the restriction, `docs/RESOURCE-GOVERNANCE.md` said so plainly, and for free the gateway was the only door.

ADR-039 opened a second door on every tier. Decision: **the restriction extends to `mldb_<ref>_admin`**, so one mechanism covers the API, the console and paid direct SQL, and no path needs a special case.

**It is a default, not enforcement, and the measurement is the point.** A role that owns a table holds `GRANT OPTION` on it implicitly. Probed 2026-08-17: after `REVOKE INSERT, UPDATE`, the owner ran `GRANT INSERT ON t TO current_user` and wrote on the next statement. Customer tables are owned by this role by design — `specs/tenant-role-model.md` grants it `CREATE` on `public` plus default privileges precisely so a customer's own tables work with `anon` and `authenticated` — so this applies to exactly the tables that matter. `tests/test_sql_console.py` asserts the re-grant, so the limitation is a fact the suite holds rather than a caveat in a comment; a change that made the restriction genuinely binding should fail that test and rewrite this paragraph, not delete it.

This is ADR-017's category exactly, one layer up: a control that binds a well-behaved client and not a determined one. It is still worth applying. It stops every accidental write and every ORM; it makes the deliberate escape an auditable act rather than the default state; and the maintenance pass re-measures and re-applies, so a customer who re-grants is in a loop rather than through a door. ADR-009's layering is the answer to no single layer being sufficient, and this is one of the layers.

**What this replaced.** Phase 08 slice 1 first held a restricted project by putting the console's session in a read-only transaction. A probe on the same day showed the submitted text escapes it: `SET default_transaction_read_only = off` is accepted inside a read-only session and the next statement writes. `default_transaction_read_only` is `USERSET` and PostgreSQL offers no way to withhold it, so that approach could not be repaired — the code and the comment claiming otherwise were both removed. Recorded because it is the second time in two slices that a session-level GUC was mistaken for a control, and the first time was ADR-017.

Consequences.

- `DELETE` and `TRUNCATE` still work for a restricted project, which is what makes the state recoverable rather than terminal. This is more usable than Supabase, whose read-only mode blocks deletes until the customer explicitly disables it.
- A restricted project's console statement now fails with `42501 permission denied` from the customer's own table. `ExecutionOut.storage_restricted` reports the state so a dashboard can explain that error instead of leaving a customer to guess.
- `storage.release` restores the grant to the admin role along with the other two, so returning below quota needs no separate repair.
- Genuine enforcement would mean the customer not owning their own tables, which contradicts `specs/tenant-role-model.md`, or the console refusing to run their text at all, which removes the only schema surface free has. Neither is worth the trade for a quota whose real backstop is node capacity management.

## ADR-041 — Storage restriction covers `service_role`, because the role named in a request cannot be a control

Status: Accepted — decided 2026-08-17 by the repository owner, during Phase 08 slice 3. Amends ADR-040 and `docs/RESOURCE-GOVERNANCE.md`; supersedes the `service_role` exemption recorded in `services/control_plane/storage.py` since Phase 05.

Phase 05 revoked `INSERT` and `UPDATE` from `anon` and `authenticated` when a project passes its quota, and ADR-040 added the project's admin role. `service_role` was deliberately left out, on a stated premise: it "is reachable only from the project's own backend", and that backend's route to it is the gateway, which already refuses writes at quota. The exemption existed so a customer's cleanup job — the most likely user of that role — kept working.

**Slice 3 falsified the premise.** Impersonation lets a customer ask the console to run a statement as `anon`, `authenticated` or `service_role`, and the gateway never sees it.

The first fix refused the *request* to impersonate `service_role` while a project was restricted. That was wrong, and the security review before merge found it. **`SET ROLE` is authorized against the session user, not the current role.** The session user on an impersonating connection is `mldb_<ref>_authenticator`, which is a member of all three shared names, so a request that asks for `anon` reaches `service_role` in one statement of its own text — no `RESET ROLE`, no grant, nothing the platform can see coming. Measured 2026-08-17 and asserted in `tests/test_impersonation.py`.

Decision: **`storage.RESTRICTED_ROLES` becomes `("anon", "authenticated", "service_role")`**, and the request-level refusal is removed rather than kept as a second layer that would only teach the next reader that the role in a request means something.

Why this is the right direction and not merely the available one:

- It is ADR-040's own principle applied consistently: the restriction belongs in grants, where it binds whatever role the session ends up in, rather than in a control-plane check on a value the customer controls.
- The exemption's purpose survives. Restriction removes `INSERT` and `UPDATE` only, so `DELETE` and `TRUNCATE` — which is what a cleanup job needs, and the whole reason `service_role` was spared — still work. `service_role` was never able to write past the quota through the gateway anyway, so no working path is being taken away.
- The alternative, leaving the check, is a control whose bypass is one line of the customer's own SQL. A control that can be stepped over is worse than none, because it is believed.

Consequences.

- A restricted project's `service_role` loses `INSERT`/`UPDATE` on `public` for the first time. Nothing that previously worked stops working: gateway writes were already refused at quota, and this is the same set of privileges the other two roles have been losing since Phase 05.
- `storage.release` restores it symmetrically, matching bootstrap 004's `ALL` grant, so returning below quota needs no repair.
- ADR-040's admission stands unchanged and now covers this too: a table owner can grant `INSERT` back to itself, so this is a default rather than enforcement. `service_role` does not own the customer's tables and cannot do that; the admin role can, and always could.
- **The general rule, worth more than this instance: on the mediated SQL surface, the role named in the request selects a credential and nothing else.** It is not a permission boundary, because the submitted statement can move between any roles the connection's session user is a member of. `api/tenant_access.py` says so where the allowlist is defined.

**Three things the review of this fix turned up, recorded because two of them are older than this ADR.**

1. **`storage.evaluate` applied the revoke only when a project *changed* state**, so widening `RESTRICTED_ROLES` would never have reached a project already sitting in `restricted` — the fix would have been a no-op for exactly the population that needed it. Worse, ADR-040 accepts its own residual risk on the stated grounds that "the maintenance pass re-measures and re-applies, so a customer who re-grants is in a loop rather than through a door", and with the revoke inside the transition branch **that loop did not exist**: one re-grant held until the project dropped below quota. The revoke now runs on every pass where the state is `restricted`, while the audit event and the timestamp stay on the transition — which is what the idempotence was really protecting. `tests/test_storage.py::test_a_re_grant_is_taken_away_again_by_the_next_pass` asserts the loop, so ADR-040's mitigation is now a fact the suite holds rather than a sentence in a decision record. It also means no backfill is needed here, and none for the next change to the restricted set.

2. **The admin role can re-arm the impersonation path**, not only its own: it owns the customer's tables, so `GRANT INSERT ON t TO service_role` is available to it in one non-impersonating request, and unlike a self-grant that re-arm is durable across later impersonating requests. Same class as ADR-040's accepted residual, same auditable `GRANT`, same loop closing it on the next pass.

3. **`release` re-grants rather than restores.** `_apply(revoke=False)` issues a blanket `GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public` to every restricted role, so a customer who had deliberately revoked writes from one of those roles on one table gets them back when the project returns under quota. That behaviour predates this ADR and is materially worse for `anon` than for `service_role`; adding `service_role` to the set extends it rather than introducing it. Recorded as a known consequence rather than fixed here: doing it properly means recording the grants that were actually revoked and replaying exactly those, which is a Phase 05 change and should be decided as one for all four roles at once.

## ADR-042 — Migration is a CLI the customer runs, and the platform never holds their Supabase credentials

Status: Accepted — decided 2026-08-17 by the repository owner, answering the first `## Migration` open question before Phase 08 slice 5.

A migration has to read the *source* Supabase project: its catalogue, its rows, its auth users. That needs the customer's Supabase database connection string, and for the Auth half their service-role key. The question was which end of the wire runs the tool.

Decision: **a CLI the customer runs, distributed with the platform, driving the Phase 08 slice 1-3 API for the destination and the customer's own credentials for the source.** A dashboard-triggered migration is not ruled out; it is deferred until the custody question below has an answer.

The deciding argument is not developer experience, it is custody. A dashboard-driven scanner means the control plane accepts, stores and uses a third party's production credential on the customer's behalf — a secret class `docs/SECRETS.md` does not currently have, with a blast radius that is somebody else's platform and a revocation path we do not own. Run from the customer's machine, that credential never leaves it, and the platform's exposure to a compromised control plane does not grow by a single Supabase project.

Two supporting reasons, neither sufficient alone:

- ADR-025 puts the frontend in its own repository, so "dashboard first" means Phase 08 blocking on work that is not in this one.
- A migration is a long, restartable, output-heavy operation against two databases. That is a shape a terminal fits and a request/response API does not, and the alternative is inventing a job runner for it in the control plane.

Consequences.

- The scanner and the migrator are one binary with two subcommands, not a control-plane route. `docs/MIGRATION-FROM-SUPABASE.md` describes that flow.
- The destination side goes through `POST /v1/projects/{ref}/sql` and the introspection route, authenticated with the customer's own platform session or personal access token. **The CLI gets no privileged path**: it is a customer of the same API a dashboard would call, which is what keeps ADR-039's ceiling meaningful.
- The source side is read-only, which is already an acceptance criterion in `tasks/PHASE-08-SUPABASE-MIGRATION.md` ("source is not modified unexpectedly").
- A customer who wants migration driven from the dashboard needs the credential-custody decision first. Recorded in `docs/OPEN-QUESTIONS.md` rather than left implied.

## ADR-043 — Initial migration supports exactly what the compatibility matrix supports; everything else is a scanner blocker

Status: Accepted — decided 2026-08-17 by the repository owner, answering the second `## Migration` open question.

Decision: the first migration launch covers **the database (schema, data, sequences, constraints, indexes, views, functions, triggers, RLS policies, allowlisted extensions), email/password Auth users and identities, and Realtime Postgres Changes configuration.** Everything else is reported by the scanner as a blocker before cutover.

The scope is not a judgement about what customers want. It is `specs/compatibility-matrix.yaml` read back: those are the surfaces that carry a `supported` status earned by the official-client suite. `AGENTS.md` forbids claiming compatibility the tests do not support, and a migration that silently carried something the platform cannot serve would be exactly that claim, made in the one place a customer cannot check it — their own production cutover.

Blockers at launch, each with the reason it is one:

- **Storage buckets, objects and policies** — Phase 10. `deferred` in the matrix, no surface to migrate into. **Amended 2026-08-25, Phase 10 slice 6 (ADR-063): buckets and object bytes are no longer a blocker.** The matrix entry that made them one is now `supported`, and this list is read from the matrix rather than kept beside it. Policies remain undone, and for a reason this list would otherwise hide: there is nowhere to put them, because a customer cannot author a policy on `storage.objects` at all (ADR-061). The scanner reports them, and says which direction their absence fails in.
- **OAuth, magic link, MFA and enterprise SSO identities** — `deferred` in the matrix. A user row can be migrated; an identity that only a provider configuration can authenticate cannot, and migrating the row alone produces an account nobody can sign in to.
- **Realtime broadcast and presence** — `deferred`.
- **Edge Functions** — no equivalent surface exists in any phase yet.
- **Anything the extension allowlist does not carry** — ADR-045.

Consequences.

- The scanner's output has two severities and the distinction is load-bearing: a **blocker** means the migration will not complete correctly and must not be attempted; a **warning** means something a customer should know about and can proceed past.
- A project using a blocked feature is not "unmigratable" forever — it is unmigratable until the phase that builds the surface lands. The scanner names the phase, so the answer is a date rather than a refusal.
- Password hashes migrate where GoTrue's format allows it. Where it does not, the honest outcome is a password reset for those users, reported by the scanner in advance rather than discovered at cutover.
- This ADR is a snapshot of the matrix, not a copy of it. When a surface is promoted to `supported`, the migration scope grows with it and this ADR does not need amending — the matrix is the authority.

## ADR-044 — Cutover is a measured write freeze, and the window is published rather than promised away

Status: Accepted — decided 2026-08-17 by the repository owner, answering the third `## Migration` open question. Consistent with `docs/MIGRATION-FROM-SUPABASE.md`, which already staged zero-downtime as a later objective.

Decision: the initial migration requires a **controlled write freeze** on the source project during final sync and cutover, and Phase 08 **publishes an expected window as a function of data size**, measured by its own validation runs rather than estimated.

Zero-downtime migration means streaming changes from the source while it is live — logical replication out of Supabase, or a change-data-capture layer — plus a reconciliation step and a rollback story for a cutover that fails halfway. That is a phase of work, not a slice, and building it first would delay every customer who would happily take ten minutes of downtime on a Sunday.

The freeze is the honest mechanism. What makes it usable is the number: "expect some downtime" is not something a customer can schedule a maintenance window around, and a figure nobody measured would be worse than none. So the validation runs in slice 8 record the wall-clock time of each stage against the data volume, and the runbook carries the result.

Consequences.

- The runbook has an explicit freeze step, and the scanner's report includes the estimated window for *that* project's measured size — which is why "estimated data size" is already a scanner output in `docs/MIGRATION-FROM-SUPABASE.md`.
- **The platform cannot enforce the freeze**, and the runbook must say so plainly. The source is Supabase; stopping writes to it is the customer's action, in their own application. A migration where writes continued produces a destination that is quietly missing rows, which is the worst failure this phase can have — so the validation step compares row counts and the report names any table that moved.
- Zero-downtime stays an objective and is not claimed anywhere until it is implemented and tested, which `docs/MIGRATION-FROM-SUPABASE.md` already requires.

## ADR-045 — A customer may install an allowlisted extension themselves

Status: Accepted — decided 2026-08-17 by the repository owner, answering the fourth `## Migration` open question. Clarifies ADR-010; supersedes nothing.

Today no customer on any tier can install any extension: negative test H asserts `permission denied` for `CREATE EXTENSION` as `mldb_<ref>_admin`. Supabase's free tier installs from a 60-plus allowlist through `supautils`, and a migrated schema routinely opens with `create extension if not exists "uuid-ossp"` — so a migration fails on its first statement.

**ADR-010's text does not require the stricter reading.** It says customers cannot install *arbitrary* extensions on a shared node, not that they can install none. The implementation is stricter than the decision, in the same shape as the ADR-005 finding: the code was written against a paraphrase.

Decision: **a customer may install an extension that is on the platform's allowlist, themselves, without an operator.** The mechanism is a `SECURITY DEFINER` installer owned by the platform role that checks `specs/extension-allowlist.yaml` and refuses anything else. Anything off the list stays a `permission denied`, which is ADR-010 unchanged.

Why self-service rather than an operator-applied install: the alternative makes every migration a support ticket. Phase 08 exists to produce a migration that completes unattended, and a step that stops for a human in the middle of a cutover — during the write freeze ADR-044 just committed to — is not a migration, it is a scheduled outage with a meeting in it.

**The security objection is answered by machinery that already exists**, which is what makes this cheap rather than brave. `bootstrap/005_extension_hardening_trigger.sql` fires on `CREATE EXTENSION`/`ALTER EXTENSION`, revokes the new functions from `anon`, and is deliberately not exception-handled, so a failed revoke aborts the install. That is the hard half of what `supautils` does, and it has been in every tenant since Phase 00. It is also the control that keeps ADR-018's finding fixed — `pgcrypto`'s `gen_salt` reachable by `anon` — for extensions nobody has reviewed yet.

The allowlist's admission criterion, which matters more than its current contents:

- **The extension must be one PostgreSQL itself marks `trusted`**, or it must carry a written per-extension review in `specs/extension-allowlist.yaml`. `trusted` is the upstream statement that an extension is safe for a non-superuser with `CREATE` on a database, which is a stronger claim than any list we would curate by hand.
- **It must not reach outside its own database.** Anything with a filesystem, network or cross-database path — `plpython3u`, `plperlu`, `dblink`, `postgres_fdw`, `file_fdw`, the `http` extensions, `adminpack` — is refused whatever its `trusted` flag says.
- **It must not read cluster-wide state.** `pg_stat_statements` is the example worth naming: its view is populated for the whole cluster, so on a shared node it is a window onto other tenants' activity. It also needs `shared_preload_libraries`, which is a node decision rather than a tenant one.
- **It must not need `shared_preload_libraries` or a background worker.** Those are cluster resources, and ADR-012 records that `maludb_core` deliberately needs neither.

Consequences.

- `specs/extension-allowlist.yaml` is the authority, and it is data rather than code, so adding one is a review and a merge rather than a release.
- The scanner reports an extension off the list as a blocker naming the extension, so a customer learns before cutover rather than during it (ADR-043).
- The installer is not built by slice 4. It is the first thing the schema-migration slice needs, and it lands there with its own negative tests — including that an extension off the list is still refused, which is negative test H generalised rather than replaced.
- Node capacity gains a variable: extensions are per-database and some are large. `docs/CAPACITY.md`'s per-project cost is measured with the provisioning set, and an allowlist that grows changes it.

**Amended 2026-08-18, during Phase 08 slice 6: the mechanism is not a `SECURITY DEFINER` installer.** Building one showed why it cannot be. An installer is a function a customer calls *instead of* writing `CREATE EXTENSION` — so a migrated schema's opening line still fails, and a migration could only work by rewriting the customer's own SQL before applying it. The motivation for this ADR was that literal line; a mechanism that does not make it run does not satisfy the ADR that demanded it.

What shipped instead puts the check where the DDL already is, in `bootstrap/010_extension_allowlist.sql`:

- **`GRANT CREATE ON DATABASE` to `mldb_<ref>_admin`.** PostgreSQL 13+ lets a non-superuser holding it install an extension marked `trusted`, and refuses an untrusted one itself — measured: `citext` installed, `postgres_fdw` answered "Must be superuser to create this extension". Criterion 1 already leans on `trusted` as a stronger claim than a hand-curated list, and this is that claim doing the coarse filtering. The grant also gives `CREATE SCHEMA`, which the migration slices need anyway: a migrated project brings schemas of its own.
- **An event trigger that narrows `trusted` to the allowlist**, aborting at `ddl_command_end` so a refused install rolls back — the same property bootstrap 005 already depends on. Necessary because `trusted` is set by whoever packaged the extension, so without it a node's package set would decide what tenants may install.
- **A per-tenant `maludb_platform.allowed_extensions` table**, synced from `specs/extension-allowlist.yaml` at provisioning and by `cp-manage extensions sync`. Baking the list into immutable bootstrap SQL would have frozen it at whatever each project was provisioned with, so what a customer may install would depend on the month they signed up. The sync **removes** as well as adds, because taking an extension off the list is how a security decision gets reversed.

Two things the implementation measured that the design would not have caught, recorded because both produced a control that looked like it was working:

- **`current_user` inside a `SECURITY DEFINER` function is the function's owner**, not the caller. The superuser exemption — which exists so provisioning can install `maludb_core` — was written against `current_user` and was therefore unconditionally true, so the trigger refused nothing. It is `session_user` now: the platform's superuser when provisioning installs, and `mldb_<ref>_executor` when a customer's console statement does.
- **`object_identity` from `pg_event_trigger_ddl_commands()` is quoted where the name needs it**, so `uuid-ossp` arrives as `"uuid-ossp"` and never matches the allowlist. That refused precisely the extension this ADR exists for. The trigger joins on `objid` to `pg_extension.extname` instead.

An extension that is allowlisted but *not* `trusted` — `vector` is the case — remains platform-installed only: provisioning puts it in every tenant, so `create extension if not exists vector` is a no-op that succeeds, but a customer cannot create it from nothing. The allowlist says what may exist in a tenant; `trusted` decides who may create it.

**The pre-merge security review of slice 6a found three things, all in the new mechanism rather than in the decision.** Recorded because two of them made a control that looked like it was working, which is the third time in this phase that has happened.

- **`CREATE EXTENSION x CASCADE` reports only *x* to an event trigger.** Its dependencies are installed without a firing of their own, so an allowlisted entry with an unlisted dependency would admit that dependency by the back door, with its install script running as the bootstrap superuser. Measured with a synthetic trusted pair. Not exploitable against the allowlist as it stands — no current entry declares `requires` — but the file is designed to grow. The trigger now walks the `pg_depend` closure, and `specs/extension-allowlist.yaml` gains **criterion 5**: an entry's `requires` closure must itself be listed, asserted against the node's own `pg_available_extensions`. PostgreSQL's `trusted` check does apply to cascaded dependencies, so the exposure was bounded to trusted-but-unlisted extensions.
- **ADR-018's hardening only ever looked at `public`.** That was sufficient while no tenant role could install anything and the platform put everything there. `GRANT CREATE ON DATABASE` ends both halves: measured, a customer created a schema, installed an allowlisted extension into it, granted `anon` `USAGE`, and every function of that extension became `anon`-executable with no revoke ever applied. `specs/tenant-role-model.md` lists that among the things the admin role must never be able to do, and the test asserting it only tried the direct grant on a function in `public` — so the invariant had become true by accident. Bootstrap 011 drops the schema filter and re-runs the revoke.
- **`pg_event_trigger.evtenabled` has four values.** `ENABLE REPLICA` fires only when `session_replication_role = 'replica'`, which is never for customer DDL, and `tenant_bootstrap.verify` accepted it because it compared against `'D'` alone. A tenant in that state installed a non-allowlisted extension while `verify` reported it healthy. All three trigger checks now require `'O'` or `'A'` and say the observed value. A customer cannot reach that state themselves; a fleet repair script or a mistyped `ALTER EVENT TRIGGER` can, which is what those checks exist for.

The review also caught `vector` recorded as `trusted: true` in the allowlist when the node reports otherwise — the one field a future reviewer would trust when applying criterion 1. Corrected; it carries the written `review:` that criterion 1 requires of an untrusted entry.

## ADR-046 — A row cap is not a memory cap: platform-mediated SQL carries a byte budget, and the residual is written down

**Status:** Accepted — 2026-08-19

**Context.** Phase 08's plan required a security review before merge on every slice. Slice 1 — the slice that introduced `POST /v1/projects/{ref}/sql`, and the one the plan named as "not mergeable on a green suite alone" — is the one slice of the phase with no review recorded, in its commit or its progress entry. The catch-up review run while closing the phase found this.

Every declared limit on the SQL console bounds a count or a duration. None bounds a size. Measured on 2026-08-19 against the free tier's own numbers:

- `SELECT repeat('x', 1000000) AS c FROM generate_series(1, 100)` returns **exactly** `sql_console_row_limit` rows, reports `truncated: false`, completes in 1.65s against an 8,000 ms ceiling, and produces a **100 MB response body** for **~200 MB** of control-plane resident memory. Nothing was exceeded; there was nothing to exceed.
- The same hole exists in the schema route, on a different axis. `introspection` caps each catalogue's rows — `CATALOG_ROW_CAP` is 5,000 — and a function body is customer-authored text of no fixed size. Five thousand rows is a row count that module considers reasonable and a response size it does not.

This is a *shared-process* limit rather than a tenant one. A tenant that exhausts its own node's memory has harmed itself; a tenant that exhausts the control plane's has taken the API away from every other tenant. Free is the exposed tier: two projects, one concurrent statement each, and signup is open.

**Decision.** A plan grants `sql_console_max_bytes` alongside its row limit, and the fetch spends it.

- **Per response, not per result set.** A statement returning ten result sets is ten times the cost of one, and a per-set budget would not notice. The schema route spends one budget across all eight catalogues for the same reason: they are returned as one document.
- **A value that does not fit is dropped, not cut.** Half a returned string is a corruption dressed as a limit — a client cannot tell it from a short value. The row it belonged to is left out and `truncated` says so.
- **One `truncated` for both caps.** They answer the same question — you are not seeing all of it — and a client rendering "showing the first N" does not act differently on which limit bit.
- **Zero falls back to the tier default**, like `sql_console_timeout_ms` and unlike `sql_console_row_limit`. A zero row limit returns nothing and harms whoever set it; a zero byte budget would take the ceiling off a shared process.
- **2 MiB free, 8 MiB starter, 32 MiB production**, in `specs/plans-and-limits.yaml` and overridable per plan without a deploy. Production's is the number to lower first if a control plane is sized smaller than its plan assumes: ten concurrent statements may each reach it.

**What this does not fix, measured rather than assumed.** libpq buffers an entire result set before the first row can be refused, so the transient cost is untouched: **202 MB peak with a 100 MiB budget, 203 MB with a 2 MiB one**. What the budget changes is what is still held once the tenant connection closes — **100.0 MB against 2.0 MB** of live Python objects — and that is the half whose duration a *caller* controls, because it is held while the response is serialised and read. A slow reader can no longer pin a hundred megabytes per request; a burst of requests can still spike the process by whatever their databases will emit inside the wall-clock cancel.

Streaming closes the other half — `Cursor.stream` over the same query costs **+5 MB against +101 MB** — and cannot be used on this route. It is the extended protocol, and this route takes multi-statement text: psycopg answers `cannot insert multiple commands into a prepared statement`. Streaming only single statements would be a control an attacker opts out of with a semicolon, and splitting submitted text into statements ourselves would need a SQL parser and would break the implicit-transaction semantics a multi-statement submission has today. Both were rejected for those reasons rather than for effort.

The residual is therefore real and recorded in `docs/OPEN-QUESTIONS.md`: closing it means single-row mode through libpq below psycopg's `stream`, or fetching out of process, or a per-request memory guard. Until then the process needs an operational memory limit, which is a deployment property this repository does not yet assert.

**Consequences.**

- `sql_console.execute` and `introspection.snapshot` both take `max_bytes` as a required keyword. Required rather than defaulted on purpose: a default is how the first version of this got written.
- `sql_console.Budget`, `approx_bytes` and `fetch_bounded` are shared by both callers, so a third one cannot acquire the row cap without the byte cap.
- `approx_bytes` measures text and bytes exactly, counts everything else flat, and stops walking nested containers at a fixed depth — a recursive estimate over customer-supplied jsonb is otherwise a `RecursionError` a tenant can post into the control plane.

## ADR-047 — Paid direct SQL connects as a role of its own, not as the tenant admin role

**Status:** Accepted — decided 2026-08-19 by the repository owner, before Phase 09 slice 2 wrote any code. Extends `specs/tenant-role-model.md`; narrows ADR-005 and ADR-039's account of what "direct database access" hands over.

**Context.** ADR-005 makes a direct PostgreSQL connection a paid capability and `specs/tenant-role-model.md` has named `mldb_<ref>_admin` as "the role for paid direct SQL" since Phase 02. Provisioning generates that role's password whether or not the plan entitles the project to use it, and `set_direct_sql_access` flipped its `LOGIN` attribute. Phase 09 slice 1's planning found the other half: **no route has ever returned that password to anybody.** Paid direct access was a delivery problem, and the delivery was about to be built.

Handing over the admin role's password would have worked, and three things follow from it that are not worth living with:

- **Rotation becomes a platform outage.** `mldb_<ref>_admin` is what ADR-039's mediated SQL enters by `SET ROLE`, what `plan_apply` reconciles, and what maintenance uses. Rotating a leaked customer credential means rotating the identity the platform acts under.
- **Revocation cannot be told from breakage.** Turning off a downgraded project's direct access and breaking its SQL console would be the same operation on the same role.
- **The identity the platform acts on a customer's behalf under ends up in their `.env`.** That is a different kind of secret from the one they were sold.

**Decision.** A paid project gets `mldb_<ref>_client`: a login role of its own, with its own stored credential, whose whole privilege is membership of `mldb_<ref>_admin`.

It is the executor role's shape with a different caller, and that precedent is deliberate — `create_executor_role` already records the reasoning this ADR generalises: "what this role buys is that the stored credential is not the admin role's password, that connections are capped separately, and that a free project never receives a role it can log in as."

- **`NOINHERIT`, member of `mldb_<ref>_admin` and of nothing else, owning nothing.**
- **`ALTER ROLE ... IN DATABASE ... SET role = mldb_<ref>_admin`**, so a session arrives already in the admin role.
- **`mldb_<ref>_admin` is now `NOLOGIN` on every tier, permanently.** Direct access is the client role's `LOGIN` attribute; the admin role's password is never issued. `plan_apply` reconciles both, so a project provisioned before this shows up in `cp-manage plans drift` as `excess` rather than staying quietly reachable.

**The `SET role` default is load-bearing, and was measured before being chosen.** Without it, a table a customer creates over their direct connection is owned by `mldb_<ref>_client` — and Phase 08 already found what that costs once: `ALTER DEFAULT PRIVILEGES` only affects objects created by the role it names, so objects created under the wrong identity are not reachable by the customer's own data API, and the SQL console (running as admin) could not alter them either. Measured 2026-08-19 on a throwaway cluster:

```
on login:          current_user = ..._admin,  session_user = ..._client
table owner:       ..._admin
after RESET ROLE:  current_user = ..._admin
```

So an object created over a direct connection is indistinguishable from one created through the console, which is the property that matters and the one a customer would never think to check.

`RESET ROLE` returning to the *admin* role rather than to the client role is a consequence of the same setting: `RESET` restores the session default, which is admin. That is not an escape. It is the ceiling ADR-039 already documents for the executor, reached by a different door, and the client role has no privilege of its own to fall back to anyway.

**Consequences.**

- The ceiling is unchanged. A leaked client credential is admin-equivalent *inside that tenant's database*, which is what direct SQL means; everything `tests/test_direct_sql.py` pins about `mldb_<ref>_admin` — no cross-tenant reach, no `CREATE EXTENSION` outside the allowlist, no `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS` or `REPLICATION` — bounds this role too, through the membership rather than through a second set of grants that could drift.
- What it buys is operational: rotate the customer's credential without touching the platform's, revoke direct access without breaking the console, and issue a secret that was minted to be given away.
- ADR-016 applies unchanged and in the permitted direction only: the client role must never be granted *to* a shared name.
- Existing projects need `cp-manage project backfill-client`, on the `backfill-executor` precedent, because a project provisioned before this has passed the point where its pipeline runs.

## ADR-048 — A subscription records what is paid for; it never writes an entitlement

**Status:** Accepted — decided 2026-08-19 by the repository owner, before Phase 09 slice 3 wrote any code. Narrows the blocking statement in `plans/completed/phase-09-billing.md` and adds `subscriptions` to the control-plane schema. Depends on ADR-020 for where billing attaches, and on ADR-006 for what an upgrade may not move.

**Context.** The platform has had exactly one fact about a project's plan since Phase 01: `projects.plan_id`. It is the **entitlement** — what `entitlements.for_project` resolves, what the gateway counts against, and, since slice 0, what `plan_apply` writes to a node. There has never been anywhere to record the other fact, which is whether anybody is paying for it.

That absence is visible in the shape of what shipped. Slice 1's `cp-manage project set-plan` moves a project to any plan in the catalogue and takes no money, and its own docstring says so; `upgrade_requests` (migration 0015) exists because a customer pressing a button had to land somewhere that was *not* `projects.plan_id`, and its comment gives the reason: "an upgrade that took effect here would grant paid entitlements to a project nobody has billed." Both are the same gap seen from opposite ends.

The tempting shape is a webhook handler that writes `projects.plan_id`. It is one function, it is obviously correct on the happy path, and every property that makes billing hard is a property it does not have: a provider's vocabulary reaches the node, a retry is a plan change, an out-of-order delivery is a downgrade, and swapping providers means rewriting the thing that grants entitlements.

**Decision.** Two facts, two tables, one direction of flow.

- **`subscriptions` records what has been paid for.** Org, project, plan code, state, and the moment that state became true. Nothing in `services/control_plane/subscriptions.py` writes `projects.plan_id`, opens a node connection, or decides what a plan grants.
- **`subscriptions.reconcile` is the only seam,** and it hands the entitled plan to `plan_change.change_plan` — the operation that already owns moving a project between plans and already asserts ADR-006's identity. Billing state proposes; slice 0's apply disposes.
- **The states are MaluDB's own**, not a provider's: `incomplete`, `trialing`, `active`, `past_due`, `canceled`. A provider's states are mapped onto them in slice 4.
- **No provider column, no provider id, no route.** The first `## Billing` open question is unanswered, and a nullable column guessing at its answer would be a guess that has to be right. `ALTER TABLE ... ADD COLUMN` in slice 4 costs nothing.

**A subscription belongs to an organization and covers one project.** `docs/ACCOUNTS.md` has put billing on the organization since ADR-020 — the `owner` and `billing` roles are org-scoped — while `projects.plan_id` puts the plan on the project. Both stay true: `org_id` is who pays, `project_id` is what the plan applies to.

The pair is a **composite foreign key** against a new `UNIQUE (id, org_id)` on `projects`, not two independent references. Two separate ones would permit a row naming org A and a project belonging to org B, and that is a cross-tenant control rather than a typo: it would let one organization move another organization's project between plans. Enforcing it in the schema means no future caller — a webhook handler, a backfill, a psql session — can get it wrong. The module additionally reads `org_id` from the project rather than accepting it, which is the same property one layer up; neither is load-bearing alone.

**`state_as_of` is an ordering guard, and it is here rather than in slice 4 because it is a property of the record.** Providers retry and deliver out of order, so a `canceled` can arrive after the `active` that superseded it — and ordered by arrival, that downgrades a paying customer. Every transition carries the moment the provider says the fact was true, and one older than what is on the row is refused as stale. Equal is accepted, because a redelivery of the current truth is idempotent and exact duplicates are slice 4's event-id idempotency, which is a different control. A timestamp rather than a sequence number because it is the only ordering key all three candidate providers expose.

**`past_due` keeps its plan, and that is a default rather than an answer.** How long a failed payment is tolerated, and what happens to a project holding paid-sized data when it ends, is the third `## Billing` question and slice 5's business. What is settled here is only that a failed payment is not, by itself, a downgrade — which is the direction acceptance criterion 4 demands.

**Consequences.**

- Acceptance criterion 3 is satisfied by structure rather than by care: billing state cannot write an entitlement because the code that holds it has no path to one.
- `cp-manage subscription drift` reports a class of divergence that has always existed and was never visible: a project on a plan no subscription pays for. **Every paid project on the platform is one the day this ships**, because `project set-plan` takes no money. That is a report to work through, not a bug.
- Drift is reported and not corrected, on `plans drift`'s precedent (ADR-notes in slice 0) plus a stronger reason: moving a project between plans unattended is a change that should have somebody's name on it.
- The slice-3 block in `plans/completed/phase-09-billing.md` is narrowed to slices 4–6. Slices 4 and 5 remain blocked on decisions 1, 3 and 4; slice 3 never depended on them, which is what "provider-shaped but not provider-specific" was asking for.
- A `subscription_events` or transition table was considered and rejected. `plan_changes` is a table because a plan change is a resumable *operation* with a half-done state; a subscription transition is a fact that either was recorded or was not, and `audit_events` already holds facts, already outlives the row it describes, and is already shown to customers through an allowlist.

## ADR-049 — Stripe is the provider, and merchant-of-record status is configuration rather than code

**Status:** Accepted — decided 2026-08-20 by the repository owner. Answers the
first `## Billing` question in `docs/OPEN-QUESTIONS.md` and unblocks slice 4 of
`plans/completed/phase-09-billing.md`. Constrains how that slice builds checkout.
Depends on ADR-048 for the boundary that makes a provider swap survivable.

**Context.** The question was framed as processor versus merchant of record,
because that is the axis that decides who owes tax. MaluDB is a US business
selling a subscription internationally. With a payment processor, MaluDB is the
seller of record: EU B2C digital services have **no** registration threshold —
one sale creates the obligation — and collapse into a single OSS registration
and a quarterly return; the UK requires its own registration post-Brexit; and
the US fragments per state under *Wayfair* economic nexus, with SaaS taxability
varying by state. With a merchant of record, the MoR is the legal seller and
all of that is theirs.

The framing assumed those were different vendors. They are not any more.
**Stripe Managed Payments is Stripe's own merchant-of-record offering** —
Stripe becomes the legal seller, invoicing as *Sold through Link, LLC*, and
registers, files and remits sales tax, VAT and GST in more than 80 countries
including the US, the EU 27 and the UK. Stripe Tax, by contrast, calculates and
monitors thresholds and leaves MaluDB the seller: it does not register, does
not file, and does not assume liability.

Both are the same API. Same `Customer`, same Billing `Subscription`, same
`Price`, same Checkout Session, same webhook signature scheme, same event-id
idempotency. Managed Payments is enabled per account and per Checkout Session.

**Decision.** **Stripe.** The merchant-of-record posture is a deployment
configuration, not an integration: slice 4 is built once and works either way.

The recommendation, which the platform must not depend on, is to **launch with
Managed Payments enabled** and revisit when its fee exceeds what compliance
would otherwise cost. Its price is **3.5% on top of standard processing** —
roughly 6.4% + $0.30 on a domestic US card, plus 1.5% on international cards
and 1–2% currency conversion unless Adaptive Pricing is on. Against that: the
free tier means most projects never generate a tax event at all, and the
failure mode it removes is back-VAT plus penalties in a jurisdiction nobody
remembered to register in — which is not a percentage of revenue, it is a
number somebody else chooses.

**What this constrains in slice 4, and why it is here rather than there.**
Managed Payments works only with **hosted Checkout and Payment Links**. It does
not support Elements or other advanced integrations, and **a subscription
cannot be created outside Checkout** — no `subscriptions.create` against a
saved payment method. So:

- **Slice 4 uses hosted Checkout.** An Elements integration would foreclose the
  MoR option, and would foreclose it silently: it would work, and the cost of
  the choice would only appear the day somebody tried to turn Managed Payments
  on. That is exactly the shape of decision this file exists to stop.
- **The mapping table carries a Stripe product tax code alongside the price
  id.** Managed Payments requires an eligible code per product. MaluDB is a
  managed database platform, which is `txcd_10102000` (PaaS — business use) or
  `txcd_10103001` (SaaS — business use) depending on how the offering is
  characterised. **The platform stores whichever code the tax advisor confirms;
  this ADR does not decide it,** and getting it wrong is a mispriced tax rather
  than a broken integration.
- **No Connect.** MaluDB sells directly and is not a marketplace, so this costs
  nothing, but it does mean any future reseller or agency arrangement leaves
  the MoR path.
- **No one-off invoices, and no invoice items attached to a subscription.** An
  enterprise tier billed by negotiated invoice would have to sit outside
  Managed Payments. That is a constraint to notice before selling one, not a
  reason to reject the choice now.

Sales to customers in countries Managed Payments does not cover fall back to
MaluDB as seller, with Stripe Tax available and its calculation fee waived on
Managed Payments transactions. Stripe Tax is the only tax integration Managed
Payments permits; third-party tax providers are not supported.

**Consequences.**

- ADR-048 already made the provider a slice-4 concern rather than a platform
  one. This ADR spends that: choosing Stripe adds a provider column, an event
  table and a mapping table, and touches nothing that grants an entitlement.
- **Stripe's states are mapped onto MaluDB's, never the reverse.** `trialing`
  and `past_due` happen to share a name with Stripe's; that is a coincidence
  the mapping code must not rely on, because a provider swap would end it.
- Eligibility is ongoing rather than granted once: Stripe monitors dispute rate
  and can withdraw access, and can itself issue refunds within 60 days to head
  off a chargeback. A refund the platform did not initiate is therefore a
  webhook the platform must handle, not an impossibility.
- Nothing in the test suite may reach Stripe. Webhook handling is tested
  against recorded payloads signed with a test secret, as the phase plan's
  preconditions already require.

## ADR-050 — Hard limits, not overage: the platform has no metering pipeline

**Status:** Accepted — decided 2026-08-20 by the repository owner. Answers the
second `## Billing` question and settles the shape of slice 6.

**Context.** Overage billing would require per-project usage aggregated per
billing period and reported to the provider: a new subsystem whose correctness
is somebody's money, where a double-reported unit is a wrong charge and a
dropped one is lost revenue. Hard limits reuse what Phase 05 already enforces
and add nothing.

**Decision.** **Hard limits.** A plan's entitlements are ceilings. Exceeding
one is refused at the point of use — by the gateway, by `storage.py`, by
`mail.py`, by the role GUCs slice 0 applies — and never becomes a charge. The
platform reports no usage quantity to any provider.

Two things happen to agree here, and the agreement is worth recording because
it removes a temptation later. Managed Payments (ADR-049) **cannot** bill
overage: invoice items cannot be attached to a Managed Payments subscription,
and one-off invoices outside the billing period are unsupported. So a future
"just add metered billing" would not be an incremental feature; it would be a
decision to leave merchant-of-record status, with everything ADR-049 describes
following from it.

**Consequences.**

- **Slice 6 reports the billing period, not a metered quantity.**
  `/v1/projects/{ref}/usage` gains period boundaries from the subscription and
  keeps showing usage against the plan's ceiling, which is what it already
  computes. No number in it is ever sent to Stripe.
- The upgrade path is the product: a customer who hits a ceiling changes plan.
  That makes ceilings a conversion surface and therefore a product concern —
  a limit hit with no visible way forward is a churn event, not a saved dollar.
- No usage record is a billing record, so no usage bug is a billing bug.

## ADR-051 — A failed payment costs write access after fourteen days, and never costs data

**Status:** Accepted — decided 2026-08-20 by the repository owner. Answers the
third `## Billing` question and unblocks slice 5. Uses ADR-040's mechanism;
depends on ADR-048 for `past_due` not being a downgrade by itself.

**Context.** Phase 09's fourth acceptance criterion forbids destroying customer
data. The competing cost is real: a project holding paid-sized data whose plan
reverts to a tier with a far smaller quota is storage MaluDB pays for and
nobody pays MaluDB for. ADR-040 already built and tested the mechanism that
sits between those — revoke `INSERT` and `UPDATE`, keep `SELECT`, `DELETE` and
`TRUNCATE` — so a customer can still read everything and can still shrink out
of the restriction under their own power.

**Decision.** Three stages, and the third one never arrives.

1. **Fourteen days of unchanged service.** The subscription is `past_due`; it
   keeps its plan and every entitlement it had. Cards expire, banks decline
   for reasons unrelated to intent, and a platform that restricts on the first
   failure punishes the wrong thing.
2. **Then the ADR-040 storage restriction, and reconciliation to the default
   plan.** The subscription becomes `canceled`, `subscriptions.reconcile` hands
   the default plan to `plan_change.change_plan`, and the project keeps its
   database, its `project_ref`, its API keys and its rows — ADR-006 unchanged.
   Direct database access ends, because that is what `plan_apply` does with a
   plan that does not grant it. Writes stop; reads do not.
3. **No automatic deletion, at any point.** Not after the grace period, not
   after a dormancy window, not as a storage-reclamation pass. Data leaves this
   platform when a customer asks for it to leave.

**Fourteen days is a number, and it belongs in configuration.** It is not a
constant in application logic — that is the development rule against hard-coded
plan limits, and a grace period is one. A deployment may lengthen it; nothing
in the code may assume its value.

**Consequences.**

- **The criterion is satisfied by there being no code that deletes.** Slice 5's
  deliverable is the test that a restricted project's rows survive the whole
  transition, which is the one acceptance criterion whose failure cannot be
  undone.
- The restriction is recoverable by the customer without contacting anybody:
  pay, or delete enough rows to fit the free quota. Both are doors they can
  open themselves, which is the property ADR-040 was chosen for.
- **Indefinite retention of restricted projects is an accepted, unbounded
  cost.** It is accepted because the alternative is the thing criterion 4
  forbids. What is *not* settled here is whether a project restricted for a
  very long time is ever reclaimed after explicit, delivered notice; that is a
  narrower question than the one this ADR answers, and it stays open in
  `docs/OPEN-QUESTIONS.md` rather than being decided by silence.
- A payment that succeeds during grace is an ordinary `past_due` -> `active`
  transition and reconciles to nothing, because the plan never moved.

## ADR-052 — Prices live in the provider; the platform stores only a mapping

**Status:** Accepted — decided 2026-08-20 by the repository owner. Answers the
fourth `## Billing` question. Constrains slice 4's mapping table and confirms
what `specs/plans-and-limits.yaml` is.

**Context.** A price could live in the repository, in the provider, or — the
default outcome of not deciding — in both.

**Decision.** **Only in the provider.** The platform stores the mapping
`plan_code` -> Stripe price id, plus the product tax code ADR-049 requires. It
stores no amount and no currency, and it never computes what a customer owes.

Two sources of truth for a number a customer is charged is the drift that
becomes a refund: the repository says one thing, Stripe charges another, and
the customer is right whichever way it went. A mapping cannot drift into a
wrong charge — it can only point at the wrong price, which fails visibly at
checkout rather than silently on a statement.

**Consequences.**

- **`specs/plans-and-limits.yaml` remains an entitlement catalogue,** which is
  what `plans.router` already calls itself in its own comment. Nothing in it
  is money.
- A displayed price is read from Stripe, not from configuration. A price list
  in the public API stays out of scope, as the phase plan's non-goals say.
- Changing a price is a Stripe operation followed by a mapping update, in that
  order. The mapping pointing at a stale-but-valid price id charges the old
  price; pointing at a deleted one fails at checkout. Both are better than
  charging an amount no record supports.
- The mapping is deployment configuration: test-mode price ids and live-mode
  price ids are different strings for the same `plan_code`, and no test may
  need a network to resolve one.

## ADR-053 — The webhook endpoint is public and does not reconcile; the maintenance pass does

**Status:** Accepted — decided 2026-08-20 during Phase 09 slice 4, because two
existing decisions turned out to constrain each other and the resolution is not
obvious from either. Depends on ADR-037 (the public/internal split), ADR-038
(node credentials stay out of the internet-facing process), ADR-048 (billing
state proposes, `plan_change` disposes) and ADR-049 (Stripe).

**Context.** Slice 4 needs an endpoint Stripe can post events to. Two rules meet
there and pull in opposite directions.

ADR-037 splits the control plane into a public application and an internal one,
and the internal one must never be bound to a public interface. GoTrue's
send-email hook is the existing precedent for a non-customer endpoint, and it is
**internal** — which is right, because the caller is a worker on a node, inside
the platform's own network.

Stripe is not. It posts from the internet, to a URL configured in a dashboard.
An endpoint on the internal listener is an endpoint Stripe cannot reach.

The other rule is ADR-038: the process bound to the internet must not be able to
obtain a node's superuser credential. And applying a plan needs one — that is
what `plan_apply` does, and it is the whole content of what a customer has just
paid for.

So a webhook handler that received a payment and granted the plan would have to
hold node credentials in the internet-facing process. That is the exact thing
ADR-038 exists to prevent, and it arrived wearing a good disguise: the code
would be short, obviously correct on the happy path, and the ADR it violated is
about provisioning rather than about billing.

**Decision.** Split the two halves along the process boundary that already
exists.

- **`POST /webhooks/stripe` is on the public listener**, and out of the OpenAPI
  document because it is not part of the customer API. The **signature is the
  authentication** — the sentence `hooks.py` already writes about the same
  problem — verified before the body is parsed, so no unverified payload is ever
  a parsed object. Network position is not a control here at all, and pretending
  otherwise by requiring a reverse-proxy exception for one path would make the
  deployment depend on something nobody wrote down.
- **The handler records and never applies.** It writes billing state and
  nothing else: no node connection, no `projects.plan_id`, no `plan_apply`.
  ADR-048 already drew that line for a different reason; this makes it
  load-bearing for a second one.
- **`maintenance.reconcile_subscriptions` applies it**, running where node
  credentials already live, alongside the passes that already hold them.

**A customer's plan therefore arrives a pass after their payment.** Seconds to a
minute. That is the honest cost of the split and it is worth naming rather than
optimising away, because every way of removing it puts a node credential in the
process that answers the internet.

**This is not the reconciler `report_plan_drift` refuses to be**, and the
difference matters because that refusal is a rule this repository keeps: a
reconciler on a timer would undo `cp-manage project direct-sql --disable` within
the hour, a control cancelling a control.

Three things separate them.

- **It runs on a queue, not a sweep.** `pending_reconciliation` returns
  subscriptions whose entitling facts have changed since they were last applied
  — a specific billing event, with a customer's payment behind it. It never
  looks at a project nothing happened to.
- **It goes through `plan_change`, which no-ops when the plan already matches.**
  An operator's incident measure changes the *node*, not the plan, so this pass
  cannot see it and cannot undo it. `plan_drift` remains the report for that,
  uncorrected.
- **Pre-existing divergence stays reported.** `cp-manage subscription drift` is
  unchanged: a project on a plan nothing pays for is still a question for a
  person, because there is no event saying what should happen to it.

**The queue is a predicate over `(state, plan_code)`, not over a timestamp**,
and that was a bug before it was a design. The first implementation compared
`state_as_of` against the last reconciled value — but that timestamp comes from
the provider, Stripe's event timestamps are whole seconds, and
`checkout.session.completed` and `customer.subscription.updated` routinely
arrive inside the same one. A queue keyed on it can mark the second fact done by
applying the first: silent loss, in the one place whose entire purpose is that
nothing is missed. A test caught it. Comparing the two values
`entitled_plan_code` actually reads asks the real question — has the entitlement
moved — and is exact rather than nearly always right.

**Consequences.**

- The public surface gains a route that is not a customer route, and
  `tests/test_control_plane_surfaces.py` carries it explicitly with the reason,
  so it cannot be read as an oversight.
- `api/billing.py` is now on the ADR-038 import-closure assertion's list, which
  matters more than the other entries: it reaches `subscriptions` ->
  `plan_change` -> `plan_apply`, the deepest any public router goes. The
  assertion is what keeps that reach from becoming a node credential.
- **The endpoint answers 200 to refusals**, and records them. A retry cannot fix
  an unknown session or an unmapped price, and days of redelivery ends with
  Stripe disabling the endpoint — taking with it the events that would have
  worked. `cp-manage billing events` is where a refusal is found, which is why
  every event is written down before it is acted on rather than after.
- A deployment must run the maintenance pass for billing to work at all. It
  already had to, for storage and for sleeping workers; billing makes a missed
  pass visible to a customer rather than only to an operator, and
  `cp-manage billing status` reports the queue depth for that reason.

## ADR-054 — A grace period is measured from when a state began, not from when it was last true

**Status:** Accepted — decided 2026-08-20 during Phase 09 slice 5. Amends nothing;
it records a distinction ADR-051 assumed without naming, and which the obvious
implementation gets wrong. Depends on ADR-048 for `state_as_of` and on ADR-051
for the period being measured.

**Context.** ADR-051 gives a failed payment fourteen days of unchanged service.
That has to be measured from something, and the column already on the row looks
exactly like the right one.

It is not. `state_as_of` is **when this fact was true**, written by whoever
asserted it, and it moves on every delivery — including a delivery that asserts
the state already on the row. That is deliberate: ADR-048 built it as an
*ordering* guard, so that a stale `canceled` arriving after the `active` that
superseded it is refused. Ordering wants the latest timestamp. A clock wants the
first one.

Stripe re-sends `customer.subscription.updated` with `status=past_due` on every
dunning retry, each carrying a newer `created`. A grace period measured from
`state_as_of` therefore restarts on every retry and **never expires**. The
customer keeps a paid plan indefinitely, no error is raised anywhere, and the
outcome is indistinguishable from the system working — which is the property
that makes it worth an ADR rather than a comment.

**Decision.** Two timestamps, because they answer two questions.

- **`state_as_of` — when this fact was true.** Unchanged. Moves on every
  delivery, and remains the ordering guard.
- **`state_since` — when this state began.** Written when the state changes,
  left alone when the same state is re-asserted. Every duration measured over a
  subscription's state uses this one.

The write is a `CASE` inside the same UPDATE, which reads the pre-update row, so
"the state actually changed" is evaluated by the database rather than by a
caller that could forget.

**Consequences.**

- `subscriptions.in_expired_grace` and `in_grace` read `state_since`, and the
  comparison happens in SQL against `now()` — so a control-plane host with a
  drifted clock cannot end somebody's grace early.
- The two columns are equal until a state is confirmed twice, which is why the
  distinction is easy to miss and why the test that catches it fires three
  dunning retries deliberately.
- The generalisation is free and worth having: any later question of the form
  "how long has this been in this state" now has an honest answer, rather than
  one that is right only until a provider retries.

## ADR-055 — SeaweedFS is the object store, S3 is the boundary, and the endpoint is configuration

Status: Accepted — decided 2026-08-22 by the repository owner, while planning
Phase 10. Answers the first `## Storage` open question, outstanding since
Phase 01, and settles `docs/STORAGE.md`'s "no provider is selected yet".

**Context.** `docs/STORAGE.md` requires that the implementation not couple the
tenant-database lifecycle to one object-storage vendor, and
`tasks/PHASE-10-STORAGE.md` opens its scope with "object-store provider
abstraction". Both read as an instruction to write one.

**The abstraction already exists and it is upstream's.** `storage-api` selects
its backend with `STORAGE_BACKEND=s3|file` and addresses an S3 one with
`STORAGE_S3_ENDPOINT`, `STORAGE_S3_REGION`, `STORAGE_S3_FORCE_PATH_STYLE` and
`STORAGE_S3_BUCKET`. So the requirement is met by configuration, and **the
platform writes no provider abstraction of its own** — a MaluDB-side driver
interface above an existing one would be owned by us, tested by us, and would
add nothing. `AGENTS.md` prefers upstream's arrangement, and this is one.

**Decision: SeaweedFS**, with S3 as the boundary.

MinIO was the obvious candidate and was rejected after checking its current
state rather than its reputation. Its open-source repository went to
maintenance in December 2025 and was **archived on 25 April 2026**: admin
features stripped from the community console, binary distribution stopped,
source-only builds, no security patches. For the component holding every
customer's files, unpatched is disqualifying. This is recorded because the
recommendation was made and withdrawn during the same planning session, and the
withdrawal is the part a later reader needs.

Why SeaweedFS over the alternatives:

- **Apache 2.0, not AGPL.** This is a commercial platform running the service,
  and that distinction is worth more here than any feature difference. It is
  also what rules out **Garage**, which is otherwise attractive for its small
  operational surface.
- **Actively developed**, unlike MinIO's community edition — which is the whole
  reason this decision is being made at all.
- **Single Go binary.** `weed server -s3` runs master, volume, filer and the S3
  gateway in one process, so a small deployment costs a process rather than a
  cluster.
- **Ceph RGW** was considered and rejected as disproportionate. It is the most
  complete S3 implementation of the four and would be the right answer if the
  Proxmox cluster already ran Ceph for VM storage, since Proxmox manages the
  MONs and OSDs itself and RGW is a package on top. Standing up a three-node
  Ceph cluster for one phase is not. **RustFS** is viable and Apache 2.0 but has
  the least evidence under sustained load.

**Where the bytes live: the existing Proxmox hardware, with a stated exit.**
Not a dedicated storage box yet, and not because separation is wrong — a
separate failure domain for object bytes is better, and the exit is expected to
be taken. It is deferred because it costs money now and buys nothing until
there is data worth protecting.

**What makes the exit cheap is an invariant rather than discipline.** ADR-035
forbids a rootless Podman container from reaching node loopback — measured in
Phase 06, after a Realtime container turned out to reach a different cluster's
PostgreSQL through `allow_host_loopback`. So `storage-api` must address
SeaweedFS on a data address whether it is one hop away or one datacenter away.
The local deployment is addressed as though it were remote, because it cannot
be addressed any other way. The thing that normally makes "we will separate it
later" untrue — code that quietly assumes co-location — is prevented by a
containment that already exists and is already tested.

Moving to dedicated hardware is therefore: stand up SeaweedFS on the new box,
copy the buckets, change `STORAGE_S3_ENDPOINT`. No platform code changes.

**`STORAGE_BACKEND=file` was rejected as a starting point**, despite being
simpler than running SeaweedFS locally. It is a different code path inside
`storage-api`, not a different address, so every behavioural difference between
the file and S3 backends would surface during a live migration of customer
data — and it would leave the vendor-decoupling requirement above untested,
because nothing would have exercised the S3 path at all.

**Consequences.**

- The S3 endpoint is configuration from the first commit, never a default and
  never a loopback address. `render_env` refuses an unusable value, as it does
  for the Realtime data address.
- A provider change is an endpoint change plus a copy. That is the point of the
  boundary and the reason a slice-0 compatibility gap in SeaweedFS changes the
  provider rather than the design.
- **Nothing tested SeaweedFS against `storage-api` specifically.** Upstream
  needs AWS SigV4, multipart create/complete/abort, copy-object and presigned
  URLs. Phase 10 slice 0 is a bake-off before any slice depends on it.
- SeaweedFS's Apache 2.0 core covers everything Phase 10 needs. Automatic
  erasure-coding repair, EC vacuum, self-healing and point-in-time recovery sit
  behind a per-TB Enterprise licence, free under 25 TB for development and
  test. Those are durability features, so this is a **Phase 11** input — where
  backups, restore and PITR already live — and it is written here so it arrives
  as a decision rather than as a discovery.

## ADR-056 — Storage is available on every tier, bounded by hard ceilings

Status: Accepted — decided 2026-08-22 by the repository owner, while planning
Phase 10. Answers the third `## Storage` open question ("egress model?").
Applies ADR-050 to a new pair of resources; supersedes nothing.

**Context.** Object storage introduces the platform's first resource whose cost
is driven by bytes served rather than bytes held, and the first that a customer
can cause an anonymous third party to consume: a public bucket is served to
whoever has the URL.

**Decision.** **Free projects get Storage**, and two new entitlements bound it:
`object_storage_bytes` and `egress_bytes_per_month`. Both are **hard ceilings
under ADR-050** — enforced at the point of use, refused when exceeded, never
converted into a charge, never reported to any provider. The platform still has
no metering pipeline and this does not build one.

**Why free gets it, rather than this being the paid line.** ADR-005 makes the
free tier API-only, and Storage over the gateway is API access: no credential is
issued and no port is opened, which is exactly ADR-039's test. Making Storage
paid-only would also aim at the wrong target — `services/migrate/rules.py`
turns away every Supabase project that uses Storage today, so a Supabase user
with files could not evaluate MaluDB at all. That is the customer this
compatibility work exists to reach.

The paid line stays where ADR-039 put it. The **S3 protocol endpoint** —
upstream's `S3_PROTOCOL_ACCESS_KEY_*`, which lets a customer point an S3 client
at their project directly — is a credential and a reachable endpoint, which is
ADR-039's paid line almost word for word. It is deferred rather than assumed,
and deserves its own decision.

**Consequences.**

- Two entitlements added to `entitlements.Entitlements`, `DEFAULTS` and
  `specs/plans-and-limits.yaml`, at every tier. `AGENTS.md` forbids hard-coding
  production plan limits in application logic, so the numbers are configuration
  like every other limit.
- **Egress is accounted at the gateway**, because that is where the bytes pass:
  Supabase serves a signed URL through `storage-api` rather than redirecting to
  the object store. Phase 10 slice 0 confirms that before slice 4 depends on it.
- A consequence of the above worth stating plainly: the object store's own
  transfer allowance is close to irrelevant next to the node's bandwidth, which
  is what ADR-055's hosting decision actually spends.
- Egress accounting lands on a path ADR-026 published a throughput number for.
  The regression is measured, not asserted.
- ADR-050's product point applies unchanged and is sharper here: a ceiling hit
  with no visible way forward is a churn event, not a saved dollar. A project
  that has stopped serving its users' files needs to be told why and what to do.

### The accounting measurement this ADR asked for

**Measured 2026-08-25**, Phase 10 slice 4, with `scripts/bench-gateway.py`,
which now drives `/storage/v1` twice against the same stub: once with the meter
that counts and flushes, once with one that does nothing.

**Sequentially the accounting is not distinguishable from zero.** Three runs of
500 requests put it at **-0.22, -0.44 and +0.74 ms at p50** — straddling zero,
so the honest statement is that it is inside this machine's noise rather than
that it is fast. That is the design working rather than a surprise: the ceiling
is read from a per-project cache with a 30-second TTL, and the bytes are
accumulated in memory and written in batches, so what a request pays is a
dictionary update under a lock.

Under 20 concurrent the runs disagree by more than the effect (39.8–43.4 rps
counted against 40.0–45.1 uncounted, with p95 between 1.0 and 1.2 s in both),
and both figures are bounded by the thread-per-request stub the way ADR-026's
own concurrent pair is. Nothing is claimed from that beyond the absence of a
large regression.

The gateway's own overhead on the Data API is unchanged at **+6.3 to +7.0 ms**
across the same three runs, against the +6.3 ms this ADR's parent recorded in
Phase 03 — so slice 4 did not move it.

What is bought with the batching is bounded and stated rather than hidden: a
project can serve up to one flush interval past its ceiling, and an ungraceful
exit loses at most that interval's bytes. A graceful one loses none, because the
process flushes on shutdown.

## ADR-057 — One platform bucket; tenancy for objects lives in metadata, not in the object store

Status: Accepted — decided 2026-08-22 by the repository owner, while planning
Phase 10. Answers the second `## Storage` open question ("tenancy/bucket
design?").

**Context.** `STORAGE_S3_BUCKET` is singular. Upstream names one bucket for a
whole deployment; a customer's "bucket" is a row in the tenant database's
`storage.buckets`, and an object is a key inside that single bucket.

**Decision.** **Keep upstream's design.** One platform-owned S3 bucket holds
every tenant's objects, keyed by tenant and bucket. A customer bucket is
metadata.

This is not really a choice between two good options. A bucket-per-project
scheme would be a MaluDB-specific alternative to an upstream behaviour, which
`AGENTS.md` forbids, and would put a per-project object-store API call into the
provisioning path — coupling the tenant lifecycle to the vendor, which is the
one thing `docs/STORAGE.md` explicitly requires this phase not to do. It would
also hit per-account bucket ceilings on most providers at a few thousand
projects.

**The consequence, which is the important half of this record: tenant isolation
for objects is not provided by the object store.** One bucket holds everything.
What separates tenants is the key prefix and the tenant database rows that map
a request to it — the metadata layer and the worker's credential scoping.

So `tasks/PHASE-10-STORAGE.md`'s third acceptance criterion, "cross-project
object access is denied", is a property of code this platform writes, not of a
product it buys. It must be tested directly, as a denial, against a real second
project. It is the highest-value security review in Phase 10, and a reviewer
should treat any path that derives an object key from customer-controlled input
the way `AGENTS.md` already asks them to treat generated SQL identifiers.

Two things follow that are easy to miss:

- **RLS on `storage.objects` is load-bearing.** Measured in Phase 08 and
  recorded at `services/migrate/source.py:248`: Supabase enables RLS on
  `storage.objects` and `storage.buckets` by design, because storage policies
  *are* RLS policies. This phase cannot harden by turning RLS off.
- **`service_role` bypasses RLS**, so ADR-041's rule applies in a new place: on
  any surface where the caller influences which role is used, the role named in
  a request selects a credential and nothing else. It is not a permission
  boundary. A path that lets a customer cause `storage-api` to act as
  `service_role` for a bucket they do not own defeats every policy at once.

**Consequences.**

- Provisioning makes no object-store API call, so a project can be created
  while the object store is unreachable, and the tenant lifecycle stays
  decoupled from the vendor.
- **Deleting a project must delete its objects.** They live outside the
  database and outside the roles, so `jobs.cleanup` — which today drops both —
  would otherwise leave a deleted customer's files in the platform bucket
  indefinitely. That is a data-retention problem first and a cost problem
  second, and it is assigned to Phase 10 slice 3 rather than to a later
  tidying pass, because it is cheap there and expensive everywhere after.
- Per-tenant object accounting is a prefix scan or a metadata sum rather than a
  bucket-level figure the provider reports. ADR-056's `object_storage_bytes`
  is measured from `storage.objects`, which is also the only source that stays
  correct across a provider change.

## ADR-058 — Storage is one shared multi-tenant container per node, and ADR-034's reasoning does not carry over

Status: Accepted — decided 2026-08-22 from measurements taken in Phase 10
slice 0. Recorded in `specs/storage-server-model.md`. Introduces no new runtime:
it applies ADR-033's container pattern and ADR-035's containment to a second
component. **Does not supersede ADR-034**, which remains correct for Realtime.

**Context.** ADR-034 put one Realtime instance per project and paid ~146 MB
each for it. The natural assumption for Storage was that the same answer
applied, and the plan for this phase deliberately refused to make it, on the
grounds that ADR-034's reason was specific rather than general.

It was specific. `SLOT_NAME_SUFFIX` is a server-level environment variable and
PostgreSQL replication slot names are **cluster-unique**, so one Realtime server
serves one tenant per cluster — a hard constraint, not a preference.
`storage-api` has no cluster-unique resource. Its multi-tenancy resolves a
tenant per request and reads that tenant's configuration from a metadata
database.

**Decision.** **One shared `supabase/storage-api` instance per node**, in
`MULTI_TENANT=true` mode, pinned at `v1.70.6` under rootless Podman and
supervised by systemd.

**The measurement, which is the whole argument.**

| | dedicated | shared |
|---|---|---|
| instance, cgroup | 105.8 MB | 119.8 MB at 8 tenants |
| marginal per tenant | ~106 MB | **~0.7 MB** |

Six tenants added 4.1 MB. A dedicated instance per project would make Storage
the most expensive capability a project could enable — more than three times an
entire warm project at ADR-022's 31.8 MB, and in the same class as Realtime,
which ADR-034 accepted only because nothing else worked. Here something else
works.

**Why this is safe, which is the part that had to be checked rather than
assumed.** ADR-034 preferred per-project partly for blast radius: one instance
holds one tenant's credential rather than N. A shared instance does hold every
registered tenant's DSN, so the trade is real and was measured rather than
waved through.

Two independent boundaries, both verified with two tenants holding different
`jwtSecret` values:

- `X-Forwarded-Host` selects the tenant, through
  `REQUEST_X_FORWARDED_HOST_REGEXP`. An unregistered host is `400
  TenantNotFound`; a host not matching the pattern is `400 Invalid tenant id`.
- **The selected tenant's own JWT secret must verify.** A token signed with
  tenant A's secret, presented against tenant B's host, is refused with `403
  signature verification failed`.

Two tenants each created a bucket of the same name holding a key of the same
name, and each read back only its own bytes. The separating prefix is
`tenant_id`, taken from the resolved tenant and never from the request path or
body.

**Consequences.**

- **The gateway must set `X-Forwarded-Host` authoritatively and strip any
  client-supplied value.** This is the load-bearing consequence of the whole
  decision. The header is the tenant selector: a client able to set it chooses
  which tenant's configuration and connection pool a request is evaluated
  against. The JWT check means that is not by itself a path to another tenant's
  data, but it is a denial-of-service and information-disclosure surface, and
  slice 4 must be reviewed as though the JWT check were absent. A second layer
  is not a reason to weaken the first.
- **A tenant is registered through an admin API**, on `SERVER_ADMIN_PORT`,
  authenticated with an **`apikey`** header. That port is internal in ADR-037's
  sense — it can create and reconfigure any tenant, including its database URL —
  and must never face the internet or the gateway's proxy path.
- **The metadata database is platform-owned and holds every tenant's DSN.** It
  is a control-plane-grade secret store and is treated as one under ADR-023.
- **Blast radius is honestly worse than per-project**, and this is the accepted
  cost. It is accepted because the alternative is ~106 MB per project for a
  capability intended to be on by default on the free tier, and because the
  containment that actually bounds the damage — ADR-035's network namespace,
  verified again here — is unchanged by the topology.
- **The density figures are for idle tenants.** Per-tenant connection pooling
  under sustained load is the term that could move them, bounded by
  `DATABASE_MAX_CONNECTIONS` and released by
  `DATABASE_FREE_POOL_AFTER_INACTIVITY`. Slice 3 takes that measurement before
  the topology is committed to in code, and this ADR should be revisited rather
  than defended if it turns out badly.

  **Measured 2026-08-24 in slice 3, and it does not move the decision.** Eight
  tenants under concurrent load — 400 upload-and-download operations in 5.8 s —
  took the instance from 146.8 MB to 161.3 MB, so roughly **1.8 MB per
  actively busy tenant** on top of ~0.46 MB per registered idle one. Eight
  simultaneously busy tenants cost about 18 MB above idle against 105.8 MB for
  a single dedicated instance. The memory does not return promptly when traffic
  stops, which is `DATABASE_FREE_POOL_AFTER_INACTIVITY`'s business over a longer
  window than the measurement covered. `specs/storage-server-model.md` carries
  the table. The figure to watch on a node is total concurrency, not tenant
  count.

**Revisit if** a tenant's load can be shown to affect another's on a shared
instance, or if upstream introduces a server-level setting that is
tenant-specific in the way `SLOT_NAME_SUFFIX` was — which is exactly what
made Realtime's answer different.

## ADR-059 — The tenant `storage` schema is owned by a per-tenant service role, and its RLS is deliberately unforced

Status: Accepted — decided 2026-08-24 from measurements taken in Phase 10
slice 1. Recorded in `specs/storage-server-model.md` and implemented by
bootstrap `012_storage_schema.sql`. Applies ADR-004, ADR-016 and ADR-018 to a
third service; supersedes nothing.

**Context.** Upstream `supabase/storage-api` expects to own its own database
arrangements. `.env.sample` ships `DB_INSTALL_ROLES=true` and
`DB_SUPER_USER=postgres`, and migration `0002-storage-schema.sql` acts on that:
it creates `anon`, `authenticated`, `service_role`, a `supabase_storage_admin`
superuser and an `authenticator`, then hands the schema to the first of those.

Every one of those collides with something already decided. ADR-004 keeps
database ownership with the platform and gives customers no superuser. ADR-016
makes the three Supabase role names **shared cluster-wide**, so a component
that believes it may create them is a component that believes it is alone on
the cluster — and on a shared node it is wrong in a way that reaches every
other tenant. This is the same shape as the GoTrue problem bootstrap 007 solved
in Phase 04.

**Decision.**

1. **`DB_INSTALL_ROLES=false`, and the platform does the half upstream would
   otherwise do.** Not a hardening option; the alternative is not available.
2. **A new per-tenant role, `mldb_<ref>_storage`, owns the `storage` schema**,
   on bootstrap 007's precedent that the service which migrates a schema owns
   it. `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION`,
   `CONNECT` on one database, no grant on `public`, and a platform-internal
   credential in the same class as `mldb_<ref>_auth` — never issued to a
   customer. Created for every project, because ADR-056 puts Storage on every
   tier.
3. **Its `search_path` is pinned to `storage`, `IN DATABASE`.**
4. **The three shared names are granted `USAGE` on the schema** — the grant
   upstream makes only inside the branch that is now off — **and are granted to
   the storage role**, ADR-016's permitted direction, because `storage-api`
   switches role per request.
5. **Row-level security stays on and stays unforced.**
6. **`mldb_<ref>_admin` receives no privilege on `storage`.**

**What was measured, because points 3 and 5 are not obvious.**

All 63 tenant migrations complete under that constrained role — *except one*.
`0011-add-trigger-to-auto-update-updated_at-column.sql` opens with an
**unqualified** `CREATE OR REPLACE FUNCTION update_updated_at_column()` and
fails with `permission denied for schema public`. Upstream lands it in
`storage` only because its own migration 0002 sets `search_path` on
`supabase_storage_admin`, inside the branch this ADR turns off.

The obvious fix — `GRANT CREATE ON SCHEMA public` — makes the migration pass
while dropping a platform function into the one schema PostgREST exposes. That
is Phase 00 finding 4 exactly, and it fails *silently*: the migration reports
success. Pinning `search_path` instead makes all 63 pass with nothing added to
`public`, asserted as a before/after diff in `tests/test_object_storage.py`.

Point 5 is the one a reader will question. `storage-api` does not query as the
owner: `dist/internal/database/postgres/scope.js` issues
`set_config('role', <role from the JWT>, true)` — a `SET LOCAL ROLE` — per
request, which is what makes RLS apply to customer-scoped work at all.
Measured on a migrated tenant holding one object and no policies: the owner
sees it, `authenticated` and `anon` see nothing, `service_role` sees it.
**Forcing RLS would deny the owner too**, and with no policies present that
denies `storage-api`'s own migrations, multipart reaping and deletion. The
service would not run. So the control is that the owning role is not
customer-reachable, not that the owner is filtered.

`service_role` bypassing storage policies is ADR-041's finding in a new place
and is upstream's behaviour rather than a MaluDB choice: a role named in a
request selects a credential, never a permission boundary.

**Consequences.**

- **Hardening is a function behind an event trigger, not statements.**
  `maludb_platform.harden_storage_schema()` re-applies the schema grant,
  enables RLS on any table lacking it, and revokes the surface Phase 10 does
  not expose. It must be, because bootstrap runs at provisioning and the tables
  appear when the worker first serves the tenant and again on every upgrade —
  a one-shot revoke would harden an empty schema and be recorded as applied
  forever, which is bootstrap 003's mistake and the reason 005 exists.
- **Upstream's grant-only migrations are not covered by that trigger**, by
  design: a hardening function that issues `GRANT` and `REVOKE` should not fire
  on `GRANT` and `REVOKE`. The worker calls it explicitly after migrating.
- **A customer cannot author a storage policy.** `CREATE POLICY` requires
  ownership of `storage.objects`; privileges are not enough. Supabase's
  dashboard can because its `postgres` role is a member of
  `supabase_storage_admin`. Enforcement works here and authoring does not
  exist, which is a real compatibility gap, recorded as
  `storage_policy_authoring` in `specs/compatibility-matrix.yaml`. The MaluDB
  analogue would grant `mldb_<ref>_admin` membership in the storage role — 
  owner-level bypass of every storage policy plus write access to metadata the
  object store is kept consistent with — so it is a decision for the slice that
  serves the Storage API rather than a consequence of this one.
- **The storage role is not a read barrier against the tenant's own tables.**
  It holds nothing on `public` itself, but it can switch into `anon`,
  `authenticated` and `service_role`, which bootstrap 004 grants
  `ALL ON ALL TABLES IN SCHEMA public`. Its reach through a role switch is
  PostgREST's authenticator's reach. Stated because the first draft of this
  slice claimed otherwise.
- **Two migrations grant to a hard-coded role name.** 0046 grants `ALL ... WITH
  GRANT OPTION` on `buckets` and `objects` to `storage.super_user`, and 0049
  does the same to the literal `postgres` where that role exists. On a MaluDB
  node `postgres` is the platform owner and a superuser, so nothing is
  conferred; a deployment that gives that name to something else inherits a
  grant it did not choose.

**Revisit if** upstream stops setting `search_path` as the mechanism for
migration 0011's unqualified function, if a future migration requires a
privilege the constrained owner does not have, or when slice 4 decides how a
customer authors a storage policy.

## ADR-060 — A hit storage ceiling answers 429 for egress and 413 for held bytes, and never refuses a read

Status: Accepted

Decided 2026-08-25, Phase 10 slice 4, by the repository owner. Applies ADR-050
and ADR-056 to the wire. Supersedes nothing.

**Context.** Slice 2 added `object_storage_bytes` and `egress_bytes_per_month`,
measured both, recorded `ok`/`warning`/`exceeded`, and deliberately refused
nothing — enforcement was left to the slice that serves the API. That left an
unanswered question, and it is a compatibility question rather than an internal
one: **what a customer's client sees when a ceiling is reached.** The official
Supabase client has error paths for the statuses upstream `storage-api` returns
and a generic branch for everything else, and `specs/compatibility-matrix.yaml`
is where the platform is obliged to say which it produces.

**Decision.** Three parts.

**Egress over the monthly ceiling answers `429` with `Retry-After`** set to the
number of seconds until the UTC month turns, and a body naming bytes used,
bytes allowed, and the reset date. 429 is what this gateway already returns
when a project has spent an allowance that refills, and an egress ceiling is
exactly that — it is not a permanent condition and the client's correct
response is to come back later. `Retry-After` is computed from
`object_storage.next_period_start`, beside the function that decides what a
period is, because a `Retry-After` measured against a different calendar than
the counter resets on is a client told to return too early, forever.

**A project over its held-bytes ceiling answers `413` to uploads.** Upstream
returns 413 for its own per-file size limit, so this is a status the official
client already handles on exactly this call; a customer's existing error branch
catches it. The body distinguishes the two by naming the *project's* usage
rather than the file's.

**Reads are never refused for a full project, and neither are deletes.** A
customer over their storage ceiling has two ways back under it: see what they
have, and remove some of it. Refusing the whole surface takes both away and
leaves a paying customer with no action that helps — the churn ADR-050 names as
the actual cost of a badly-shaped limit. Only `POST`, `PUT` and `PATCH` are
refused.

**Consequences.**

- The refusals carry a way forward in words, not only a status: how much of how
  much, and either the reset date or "delete objects or upgrade the plan". A
  ceiling that says only "no" is the failure ADR-050 warns about.
- The held-bytes refusal reads `projects.object_exceeded_at`, which slice 2's
  maintenance pass writes. The gateway does not measure — measuring means
  reaching the object store, and this is the request path. The lag is one
  maintenance interval and it is in the customer's favour.
- Both statuses go in the compatibility matrix as MaluDB behaviour with no
  Supabase equivalent, because Supabase meters and bills these rather than
  refusing them.
- A ceiling of zero refuses everything, by `object_storage.classify`'s existing
  rule that a non-positive quota is exceeded rather than unlimited. That is a
  misconfiguration failing closed, and it fails closed on a customer, so a plan
  with a missing number is a plan that must not ship.

**Revisit if** overage billing ever exists (ADR-050 says it does not), or if
upstream changes what it returns for its own size limit — the 413 is chosen to
match it, and a divergence there would make the two indistinguishable to a
client for the wrong reason.

## ADR-061 — Customers still cannot author storage policies, and the storage role stays out of reach

Status: Accepted

Decided 2026-08-25, Phase 10 slice 4. Closes the question ADR-059 and
`specs/compatibility-matrix.yaml` both deferred to "the slice that serves the
Storage API". Supersedes nothing; it decides a deferral rather than lifting one.

**Context.** On Supabase a customer writes their own RLS policies on
`storage.objects` — the dashboard's policy editor runs `CREATE POLICY`, and it
works because Supabase's `postgres` role is a member of
`supabase_storage_admin`, which owns the table. `CREATE POLICY` requires
ownership; privileges are not enough. Here the owner is `mldb_<ref>_storage` and
no customer-reachable role is a member of it, so the customer cannot author one.
Slice 4 was named as the place to decide whether that changes.

**Decision.** It does not change. `mldb_<ref>_admin` is **not** granted
membership in `mldb_<ref>_storage`, and the gap stays recorded as
`storage_policy_authoring`.

**Why.** The membership is not a small grant with a policy-shaped hole in it. A
member of the owning role is the owner for these purposes: it bypasses every
policy on `storage.objects` — including ones written by the customer, so the
feature would undermine itself — and gains write access to the metadata rows the
object store is kept consistent with. Object bytes live outside the database
(ADR-055) and the metadata is what says which bytes belong to whom; a customer
able to rewrite it can point a row at another project's prefix. That is a
cross-tenant path, which is a different category of thing from a missing
convenience.

Enforcement is not what is missing. `tests/test_object_storage.py` shows a
policy on `storage.objects` gating a switched role today, and slice 5 asserts it
through the official client. What is missing is authoring, and the shape that
gives it safely is a platform-mediated surface that validates what it creates —
the same shape ADR-045 chose for extensions and ADR-039 for privileged SQL. That
is a slice, not a line in this one.

**Consequences.**

- A customer migrating from Supabase with storage policies must have them
  applied by the platform. `specs/compatibility-matrix.yaml` says so rather than
  leaving it to be discovered, and slice 6's migration path inherits the
  constraint.
- Public buckets are unaffected, which is what most Supabase Storage users
  actually rely on: `getPublicUrl` needs no policy at all.
- The entry stays `deferred` rather than `intentional_incompatibility`. The
  first says "not yet"; the second would claim MaluDB had decided customers
  should never author policies, which is not what was decided here.

**Revisit if** a mediated policy surface is scoped, or if upstream moves storage
policy enforcement off table ownership — the constraint here is PostgreSQL's
rule for `CREATE POLICY`, not upstream's design.

## ADR-062 — Storage authorises an opaque key by minting a role token, and a signed URL needs no key

Status: Accepted

Decided 2026-08-25, Phase 10 slice 5. Amends how the gateway builds the
`Authorization` it forwards, for the Storage surface only. ADR-028 (opaque keys)
and ADR-037 (the two surfaces) are unchanged; this is about the translation
between them.

**Context.** Slice 4 gave `/storage/v1` its branch in the gateway and reused the
Data API's rule for what upstream sees. Slice 5 pointed the official
`@supabase/supabase-js` client at it, which is what `AGENTS.md` asks for, and
two things broke that nothing in the Python suite could have shown — because a
hand-written client sends what its author assumed, and the assumption was the
bug.

1. `supabase-js` presents the project key as `Authorization: Bearer <key>` on
   every request. MaluDB keys are opaque, so the gateway dropped the header;
   for PostgREST that is right, because the *absence* of a token is what selects
   `db-anon-role`. `storage-api` has no such fallback. It reads the bearer,
   fails `verifyJWT` on an empty one, and answers 403 before consulting any
   policy (`dist/http/plugins/jwt.js`). Every anonymous Storage call failed —
   the whole free-tier surface, and every signed-out visitor of a paid one.
2. `createSignedUrl` returns a link carrying a `token` query parameter and no
   `apikey`, because the point of it is that the link works on its own: mailed,
   pasted, put behind an `<img>`. The gateway required a key and answered 401.
   A signed URL that needs an API key to follow is not a signed URL.

**Decision.** Two changes, both narrow.

- On the Storage surface, a publishable key with no end-user JWT is translated
  into a short-lived **`anon`** token rather than dropped, exactly as a secret
  key is already translated into a `service_role` one. The role is chosen from
  the key type the gateway authenticated and never from anything the request
  carries. An end-user JWT still passes through untouched.
- `GET /storage/v1/object/sign/` joins `/storage/v1/object/public/` as a path
  reachable with no API key, read-only, and subject to the same dot-segment
  refusal.

**Why the first is not a loosening.** `anon` is the least-privileged role in a
tenant, and minting it grants nothing that holding the publishable key did not
already grant — it is the same caller, named. It is also what makes bootstrap
012's model real rather than theoretical: that file grants `anon` `USAGE` on
`storage`, enables RLS on every table upstream creates, and leaves the decision
to policy. A policy can only decide about a role that arrives. On Supabase the
anon key *is* a `role: anon` JWT, so this restores upstream's semantics rather
than inventing MaluDB ones.

The alternative — handing customers a signed anon JWT as their key — is ADR-028
reversed, and was rejected there for the reason that still holds: a JWT is valid
until it expires whatever the platform later decides, while an opaque key is
revocable and is checked against the project on every request.

**Why the second is not an open door.** The route carries its own
authorisation. Upstream registers it in the group with no JWT plugin at all and
verifies the `token` against the tenant's signing secret, checking that the
signature covers the very path being requested
(`dist/http/routes/object/getSignedObject.js`). The project minted the link;
holding one is the permission. The gateway still resolves the tenant from the
hostname, so a link signed for one project is refused at another — asserted
through the client.

`PUT /object/upload/sign/` — upstream's signed *upload*, in the same
unauthenticated group — stays closed. It is a write reachable with no API key,
which is a decision of its own rather than a consequence of how prefixes are
spelled, and it is recorded as `signed_upload_urls` rather than arriving by
omission.

**Consequences.**

- The gateway decrypts a project's signing secret on the publishable path too,
  where before only the secret path needed it. It is cached per process, so the
  cost is one decrypt per project rather than per request.
- Anonymous Storage requests now reach the tenant database as `anon` and are
  governed by RLS. A project whose `storage.objects` carried broad grants with
  RLS *off* would be world-readable to anyone holding its publishable key —
  which is exactly the failure bootstrap 012's hardening exists to prevent, and
  it is now load-bearing rather than defensive.
- Bytes served through a signed URL are counted against
  `egress_bytes_per_month` like every other Storage response. An unauthenticated
  path that was not metered would be ADR-056's ceiling with a hole in it.
- Both refusals were reachable only through the official client. The lesson is
  the one `AGENTS.md` already states, and it is recorded here because slice 4
  passed a full Python suite with both bugs in it.

**Revisit if** a mediated surface for signed uploads is scoped, or if upstream
gives `storage-api` a configurable anonymous role — the minting exists because
it has none, not because MaluDB wants to issue tokens.

## ADR-063 — Storage migrates with the customer's own two keys, and object bytes never touch the control plane

Status: Accepted

Decided 2026-08-25, Phase 10 slice 6. Extends ADR-042 to the Storage half of a
migration and decides the question ADR-042 did not have to: what writes to the
*destination* when what is being written is not SQL.

**Context.** Everything the migrator has carried so far is rows. Schema, data
and Auth all reach the destination through the control plane —
`POST /v1/projects/{ref}/sql` for the first two and the mediated
`/auth/import` route for the third — authenticated with the customer's platform
token. Storage is the first thing that is not rows. Buckets and the object list
are in the source's database, but the object *bytes* are in Supabase's object
store, and MaluDB's are in the node's. Neither is reachable with a platform
token.

**Decision.** Three credentials, all the customer's, none minted by the tool.

- `MALUDB_SOURCE_STORAGE_URL` and `MALUDB_SOURCE_SERVICE_KEY` read Supabase's
  Storage API. A service-role key is what can read every object past every
  policy, which is exactly what a migration needs and nothing less will do.
- `MALUDB_PROJECT_KEY` — **the destination project's own secret key** — writes
  to `<api_url>/storage/v1` through the gateway.

All three are environment-only. There is no `--service-key` or `--project-key`
argument, unlike `--source-dsn`, which exists for scripting and says so: a DSN
is scoped to one database, while either of these is an entire project, and
neither belongs in `ps` output or shell history.

**Why not a key the tool mints.** The CLI holds a platform token and
`POST /v1/projects/{ref}/api-keys` would issue a secret key with it. That is
convenient and wrong. Creating a key is *"closer to adding an owner than to
reading a setting"* — the route's own words — and a migration that quietly
issues one leaves a live credential behind whenever it fails partway, which is
precisely when nobody is looking. Asking the customer for a key they already
have costs one line of setup and creates nothing.

**Why not a control-plane route.** `/auth/import` exists because the console
role cannot write `auth.users` at all. Storage has no such problem: the Storage
API is public, the project's secret key authenticates it, and slice 5 proved the
whole path with the official client. Routing object bytes through the control
plane would make it proxy gigabytes it has no reason to see, on a topology
(ADR-058) built so that object traffic never reaches it.

**Consequences.**

- **Bytes travel through the customer's machine**, source to laptop to
  destination. Slower than a server-side copy between two object stores, and the
  arrangement ADR-042 already chose for every other part of a migration.
- **Storage policies are not carried.** No customer-reachable role can author
  one (ADR-061), so there is nowhere to put them. The scanner reports them —
  which needed a new probe, because `_POLICIES` filters to the customer's own
  schemas and `storage` is one of Supabase's, so nothing saw them at all. The
  failure is closed rather than open: with no policy, RLS denies every role but
  `service_role`, so what breaks is the application's access and not the privacy
  of the files.
- **`owner_id` and the original timestamps are not preserved.** The Storage API
  has no way to accept them; an uploaded object is owned by the token that
  uploaded it and is created now. Writing them would mean writing
  `storage.objects` directly, which is the metadata the object store is kept
  consistent with — and ADR-061 is the decision not to hand that out.
- **Objects over the destination's upload ceiling are skipped**, named, and
  skipped *before* being downloaded. The tool cannot ask the destination what
  its ceiling is — nothing in the public API reports
  `storage_max_upload_bytes` — so it assumes the documented 50 MiB default and
  takes `--max-object-bytes` for a deployment that changed it. Getting it wrong
  costs a needless skip, never a needless transfer.
- **A run that lost files exits non-zero**, and `--receipt` writes every one of
  them to a sidecar file. These are the customer's own files: an exit code of 0
  is a script's permission to cut over, and twenty lines of scrolled-past
  stderr is not a record.

**Revisit if** a mediated policy surface is built (the first consequence stops
being true), or if the platform ever offers a server-side import that reads the
customer's object store directly — which needs the credential-custody question
in `docs/OPEN-QUESTIONS.md` answered first, exactly as a dashboard-driven
migration does.

## ADR-064 — A backup repository in the same failure domain as the data is not a backup

Status: Accepted

Decided 2026-08-27, Phase 11 slice 1. Proposed in
`plans/completed/phase-11-production-resilience.md` on 2026-08-26 and deliberately
left unratified until the owner decided, because a plan may not override this
file. Answers the "WAL archive target" bullet of `docs/OPEN-QUESTIONS.md`'s
`## Backups` section, whose *interface* half ADR-067 had already settled.

**Context.** Phase 10 put SeaweedFS on the existing Proxmox hardware (ADR-055),
with the exit stated at the time. pgBackRest speaks S3, the platform already
operates an S3 endpoint, and the path of least resistance is therefore to point
the backup repository at the object store already running beside the cluster.
That arrangement passes every functional test: backups complete, restores work,
`pgbackrest check` is green. It fails exactly once, and only in the case the
whole phase exists for — the loss that takes the hardware takes the backups
with it, and the platform discovers this at the moment it has nothing left to
discover it with.

**Decision.** A node's backup repository must not share a failure domain with
the node's data. In **production this is refused**: `backup.BackupReadiness`
reports a repository co-located with the data directory as a failure, so
`cp-manage node backup-check` exits non-zero and a node-build script stops.
Outside production it is **recorded and reported as a warning**, and the node is
still usable.

The split is not a compromise, it is what makes the control enforceable.
`scripts/backup-test-cluster.sh` puts the repository on the same filesystem as
the data directory on purpose — a measurement fixture needs both on one
development box — and a rule that refused it everywhere would have been turned
off inside a week, which is how a control becomes prose. `config.is_production`
already exists and is the same lever ADR-024 uses to disable the docs UI.

**Consequences.**

- **The check is shallow, deliberately, and in the conservative direction.** It
  compares path prefixes and `st_dev`, so it catches the default and the common
  mistake: a repository under `/var/lib/pgbackrest` on the host that holds
  `/var/lib/postgresql`. It cannot see an NFS mount backed by the same SAN, or
  an S3 endpoint served by the same Proxmox host as the cluster. A repository
  addressed by URI is reported as not co-located because this process cannot
  stat it — which is why this ADR is a decision with a runbook and not merely a
  function. **A green check is not proof the rule holds; a red one is proof it
  does not.**
- **Deploying this platform for real needs a second location.** That is a
  hardware and cost consequence, and it lands before the first production node
  rather than at the first incident.
- **It is not a multi-region product feature.** Cross-region replication as a
  customer-facing capability remains out of scope for Phase 11.
- **A repository that could not be inspected is reported as unexamined, not as
  passing.** pgBackRest runs on the node; a control plane elsewhere cannot see
  the repository at all. Reporting that silence as health is the failure this
  whole slice exists to prevent.

**Revisit if** the repository moves behind an interface that can describe its
own failure domain, or if the platform gains a second site and the check can be
strengthened from "not the same filesystem" to "not the same site".

## ADR-067 — pgBackRest is the backup tool, and ADR-031's reject does not have to move

Status: Accepted

Decided 2026-08-26, Phase 11 slice 0. Answers the first two bullets of
`docs/OPEN-QUESTIONS.md`'s `## Backups` section — physical backup technology,
and whether the platform's own security posture permits one at all.

**Context.** ADR-031 requires every node to carry `host replication all <cidr>
reject`, after a non-superuser holding `REPLICATION` took a 484 MB physical copy
of every database on a cluster. The obvious backup tools are built on
`pg_basebackup`, which is precisely what that line refuses. So the phase opened
on a question with two bad answers available: either the platform cannot take a
physical backup, or the control that stops one tenant reading every other
tenant's bytes has to be narrowed to let a backup through.

Neither is the answer. **pgBackRest takes a full backup of a cluster on which
`pg_basebackup` is refused for the superuser**, because it copies the data
directory between `pg_backup_start()` and `pg_backup_stop()` over an ordinary
libpq connection and opens no replication connection at all — `0` walsenders
during the backup. `archive-push` is the same shape. Measured both ways: on a
cluster built without the reject, `pg_basebackup` succeeds and produces a 39 MB
copy of every database, and pgBackRest is unaffected either way. Full detail and
the control experiment are in `specs/backup-restore-model.md`.

**Decision.** pgBackRest is the physical backup tool. ADR-031's reject stays
exactly as written, and no node configuration is relaxed to accommodate backup.

Barman and wal-g were not examined. That is a deliberate stop rather than an
oversight: the question that gates this phase is the ADR-031 interaction, the
first tool tried answered it, and a comparison between two tools that both work
would have spent the measurement budget on a preference.

**Consequences.**

- **Every scheduled backup passes `--start-fast`.** pgBackRest's default waits
  for the next *regular* checkpoint, and PostgreSQL skips timed checkpoints when
  no WAL has been written — so on an idle cluster there is no next checkpoint
  and the backup waits indefinitely. Measured: over 15 minutes at 0% CPU with
  `num_timed = 0` after forty minutes of uptime. This is the free tier's exact
  shape (ADR-022 rests free-tier economics on projects that sleep), so an
  untuned nightly backup of a quiet node hangs rather than fails, silently. The
  verification pass must therefore treat "no new backup" as a failure rather
  than waiting for one.
- **Retention is configured explicitly, both halves.** Unset,
  `repo1-retention-full` warns that the repository may run out of space and
  `repo1-retention-archive` leaves WAL to outlive every backup it belongs to.
- **Backup is a capacity term, not only a durability one.** A full backup of a
  219.7 MB cluster took 105.9 s at the default `process-max=1` and 46.9 s at 4,
  on six cores that the node's tenants are also using. Extrapolated to
  `DEFAULT_MAX_PROJECTS = 200` that is roughly 40 minutes, or 17 parallel.
- **Backing up a MaluDB node compresses unusually well** — 9.4:1 measured — and
  the reason is ADR-015: the same ~15 MB of `maludb_core` is in every tenant
  database, and identical bytes compress to nothing.

**Revisit if** a node ever needs a streaming replica or a standby-side backup.
Both use the physical replication protocol, and both would reopen ADR-031 in a
way this decision deliberately does not. High availability is a non-goal of
Phase 11 for that reason among others.

## ADR-068 — Recovery is sold as a window in time, and the node has to be able to keep it

Status: Accepted

Decided 2026-08-28, Phase 11 slice 3. Answers the last of
`docs/OPEN-QUESTIONS.md`'s `## Backups` bullets — paid retention and PITR tiers
— and decides the free-tier policy `docs/BACKUP-RECOVERY.md` had carried as
"TBD" since Phase 01.

**Context.** ADR-067 made node backups real and slice 2 made per-tenant restore
real, which left the product question: what does a plan actually buy. The
obvious answer — "paid tiers get longer retention" — does not survive contact
with how any of this is stored.

**A pgBackRest repository retains per stanza, and a stanza is a whole node.**
ADR-002 puts every tenant in its own database inside a shared cluster, so one
node's repository holds all 200 of them in one backup set. There is no setting
that keeps one tenant's bytes for thirty days and its neighbour's for seven, and
inventing one would mean a repository per tenant — which is a different
architecture, at 200x the metadata, to sell a difference nobody can see.

So retention cannot be a storage policy. **It is a promise**: how far back the
platform will honour a request. That reframing is the whole of this ADR, and it
has three consequences that a storage policy would not have had.

**Decision.**

1. **`backup_retention_days` and `pitr_window_hours` are plan entitlements**,
   resolved through `entitlements.py` with defaults a deployment overrides in
   `plans.config_json`. `AGENTS.md` forbids hard-coding production plan limits
   in application logic, and a recovery window is a plan limit in exactly the
   way a storage quota is. Shipping defaults: free 7 days and no PITR, starter
   14 days with a 7-day PITR window, production 30 days with a 30-day window.

2. **The free tier is backed up and gets no point in time.** Its bytes are in
   the node backup whether or not anyone sells them, and slice 0 measured what
   that costs: a tenant at the 24 MB floor is about 2.5 MB of repository after
   the measured 9.4:1 compression, because ADR-015 puts the same ~15 MB of
   `maludb_core` in every tenant database and identical bytes compress to
   nothing. A retention of zero would be a fiction told for a pricing reason.
   What free does not get is a second of its choosing, because that is the half
   with a marginal cost: PITR is paid for in archive — `archive_timeout` forces
   a segment per minute whether or not anyone is writing, and a tenant that is
   writing costs about 120 MB of archive an hour — and every request is a
   ~3-minute scratch-cluster restore of the whole node.

3. **A node must be able to keep the longest promise any offered plan makes**,
   and `cp-manage node backup-check` fails it otherwise. This is the check the
   reframing makes possible and it has a sharp edge: pgBackRest's
   `repo1-retention-full` is a **count of full backups** unless
   `repo1-retention-full-type=time` says otherwise, and a count cannot be
   compared with a window without knowing the backup schedule. So the check has
   three outcomes — kept, not kept, and **not checkable** — and the third is
   reported as a warning naming the option that would make it checkable. A count
   is not a failure; thirty nightly fulls is thirty days. Reporting an unchecked
   promise as checked would be.

4. **Two bounds are enforced on a restore, and the refusal says which one
   refused.** The policy bound is the plan's window. The physical bound is the
   repository's oldest backup, because recovery replays forward from a
   consistent point and a target before that has nothing to replay onto. They
   have opposite fixes — one is answered by changing a plan, the other by
   accepting that the data is gone — and a customer told to upgrade when the
   real answer is the second has been sold a recovery the platform cannot
   perform.

5. **The override is explicit and loud.** `cp-manage restore run
   --beyond-entitlement` restores past what a plan sold, and logs that it did.
   An incident is a real reason to, and a control that cannot be overridden
   during one is a control that gets deleted rather than used.

**Consequences.**

- **`pitr_window_hours` is clamped by `backup_retention_days` at the point of
  use, never silently repaired in storage.** A deployment can write 720 hours of
  PITR next to 7 days of retention; that pair is not a 30-day window, it is a
  7-day window and a misconfiguration. The shorter one is honoured — failing
  closed — and `cp-manage node backup-policy` prints the inconsistency, because
  a value quietly rewritten is one nobody fixes.

- **`backup_retention_days` is enforced against the node, not against the
  request.** A restore reaches either a named time or the newest backup, so
  there is no request that reaches an *old backup* for retention to bound; what
  it does bound is the PITR window above it and the node check below it. Stated
  here because a reader looking for a retention check on the restore path will
  not find one, and its absence is a decision rather than a gap.

- **A young repository is reported, not failed.** A node backed up for the first
  time this morning cannot reach back thirty days and is not thereby broken. The
  restore refuses on whichever bound is tighter; readiness says how deep the
  repository actually goes.

- **The free tier's restore is an operator action, not a product feature.**
  Nothing here exposes restore to customers; ADR-067's reasoning still holds
  that `CREATE EXTENSION maludb_core` requires superuser (ADR-015), so there is
  no non-superuser path that produces a working tenant database.

- **PITR covers the database and not the object bytes.** A restored tenant gets
  its `storage.objects` rows back at the target time; the bytes in the shared
  bucket (ADR-057) are present-day and unversioned. `docs/BACKUP-RECOVERY.md`
  states this in the customer-facing text rather than letting "point-in-time
  recovery" imply more than it covers. Reconciliation is Phase 11 slice 4.

**Revisit if** per-tenant repositories ever become cheap enough to make
retention a storage policy rather than a promise, or if object versioning
arrives and PITR can cover both data sets at one target.

## ADR-069 — Object durability is declared and checked; drift is reported and never collected

Status: Accepted

Decided 2026-08-28, Phase 11 slice 4. Closes the two things Phase 10 deferred
here in writing — `docs/STORAGE.md`'s "Notes for Phase 11" and
`docs/RESOURCE-GOVERNANCE.md`'s orphaned-bytes paragraph — and the object half
of ADR-068's honesty problem.

**Context.** A project is two data sets. ADR-002 puts its rows in a dedicated
database; ADR-057 puts its files in a shared platform bucket. Slices 1 to 3 gave
the first backups, a restore, and a window a customer is sold. The second had
none of that, and nothing had ever compared the two.

Slice 3 made the gap urgent rather than tidy. A point-in-time restore returns
`storage.objects` to a moment in the past while the bucket stays present-day, so
after the one operation this phase exists to provide, the two sets are
**guaranteed** to disagree: every object deleted since the target has a row and
no bytes, and every object uploaded since has bytes and no row.

**Decision.**

1. **Durability is declared, and checked where it can be.** `cp-manage storage
   durability` reads the store's replication factor and fails in production when
   the answer is one copy. Three outcomes, not two — replicated, single copy,
   and **undeclared** — because a managed S3 service exposes no endpoint that
   answers the question, and a platform that read silence as a fault would
   refuse every such service while one that read it as fine would be making the
   claim on the operator's behalf. The same shape as ADR-068's retention check,
   and the same production/development split as ADR-064.

2. **The reconciliation reports and deletes nothing.** There is no collection
   path in `services/control_plane/reconcile.py` at all, asserted against the
   parsed AST rather than by grepping the text. Both populations are
   consequences of a failure somewhere else, and the first thing to do with a
   discrepancy of unknown cause is look at it. Whatever eventually collects
   orphans is a decision with its own ADR, not a default in a pass that runs at
   three in the morning.

3. **Orphaned keys are aged; in-flight multipart uploads are not, and are never
   touched.** An upload writes bytes before it commits its row, so a key with no
   row that is minutes old is a healthy upload rather than a fault — orphans are
   reported past a threshold taken from `LastModified`. Multipart uploads have
   no such age: the S3 API specifies `Initiated` on a listing entry and this
   store returns only `Key` and `UploadId`, measured. So an upload abandoned
   last week cannot be told from one three seconds old, and aborting a live one
   destroys a customer's file mid-write.

**Consequences.**

- **The platform's object store currently keeps one copy**, and that is now
  stated rather than implied. A production deployment must set a replication
  factor; the free Apache core provides one, so a second copy costs disk rather
  than a licence. **Automatic repair does not**: SeaweedFS puts self-healing, EC
  repair and EC vacuum behind its Enterprise line, so a lost replica is replaced
  by an operator. That is a gap to plan around, not a reason to run at one copy.

- **Incomplete multipart uploads hold storage that nothing counts.** The
  store-side measurement lists objects and the quota reads metadata; neither
  sees them. Reported by the reconciliation, and named in
  `docs/RESOURCE-GOVERNANCE.md` next to the existing admission that the platform
  under-counts rather than over-charges.

- **A comparison that did not run records nothing.** `objects_dangling` and
  `objects_orphaned` stay NULL until a reconciliation completes; zero means
  checked and consistent. A pass that wrote zeroes for a store it could not read
  would file a clean bill of health it never established *and* sort that project
  to the back of the queue — so the one project the platform cannot see would be
  the one it stops looking at.

- **Object recovery is still not a thing this phase provides.** An overwrite
  replaces the key rather than accumulating versions (measured), so there is no
  version history in the bucket to recover from. Object PITR would require
  turning versioning on, which is a decision this phase does not take and which
  `docs/STORAGE.md` now states in customer-facing terms rather than leaving
  "point-in-time recovery" to imply.

**Revisit if** the object store starts returning `Initiated` on a multipart
listing — ageing abandoned uploads becomes possible and the decision never to
abort them should be re-examined — or if bucket versioning is turned on, which
would make object recovery to a point in time a question worth asking rather
than one with a known negative answer.

## ADR-070 — A control plane that cannot decrypt must refuse to start, and a dump without keys is not a backup

Status: Accepted

Decided 2026-08-28, Phase 11 slice 5. Answers `docs/OPEN-QUESTIONS.md`'s
break-glass question — which secrets are regenerable and which are
unrecoverable — and closes the gap the Phase 11 plan added to scope on the
grounds that it was "the single highest-consequence gap on the platform and it
is in no phase's scope bullets".

**Context.** Slices 1 to 4 make a *node* recoverable. Nothing made the thing
that administers nodes recoverable. ADR-023 keeps the KEK out of the
control-plane database on purpose — "a dump of it must be useless alone" — and
that correct property has a consequence nobody had written down: **a backup of
the control-plane database is not a backup of the control plane.** Recovery
needs two artefacts, stored separately by design, and both must survive.

**The failure, measured.** It takes two steps, which is why it had gone
unnoticed.

The schema defends itself once: `nodes.admin_key_version` and
`project_credentials.key_version` are foreign keys into `encryption_keys`, so a
dump taken with `--exclude-table-data=encryption_keys` — which exits 0 — cannot
restore those constraints, and psql prints an ERROR for each.

But `ON_ERROR_STOP` is opt-in. A restore that does not set it, which is the
default and what any `psql -f dump.sql` in a script does, prints those errors
and carries on. The result is a database holding every secret, missing the
foreign keys that objected, and containing no keys at all.

Then the control plane started, and that is where it became unrecoverable.
`KeyRing.load` found no keys, minted a fresh version 1, marked it active, and
returned **successfully**. The service was healthy by every check it had. Every
ciphertext was permanently undecryptable, and the failure surfaced later and one
secret at a time as `ciphertext failed authentication`. The new key also occupied
version 1, so the real keys could no longer be re-imported without a collision —
**the silent success destroyed the recovery path as well as the data.**

**Decision.**

1. **Minting a first data encryption key is refused on any database that already
   holds ciphertext.** Creating one is correct on a virgin deployment and
   catastrophic on a restored one, and the two are indistinguishable from
   `encryption_keys` alone — both are empty. The discriminator is whether
   anything else in the database is already encrypted. The refusal names the
   columns it found, so an operator learns the blast radius from the error.

2. **A dump with no `encryption_keys` rows is not a backup**, and
   `cp-manage control-plane backup` exits non-zero rather than recording one. The
   check parses the dump's `COPY` blocks rather than trusting `pg_dump`'s exit
   code, because the whole failure mode is a dump that succeeded and is missing
   one table.

3. **A restore is verified by opening something, not by starting.**
   `cp-manage control-plane verify` loads the key ring and unwraps a sample of
   every kind of stored secret; `--reach-nodes` goes further and opens an actual
   connection with a recovered credential. Decrypting proves the key material
   survived; connecting proves the credential is still true, and those are
   different claims that the report keeps apart.

4. **The runbook requires `ON_ERROR_STOP=1`.** That turns the constraint errors
   above into a stopped restore at the point of failure. It is the first line and
   the guard in (1) is the second, because a runbook is a thing people follow
   under pressure at three in the morning.

**The break-glass answer.** If the KEK is lost, in ascending order of harm:

- **Regenerable, no customer impact.** Node admin DSNs (reset the role's
  password and re-record) and the object store's credential. Costs a touch of
  every node.
- **Regenerable, customer impact.** Publishable API keys — verification is
  Class A and independent, so the key still *works*; what is lost is the
  dashboard's ability to display it, and reissuing invalidates whatever is in a
  customer's deployed client bundle.
- **Customer-supplied, so the customer re-enters it.** SMTP credentials and auth
  hook secrets. Until they do, that project sends no email.
- **Not regenerable, and the one a customer notices.** Per-project JWT signing
  keys. Every access and refresh token ever issued to a tenant's end users stops
  verifying, so every end user of every project is signed out.
- **Unrecoverable.** Platform-user TOTP seeds. Every user with MFA must re-enrol,
  which needs an account-recovery path that does not itself depend on MFA — so
  this is the row that decides whether losing the KEK locks the operators out of
  their own dashboard.

**Consequences.**

- **`pg_dump`, not a physical backup, and the RPO is the dump interval.** The
  control plane is one small database on ordinary PostgreSQL rather than a node,
  so a physical backup would mean owning archiving and a stanza for a cluster
  whose whole content restores in seconds from a copyable file. What that gives
  up is point-in-time recovery between dumps, and `docs/BACKUP-RECOVERY.md`
  states the cost rather than leaving it to be discovered.

- **The dump is written 0600.** It carries every node's admin DSN. Ciphertext,
  and useless without the KEK by design — but "useless without a second
  artefact" is a reason for care, not for a world-readable file.

- **Two lists must agree**: the columns `crypto` checks before refusing, and the
  columns `recovery` classifies for break-glass. A test asserts they do, so
  whoever adds the next encrypted column is told rather than trusted.

- **This does not decide where the KEK lives.** That remains
  `docs/OPEN-QUESTIONS.md`'s load-bearing open question and a production
  deployment decision. What changes is that the *dependency* is now explicit and
  checkable rather than implied.

**Revisit if** the control-plane database grows to a size where a logical dump
stops being seconds, or if a KEK store is chosen that can itself be backed up
alongside the database — at which point "two artefacts" becomes an operational
procedure rather than a property to be careful about.

## ADR-065 — A project's node pool is a plan entitlement, and it ships switched off

Status: Accepted

Proposed 2026-08-26 in the Phase 11 plan; **ratified 2026-08-28 on the
repository owner's instruction**, implemented as Phase 11 slice 6. Recorded as
accepted rather than left as a proposal because the slice depends on it; if the
owner reverses it, the revert is this ADR plus one entitlement field.

**Context.** `nodes.node_pool` has existed since migration 0002,
`eligible_nodes` has always filtered on it, and `cp-manage node register
--node-pool` has always set it. What never existed was the policy that chooses
one. `api/projects.py` called `reserve_placement` without a pool argument, so
for eleven phases every project on the platform took the parameter default and
landed in `shared` regardless of what it paid for. The mechanism was plumbed and
nothing was connected to it.

**Decision.** The pool is a plan entitlement, resolved through
`entitlements.resolve` like every other per-plan value and overridable in
`plans.config_json`. `entitlements` is already the layer that answers "what does
this project get", and putting the choice anywhere else would create a second
place where plan names are hard-coded — which `AGENTS.md` forbids on its own
terms.

**Every tier ships entitled to `shared`.** The mechanism arrives inert: a
deployment that upgrades sees no change in placement. Separation is then an
explicit act — set `node_pool` on the plan and register nodes in that pool.

The alternative was to ship the production tier entitled to a `production` pool,
which would make the phase's fourth objective true by default. It would also
answer **503 to every paid signup** on any deployment that had not yet built
that pool, from the moment the change merged. A policy that takes paid signups
down on upgrade is one that gets reverted rather than configured, and the
objective is better served by a mechanism that works plus a report that says
loudly when it is switched off.

**There is no fallback to `shared` when the entitled pool is empty**, and this
is the part that matters. A pool exists so that a paying tenant is not
co-resident with the free tier; quietly placing that tenant in `shared` because
the intended pool was unavailable would put them exactly where the pool was
meant to keep them out of, while the platform reported success. That is a
control which claims to be applied and is not — the shape this phase has now
found four times. A project whose plan names an empty pool is **refused**, the
request rolls back, and a retry succeeds once the pool exists.

**Consequences.**

- **`cp-manage node pools` says when no separation is in effect.** The risk the
  safe default creates is not an outage, it is a false belief: a deployment can
  have the mechanism and no separation, and nothing about that state announces
  itself. The report announces it, lists each plan's entitled pool against the
  nodes actually in it, and exits non-zero when a plan is entitled to a pool
  that has no node — so it can gate a build script rather than only inform.

- **A plan change does not move a tenant.** A project whose plan is retargeted
  stays where it is; the report names it as misplaced and stops there. Moving a
  tenant is slice 7, and ADR-066 makes movement operator-initiated. Anything
  beyond refusing and reporting was explicitly out of this slice's scope.

- **A pool name is validated, not trusted.** It comes from operator-supplied
  `plans.config_json` and reaches a `WHERE node_pool = %s` comparison and a
  placement decision. An unusable value falls back to the plan's own default
  rather than to `shared`, which is the less surprising of two safe answers.

- **The refusal does not name the pool to the customer.** Which hardware a plan
  is entitled to is platform topology; the 503 says there is no capacity, and
  the operator-facing report says why.

**Revisit if** a deployment ever needs a project placed by something other than
its plan — a compliance region, a customer-specific node — at which point the
entitlement stops being sufficient and placement needs an input the plan does
not carry.

## ADR-066 — Tenant movement is operator-initiated, never automatic

Status: Accepted

Proposed 2026-08-26 in the Phase 11 plan; accepted for Phase 11 slice 7. The
deferred requirement in `docs/REQUIREMENTS.md` is **automatic cross-node tenant
migration**, and `docs/ARCHITECTURE.md` puts tenant movement before
rebalancing. This slice delivers the move operation and its runbook; it does
not deliver a scheduler that chooses to move customer data by itself.

**Context.** `draining` already exists as a node status, and placement already
refuses new projects on a draining node. Before slice 7, that is only a label:
projects already on the node remain there forever, and a drain never completes.
ADR-065 deliberately stops at reporting projects whose plan now entitles them
to another pool, because moving data is a materially different operation from
choosing where a new project starts.

Moving a tenant is not an entitlement reconciliation. It copies a database,
recreates cluster-scoped roles on the target, verifies ownership and privilege
shape, updates the control-plane placement row, and leaves the source to be
cleaned up only after the destination is known good. It also creates an
application-visible freeze window. The operator choosing that window is part of
the control.

**Decision.** Tenant movement is an explicit operator command. No maintenance
pass, plan change, billing reconciliation, capacity report, or pool report may
move a project on its own. Those paths may report that a project is misplaced or
that a node is draining; the act of moving it requires an operator-named source,
target and project.

The move operation preserves project identity: `project_ref`, hostnames, API
keys, subscription state, object-storage namespace and customer-visible URLs do
not change. Only the node placement changes after the target database has been
loaded and verified.

**Consequences.**

- **A drain is a runbook, not a background rebalance.** Draining a node prevents
  new placement. Emptying it is a series of explicit project moves, followed by
  a report that no active projects remain.

- **Reports do not repair.** `cp-manage node pools` and future capacity alerts
  name work for an operator; they do not perform it. This keeps an alert from
  becoming a data-moving control plane.

- **The freeze is measured and named.** This originally said Phase 08 had
  already built the write freeze used for cutover and that slice 7 would reuse
  its shape. That premise was wrong -- ADR-044 records that the platform
  *cannot* enforce a freeze against a system it does not run, so there was
  nothing to reuse. ADR-071 supersedes this bullet and specifies the mechanism.

- **Cleanup follows verification, and "cleanup" is a rename.** A source
  database or role is never removed merely because a dump or restore process
  exited. The target must have the tenant roles, data, ownership and
  control-plane identity expected by the move. Even then the source is
  *retained*, not dropped: it is renamed to `<db>_pre_move_<timestamp>` and
  left on the old node, so a move that turns out to have been wrong is undone
  by renaming it back and repointing one column. Reclaiming the disk is a
  separate, later, deliberate act -- the same choice `restore.activate` makes.

- **The two nodes are proved distinct physically, not just in the schema.**
  The `nodes` rows differing is a control-plane fact; two rows can address one
  cluster through a copy-pasted DSN or a node re-registered under a new name.
  A move compares `pg_control_system()` system identifiers before it freezes
  anything, because loading a tenant's dump over that tenant's own live
  database is the one failure in this operation with no recovery.

**Revisit if** automatic rebalancing becomes a product requirement. That change
needs a separate policy for when movement is allowed, how customer-visible
freeze windows are scheduled, and what happens when a background move fails
halfway through.

## ADR-071 — A move freezes a tenant by taking its CONNECT away, not its privileges

Status: Accepted

Decided 2026-09-09, Phase 11 slice 7. **Supersedes the Phase 11 plan's
instruction to reuse ADR-044's freeze**, which turned out not to be a thing that
exists.

**Context, and the plan's mistaken premise.** The phase plan says to "reuse
ADR-044's measured write freeze rather than inventing a second freeze mechanism —
Phase 08 already built and measured one for cutover". ADR-044 says the opposite
in its own text:

> **The platform cannot enforce the freeze**, and the runbook must say so
> plainly. The source is Supabase; stopping writes to it is the customer's
> action, in their own application.

Phase 08 did not build a freeze. It recorded that the platform could not enforce
one against a system it did not run, and pushed the action to the customer. There
is nothing to reuse, and `AGENTS.md` requires documenting that conflict rather
than deviating silently — this ADR is that record.

A move is a different problem. **Both ends are inside the platform**, so a freeze
can be enforced here. ADR-044's limitation was about not controlling Supabase,
not about freezes being unenforceable in general.

**The mechanism that was rejected, and why.** The obvious candidate is ADR-040's
existing write restriction, `storage.restrict()`. It is unusable for a move:
`DELETE` and `TRUNCATE` stay open **by design**, so a customer over quota can
shrink out of it. Freezing a tenant that way and copying it would let rows vanish
mid-copy, producing a destination quietly missing data — which ADR-044 itself
calls "the worst failure this phase can have". Privilege revocation also leaves
DDL, sequence advancement and `SECURITY DEFINER` functions able to change state.

**The second mechanism that was rejected, by measurement.** `ALTER DATABASE ...
ALLOW_CONNECTIONS false` is a single atomic switch and looks ideal. Measured: it
blocks **superusers too**, so `pg_dump` cannot run against a database frozen that
way. The freeze would make the copy impossible.

**The third, which shipped first and froze nothing.** The initial slice 7
implementation froze by calling `set_direct_sql_access(enabled=False)`. That
reaches only the roles holding direct SQL access — so for a project that never
had it, which is every free project by ADR-039 and the default for paid ones,
the "freeze" was a no-op and the copy ran against a live, writable tenant.

**Decision.** A move freezes a tenant by **revoking `CONNECT` on its database
from the tenant roles and `PUBLIC`, then terminating their backends.** The
platform owner keeps `CONNECT`, so the dump proceeds.

No connection means no write of any kind — no `DELETE`, no `TRUNCATE`, no DDL, no
sequence bump, no `SECURITY DEFINER` side effect. It is one mechanism covering
every path, rather than an enumeration of privileges that has to stay complete.

**Consequences.**

- **The freeze is visible to the customer as connection refusal**, not as a
  read-only period. A move is downtime for that project and the runbook says so;
  the window is measured rather than promised away, which is the half of ADR-044
  that does carry over.
- **The workers are stopped first.** They hold connections, and a terminated
  backend from a supervised worker is a worker that reconnects. Order matters:
  stop supervision, then revoke, then terminate whatever is left. `begin`
  refuses a project whose workers are still running, so this is enforced rather
  than merely documented.
- **Releasing is not the inverse of one statement.** `CONNECT` was granted to
  specific roles at provisioning time; the release restores exactly those grants
  rather than issuing a blanket `GRANT ... TO PUBLIC`, which would leave the
  tenant more open than the move found it. That is why `Freeze` records what it
  took before taking it.
- **A failed move unfreezes itself, and this is safe for a stated reason.**
  An earlier draft of this ADR said a failed move should leave the tenant frozen
  until an operator decided the move was abandoned. That is wrong here: every
  failure path in `move_tenant` is raised *before* the repointing `UPDATE`, so
  the destination has never served traffic and the source is still the only live
  copy. Leaving it frozen would take a tenant offline to recover from a move
  that changed nothing. The freeze is held only for as long as the copy.
- **The exception is a release that itself fails**, which strands the tenant.
  That is recorded as `tenant_moves.still_frozen`, reported by name in the
  command's output, and fixed with `cp-manage node release-freeze`, which reads
  what to give back from the catalogue rather than from the move record and so
  is safe to run twice.

**Revisit if** logical replication between nodes is ever built, which would
replace the freeze with a catch-up window and change what "moving a tenant"
costs a customer.

## ADR-072 — A node holds the keys to the fleet, and that is not what any earlier decision said

Status: **Accepted** 2026-09-09 by the repository owner. **All three points
implemented**; point 2 landed 2026-09-10.

Point 2's mechanism is worth stating here because the obvious version of it is
not a control at all. The row policies resolve `current_user` through
`nodes.gateway_role` — not a `MALUDB_GATEWAY_NODE` setting and not a session
variable, because the threat this decision names is a *compromised gateway*, and
a compromised gateway sets an environment value or a GUC to whatever it likes.
The role a connection authenticated as is the one thing the process cannot
restate.

Three consequences of that shape, all deliberate:

- **An unmapped role sees nothing.** `gateway_node_id()` returns NULL, `node_id
  = NULL` is never true, and the gateway serves no project rather than every
  project. `cp-manage deploy preflight` and the gateway's own startup check both
  say so, because from outside it looks like a healthy process losing every
  tenant.
- **The control plane is exempt** by owning the tables, which PostgreSQL honours
  unless `FORCE ROW LEVEL SECURITY` is set. It is deliberately not set, and a
  test asserts the owner still sees every row — a stray FORCE would break
  provisioning, billing and every maintenance pass at once, silently, as rows
  that stopped existing.
- **`gateway_node_id()` pins its `search_path` with `pg_temp` named last and
  qualifies `public.nodes`.** PostgreSQL searches the temporary schema before
  the rest of the path unless pg_temp is listed explicitly, so an unqualified
  reference would let the gateway create a temp table called `nodes` and name
  whichever node it liked. Measured rather than reasoned about: an unpinned twin
  of the function returned an attacker-chosen id.

Raised 2026-09-09 while planning `tasks/DEPLOYMENT.md`. Writing systemd units
meant answering "what is a node trusted with", and the answer turned out not to
be the one the architecture documents assume.

**The finding.** The gateway is an internet-facing process
(`uvicorn.run(build(), host="0.0.0.0", ...)`, annotated in its own source as
"the public listener"). It runs on a node, because it proxies to
`http://127.0.0.1:{port}` and drives systemd to wake sleeping workers. And at
startup it does two things:

```python
db.init_pool(settings.database_url)      # the control-plane database
key_ring = crypto.KeyRing(settings.kek)  # the KEK
key_ring.load(conn)                      # every DEK, not one
```

`settings.database_url` is a single field: there is no gateway-specific
connection string, so the gateway holds the **control plane's own database
credentials**. DEKs are global and versioned — `encryption_keys` — not
per-project and not per-node.

Those two facts compose into something neither of them says alone.
`nodes.admin_dsn()` needs a control-plane connection, a loaded key ring, a
`node_id`, and an AAD of `aad_for("nodes", "admin_ciphertext", str(node_id))`.
The gateway has the first two, can read the third from the `nodes` table, and
the fourth is derived from the first three. **So the gateway process on any node
can recover the PostgreSQL superuser DSN of every node in the fleet**, and with
it every tenant database on every machine.

The same composition yields every project's credentials platform-wide, not only
those of the tenants living on that node.

**Why this was not caught.** ADR-038 is precisely about this hazard. It moved
provisioning into a worker so that "the internet-facing application never holds
node admin credentials", and it does not merely assert that — it is enforced by
`tests/test_control_plane_surfaces.py`, which walks the import graph from every
public router and fails on `FORBIDDEN_CALLS = {"admin_dsn", "provision"}`.

That test guards *the control plane's public application*. The gateway is a
second internet-facing application, built later, and nothing equivalent guards
it. ADR-038's own text is careful about the limits of what it enforces — "the
property a test can assert is narrower than the property intended" — but the
gap it anticipated was reflection and future routes inside one process, not a
whole second process on the other side of the fleet.

`services/gateway/main.py` states the intended boundary in its docstring:

> They sit on different sides of a trust boundary: the control plane
> authenticates platform users and holds the KEK, while this listens to the
> public internet.

The code in that same file does the opposite. The comment is not stale
documentation to be corrected; it is the design, and the deviation from it was
never decided.

**Why the KEK cannot simply be removed from the gateway.** It is load-bearing.
Waking a sleeping project means decrypting that project's database password to
start PostgREST or GoTrue, and verifying a tenant's JWT means its `jwt_signing`
key (`app.py:690, 748, 760, 771, 1106`). The first is a cold-start path and could
tolerate a round trip; the second is per request and could not. A gateway with
no access to per-project secrets is a different gateway.

**Decision (proposed).** Keep the KEK on the node, and remove the fleet from its
reach by narrowing what the node's database credentials can see. Specifically:

1. The gateway gets **its own PostgreSQL role** on the control-plane database,
   not the control plane's. That role has **no `SELECT` on `nodes.admin_ciphertext`,
   `nodes.admin_nonce`, `nodes.admin_key_version`** — so `admin_dsn()` cannot be
   completed regardless of what the process holds in memory.
2. That role sees **only rows for projects placed on its own node**, through a
   view or row-level security keyed on `node_id`. A gateway then decrypts the
   credentials of tenants whose data is already on its disk, and nothing else.
3. `tests/test_control_plane_surfaces.py` grows a second assertion covering
   `services/gateway/`, so this is enforced the way ADR-038 is rather than
   documented the way its docstring was.

This is chosen over the two alternatives because of what each costs.

**Accepting it as-is** — writing down that a node is as trusted as the control
plane — is cheap and honest, and it is the wrong trade for a platform whose
product is isolation. One compromised public listener would yield superuser on
every customer database the platform runs. That is not a residual risk to note;
it is the failure the rest of this architecture exists to prevent.

**Per-node key hierarchy** — so a node's key unwraps only its own projects — is
the strongest answer and much the largest. DEKs are global and versioned, and
`nodes.admin_key_version` and `project_credentials.key_version` are foreign keys
into `encryption_keys`; changing that reaches rotation, restore (ADR-070) and
the control-plane backup path. It is a phase, not a slice. The decision above
does not preclude it and removes most of its urgency.

**Consequences.**

- Deployment grows a database role and its grants, which `docs/DEPLOYMENT.md`
  must create and `cp-manage deploy preflight` must verify. A gateway
  accidentally configured with the control plane's DSN would work perfectly and
  silently restore the hazard, so the preflight checks the role, not the
  behaviour.
- The blast radius of a compromised node becomes "the tenants on that node",
  which is what an operator would already assume from the machine holding their
  data.
- `services/gateway/main.py`'s docstring becomes true.
- This does not make a node untrusted. It holds tenant data and a key that opens
  those tenants' credentials. The claim is only that it stops holding everyone
  else's.

**Revisit if** the gateway ever needs to route to another node — today it never
reads `nodes.internal_host` — because that is the change that would give it a
reason to read rows outside its own placement, and the narrowing above would
have to be reasoned through again rather than widened by reflex.

## ADR-073 — Placement admits on ceilings and orders on one ratio; there is no capacity score

Status: **Accepted** 2026-09-09 by the repository owner, closing Phase 11
slice 8. It records what `services/control_plane/nodes.py` has done
since Phase 05 rather than proposing a change, and it exists because writing
the answer to `docs/OPEN-QUESTIONS.md`'s "exact capacity score formula?" made
plain that the answer **narrows** `docs/RESOURCE-GOVERNANCE.md` §5 rather than
satisfying it. Silently answering an open question with less than the
requirement asked for is the deviation this file exists to prevent.

**Decision.** `nodes.eligible_nodes` admits a node on ceilings and then orders
the survivors on `utilisation`, which is `current_projects / max_projects` and
nothing else. No term is weighted against any other, and no composite score is
computed anywhere.

The ceilings, each of which refuses with a sentence naming its own numbers:
total projects, warm projects, projected connections against
`usable_connections`, free disk against `min_free_disk_bytes`, replication
slots for a project that asks for Realtime, and health freshness — a node whose
`last_health_at` is stale is not a candidate at all.

**Why gates rather than a score.** A gate can say "no connection headroom (96
projected of 87 usable)". A weighted score refuses arithmetically, and the
operator's next question — which term ran out — is exactly what the weighting
destroyed. The ordering is allowed to be crude because it runs *after*
admission: every node it ranks has already cleared every ceiling, so a bad
ranking picks a worse node, never an unsafe one.

**What this does not do, stated plainly.** `docs/RESOURCE-GOVERNANCE.md` §5
asks that capacity scoring consider CPU, memory, disk latency/IOPS, active
queries and recent saturation. **None of those are consulted.** `record_health`
stores whatever a node reports in `metrics_json`, and placement reads exactly
one key out of it, `free_disk_bytes`; the rest is recorded and ignored.
Connections are *projected* from each warm project's entitled pool size rather
than read from `pg_stat_activity`, so a node whose tenants are behaving
unusually is invisible to placement until the projection changes.

That is a narrowing of §5 and is recorded here as one. The judgement is that
the terms which are enforced are the ones ADR-022 measured as binding, and that
a scheduler weighing a CPU figure nobody has validated would be less honest
than one that refuses on counts it can defend.

**Reserves are per term, never a global percentage**, because the terms fail
differently: `reserved_connections` plus a fixed platform allowance of ten
connections so a full node can still be administered,
`realtime.PLATFORM_SLOT_ALLOWANCE` from the slot count, and a
`min_free_disk_bytes` floor which is also a restore precondition — restoring to
scratch needs room for a second copy of the cluster.

**The cap is configuration, per node, in `nodes.capacity_json`.** Defaults of
200 total and 20 warm projects are conservative guesses; a node that has
reported its real settings through `record_node_limits` is held to those. The
production numbers are still open in `docs/CAPACITY.md`, and this decision is
about the shape of the mechanism, not about them.

**Consequences.**

- `maintenance.check_capacity` warns at 80% of any ceiling
  (`CAPACITY_WARN_AT`) so a node is reported before it starts refusing
  customers. It reports and never repairs: ADR-066 makes movement
  operator-initiated.
- A node under real pressure that is nonetheless below every count-based
  ceiling will still accept projects. The mitigation is operational — the
  capacity pass and `cp-manage capacity report` — not automatic.
- `docs/RESOURCE-GOVERNANCE.md` §5 stays as written, as the target. This ADR is
  the record of the distance to it.

**Revisit if** a node is ever observed saturating a term that placement does
not model — CPU or IO being the likely ones — or if per-node hardware profiles
arrive and make `max_projects` a number worth trusting rather than a guard
rail. Either would justify feeding live metrics into admission as a further
gate. Neither justifies a weighted score, which would still refuse without
saying why.

## ADR-074 — Phase 12 leads with the data-model graph, as an opt-in surface reached through platform-owned RPC wrappers

Status: **Accepted** 2026-09-12 by the repository owner, deciding the five
questions below one at a time before any Phase 12 code. **Amends ADR-015**, and
says so in its own section rather than leaving the two to contradict each other.

**Context.** `tasks/PHASE-12-MALUDB-FEATURES.md` lists six candidate surfaces
"to be prioritized into a product decision" and three delivery mechanisms, and
`docs/OPEN-QUESTIONS.md` carried "exact memory features to expose first?", "SQL
surface?", "API/SDK surface?" and "compatibility interaction?" as open. Nothing
in Phase 12 could be planned until those were answered, because they decide what
the SQL, the API and the tenancy model even have to be.

What was measured before deciding, on `maludb_core` 0.104.0:

- `maludb_core.enable_memory_schema(name)` stacks fifteen versioned facade
  builders into a schema and returns **165 objects — 74 relations and 84
  functions — in 0.55 s, adding ~1 MB** to a database whose baseline is 23 MB.
- The data-model facades (`maludb_datamodel_refresh`, `maludb_datamodel_describe`)
  are built **per memory schema**, guarded by
  `maludb_core._memory_schema_assert_manageable`, and may read only that schema,
  `maludb_core` or `public`. They are `SECURITY DEFINER`.
- The gateway has **never opened a connection to a tenant database**. It proxies
  HTTP and WebSockets to PostgREST, GoTrue, Realtime and Storage, which hold those
  connections themselves.

### 1. The data-model graph leads

`maludb_datamodel_refresh` introspects a project's own schema — tables, columns,
primary and foreign keys, views and their dependencies, routines, triggers — into
MaluDB's graph, and `maludb_datamodel_describe` answers for one relation.

Chosen over the memory pipeline, vector search and the SVPOR knowledge graph
because it is the only candidate that needs **nothing but one memory schema per
project**. The memory pipeline is account-scoped through `current_account_id`,
so leading with it means deciding how a project maps onto MaluDB accounts — per
project, per end user — and building that before any customer-facing code.
Vector search is closest to what Supabase already offers through `pgvector`, so
it differentiates least, and it needs the `vector` 0.8.3/0.8.4 drift pinned
first. The knowledge graph asks customers to bring graph data before they see
value; it shares the data-model graph's import path, which makes it a cheap
second rather than a strong first.

### 2. Opt-in per project, enabled by the platform

A project turns the surface on; the platform runs `enable_memory_schema` into a
fixed, platform-owned schema. Customers are not granted a MaluDB role that could
call it themselves.

Cost did not decide this — ~1 MB and half a second is trivial either way. What
did is the **exposure and upgrade surface**. Always-on would put 84 functions,
several of them `SECURITY DEFINER`, into every tenant whether or not it ever
calls one, and make every extension upgrade rebuild facades fleet-wide. Opt-in
follows the Realtime precedent (`projects.realtime_enabled`) and the `AGENTS.md`
invariant that privileged SQL capabilities are allowlisted. Customer-created
schemas were rejected because they need a MaluDB admin-level role in customer
hands and make per-project version tracking guesswork.

### 3. Called through PostgREST, via platform wrappers; the gateway enforces opt-in

Platform-owned wrapper functions with stable names, in a dedicated schema added
to the project's PostgREST `db-schemas` when the surface is enabled, called the
way Supabase users already call functions:
`supabase.schema('maludb').rpc('datamodel_describe', { relation: 'orders' })`.

- **The raw facades are never exposed.** Publishing the memory schema itself
  would repeat ADR-018's finding on purpose: every facade an RPC endpoint, grants
  that must be exactly right on each, and customer code broken by any upgrade
  that changes a facade's signature. The wrappers are the contract; the facades
  are an implementation detail behind them, and each wrapper pins its own
  `search_path`.
- **`EXECUTE` goes to `service_role` only**, not to `anon` or `authenticated`.
  `describe` runs as its definer, so until it is measured it must be assumed able
  to reveal the structure of tables the caller cannot read. Widening the grant is
  a later decision that needs that measurement first.
- **The gateway answers for projects that have not opted in**: a request for the
  `maludb` schema on such a project gets a clear "MaluDB features are not enabled
  for this project" rather than PostgREST's generic schema error. That gives
  opt-in the clean behaviour a dedicated endpoint would have had, without the
  endpoint.

`/maludb/v1` gateway endpoints were considered seriously and rejected **for now**.
Their opt-in story is cleaner and per-feature metering is easier at the gateway,
but an endpoint needs something to run the SQL. Either the gateway starts querying
tenant databases — a capability the internet-facing process has never had, right
after ADR-072 narrowed it, and bringing its own pooling, timeouts and
cancellation — or it forwards to the control plane's mediated SQL path (ADR-039),
which is a routing design of its own. PostgREST already holds the connection,
enforces the JWT's role, and applies timeouts and the pool. A `/maludb/v1` layer
can be added over the same wrappers later; this does not close that door.

This **extends** the Supabase-compatible surface rather than altering it:
`public` is untouched, and a project that never enables the surface is
indistinguishable from one on a platform without it.

### 4. Every plan, with refresh limited per plan

An entitlement on every tier, with refresh frequency limited per plan through
`entitlements` — configuration, per the rule that production plan limits are
never hard-coded. Free is where developers evaluate the platform and this is the
differentiator, so gating it behind payment hides what should sell the platform.
The refresh limit contains the one real abuse vector: introspecting a very large
schema, repeatedly, on a shared node. `service_role`-only access means a
project's end users cannot trigger it at all; only the developer's own server
can.

### 5. Extension upgrades are operator-run: a canary, then batches

`cp-manage extension upgrade --node <name>` upgrades one canary tenant, verifies
it, then proceeds in batches, **stopping at the first failure**. A tenant that
fails stays on its previous version and is recorded against the project, rather
than being rolled forward half-done. Per-project versions are already recorded
(`projects.extension_versions` and `bootstrap_version`, migration 0005); what did
not exist was who triggers an upgrade and how it rolls out.

This matters more because of decision 3. Before Phase 12 an extension upgrade was
invisible to customers; once they call `maludb_core` through wrappers, it changes
an API they depend on. Automatic upgrades in the maintenance pass were rejected
as the data-changing control plane ADR-066 exists to prevent — a bad release
would reach every tenant before anyone looked. Per-project upgrades on customer
request were rejected because the fleet fragments across versions indefinitely
and security fixes wait on each customer.

### Amendment to ADR-015

ADR-015 says MaluDB capability "is a property of the platform, not an add-on a
project opts into, so there is no conditional path and no 'MaluDB-enabled'
project flag." Decision 2 adds exactly such a flag, so the two cannot both stand
as written.

**What still holds:** `maludb_core` is installed in every tenant database, with
no conditional path, and "Phase 12 can assume MaluDB functions are present in
every tenant database" remains true. **What changes:** ADR-015 was written about
the *extension*, before anyone had asked what exposing its features would cost.
The **customer-facing feature surface** built on it — an enabled memory schema
and the wrappers that publish it — is opt-in per project. The extension is
platform; the surface is a product.

### Deferred, because the data-model graph does not need them

- **How a project maps onto MaluDB accounts** (`current_account_id`). Required
  before the memory pipeline; not before this.
- **Dependency version pinning.** Required before vector search. The upgrade
  procedure keeps recording what each project actually has installed meanwhile.
  **Decided by ADR-075** (2026-09-12).
- **`auth_token_*` versus the control-plane `api_keys`**, and **`maludb-restd`**,
  which cannot be a public surface without TLS and JWT signature verification.

**Consequences.**

- A migration adds the per-project enablement flag, an entitlement for the
  surface, and a per-plan refresh limit.
- A project's PostgREST configuration becomes conditional on that flag. Whether
  adding a schema to `db-schemas` needs a worker restart or only a config reload
  is measured in Phase 12 slice 0, because a restart is a brief outage on the
  request that turns the feature on.
- **ADR-018's event trigger does not cover the facades.** It re-applies revokes
  on *extension-owned* functions; `enable_memory_schema` creates its 84 functions
  at runtime, so they are not extension members and carry PostgreSQL's default
  `EXECUTE` grant to `PUBLIC`. The memory schema is not in `db-schemas`, so
  PostgREST cannot reach them — but a paid project's direct connection might.
  Measured in slice 0 before anything is enabled for a customer.
- The compatibility matrix gains a MaluDB-extension section; nothing in the
  Supabase-compatible rows changes.

**Revisit if** slice 0 finds that `describe` does not disclose structure beyond
the caller's own privileges — that is what would justify granting `authenticated`
— or if per-feature metering becomes a billing requirement, which is the point at
which `/maludb/v1` stops being optional.

### Phase 12 slice 0 findings (2026-09-12) — decision 3 does not work as written

Measured against real bootstrapped tenants; `specs/maludb-datamodel-model.md`
has every figure and `scripts/spike-datamodel.py` reproduces them. Recorded here
because two of them contradict this ADR, and an accepted decision that is wrong
in its own text should say so where it is read.

- **Decision 3 is infeasible as written, and is open again pending the owner.**
  Both data-model facades call `_memory_schema_assert_manageable`, which checks
  `CREATE` on the memory schema for **`session_user`**, not `current_user`. A
  `SECURITY DEFINER` wrapper cannot change `session_user`, and PostgREST always
  logs in as the authenticator, so every wrapper call fails:
  `mldb_<ref>_authenticator lacks CREATE on schema maludb_memory`. Reading the
  refreshed graph's views directly fails too; they chain into `maludb_core`
  objects only MaluDB's own roles can read. The spec sets out four options.
  None is adopted by this note.
- **The consequence above about `PUBLIC` is wrong.** It says the facades "carry
  PostgreSQL's default `EXECUTE` grant to `PUBLIC`". `enable_memory_schema` writes
  explicit ACLs granting only MaluDB's own roles, and no customer role — `anon`,
  `authenticated`, `service_role`, the authenticator, admin, executor or client —
  reaches any of them, including through transitive membership.
- **The disclosure fear was right.** `describe` returns the full structure of a
  table the caller has no privilege on; only schema visibility limits it. So
  `service_role`-only access is the entire control, and the revisit condition
  above — granting `authenticated` — is met in the negative.
- **The facades run as the node superuser**: `SECURITY DEFINER`, owned by the
  role that installed the extension over provisioning's superuser connection.
- **PostgREST needs no restart.** `NOTIFY pgrst, 'reload config'` applies a
  changed `db-schemas` in under half a second, so the consequence about a restart
  outage does not arise.
- **`ALTER EXTENSION ... UPDATE` does not rebuild an enabled schema's facades**;
  re-running `enable_memory_schema` does, idempotently and without losing the
  graph. Decision 5's procedure has to include that step.

### Decision 3, as amended 2026-09-12 by the repository owner

Decided after the findings above, choosing option B of
`specs/maludb-datamodel-model.md` and then how a refresh is triggered. **This
replaces decision 3.** The original text stays above as the record of what was
decided before anyone had measured the extension.

**The platform refreshes; customers read a copy.**

- **A refresh is requested, not executed on the request.** `POST
  /v1/projects/{ref}/maludb/datamodel/refresh` on the control plane,
  authenticated like every other project operation — a personal access token or
  a session, the Supabase Management API pattern — enqueues a refresh. The
  dashboard's button calls the same route. **The per-plan limit is enforced
  there**, as an immediate 429, rather than as a queued row that silently never
  runs.
- **The platform runs it.** A worker claims the request and calls the facade over
  the node admin connection, where `session_user` passes the guard, then **copies
  the graph** — nodes, edges, and a `describe` for every relation — into ordinary
  platform-owned tables in the project's `maludb` schema.
- **Customers read the copy through the official client**, e.g.
  `supabase.schema('maludb').from('datamodel_relations').select()`, with `SELECT`
  granted to `service_role` only. Each copy records when it was refreshed.
- **No function is exposed at all.** The superuser-owned, `SECURITY DEFINER`
  facades are never reachable from a request; PostgREST serves plain tables. A
  later decision to let `authenticated` read part of the graph becomes a
  row-level-security question on ordinary tables, which is the only form in which
  `describe`'s disclosure could ever be narrowed — no grant on the facade can.
- **The gateway's opt-in check stands** for reads: a request for the `maludb`
  schema on a project that has not enabled the surface gets a clear error.

Why the Management API rather than a request row inserted through the official
client: discovering tenant-side requests means polling or listening on every
enabled tenant database, and connections are the node's binding constraint
(ADR-022); and an over-limit request could only be refused after the fact. A
schedule alone was rejected because a customer who has just run a migration
could not ask for a current graph.

**What this costs, knowingly.** `describe` answers as of the last refresh, not
live — the graph was already a snapshot, and every copied row says when it was
taken. A copy step has to be built. And the copy tables are the platform's own,
so re-running `enable_memory_schema` on upgrade, which drops and recreates the
facade's objects, cannot cascade into them.

**Revisit if** `maludb_core` changes its guard to check `current_user`. Live
wrappers over the facades then become possible, and whether they are worth their
exposure against a copy that already works becomes a question worth asking
again — not an automatic switch.

## ADR-075 — Extension versions are pinned per node, from a tested list, and a node that disagrees stops taking work

Status: **Accepted** 2026-09-12 by the repository owner, deciding the five
questions below one at a time. Answers the open question ADR-074 deferred —
"which dependency versions does provisioning pin?" — before vector search, the
first surface that depends on the answer. Supersedes nothing; extends ADR-074's
decision 5 to a second extension.

**Context.** `docs/MALUDB.md` recorded the drift: a database created at one time
has `vector` 0.8.3 and one created later has 0.8.4, because `CREATE EXTENSION`
installs whatever the OS package currently provides. What was measured before
deciding, on the development node (PostgreSQL 17.10, pgdg noble):

- **The library is per node; the extension version is per database.** Every
  tenant on a node loads the same `vector.so`. Upgrading the package changes the
  running code under every tenant at once, including a database whose
  `extversion` still says 0.8.3. A pin written only into provisioning would
  record one version while the node ran another.
- **An older version cannot be installed from the package.** The node carries
  one full install script for `vector` — `vector--0.8.4.sql` — and upgrade
  scripts for everything before it, so `CREATE EXTENSION vector VERSION '0.8.3'`
  fails. `maludb_core` ships full scripts back to 0.74.0, but against a single
  library built from one commit, so installing an older one is untested
  territory rather than a pin.
- **The drift continues unless something stops it.** No package is held; apt
  already offers `postgresql-17-pgvector` 0.8.5 and 0.8.6 and `postgresql-17`
  17.11. The next routine `apt upgrade` moves the whole node.
- **Nine of the eleven allowlisted extensions ship inside `postgresql-17`** —
  `pgcrypto`, `pg_trgm`, `btree_gist`, `uuid-ossp`, `citext`, `hstore`, `ltree`,
  `unaccent`, `btree_gin`, `tablefunc`. Only `vector` (`postgresql-17-pgvector`)
  and `maludb_core` (built from a pinned upstream commit, not apt) are packaged
  separately. CI takes pgvector from apt unpinned.

### 1. The pin controls what the node runs

The OS packages of pinned extensions are **held** on the node, provisioning
installs the pinned version **explicitly** — `CREATE EXTENSION vector VERSION
…`, then `maludb_core VERSION …`, rather than relying on `CASCADE` to pick
defaults — and a node whose installed packages disagree with its pin is refused
(decision 4).

Rejected: **pinning only in provisioning**, because of the first measurement — it
leaves the library free to move under every tenant, so the pin describes the SQL
objects and not the code. Rejected: **recording and alerting only**, which is
what exists today; it makes drift visible and prevents none of it.

The package hold is a node-build step, like `realtime-check`'s preconditions, and
the control plane cannot see `dpkg`. What it verifies is the outcome that
matters: the `default_version` PostgreSQL reports for each pinned extension, which
comes from the control file the same package installed alongside the library.

### 2. `vector` and `maludb_core` are pinned exactly; contrib follows the PostgreSQL minor

The two separately packaged extensions change independently of PostgreSQL and
are what vector search and every MaluDB surface depend on, so each has an exact
pin. The nine contrib extensions change only when PostgreSQL's minor version
does. The node's `server_version` and their versions are **recorded and
reported**, never held.

Rejected: **pinning the PostgreSQL minor too**. Fully identical nodes, at the
price of turning every PostgreSQL security release into a manifest change and a
fleet rollout — a pin that delays a CVE fix is a worse outcome than the drift it
prevents. Rejected: **`maludb_core` plus its four required extensions**, because
three of those four are contrib, so it is the previous option under another
name.

### 3. One procedure moves a node and its tenants together

`cp-manage extension upgrade` is extended from `maludb_core` to `vector`. Moving
a pinned extension is one procedure: change the node's pin, install the package,
then upgrade every tenant through the existing canary-then-batches run, one
transaction per tenant, stopping at the first failure. A tenant whose recorded
version lags its node's pin is **reported as drift** until the run reaches it;
it keeps serving, because pgvector's newer library runs older extension
versions.

Rejected: **lazy, per-feature updates**, which leave a node's tenants on mixed
versions indefinitely — the condition this ADR exists to end. Rejected: **new
projects only**, which is the same with no end date.

### 4. A node that disagrees with its pin stops taking work and keeps serving

A mismatch is a placement rejection, not a node status: the node takes **no new
projects, no restores and no moves in**, and its existing tenants keep serving.
The one operation allowed on it is the corrective one — the extension upgrade
run, once the pin and the package agree. A node with **no pin** reads as
mismatched, following `realtime_ready` and `backup_ready`: a node nobody has
checked refuses rather than accepting and failing later.

Provisioning also checks at the moment it installs, not only when a report last
ran: an `apt upgrade` between two checks would otherwise place a project on
exactly the version the check exists to refuse.

Rejected: **reusing `unhealthy`**. Same placement effect, but `unhealthy` also
refuses the extension upgrade command, so fixing a mismatch through the normal
procedure would need a manual state change first. Rejected: **warning only**,
which places new projects on whatever the package provides.

**What this costs, knowingly.** Refusing restores means a project on a
mismatched node cannot be recovered to a point in time until the mismatch is
fixed. That fix is a package install or a pin change, minutes rather than an
outage, and restoring a database into a library its extension version was not
tested against is its own way to lose data.

### 5. The repo lists what CI tested; each node's pin is a control-plane row

A file in `specs/` lists the versions of each pinned extension that CI has
tested — for `vector` the Debian package version, for `maludb_core` the upstream
commit and the `extversion` it builds. Changing it is a reviewed pull request.
Each node's pin is a row in the control plane, set through `cp-manage`, audited,
and **refused unless the version is on that list**. CI installs the newest listed
version of each, exactly, rather than whatever apt currently offers.

Why both: a single fleet-wide file cannot express a rollout, because nodes
upgrade one at a time and the moment the file changed every node not yet upgraded
would mismatch and stop placing. Pins held only in the database would roll out
cleanly, but nothing would tie a node's pin to a version anyone has tested, so a
typo or an untested release becomes a production pin.

**Consequences.**

- A migration adds per-node pins and an audit event for setting one; placement,
  moves and restores gain a rejection reason naming the pin and what the node
  actually provides.
- **A rollout changes the pin before the package**, so the node fails safe:
  between the two it refuses placement, and a node that never gets the package
  stays out of placement loudly rather than drifting quietly.
- **Existing deployments stop placing on upgrade until each node is pinned.**
  That is decision 4's "no pin reads as mismatched" applied honestly, and the
  rollout note must say so.
- `apt.postgresql.org` currently carries several versions of a package but does
  not keep them forever; a listed version that is pruned stays installable from
  `apt-archive.postgresql.org`, and one that is not is removed from the list.
- The pre-existing `maludb` database at `vector` 0.8.3 is not a tenant and is
  not in scope. What is in scope is that no two tenants on a pinned node can
  differ once the upgrade run has passed them.

**Revisit if** a PostgreSQL minor release breaks a contrib extension a customer
depends on — that is the case decision 2 bets against — or if the platform ever
needs two nodes in one pool on different pins for longer than a rollout.

### Pinning slice 0 findings (2026-09-12)

Measured in a disposable container against tenants the provisioning module
built; `specs/extension-pinning-model.md` has the figures. None contradicts a
decision above. Three sharpen its consequences:

- **The package is the upgrade.** Every `vector` script from 0.8.0 to 0.8.6 is
  empty, so a point release changes only `vector.so` — decision 1 would have been
  wrong to pin in provisioning even more plainly than it argued. And
  `ALTER EXTENSION vector UPDATE` blocked no concurrent reader or writer and
  rebuilt no index, so decision 3's one-transaction-per-tenant run stands.
- **A node is at its pin when its old sessions are gone, not when the package is
  installed.** A backend that loaded the library before `dpkg` replaced it keeps
  the old one mapped until it disconnects, and pooled workers hold connections
  for as long as they run. The node check reports such backends.
- **Moves and restores install the receiving node's version**, because a dump's
  `CREATE EXTENSION` carries none. Decision 4's refusal therefore extends to a
  move between nodes whose pins differ, and a pin may not move down on a node with
  tenants: a downgraded package leaves their catalogues ahead with no path back.

One is outside this decision and is recorded in `docs/OPEN-QUESTIONS.md`: ADR-018's
revoke from `PUBLIC` leaves **no customer role able to execute any extension
function in `public`** — `vector`'s operators, `similarity()`, `crypt()`. Vector
search cannot ship until that is decided.

## ADR-076 — Customer roles may execute extension functions; the RPC surface is closed by a pre-request check instead

Status: **Accepted** 2026-09-12 by the repository owner, deciding the five
questions below one at a time. **Supersedes part of ADR-018** — its first
bullet, the `EXECUTE` revoke — and the matching invariant in
`specs/tenant-role-model.md`; ADR-018's finding stays closed by a different
control. Answers the open question pinning slice 0 raised.

**Context.** Pinning slice 0 (`specs/extension-pinning-model.md`, finding 7)
found that **no customer role can execute any extension-owned function**. ADR-018
revoked `EXECUTE` from `PUBLIC`, `anon` and `authenticated` so that `anon` could
not call `/rpc/gen_salt`; every other role held `EXECUTE` only through `PUBLIC`,
and nothing grants it back. Bootstrap 011 later widened the revoke from `public`
to every schema. What was measured before deciding, on a tenant this platform
provisioned (`mldb_mvm00002`) and in the pinning spike:

- `public` carries **373 extension functions**: `btree_gist` 188, `vector` 118,
  `pgcrypto` 36, `pg_trgm` 31. `maludb_core` puts **none** there — its 544 are in
  `maludb_core` and `mc2db`. **None of the 373 is `SECURITY DEFINER` and none
  reads a table**: they are distance operators, hashes, salts and similarity.
- `service_role`, `mldb_<ref>_admin`, `_client` and `_executor` are refused as
  firmly as `anon`: `permission denied for function vector` on a `vector(64)`
  distance query, and `similarity()`, `crypt()`, `digest()` likewise.
- **`EXECUTE` is checked against whoever runs the statement**, including column
  defaults and trigger bodies. A table with `DEFAULT uuid_generate_v4()` or a
  trigger calling `crypt()` fails on insert as `authenticated`. Phase 08's
  migration test runs as superuser, which is why it passed.
- The trigger applies the same revoke to extensions a customer installs under
  ADR-045 — `uuid-ossp`, `citext`, `hstore` — so a `citext` column's own
  comparison operators are refused.
- Supabase installs extensions into an `extensions` schema, leaves `EXECUTE` with
  `PUBLIC`, and keeps them off the Data API because PostgREST does not expose that
  schema. ADR-018 records why that is unavailable: `maludb_core` hard-codes
  `public.gen_random_bytes`, so its dependencies cannot be relocated.

ADR-018 identified the exposure correctly — the RPC surface — and closed it with
the one control available on the day, which also removed the functions from every
use that is not RPC. The Supabase-compatibility rule in `AGENTS.md` does not
allow that trade to stand: pgvector search, `uuid-ossp` defaults and `pgcrypto`
in a migrated schema are ordinary Supabase applications, and they fail here.

### 1. Every customer role executes extension functions

`anon`, `authenticated`, `service_role`, and the tenant's `admin`, `client` and
`executor` roles hold `EXECUTE` on every extension-owned function, in every
schema — Supabase's behaviour, where all roles can run them. Defaults, triggers,
RLS policies and `match_documents`-style functions work for end users.

Rejected: **everyone except `anon`**, which leaves anonymous search and
anonymous inserts into a table with a `uuid` default broken — a common Supabase
pattern — while keeping a posture whose purpose (no RPC) decision 2 serves
directly. Rejected: **server-side roles only**, which leaves anything run through
PostgREST as an end user broken unless the customer wraps it in `SECURITY
DEFINER`.

### 2. A platform-owned PostgREST pre-request check refuses RPC to extension functions

PostgREST's `db-pre-request` runs a platform-owned function before every request.
It refuses `/rpc/<name>` when **every** function of that name in the exposed
schemas is extension-owned, reading the live catalogue. ADR-018's finding —
`anon` calling `gen_salt` over the Data API — stays closed; what changes is the
control.

- **It sees what a static list cannot**: extensions a customer installs later,
  and a customer's own function that shares a name with an extension's, which
  stays callable.
- **Its cost is a catalogue lookup on RPC requests**; other requests return at
  the first comparison.
- **It occupies PostgREST's single pre-request hook**, so a customer cannot set
  their own. Supabase customers occasionally do; that is a documented
  difference, not a silent one.

Rejected: **a gateway name blocklist**. No database cost, but the gateway cannot
see a tenant's catalogue, so a customer function named `similarity` or `digest`
is refused too and the list must follow every pin and every customer install.
Rejected: **accepting the exposure**, which is Supabase's behaviour for an
extension installed into `public` but deliberately reopens ADR-018's finding —
including `crypt()` with a high cost, callable anonymously.

### 3. The OpenAPI listing is measured before it is decided

Once `anon` holds `EXECUTE`, PostgREST's OpenAPI description may list extension
functions as RPC paths, which decision 2 then refuses. How many is not known:
most of the 373 take unnamed arguments, which PostgREST can call only in narrow
shapes, and ADR-018's examples are the callable kind. The first slice counts what
PostgREST 14.17 actually lists. **A handful is accepted**, matching a Supabase
project with `vector` in `public`; a large number comes back to the owner with
the figure.

A gateway filter was offered and withdrawn in the same conversation, because the
reason given for it was wrong: the gateway does **not** interpret response bodies
— its module docstring says so, and the one frame it rewrites is a client's
Realtime join. Filtering there would be a deliberate exception to that rule plus
a per-project name list, and is not decided by this ADR.

### 4. Explicit grants to named roles; the event trigger grants instead of revoking

`PUBLIC` stays revoked. `EXECUTE` goes to the six named roles, and
`maludb_harden_extensions` flips from revoking to **granting that exact set** on
every `CREATE EXTENSION` and `ALTER EXTENSION`. `tenant_bootstrap.verify` asserts
the exact grant set and that the pre-request function is configured, rather than
asserting that no role can execute.

Why named roles rather than `PUBLIC`: the grant stays something the platform
states and verifies, a customer-installed extension gets the same posture by
construction, and cluster roles outside the tenant — MaluDB's own, among them —
do not gain `EXECUTE` as a side effect. Rejected: **restoring `PUBLIC` and
retiring the trigger**, which is less machinery but leaves decision 2 as the only
control, with nothing asserting the grant.

### 5. Existing tenants: a canary, then batches

A per-node run in the shape of `cp-manage extension upgrade`: one canary tenant,
then batches, stopping at the first failure, every attempt recorded. **On each
tenant the pre-request check is live in PostgREST before the grants land** —
otherwise there is a window in which `anon` can call `gen_salt` over RPC.

Rejected: **per project on demand**, which leaves every existing tenant refusing
`uuid` defaults, vector search and `crypt()` until an operator touches it.
Rejected: **the maintenance pass**, the automatic data-changing control plane
ADR-066 was written to prevent.

**Consequences.**

- A bootstrap file flips the hardening function and adds the pre-request
  function; `render_config` gains `db-pre-request`. Bootstrap files are
  immutable, so 003, 005 and 011 stay as the record and a new file supersedes
  their effect.
- `specs/tenant-role-model.md` loses "grant extension functions to `anon`" from
  what the admin role must never do, and negative test R changes from "not
  executable by `anon`" to "not callable as RPC". The admin must still be unable
  to remove the pre-request check — through in-database PostgREST configuration
  on the authenticator above all, which PostgREST reads over its file.
- `tests/test_tenant_bootstrap.py` and `tests/test_direct_sql.py` assert the
  opposite of today about `anon` and `gen_salt` in SQL, and gain the RPC refusal.
- `specs/compatibility-matrix.yaml`: `extension_functions_as_rpc` keeps its
  status under a new mechanism, and extension functions usable from SQL by every
  role becomes a supported row with official-client evidence.
- **Vector search is unblocked** by this and ADR-075 together.

**Revisit if** the pre-request hook is needed for something else — a customer's
own, or a platform rate limit — or if `maludb_core` becomes relocatable, at which
point Supabase's `extensions` schema is available and both this decision and
ADR-018's are worth replacing with it.

### Grants slice 0 findings, and two decisions taken on them (2026-09-13)

Measured against a provisioned tenant and PostgREST 14.17;
`specs/extension-grants-model.md` has the figures. **No stop condition was met**:
the check sees `request.path`, refuses before the function body runs (a
non-transactional sequence did not move), answers a clean 403, adds about half a
millisecond to an RPC request, and none of 16 attempts by the tenant admin to
remove or replace it succeeded.

**Decision 2, as amended by the repository owner: refuse a name if *any*
function of it is extension-owned.** As first written, a name was refused only
when *every* function of it was an extension's, so that a customer function
sharing the name stayed callable. Measured: once the tenant admin creates
`public.gen_salt(integer)`, `/rpc/gen_salt` with a text body answered
pgcrypto's salt again — defining any function with an extension's name re-exposes
that extension's overloads on the customer's own Data API. Under "any" nothing a
customer creates can do that. The cost, accepted: a customer's own RPC named like
an extension function (`similarity`, `digest`, `uuid_generate_v4`) is refused with
a message saying why, and is renamed. Extension functions remain usable from SQL
under either rule.

**Decision 3, closed by the repository owner: the listing is accepted.** With
`EXECUTE` restored, PostgREST's description lists 19 extension function names as
RPC paths — the same for `anon`, `authenticated` and `service_role` — of 344 names
in `public`; the rest take arguments PostgREST cannot bind. Each listed path
answers 403. No gateway filter.

**One consequence the ADR did not foresee:** in-database PostgREST configuration
on the authenticator overrides the file, and an empty `pgrst.db_pre_request` set
there disabled the check. The customer cannot set it; the platform writes
`pgrst.db_schemas` to exactly that place for ADR-074. Verification therefore
asserts that no in-database `pgrst.db_pre_request` exists, and no code path may
write one.

### Grants slice 1: one amendment, and one finding fixed before merge (2026-09-13)

**Decision 1, as amended by the repository owner: `maludb_core`'s functions are
excluded from the grant.** As written, decision 1 covered "every extension-owned
function, in every schema". The measurements behind it looked only at `public`,
where `maludb_core` has none; taken literally it would have granted customer
roles `maludb_core`'s 544 — 94 of them `SECURITY DEFINER` owned by the node
superuser, and twelve in `mc2db`, a schema `PUBLIC` already reaches, including
`register_tool` and `create_server`, which write MaluDB's MCP registry. The
pre-request check would not have helped: it guards the exposed schemas, and
`mc2db` is reachable over a paid direct connection. `maludb_core` keeps its
existing posture — no customer role executes any of it — and `verify` asserts
that on every tenant.

**Found in the slice's security review, and fixed before merge: the check read
the path undecoded.** PostgREST percent-decodes the request path before resolving
the function, while `request.path` holds it as sent, so `/rpc/gen%5Fsalt`
answered pgcrypto's salt past a check that compared the raw text. The check now
decodes the path the same way, drops surrounding slashes, and refuses a path that
does not decode. Asserted through a real PostgREST for encoded spellings, and for
the ones PostgREST itself rejects. Bootstrap 013 had not been applied anywhere, so
the file changed rather than gaining a successor.

**How the rollout order is held, which the ADR left to the plan.** Two files: 013
creates the check alone, in its own `maludb_guard` schema, so the request roles'
`USAGE` does not extend to the bootstrap ledger in `maludb_platform`; 014 grants.
A worker names the check once 013 is recorded for its project, never before — a
config naming a missing function fails every request — and `tenant_bootstrap.apply`
stops before 014 unless its caller says the check is live. Provisioning says so,
because a new tenant has no worker; a serving tenant waits for grants slice 2.

### Grants slice 2: how a serving worker gets the check (2026-09-13)

**Decided by the repository owner after a measurement: through the tenant
database, with the worker's listener as the evidence.** Decision 5 said the check
must be live before a serving tenant's grants land, and the plan assumed the run
would rewrite the worker's file, reload it and probe it. That is not available:
PostgREST workers are node-local — the gateway starts them and renders their
files — and the run is on the control plane, which can reach neither.

What PostgREST 14.17 measured, with a file that did not name the check: an
in-database `pgrst.db_pre_request` on the authenticator took effect on a running
worker 0.18 s after `NOTIFY pgrst, 'reload config'`; after its listener connection
was killed, PostgREST reconnected and reloaded configuration on connect, live at
1.1 s; and a fresh start read it at once. So the run writes that setting, waits,
and grants only if `pg_stat_activity` shows no PostgREST connected to the tenant,
or one holding its `LISTEN "pgrst"` connection. A worker connected without a
listener stops the run with nothing granted.

Chosen over a **node-local run**, which could probe the worker over HTTP but needs
the node's superuser locally and control-plane writes through the narrowed gateway
role, and a **restart-driven** rollout, whose "restarted since" the control plane
cannot see. The cost: the evidence is the channel a reload travels, not the
worker's own 403.

**Grants slice 1's rule against any in-database `pgrst.db_pre_request` is
narrowed accordingly**: `verify` accepts exactly the check's name, on the
tenant's authenticator, in its database, and refuses anything else carrying the
key — the right name at database level included. The file line from slice 1
stays, and is what a moved or restored tenant has, since `pg_dump` does not carry
role settings.

## ADR-077 — Vector search exposes MaluDB's vector compartments, through platform wrappers, to `service_role`, exact search first

Status: **Accepted** 2026-09-13 by the repository owner, deciding the eight
questions below one at a time. Unblocked by ADR-075 and ADR-076. Extends ADR-074's
opt-in surface; changes nothing ADR-074 decided for the data-model graph.

**Context.** Phase 12 names "vector search, retrieval planner, and query hints"
as a candidate surface. Two things were already true before deciding:

- **pgvector search works for every customer role** since ADR-076: a customer's
  own `vector` columns, operators and a migrated `match_documents` RPC run for
  `anon`, `authenticated` and `service_role`, with RLS applying as usual, and
  `specs/compatibility-matrix.yaml` marks it supported. Supabase parity needs no
  decision.
- `maludb_core` carries **a vector store of its own**, separate from pgvector:
  compartments keyed by namespace, subject and verb
  (`register_vector_compartment`, `register_vector_chunk`), exact search
  (`search_memory_exact`, `search_memory_filter`, four implementations of
  `exact_vector_search_*`), an ANN graph (`ann_build`, `ann_rebuild`,
  `maludb_ann_search_c`) and `explain_vector_search`. Its retrieval planner
  (`retrieve_with_envelope`, `plan_retrieval`, query hints) is a different thing:
  it reads `current_account_id`, which is the project-to-account mapping ADR-074
  deferred with the memory pipeline.

Measured before deciding, in a scratch database with `maludb_core` 0.104.0
installed the way provisioning installs it:

- The compartment functions are **`SECURITY INVOKER`**, with `EXECUTE` held by
  `PUBLIC` — except `search_memory_filter`, which `service_role` cannot execute.
- **`service_role` cannot use them anyway**: it has no `USAGE` on the
  `maludb_core` schema, and no privilege on `malu$vector_compartment`,
  `malu$vector_chunk`, `malu$vector_subject` or `malu$vector_verb`, which grant
  nothing beyond their owner.
- Those tables have **no row-level security**. A compartment is kept apart only
  by its `owner_schema` column, which defaults to `current_schema()`.
- `malu_vector` accepts pgvector's text form, so a pgvector `vector` converts to it
  through `text`; the only casts it declares are to and from `bytea`.
- `ann_build` builds the graph in one synchronous C call over the whole
  compartment and stores it as **one `bytea` row** in `malu$ann_index`;
  `maludb_ann_search_c` takes that graph as an argument, which reads as every
  query detoasting the whole graph. Not measured.
- **`pg_dump` does not carry any of it.** `maludb_core` registers no table with
  `pg_extension_config_dump` (its `extconfig` is empty), so a dump of a database
  holding one compartment and one chunk contains neither. Tenant moves (ADR-066)
  and per-tenant restores (ADR-059) are built on `pg_dump`; a physical pgBackRest
  restore of a whole cluster is not affected.

ADR-074's amended decision 3 served the data-model graph by copying what the
platform computed into ordinary tables. **That cannot serve search**: the query
vector arrives with the request, so there is nothing to compute in advance.

### 1. The surface is MaluDB's vector compartments

Chosen over pgvector parity alone, which already works and differentiates
nothing, and over the retrieval planner, which needs the account-mapping decision
first and is really the memory pipeline's. Compartments need neither the memory
schema nor an account model, and the caller supplies embeddings, so no model
service, provider key or egress enters the platform.

### 2. `service_role` only

As ADR-074 decided for the data-model graph, and for a sharper reason here:
compartments have no RLS, so any role that can search a compartment reads every
chunk in it, source text included. Writes and searches come from the
developer's own server. **Per-user search in the browser remains pgvector with
RLS**, which is the Supabase pattern and already works. Opening compartments to
`authenticated` or `anon`, or letting a project mark a compartment's
visibility, is a later decision that has to say how a definer wrapper enforces
it.

### 3. Platform-owned `SECURITY DEFINER` wrappers, owned by a narrow per-tenant role

Stable names in the platform-owned `maludb` schema, each with a pinned
`search_path`, called as
`supabase.schema('maludb').rpc('vector_search', {...})`, with `EXECUTE` granted to
`service_role` alone. The definer is **a per-tenant platform role holding only
the grants the vector tables need — never the node superuser**, so a wrapper
bug reaches the vector tables and nothing else. The wrappers are the contract,
and they are where the plan's limits are enforced (decision 4).

Rejected: granting `service_role` the schema and tables directly. Upstream's raw
signatures would become the customer contract, any extension upgrade could break
callers, nothing could enforce a limit, and the pre-request check refuses
extension functions over `/rpc` in any case (ADR-076). Also rejected: granting
upstream's `maludb_memory_executor` to `service_role` — a cluster-scoped role that
reaches far more than vectors, and would need the review ADR-014 gave the
`BYPASSRLS` roles.

### 4. Every plan, with vector limits per plan

An entitlement on every tier, as ADR-074 decided, because free is where the
platform is evaluated. Limits live in `entitlements` — configuration, never
hard-coded — and are enforced by the wrappers: **maximum vectors per project,
maximum dimension, maximum compartments**. Stored vectors already count against
the database storage quota; request rate stays with the gateway. What the
per-plan limits add is a bound on the cost of one exact search on a shared node,
which the storage quota alone does not give.

### 5. Exact search first; ANN after it is measured

The first release ships exact search only, bounded by decision 4. A slice 0
measures `ann_build` (time, memory, locks) and per-query cost at several
compartment sizes on a real node, as pinning slice 0 did for `vector` upgrades.
ANN is offered afterwards, per plan, only if the numbers allow it — and its build
frequency is limited per plan the way data-model refresh is. A compartment
growing past what exact search can serve is answered by the vector limit until
then, or by pgvector HNSW, which already works.

### 6. Its own opt-in

A per-feature flag beside `maludb_datamodel_enabled`, following ADR-074 decision 2:
nothing is created in a tenant until the project asks. The `maludb` schema is
served by PostgREST while **any** MaluDB feature is enabled, and disabling one
withdraws only its own wrappers. The gateway's "not enabled for this project"
answer names the feature that is off. Enabling runs over node credentials, so a
customer request enqueues it for the worker, as the data-model graph's does
(ADR-038).

### 7. Embeddings are pgvector `vector`

The wrappers take and return pgvector's `vector` type — the `match_documents`
convention, in which supabase-js sends a number array and PostgREST passes it to
a `vector` parameter — and convert to `malu_vector` inside. Customers reuse the
embedding code they already have, and upstream's type stays behind the contract.

### 8. Compartments do not ship until a move and a restore carry them

A hard gate, not a documented limitation: moves are operator-initiated, so a
customer would lose their vectors to an operation they never saw. Slice 0 builds
the fix — the platform carries the tenant's `malu$vector_*` rows beside `pg_dump`
in both the move and the per-tenant restore paths — and a test moves and restores
a tenant with compartments and searches them afterwards. The upstream report
(register the data tables with `pg_extension_config_dump`) is filed as well, and
the platform-side copy is retired once a pinned `maludb_core` makes it redundant.

**Consequences.**

- A migration adds the flag and its timestamp; entitlements gain the vector
  entitlement and three limits, seeded for every plan in
  `specs/plans-and-limits.yaml`.
- Provisioning's role model gains a per-tenant definer role for the wrappers. It
  must survive a move and a restore the way the tenant's other roles do, and
  `tenant_bootstrap.verify` must refuse one holding more than the vector grants.
- **An extension upgrade can change the tables the wrappers read.** The upgrade
  run's per-tenant verification (ADR-074 decision 5) gains a call through the
  wrappers on an enabled tenant, inside the transaction, so an upstream change
  that breaks them rolls that tenant back rather than breaking its API.
- `search_memory_filter` is not executable by `service_role` as installed; the
  wrappers run as their definer, so this does not block them, but the plan
  records it.
- `docs/MALUDB-FEATURES.md` and `specs/compatibility-matrix.yaml`
  (`maludb_extensions`) document the surface; `public` stays untouched, and a
  project that never enables it is indistinguishable from one without it.

- **The `pg_dump` finding is wider than vectors.** Every `maludb_core` table is
  left out of a dump, including whatever the data-model graph's memory schema
  holds. Its customer-facing copy lives in ordinary tables in `maludb` and does
  travel, and a refresh rebuilds the rest; the finding is recorded in
  `docs/OPEN-QUESTIONS.md` for the memory pipeline, where it would be data loss.

**Deferred**, because exact compartments for `service_role` do not need them:
ANN (decision 5, pending measurement); compartment visibility for `authenticated`
or `anon`; platform-generated embeddings through MaluDB's model registry, which
brings provider keys and egress; the retrieval planner and query hints, with the
memory pipeline and the account-mapping decision.

**Revisit if** slice 0 shows exact search on a compartment at the free plan's
limit is too expensive for a shared node, or if customers need end-user search
over compartments rather than over their own pgvector tables.

### Compartments slice 0 findings (2026-09-13)

Measured in `specs/vector-compartments-model.md`. None changes a decision; three
settle what the decisions left open.

- **Decision 3 holds**: a non-superuser definer runs create, insert and exact
  search. It needs function `EXECUTE` grants as well as table and sequence grants,
  because bootstrap 011 revokes `EXECUTE` in every schema — and those grants must
  be read from the function bodies, since a cached PL/pgSQL plan reaches code a
  first call does not.
- **Decision 5 resolves to "not offered"**: MaluDB's ANN had a worse p95 than
  exact search at every size measured, and one build of 20k × 1536 peaked at
  951 MB in a single backend. No follow-up decision is needed unless upstream
  changes how the graph is stored.
- **Decision 8 is met by a carry**, `extension_data.carry`, called by moves and
  restores after the load and before ownership is verified, and tested through a
  real point-in-time restore.
- Exact search does not filter tombstones, so deleting a chunk deletes its row.

### Built (compartments slices 1–3, 2026-09-13): four choices the decisions left open

- **The owner's grants are derived at enable time**, not listed, from the
  installed extension's function bodies and the constraints, defaults and
  triggers of the tables they reach, fenced to the vector tables and invoker
  functions, then exercised before commit. **On an extension upgrade they
  converge** — anything the new release no longer needs is revoked — because
  refusing an owner for holding a grant a release stopped needing would roll back
  every otherwise good upgrade. On enable they are refused instead.
- **Limits are a table in the tenant database** (`maludb_private.vector_limits`),
  not a setting: a session can override a custom setting, including a customer's
  own RPC function calling a wrapper.
- **Enabling vectors draws on `datamodel_refreshes_per_hour`**, which becomes the
  project's per-hour budget for MaluDB node work; disabling draws on nothing.
- **Default limits** (free 10k × 1536 × 10, starter 50k × 1536 × 50, production
  100k × 3072 × 200) are sized from slice 0's search cost, and are configuration
  for the owner to revise.

## ADR-078 — maludb_core's data reaches pg_dump upstream, and the platform pins only a version that proves it

Status: **Accepted** 2026-09-14 by the repository owner, deciding the two questions
below one at a time. Follows ADR-077 decision 8 and maludb-core#27.

**Context.** `maludb_core` registers none of its 157 tables or 133 sequences with
`pg_extension_config_dump`, so `pg_dump` carries none of their rows, and tenant
moves (ADR-066) and per-tenant restores (ADR-059) are built on `pg_dump`. ADR-077
carries the vector tables beside the dump (`extension_data.carry`); every other
`maludb_core` table — the knowledge graph, the memory pipeline, MaluDB's secrets,
queues and audit — is still lost by a move or a restore. Measured on a fresh
database with 0.104.0 before deciding:

- **17 tables hold 298 rows the extension installs itself.** Some are pure
  catalogues (`malu$object_type`, `malu$source_type`); some mix installed and
  customer rows behind a `system_defined` flag (`malu$svpor_verb_type`) or an
  `owner_schema` (`malu$rest_endpoint`); `malu$audit_event` holds install-time
  events. Restored rows whose keys collide with the new install's fail that
  table's whole `COPY`, and `pg_restore` continues past it — the same silent loss.
- **`malu$secret_master_key` is generated per database at install.** Registering
  it puts that database's master key in every dump; not registering it leaves
  anything encrypted with it unreadable after a move.
- 12 tables carry triggers that fire on `COPY`; 14 columns are identity or
  generated. No foreign key leaves the extension; no row-level security is forced.

### 1. Fixed upstream, then pinned

`maludb_core` registers its data tables and sequences with
`pg_extension_config_dump`, each with a filter that excludes the rows the
extension installs, in a new version's upgrade script — which a fresh install
runs too. When a pinned version has it, `pg_dump` carries the data and
`extension_data.carry` retires.

Chosen over generalising the platform's carry to every table, which would put
per-table seed, trigger, generated-column and version-skew handling on the two
code paths that exist for disasters, in the wrong repository. **The knowledge
graph and the memory pipeline do not ship until that version is pinned.**

### 2. The platform drafts the upstream change and accepts a version only on proof

The platform's agent drafts the maludb-core pull request for the owner to review
and merge. **A `maludb_core` version is added to `specs/extension-versions.yaml`
only if a platform test proves `pg_dump` carries a customer row from every data
table** and none of the installed rows — the ADR-075 tested list becomes the gate,
so an upstream release that registers a table wrongly cannot become a pin.

**Consequences.**

- Until the fixed version is pinned, `extension_data.carry` stays, and it must
  **step aside for any table the installed extension registers**: otherwise the
  first move after the pin finds the vector rows already loaded by `pg_restore`
  and refuses, failing every move.
- The per-table filters are an upstream design decision made table by table,
  recorded in the plan before the pull request, not inferred from row counts.
- **`malu$secret_master_key` is decided separately**, before the pull request, as
  a security decision: the platform does not use MaluDB's secret store (ADR-023),
  and a dump file is the customer's whole database in the clear (`restore.py`).

**Revisit if** upstream declines to register the tables, which would bring the
platform-side carry back as the only option.

**Implemented 2026-09-14**, registration slice 2. maludb-core#28 (and #30) merged;
0.105.0 at `2f2acac` is on the tested list and is CI's build. What proof the list
now requires is `tests/test_maludb_core_dump.py`: a customer row in every table
the extension owns, the platform's own `pg_restore`, and a per-table comparison,
with 0.104.0 as the control that loses every row. Two things the building found:

- **A restore must run with `session_replication_role=replica`.** An extension's
  tables exist with triggers enabled before the dump's rows load; a plain
  `pg_restore` re-fires them (the SVPOR subject trigger queues embedding work) and
  exits with errors. `restore.pg_restore_argv` sets it for restores and moves.
- **The carry does not retire yet; it steps aside per table.** A point-in-time
  restore can read a backup from before a tenant's upgrade, whose source registers
  nothing, so the carry stays until no backup inside any plan's recovery window
  predates the upgrade. It refuses a carried table registered *with* a filter,
  which would be a partial dump it cannot complete.

## ADR-079 — The memory pipeline ships as memory spaces: wrapper reads, platform writes, the customer's own model keys

Status: **Accepted** 2026-09-14 by the repository owner, deciding the questions
below one at a time. Answers the question ADR-074 deferred — how MaluDB's
in-database account tenancy composes with database-per-project — and follows
ADR-078, whose pin made the pipeline's data survive a move or a restore. Measured
first: `specs/maludb-memory-pipeline-model.md` (memory pipeline slice 0).

**Context.** MaluDB's memory pipeline (sources → claims → facts → episodes and
memories, plus embedded edges and search) is the capability the product is
positioned on, and none of it is reachable by a customer today. What the
extension actually does, measured on 0.105.0:

- **The pipeline is keyed by schema, not by account.** Its tables carry
  `owner_schema = current_schema()`; `current_account_id` is a session setting any
  SQL session can change, read only by the LLM-gateway and auth-token tables.
  Nothing inside a database is a boundary — no row-level security is forced, and
  `current_schema()` follows the caller's `search_path` (spec finding 3c). That is
  ADR-013 again: the database is the boundary, and schemas and accounts inside it
  are organisation.
- **Every pipeline path runs the `session_user` guard** (`CREATE` on the schema)
  inside superuser-owned `SECURITY DEFINER` functions, so no wrapper on PostgREST's
  path can call one (finding 1d) — the finding that reshaped ADR-074.
- **Search needs none of that.** A narrow per-project definer returned the facade's
  results exactly (400 rows, 0 differences) through PostgREST, at the facade's
  latency (1b, 1c).
- **Platform-run writes are cheap and immediately searchable**: ~8 ms per embedded
  edge, and 50 of 50 searches found a memory right after commit (2a, 2b). They also
  **skip items silently** (2a), and extraction output only becomes searchable
  through harvest over embedded edges (2c).
- **Extraction needs a model worker upstream does not ship**, which works over a
  narrow role without `BYPASSRLS` (5b).
- **ADR-077's shipped vector wrappers had no owner fence** and would have listed,
  searched, deleted and counted memory-space data (1e); fixed before this ships.

### 1. Memory spaces

A project may have several named **memory spaces**, each a platform-owned memory
schema (`enable_memory_schema`) reached only through platform code that pins it.
Agents and applications are separated by which space they use. Nothing is shared
between spaces by default; sharing, when offered, uses MaluDB's object grants.
A customer with direct SQL (paid, ADR-047) can cross spaces inside their own
database, and the documentation says so: spaces organise one customer's data,
they do not protect it from that customer.

MaluDB accounts are **not** used as a boundary or a product concept in v1.

### 2. The secret key only, the space named on every call

Memory is reached with the project's secret key (`service_role`), as the
data-model graph and vector search are (ADR-074, ADR-077); publishable keys and
end-user JWTs are refused. Every call names its space. Space-scoped keys and
end-user access are later decisions and change no wrapper contract.

### 3. Reads through a narrow wrapper; writes run by the platform

Option B of the spec.

- **Search** is a platform wrapper in `maludb`, `SECURITY DEFINER`, owned by a
  per-project `NOLOGIN` role holding only the grants a reading of the installed
  extension derives (ADR-077's pattern). No superuser-owned function is reachable
  from a request. The search query re-implements upstream's, so its parity is
  re-proven on every extension upgrade, as ADR-077's grants are re-derived.
- **Ingest, extraction and harvest** are requests the platform runs: accepted by a
  route that enforces the plan's limits at enqueue (immediate 429, never a queued
  row that silently never runs), executed by the memory worker (decision 6), with
  **per-item results** — including what upstream skipped — reported through the
  request's status, because the caller is gone by then.

Rejected: granting the authenticator `CREATE` on spaces (every request one grant
from superuser definers); synchronous platform writes routed through the control
plane (reopens ADR-074's routing question); narrow writer wrappers
(~200 lines of guarded upstream logic re-implemented); waiting for upstream to
check `current_user` (inside a superuser-owned definer that always passes, and a
wrapper would still reach superuser code).

### 4. The platform calls the models, with the customer's own keys

The customer stores their provider API key with the platform and sends raw text;
the platform extracts and embeds through the provider, and the model bill is the
customer's own. *First answered as "the customer supplies extractions and
embeddings"; revised the same day, because that asked a free-tier developer to
assemble two model integrations before memory did anything.*

- A provider key is a **per-project secret**: write-only through the API (never
  returned, never logged), encrypted under the KEK (ADR-023), deleted with the
  project.
- No model is called with a MaluDB account; platform-paid models are a later
  decision and would meet ADR-050.

### 5. Providers: OpenAI and Anthropic for extraction, OpenAI and Voyage for embeddings

A space names its extraction provider and its embedding provider separately
(Anthropic offers no embeddings); one key per provider per project. **Fixed
hosts only** — `api.openai.com`, `api.anthropic.com`, `api.voyageai.com`. No
customer-supplied endpoint: a key-holder could otherwise aim platform
infrastructure at internal addresses.

### 6. A dedicated memory worker, writing as a per-project writer

Its own process on the control-plane host — not the provisioner, which holds every
node's superuser credentials (ADR-038). It connects to each tenant database as a
**per-project writer login with `CREATE` on that project's spaces only**, which is
what the guard checks, so a compromised worker reaches memory rather than the
fleet. Its only outbound network is the provider hosts of decision 5.
**Not yet measured:** that a non-superuser writer with `CREATE` and the extension's
executor rights passes the pipeline facades. It is the first thing the plan
measures, and this decision is reopened if it does not hold.

### 7. Every plan, with tiered limits

Available on every plan, opt-in per project, with per-plan ceilings on spaces,
stored memories and ingest requests — configuration in `plans.config_json`, never
constants, like every other limit. Model cost is the customer's, so the free
tier's cost to the platform is bounded storage and worker time. The numbers are
confirmed separately; the spec's per-space cost (~0.6 s, ~1 MB, 165 objects) is
their input.

**Consequences.**

- **ADR-077's vector wrappers are fenced to `owner_schema = 'maludb_core'`** and
  search by compartment id, since upstream's name-based search takes the first
  compartment with a matching name. Enabling memory spaces on a project re-verifies
  its vector wrappers first, so no tenant gains a space while holding unfenced ones.
- The platform owns two more re-implementations to keep in step with upstream —
  the search query and the model worker's harvest contract — each verified on the
  pinned version by a test, the ADR-075 way.
- A space's schema is created by the extension and owned by the superuser; its
  behaviour through a move, a restore and an extension upgrade is verified before
  customers can create one, not assumed from ADR-078.
- The worker adds outbound internet access to the control-plane host, restricted
  to three hosts; that restriction is enforced in deployment, not only in code.
- Unmeasured and planned: queue-to-searchable latency, many spaces and tenants per
  worker, real providers, concurrency within a space.

**Revisit if** upstream offers a supported, guard-free write API; if the writer
role cannot pass the facades; if customers need end-user or space-scoped access;
or if platform-provided models are wanted.

### Decision 6, as measured 2026-09-14 (memory slice 1)

**Holds, and narrower than written.** Measured on real tenants
(`specs/maludb-memory-pipeline-model.md`, "Memory slice 1"), the writer the memory
worker connects as needs exactly:

- `LOGIN`, `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT`;
- `CONNECT` on its own database only (ADR-014 refuses the rest);
- `USAGE` on `maludb_core`;
- `USAGE` and `CREATE` on each of its project's spaces — `CREATE` is what the guard
  reads, and revoking it fails every facade;
- `EXECUTE` on that space's facade functions, **granted per object**.

**Not the extension's executor rights**, which this decision's text assumed:
`maludb_memory_executor` is a cluster-wide role that also carries `EXECUTE` on
MaluDB's auth-token and secret functions, and nothing in the pipeline needs it. No
table, sequence or `BYPASSRLS` grant either — the work runs on the facades' definer
rights. The guard reads `session_user`, so the worker must connect *as* the writer;
`SET ROLE` on another connection does not satisfy it.

Measured reach: its own database, its granted spaces, and `maludb_core` only through
those facades — a second space is refused through its facades, the `_for_schema`
functions and the base tables, including under `search_path` changes. Escalation
through `CREATE` was attempted and failed: the pipeline's definers pin
`pg_catalog, maludb_core, pg_temp`, so an object the writer creates in a space is
never resolved by superuser code, and the writer cannot add triggers to or replace
anything it does not own.

**Consequences for the build (memory slice 2):**

- The writer role is created on a move or restore target **before the load**, like
  the vectors role: measured, a dump restored without it drops every grant to it
  with `role does not exist`. It joins `TenantNames`,
  `tenant_movement.prepare_target_roles` and `restore.missing_roles`.
- One space definer (`maludb_document_graph_backfill`) puts the space first on its
  path; its body is fully qualified today. An extension upgrade check asserts that
  every definer with the space on its path references only qualified objects, so a
  later release cannot turn `CREATE` into superuser execution silently.

### Decisions 3–6, as decided for memory slice 5b (2026-09-14)

Three questions the build raised, answered by the repository owner after reading
the pinned extension (0.105.0) for its extraction path.

**Extraction: the worker calls the models and writes through `ingest_edge`.**
The worker sends the customer's text to the space's extraction provider,
embeds each resulting edge with the space's embedding provider, and writes
every edge through `maludb_memory_ingest_edge` — the write 5a already makes,
and the one upstream's `harvest_extractions` ends in. Chosen over the two
upstream paths because of what they do:

- `maludb_memory_ingest_extraction` writes a richer graph but **no vector
  chunk**, so nothing it writes is found by the search wrapper of slice 3
  (spec finding 2c); making it searchable needs the entity-card embedding
  queue and a second wrapper over `maludb_semantic_search`.
- `request_extraction` → `model_response` → `harvest_extractions` needs the
  writer to hold table grants on `malu$model_request`, `malu$model_response`
  and a sequence, outside the grant set slice 1 measured; harvest drops an edge
  missing a field **without recording it** and fails the whole extraction on
  one bad edge; and `maludb_memory_model_config()` returns a tenant-supplied
  `base_url` a worker must never follow. Upstream's own worker implements no
  cloud provider (`cloud_api` answers `ADAPTER_NOT_AVAILABLE`).

The platform therefore owns the extraction prompt, modelled on upstream's
built-in edge prompt, and a result per edge. This **replaces** the consequence
above that named "the model worker's harvest contract" as a re-implementation to
keep in step: what is kept in step is `ingest_edge`'s signature, which 5a's
worker test already exercises on the pinned version.

**Egress: a platform CONNECT proxy, the only process with internet access.**
`maludb-egress-proxy` accepts `CONNECT` to exactly `api.openai.com`,
`api.anthropic.com` and `api.voyageai.com` on 443, resolves the name itself,
refuses any address that is not globally routable, and connects to the address
it checked. The memory worker's unit keeps `IPAddressDeny=any` and reaches the
internet only through it. TLS is end to end, so the proxy never holds a key.
Rejected: an IP allowlist in the host firewall (these hosts sit behind CDNs
whose addresses serve other sites, so allowing the address allows them too); an
off-the-shelf proxy (sound, but a node package whose refusals CI would have to
install to assert).

The providers are called over plain HTTP from the worker rather than through
their SDKs: one code path for three providers, one proxy setting, and no SDK
that reads a base URL from its environment — which is the redirect decision 5
exists to prevent.

**Models: named per space, free-form, with defaults.** A manager sets a space's
extraction provider and model and its embedding provider and model. A model
name is shape-checked text that only ever travels in a request body to a fixed
host, so it reaches nothing. The embedding model cannot change while the space
holds memories, because search compares only vectors of one dimension.

### Decision 6, as built for memory slice 5c (2026-09-15)

**A compromised worker reaches memory, enforced by the database.** The worker
connects as a member of `cp_memory_worker`. That role reads, column by column, what
the worker's code reads and nothing more. Row policies admit it only to memory writer
credentials and live provider keys, never another credential type. So the process
holding the KEK can open no node's superuser DSN and no tenant's database password or
signing key.

- **An allowlist, where ADR-072's gateway model is a denylist.** The worker's reads are
  enumerable and a test runs it as the role, so an allowlist is the tighter statement
  and fails in the suite when too narrow.
- **Identity is role membership, not a mapping row.** The gateway may write any table,
  so a table naming the worker is a table a compromised gateway could add itself to.
- **A role is never both a gateway and the memory worker.** The gateway's own-node policy
  plus the worker's column grants would read that node's tenant credentials.


### Search by text, decided 2026-09-15 (memory slice 6a)

A space fed with text holds vectors from a model the customer never called, so a
caller holding only a question cannot search it. The platform embeds the query with
the space's embedding model and the customer's own key.

**Where it runs.** A small internal service, `memory_embedder`, on the control-plane
host beside the memory worker. It is the only place that holds both provider keys and
provider egress (decisions 4 and 6). The gateway answers
`POST /memory/v1/spaces/{space}/search` with a text query: it asks the service for a
vector, then runs the existing search wrapper through the project's PostgREST.
Provider keys still never reach a node.

**How the service knows the caller.** The customer's own secret key, forwarded by the
gateway and verified with `api_keys.authenticate` against the named project, rather
than a new node-to-control-plane secret. A compromised node can only use keys that
already passed through it, which is the blast radius it had before, and no platform
secret is added to manage or rotate.

**Its own role.** `cp_memory_embedder` reads the columns it needs to authenticate a key
and embed: projects, spaces' models, live provider keys, and the key hashes. It holds no
memory writer credential. The worker's role holds no key hash. A listening process
should not carry every project's writer password, and a queue drainer should not carry
every project's API key hashes. As with the worker, a gateway role may never also be
the embedder: its own-node policy combined with the embedder's column grants would read
its node's provider keys.

Rejected:
- **An asynchronous search request**, queued like ingest: a second or more per lookup,
  which is the wrong shape for an agent mid-conversation.
- **Client-side query embedding only:** it asks every developer to assemble a model
  integration, the objection that revised decision 4.
- **Keys and egress on the gateway:** it reverses slice 4, and a compromised node would
  yield every key on it.

## ADR-080 — A node reports its own health, as a role that can call one function

Status: **Accepted** 2026-09-15 by the repository owner, choosing a node-side reporter
with a narrow role over a control-plane timer that reads the node through the
provisioning credential. Found by the deployment rehearsal (`plans/active/deployment-rehearsal.md`,
finding 9).

**Context.** Placement refuses a node whose last health report is older than
`nodes.HEALTH_STALE_AFTER` (five minutes), and `docs/DEPLOYMENT.md` said only that
"whatever records health must be running". The one writer, `cp-manage node health`,
needs the control plane's own database role and the KEK, so it cannot run on the node
it describes, and free disk cannot be measured from the control plane without a
command on the node. The rehearsal kept a node placeable with a loop on a developer's
machine. The same writer also replaced `nodes.metrics_json` wholesale, erasing what
`node realtime-check` and `node backup-check` record there.

**Decision.**

1. **`maludb-node-reporter` runs on each node** (`deploy/maludb-node-reporter.service`)
   and reports free disk under the PostgreSQL data directory every minute, **only while
   the local cluster accepts connections** (`PQping` answers OK). A report is a claim that
   the node can take a project; a node whose cluster is down stops claiming it and ages
   out of placement.
2. **Its control-plane role holds EXECUTE on `public.report_node_health(bigint)` and
   nothing else** — no table, no column. The function (migration 0050) is SECURITY
   DEFINER, identifies the node by `session_user` against `nodes.health_reporter_role`,
   writes only `free_disk_bytes` and `health_reported_by`, merges them into
   `metrics_json`, and stamps `last_health_at` with the database's clock. An unmapped
   role is an error, not a no-op.
3. **One role per node, mapped by `cp-manage node reporter grant`**, which refuses a
   superuser, a role that cannot log in, the owner of the control plane's tables, a
   gateway role, a memory worker or embedder group member, and a role already reporting
   for another node — and then asks the catalogue that the role holds no table privilege.
   `gateway grant` refuses a reporter role in turn. `deploy preflight` names an active
   node without a reporter (advisory) and fails a reporter holding more than the function.
4. **`record_health` merges too**, so the operator command no longer erases the
   realtime and backup records.

**Why a mapping column is safe here** when ADR-079 slice 5c rejected one for the memory
worker: the danger is a narrowed role that can write the mapping. The gateway's grant
revokes everything on `nodes` and gives back UPDATE on `last_health_at` alone; the memory
worker and embedder only read `nodes`. Only the control plane's own role writes
`health_reporter_role`.

**Rejected.** A control-plane timer reading the node's free disk with
`COPY ... FROM PROGRAM` through the provisioning credential: no new role, but a routine
every-minute task would run a shell command on the node as the PostgreSQL superuser,
widening the most dangerous credential on the platform from an occasional operator tool
to a standing loop.

**Consequences.**

- A stolen reporter password can mark one node fresh and misstate its disk. It reads
  nothing and reaches no other node.
- **The gateway can still set its own node's `last_health_at`** (ADR-072 grants that
  column so `SELECT ... FOR UPDATE` works). A compromised gateway could therefore keep
  its node looking fresh; that is its own node, which it already serves. Recorded rather
  than changed here.
- Health says the node's PostgreSQL answers and how much disk it has. It does not say the
  gateway is serving; that remains an external check.

**Revisit if** placement needs more than free disk and liveness (load, connections, the
gateway's own health), or a second node pool makes per-node roles an operational burden.

## ADR-081 — Authentication lookups a cache cannot answer are budgeted, per project and per node

Status: **Accepted** 2026-09-16 by the repository owner, choosing the uniform 401 over a
429 on exhaustion and accepting the residual availability case below as written, pre-launch
and with no live customers. Follows the security review of `fix/gateway-key-cache` and the
one of this change. Related: ADR-008 (a key is validated against the project the hostname
names), ADR-009 and ADR-030 (limits are per gateway process, and generous by default),
ADR-023 (how a key is verified).

**Context.** `Gateway.handle` authenticates before it reaches `limiter.acquire`, and it
does so deliberately: routing after authentication is what stops an unauthenticated
caller using the routing table to discover which surfaces a project exposes. The
consequence is that nothing bounded what an unauthenticated caller could cost. That was
survivable only because of a bug — a wrong key sharing a real key's public prefix was
answered from cache, so a flood of them was free. Fixing that (commit `bf4c31c`: a cached
answer belongs to one key, not to one prefix) turned every distinct wrong key into a
`SELECT` against the control plane. The gateway's control-plane role and its pool of ten
connections are shared by every tenant on the node, and `_authenticate` is synchronous
and called inline from `async def handle`, so each lookup also occupies the event loop
that serves every other tenant here.

**Decision.**

- A token bucket per project, and a second for the node, spent only on a lookup the cache
  cannot answer. Sixty per project per minute, six hundred per node. The legitimate figure
  is calculable rather than guessed: with a 30-second cache TTL each live key costs two
  misses a minute, and a cold start costs one per live key.
- The node bucket exists because the resource is the node's. Project refs are public — they
  are the hostname of every application built on one — so a per-project bound alone
  multiplies by however many refs an attacker has collected.
- Over budget, an uncached key is refused with the same 401 as a wrong key: identical
  status, headers and body. Exhaustion is logged once on entry and once on recovery.
- The cache answers more than it did, which is what keeps the common attack off the budget
  entirely: a key presenting a *known* identifier with the wrong digest is refused from
  cache, because `api_keys.key_identifier` is UNIQUE platform-wide and a key's verifier
  never changes, so no other key can present that identifier and be valid.

**What this costs, stated plainly.** While a bucket is empty, a key this gateway has never
resolved is refused without being checked. The bucket refills at its rate and an attacker
can keep it empty for as long as they keep sending, so this is a steady state, not a
window that passes. A 401 is not a status clients retry. So a sustained attack against a
project whose cache is cold — after a restart, a revocation, or a listener reconnect, all
of which empty it — can keep that project down, and issuing a new key is not a way out.

That is accepted here because the alternative is unbounded synchronous load on the one
database every tenant on the node depends on, and because the attack that made this urgent
(vary the suffix of a public prefix) now costs the attacker a cache lookup and the project
nothing. It is accepted knowing the residual case is real.

**Revisit if** any of these: a 429 with `Retry-After` on exhaustion is judged better than a
uniform 401, trading a small oracle — "this ref exists and its budget is spent", which a
caller who just spent the budget already knows — for an answer clients retry and operators
can read; the gateway learns the caller's address reliably enough to budget per caller
instead (today `MALUDB_TRUST_FORWARDED_FOR` is a deployment property this process cannot
verify); `_authenticate` stops blocking the event loop, which would remove the amplifier
that makes the node bucket necessary; or identifiers this gateway has resolved before are
given reserved headroom, which would make a restart under attack survivable for keys in
active use.

## ADR-082 — Operators get a web console: staff accounts of their own, a third listener, and read-only first

Status: **Accepted** 2026-09-16 by the repository owner, choosing staff accounts of their own
with TOTP over an external identity provider or a flag on customer accounts, a private listener
reached over a VPN over a public one, customer-visible audit only for access to content, and one
staff role for the first version. Drafted the same day at the owner's request after the customer
console redesign. Answers "Operator web console" in
`docs/OPEN-QUESTIONS.md`. Related: ADR-021 (platform identity lives in the control plane),
ADR-023 (how secrets are stored), ADR-037 (public and internal applications), ADR-038 (the
internet-facing process never reaches a node credential), ADR-052 (no amount is rendered from
the platform's own records).

**Context.** Every operator task is a `cp-manage` command, run over SSH on the control-plane
host: subscriptions and billing events, storage and egress per project, the abuse report,
node capacity, restores. `services/control_plane/manage.py` says why it is a CLI — staff
access is meant to be "explicit, time-bounded, audited, and visible to the customer"
(`docs/ACCOUNTS.md`), no staff model exists, and an HTTP surface would have had to invent one
or authenticate operators as customers. The owner now wants a site to watch sales and usage.

What exists to build on, measured rather than assumed:

- `audit_events.actor_type` already allows `staff`; one command writes it
  (`extension_pins`, actor = `SUDO_USER`).
- `user_mfa_factors` exists with an encrypted TOTP seed column; **nothing implements MFA**.
- The internal application binds a private interface and mounts *every* router, including
  the customer ones and `/internal/hooks/email/{ref}`, whose only protection is its signature.
- The reports an operator wants are already functions that need no node credential:
  `abuse_report.report`, `billing.events`, storage accounting, capacity.

**Decision.**

1. **Staff are not customers.** A staff account is a separate principal in the control-plane
   database: its own table, sessions and cookie/token format, with no organization membership
   and no path to one. A customer account can never be promoted to staff, and a staff session
   is refused by every customer route and the reverse. Rejected: a staff flag on `users`,
   because then one phished customer-facing password, one bug in organization authorization,
   or one signup with the right email becomes operator access; the flag is a single column
   between the two worlds.
2. **Staff sign in with a password and mandatory TOTP, in the control plane.** No staff session
   exists without a second factor, enrolled out of band by `cp-manage staff create`, which
   prints the enrolment once. Sessions are short (8 hours, 30 minutes idle), server-side, and
   revoked by `cp-manage staff revoke`. Staff TOTP seeds are encrypted under a **staff key of
   their own**, not the platform KEK (decision 4). Losing that key means re-enrolling every
   staff member with `cp-manage staff enrol`, and nothing else; `cp-manage` over SSH stays the
   break-glass path, so the console being unreachable never locks operators out. Rejected for now: an external OpenID Connect provider — fewer
   secrets to hold, but a dependency ADR-021 exists to keep out of the operator sign-in path;
   revisit once there is more than a handful of staff.
3. **A third application on a third listener.** `create_admin_app()` mounts only
   `ADMIN_ROUTERS` (under `/admin/v1`) plus health, on its own port bound to a private
   interface, reached over the operator VPN. It is **not** added to the internal application:
   that listener exists for node-to-control-plane traffic, and staff browsers do not belong on
   it. The admin frontend is a separate static directory served only on that listener, never
   from the public site's document root. A test asserts, as ADR-037's does, that the admin
   route set is exactly `ADMIN_ROUTERS` and that no admin router is mounted on the public or
   internal application. Network position stays defence in depth: every admin route requires
   a staff session.
4. **The admin process holds no KEK and no node credential.** Read-only reports need neither.
   An import-graph test, as for ADR-038, fails if an admin router can reach
   `nodes.admin_dsn`, `crypto.KeyRing`, or a provisioning job. Verifying a TOTP code needs the
   plaintext seed, so the admin process holds exactly one secret: the **staff key**
   (`MALUDB_STAFF_KEY_REF`), which encrypts staff TOTP seeds and nothing else. A leak of
   everything the admin process holds therefore exposes staff second factors — which still
   need each staff password — and no customer or node secret. `deploy preflight` fails if the
   staff key and the KEK are the same material.
   The admin process also connects to the control-plane database as **its own role**, granted
   SELECT on the tables the reports read and on the staff tables it writes, and nothing on any
   column `crypto.py` lists as envelope-encrypted (`nodes` admin and storage secrets,
   `project_credentials`, `api_keys`, project email settings, MFA factors) or on a password,
   session or token verifier,
   in the pattern ADR-072 and ADR-080 use — so a bug in a report cannot select a ciphertext
   either.
5. **The first version reads; it does not act.** Views are built on the functions `cp-manage`
   already has: sales (subscriptions by plan and state, recent billing events and their
   outcomes), usage (storage, file storage, egress and email per project, against plan
   ceilings), the abuse report, node capacity and health, and provisioning failures. Any
   action — suspend, move, change a plan, retry a job — stays a `cp-manage` command until an
   amendment to this ADR admits it, one class at a time, each requiring re-entry of the
   second factor and naming its `audit_events` type.
6. **Two kinds of looking, audited differently.**
   - **Platform records** — counts, plan codes, subscription states, byte figures, project
     refs, organization names and owner emails, billing events. These are the business's own
     records of who it serves; a staff session reads them without a grant, and each view of
     one organization's records writes `audit_events` (`actor_type = 'staff'`,
     `event_type = 'staff.view'`, the organization, the page).
   - **Customer content** — anything inside a tenant database, SQL, schema, API key material,
     provider keys, memory spaces' contents, Auth users. **Not reachable from the console at
     all.** Reaching it is the "support access" `docs/ACCOUNTS.md` describes — explicit,
     time-bounded, visible to the customer — and needs its own ADR.
7. **Amounts come from Stripe, not from the platform** (ADR-052 applies to staff too). The
   console shows plan codes, states, counts and links to the Stripe dashboard; revenue figures
   are Stripe's reports, not a sum the control plane computes from its own rows.

**Alternatives considered.**

- **Keep it a CLI and add reports.** Cheapest and safest; it does not give the owner a page
  to watch, which is the request.
- **A third-party admin (Retool, Metabase) pointed at the control-plane database.** Fast to
  build, but it needs a database login that can read every table — including encrypted
  credential rows and the `nodes` table — and moves staff authentication and audit outside
  the platform. Rejected.
- **An authenticating proxy in front of the internal application** (VPN identity, mTLS or
  oauth2-proxy passing a trusted header). Fewer moving parts, but it makes a header the
  credential, and ADR-037 already rejected concentrating trust in the network. Usable as an
  *additional* layer in front of decision 3.

**Consequences.**

- A new principal type, a second encryption key to hold and back up, a narrowed database role,
  three new tables (staff users, staff sessions, staff MFA factors or a
  shared factors table keyed by principal type), a new process and unit, a VPN requirement
  in `docs/DEPLOYMENT.md`, and a `deploy preflight` check that the admin listener is not on a
  public interface.
- Platform MFA is built first for staff. Customer MFA (`docs/OPEN-QUESTIONS.md`, "Platform
  MFA") can reuse the verifier but is not decided here.
- `audit_events` grows staff reads, which are frequent; retention and whether customers see
  `staff.view` entries on their organization page are open (below).

**Settled with acceptance.**

- **One staff role** in the first version: every staff account sees every read-only report.
  Roles arrive with the first write action or the first staff member who should see less.
- **Customers do not see `staff.view` events.** Platform records are the business's own; what a
  customer will see is support access to content, when that ADR exists.
- **Which VPN** is a deployment choice, not an architectural one. `docs/DEPLOYMENT.md` names the
  requirement (the admin listener is reachable only from the operator network) and preflight
  checks the bind address; the product that provides the network is the operator's.
- **Staff management is `cp-manage` only** (`staff create`, `enrol`, `revoke`, `list`), because
  with one role there is no staff member the console could safely let manage the others.

**Revisit if** staff grows past a handful (identity provider, roles), any write action is
wanted in the console (amend decision 5 per action), or support access to customer content
is needed (a new ADR).

## ADR-083 — The maintenance pass is split: control-plane passes on the control plane, sleeping workers on each node as its gateway

Status: **Accepted** 2026-09-16 by the repository owner, choosing the split over sleeping workers
remotely from the control plane or running every pass on the node. Implementation deferred by the owner
behind the free-tier launch. Answers "Where the maintenance pass runs" in `docs/OPEN-QUESTIONS.md`. Related: ADR-022 (free-tier density rests on
sleeping workers), ADR-027 (supervision is systemd, invoked rather than daemonised), ADR-038 (the
internet-facing process never holds node credentials), ADR-053 (purchases are applied by the
maintenance pass), ADR-072 (the gateway's narrowed role and own-node row policies), ADR-080 (a node
reports its own health as a role that can do one thing).

**Context.** `cp-manage maintenance run` executes `maintenance.run_all`, fourteen passes in one
process. All but one are control-plane work: retrying provisioning, ending billing grace,
reconciling subscriptions, measuring database and object storage, reconciling storage tenants and
objects, and checking replication slots, capacity, backups and plan drift. Between them they load
the KEK and unwrap node admin DSNs, which ADR-038 keeps on the control plane. The last pass,
`sleep_idle_workers`, stops each project's PostgREST, GoTrue and Realtime units through systemd --
and those units run on the node. On the two-machine deployment no host has both, so the rehearsal
schedules nothing, preflight fails "maintenance pass", and in consequence purchases are not
applied, storage is never measured or enforced, grace never ends and idle workers never sleep.

Measured while drafting:

- The gateway already holds everything the sleep pass needs. Its role reads and updates its own
  node's `projects` rows (ADR-072: `ALL TABLES` minus a list, narrowed to its node by row policy),
  including `worker_state` and `worker_last_active_at`, which the gateway itself writes on every
  request and when it wakes a worker. And `deploy/50-maludb-gateway.rules` already lets the
  `maludb-gateway` user start, stop and restart exactly `maludb-(postgrest|gotrue|realtime)@<ref>`
  units and nothing else.
- `workers.idle_workers` (and its Auth and Realtime siblings) are not filtered by node. As the
  gateway role that is already harmless -- row policies show it only its node -- but it should say so.
- **`maintenance_runs` has no row security and is not in `gateway_grants.UNREACHABLE_TABLES`**, so a
  gateway can insert rows there today. Preflight reads that table to decide whether the control-plane
  pass is running, so a compromised gateway could make a stopped pass look healthy.

**Decision.**

1. **Two schedules.** `maludb-maintenance.timer` on the control plane runs every pass except
   sleeping, as the control plane's role, about once a minute. `maludb-node-maintenance.timer` on
   each node runs only the sleep pass, as `maludb-gateway` with the gateway's own database role
   (`/etc/maludb/gateway.env`), about once a minute. Both are oneshot services, as ADR-027 prefers.
2. **The node half holds no new credential.** It runs as the gateway's OS user under the existing
   polkit rule and connects with the gateway's narrowed role. It never loads the KEK and needs no node
   admin DSN, and its reach is its own node by the same row policies that bound the gateway. Its
   module imports nothing that reaches a credential, asserted by an import-graph test as for the
   public application.
3. **The control-plane half stops trying to sleep.** `run_all` takes which passes to run; the
   control-plane command runs all but `sleep`, so it no longer calls systemd on a host where the units
   do not exist.
4. **The idle queries filter by node explicitly** when a node is named, rather than relying on row
   policies alone, so a node pass run by mistake with a wider role still touches only its node.
5. **Each half records its own runs, where only it can write.**
   - `maintenance_runs` becomes unreachable to the gateway (`UNREACHABLE_TABLES`), so only the control
     plane's role records control-plane runs.
   - A new `node_maintenance_runs (node_id, started_at, finished_at, slept, failed)` has the gateway's
     own-node row policy, so a gateway records runs for its node and no other.
6. **Preflight checks both halves.** "maintenance pass" stays as it is, for the control plane.
   "node maintenance" fails for an active node with no finished run in the last ten minutes, and warns
   on a last run that reported failures.

**Rejected.**

- **The control plane sleeps workers remotely** (SSH, or systemd over the network). It needs a
  standing path from the host holding every node's superuser DSN to a shell on every node. That is a
  new, broad trust edge, added to save a timer.
- **Everything runs on the node** with control-plane database access. That puts the KEK and every
  node's admin DSN on the internet-facing host, which ADR-038 and ADR-072 exist to prevent. One
  compromised node would open every other node's databases.
- **The gateway process sleeps workers itself**, on an internal timer. It has the privileges, but a
  loop inside the request-serving process is a second job to supervise inside a process ADR-081 is
  already careful about blocking, and ADR-027 prefers an invoked unit an operator can read, run by
  hand and stop. It stays a candidate if a timer per node becomes a burden.

**Consequences.**

- One new unit and timer per node, and one on the control plane. `docs/DEPLOYMENT.md` gains both, and
  the rehearsal installs them on 10.120.0.172 and 10.120.0.173.
- A migration (`node_maintenance_runs`, its policy) and a re-run of `cp-manage gateway grant` on every
  node, so `maintenance_runs` is revoked. Preflight already fails a gateway holding privileges on an
  unreachable table.
- A node whose gateway role cannot reach the database also stops sleeping workers. That is the right
  coupling: that node is not serving either, and its workers idle at no connection cost.
- Sleep and wake no longer share a host with billing, so a slow control-plane pass (object
  reconciliation, many nodes) cannot delay sleeping, and a stuck node cannot delay billing.

**Revisit if** nodes stop running a gateway (the node half would need a role of its own, on ADR-080's
pattern), a second pass turns out to be node-side (measuring object storage from the node, say), or a
per-node timer becomes operationally heavier than moving the loop into the gateway.

## ADR-084 — Auth is enabled for every project; its worker still starts on demand

Status: **Accepted** 2026-09-17 by the repository owner, opening the free tier (free slice 3).
Refines ADR-022; does not supersede it.

**Context.** `projects.auth_enabled` defaulted to false (migration 0009) so that a project would
get an Auth worker "because something asked for one". Nothing but the test suite ever set it: no
route, no dashboard control, no provisioning step. So the gateway answered 404 on `/auth/v1` for
every project a customer created, and the free tier's Auth -- which the public pages sell and the
official Supabase client assumes -- did not exist for customers.

**Decision.** Auth is enabled for every project: the column defaults to true and existing projects
are backfilled (migration 0057). ADR-022's cost property is unchanged, because the flag was never
what held it: the gateway starts a project's GoTrue worker on its first `/auth/v1` request, and the
node's maintenance pass (ADR-083) sleeps it after fifteen idle minutes. A project that never calls
Auth never runs a worker. `auth_workers.enable_auth` and the refusal for a disabled project stay, so
an operator can still switch Auth off for one project.

**Consequences.**
- Every project's first Auth request pays a cold start (GoTrue migrations on the very first, ADR-022's
  measured start after that), as its first Data API request already does.
- Auth now depends on email being configured in production (free slice 2): the gateway refuses to start
  a worker that would accept signups and send no confirmation.
- A dashboard switch to disable Auth per project is not built; `enable_auth`'s inverse is an operator
  action until one is needed.

## ADR-085 — A node's object store listens for S3 on the private network, for the control plane, behind a firewall of its own

Status: **Accepted** 2026-09-17 by the repository owner, with free-tier slice 4 (Storage). Related: ADR-035 (rootless containers do not reach node loopback), ADR-055 (SeaweedFS,
S3 as the boundary, the endpoint is configuration), ADR-057 (one platform bucket), ADR-058 (one shared
worker per node), ADR-069 (object durability), ADR-083 (the maintenance pass runs on the control plane).

**Context.** Nothing in production prepared a node for Storage. SeaweedFS existed only as
`scripts/storage-test-cluster.sh`: a process started by hand on a dummy interface that disappears on
reboot, with the S3 gateway, master, volume server and filer all on the data address `10.91.0.1`.
That is right for the container, and wrong for the other consumer the suite never exercised apart:
**the control plane.** ADR-083 puts the object passes there. It measures held bytes against the store,
because the fallback is `storage.objects`, which a customer holding `service_role` -- every free
project -- can rewrite to escape the ceiling; and it deletes a deleted project's objects
(`object_storage.delete_project_objects`). On two machines the data address is not routable from
the control plane, so both would quietly fall back or fail.

Measured while drafting, on SeaweedFS 4.41: `weed server` takes `-s3.ip.bind` separately from
`-ip.bind`. The S3 listener also opens a gRPC port (S3 + 10000) on the same address, which does not
authenticate, and 4.x starts an Iceberg REST catalogue on it by default. Master, volume server and
filer authenticate nothing -- the volume server returns any blob by file id and the filer reads and
writes every object.

**Decision.**

1. **The S3 gateway alone binds the node's private address**; master, volume server and filer stay
   on the data address. The worker and the control plane use one endpoint,
   `http://<node private address>:8333`, which is ADR-055's "addressed as though it were remote"
   taken literally. The Iceberg catalogue is switched off.
2. **A firewall table of the node's own** (`inet maludb_object_store`), loaded as root by the store's
   unit before it listens: S3 and its gRPC port from the control plane only; anything addressed to
   the data address from off-host dropped, because Linux accepts a packet for any local address on
   any interface and a neighbour with a route would otherwise reach the filer. It accepts nothing on
   anyone else's behalf and never flushes other rules.
3. **The control plane prepares the node** (`cp-manage node storage-prepare`): it alone holds write
   access to `nodes` for the storage root, the node's admin credential for the metadata database, and
   the S3 credential. It prints the node's two credential files to a pipe, never a terminal.
4. **The S3 credential lives in `provisioner.env`**, read by the provisioner (which deletes objects)
   and the maintenance pass (which measures them), and not in `control-plane.env`, which the
   internet-facing public application loads.
5. The data address becomes a systemd-networkd dummy interface, so it survives a reboot.

**Rejected.**

- **Object passes run on the node.** They need the KEK, write access to the control-plane database
  and the S3 credential together -- ADR-083 rejected exactly that combination on the internet-facing
  host.
- **The whole store on the private address.** Three unauthenticated services on a routable address,
  held back by a firewall alone.
- **The store on the control plane.** Customer bytes would cross the network on every request and
  sit on the host that also serves the public application.

**Consequences.**

- The S3 body travels unencrypted on the private network, as the email hook's does (slice 2).
  Requests are SigV4-signed, so the credential itself is not sent. TLS on the endpoint is the fix if
  the store moves to other hardware, and ADR-055's exit already requires a change of endpoint.
- `storage_tenants`, the reconciliation of the worker's tenant list, uses the worker's admin port on
  node loopback and so cannot run from the control plane. It reports "not ready; nothing reconciled"
  rather than failing. The gateway still registers a project on its first Storage request; what is
  lost is recovery when the worker forgets a tenant it had (its metadata database lost). Recorded for
  the launch leftovers.
- The store keeps one copy of every object on one disk (replication `000`). ADR-069 fails that in
  production when it can see the master; from the control plane it cannot, so
  `cp-manage storage durability` reports it as undeclared. This is stated in `docs/DEPLOYMENT.md` §2.7 rather than hidden, and the
  backup slice has to cover the store.

**Revisit if** the store moves off the node (TLS, and the firewall moves with it), a second node
shares one store, or SeaweedFS stops exposing an unauthenticated gRPC port beside S3.
