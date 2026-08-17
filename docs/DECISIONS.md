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
