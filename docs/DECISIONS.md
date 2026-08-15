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

Status: Accepted

Free projects do not receive public direct PostgreSQL connection credentials. This prevents bypass of API-layer rate/concurrency/quota controls.

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

Status: Accepted

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

## ADR-018 — Tenant bootstrap must harden the exposed schema

Status: Accepted

Established by the Phase 00 spike (`tasks/PHASE-00-FEASIBILITY.md`), executed 2026-08-15 against stock PostgREST 14.17 and Supabase Auth 2.195.0.

`CREATE EXTENSION maludb_core CASCADE` installs 373 functions into `public`. PostgREST exposes the callable subset as RPC endpoints, and `anon` was able to invoke them — `/rpc/gen_salt`, `/rpc/armor`, `/rpc/pgp_key_id` and others were reachable on the public Data API.

Supabase's usual mitigation — installing extensions into a dedicated `extensions` schema — is **not available**: `maludb_core` hard-codes `public.gen_random_bytes`, and relocating its dependencies fails the install. That is an upstream defect to be raised against `maludb-core`, not worked around in the extension.

Every tenant bootstrap must therefore:

- revoke `EXECUTE` from `PUBLIC`, `anon`, and `authenticated` on every extension-owned function in the exposed schema, after installing `maludb_core` and before the project becomes reachable;
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
