# Open Questions

These items do not block creation of the planning repository, but each should be explicitly decided before the associated implementation is productionized.

## Public API surface and self-serve signup

Both surfaced 2026-08-15 while deciding repository topology (ADR-025). Neither blocks Phase 01; both are cheaper to answer before routes exist.

**Which control-plane endpoints are internet-reachable?** Public signup must be. Provisioning, node management, and admin operations must not be — `docs/SECURITY.md` requires internal endpoints not be publicly reachable, but nothing yet says which are which. `specs/control-plane-api.yaml` currently places everything under `/v1` with identical security schemes. Candidate approaches: separate listeners bound to different interfaces, a gateway-level path allowlist, or a public backend-for-frontend that proxies a deliberately narrow subset. This has deployment consequences and should be settled before Phase 07.

**What controls a self-serve free tier?** The only abuse controls currently written down are email-specific (`docs/EMAIL.md`). A public free tier attracts crypto mining, spam, and throwaway-account farming, all landing on shared nodes alongside paying tenants. Phase 07 covers the signup interface; nothing covers:

- signup velocity limits per source, and whether a challenge such as CAPTCHA is required;
- what a new free project may do before it is trusted — compute ceilings, egress limits, whether extensions or long-running queries are available at all;
- detection and response for mining or spam workloads, and who reviews it;
- account-farming defences, given one user may hold multiple organizations;
- the acceptable-use policy this enforces, which is still listed as unresolved under legal and compliance.

## Platform identity

Resolved 2026-08-15 — see `docs/ACCOUNTS.md`, ADR-020, ADR-021:

- ~~Are there organizations, and are they deferred?~~ Modelled from day one; projects are owned by organizations.
- ~~How do platform users authenticate to the control-plane API?~~ Server-side sessions plus personal access tokens; scheme defined in `specs/control-plane-api.yaml`.

Still open:

- Session lifetime and idle timeout.
- Is MFA mandatory for all users, or only for `owner`?
- Is SSO/SAML needed, and at which plan?
- Are project-scoped roles needed before general availability?
- Free-tier limits on organizations per user and members per organization.
- May a user belong to unlimited organizations?
- ADR-021 ratification: does platform identity stay off tenant infrastructure?

## Control-plane implementation

- Programming language/framework?
- Control-plane database?
- Background job mechanism?
- Redis/distributed cache or gateway-local cache first?
- ~~API gateway implementation choice?~~ Resolved by ADR-026: a Python ASGI proxy for the MVP, with a measured throughput number required at Phase 03 slice 3.

## Email

Resolved 2026-08-15 — see `docs/EMAIL.md` and ADR-019:

- ~~Which email provider?~~ The MaluDB-operated relay (`malumail`), over authenticated SMTP.
- ~~Is email optional for the first Auth milestone?~~ No. Confirmation, password reset, and magic link have no non-email alternative, and GoTrue fails silently without SMTP configured.

Still open:

- Exact email quota values per plan.
- Unconfirmed-user retention interval.
- Are custom sending domains a paid-only feature?
- Template customization: platform defaults, per-project overrides, or both?
- Does the relay or the control plane own the global suppression list?
- Does the relay need a dedicated SMTP submission endpoint per node pool, or one shared endpoint with per-project credentials?

## Domain/DNS

- Final public domain?
- Wildcard TLS/DNS strategy?
- Project ref format/length?
- Custom domains later?

## Email onboarding

- ~~**What sends a free project's first confirmation email?**~~ Resolved 2026-08-15 by
  ADR-029: the platform's own MaluMail account, under `sender_mode = platform_default`,
  behind a per-project rate limit read from the plan entitlement. A new project can send
  confirmations immediately with no customer onboarding; a customer going to production
  moves to `custom_domain` with their own key and verified domain.

## API workers

- ~~systemd template units vs another supervisor?~~ Resolved by ADR-027: systemd template units, `maludb-postgrest@<ref>.service`.
- separate API worker hosts vs colocated on DB nodes?
- inactivity duration for free workers?
- cold-start target?

## API keys/JWT

- ~~exact MaluDB key format?~~ Resolved by ADR-028: `mdb_publishable_<random>` / `mdb_secret_<random>`.
- asymmetric signing-key hierarchy?
- per-project key pairs vs managed key service?
- legacy Supabase key compatibility requirements?

## Database connectivity

- ~~When must a pooler be introduced?~~ Resolved 2026-08-15: before roughly 25 warm projects per node at default `max_connections`. It is required, not optional — ADR-022, `docs/CAPACITY.md`.
- which pooler, and deployed per node or centrally?
- direct DB endpoint architecture for paid users?
- password vs short-lived credential model later?

## Secrets and key management

Resolved 2026-08-15 — see `docs/SECRETS.md` and ADR-023: secret classification, hashing algorithms by entropy class, envelope encryption with AAD row binding, and the decision not to use MaluDB's in-database secret store for platform secrets.

Still open, and blocking production:

- **Where does the KEK live?** This is the load-bearing decision, and `MALUDB_SECRET_STORE=` in `.env.example` is still empty. Candidates for self-hosted Proxmox: a secrets manager such as Vault, systemd credentials, an operator-supplied file with strict permissions, or a hardware-backed store. An operator-supplied file is acceptable for development only.
- KEK and DEK rotation cadence.
- Whether per-project JWT signing moves to asymmetric/JWKS before general availability, per `docs/AUTH.md` — this changes what is stored, though not its class.
- Who may trigger a tenant database credential rotation, and how the dependent worker restart is sequenced safely.
- Break-glass procedure if the KEK is lost: which secrets are regenerable by re-provisioning and which represent unrecoverable state.

## Capacity and cost

Measured inputs are in `docs/CAPACITY.md`. Still open:

- target warm and total projects per node;
- production node hardware profile, and therefore `max_connections`;
- free-tier inactivity threshold before a worker sleeps;
- cost per project in currency, which needs hardware pricing;
- cold-start budget against a representative customer schema, not a one-table test.

## Resource limits

Exact initial values remain TBD:

- API requests/time window;
- concurrent API requests;
- active DB queries;
- PostgREST pool size;
- statement timeout;
- work_mem;
- temp_file_limit;
- parallel query limit;
- storage quota;
- Realtime limits.

Raised by ADR-017: since role/database GUCs are tenant-overridable, what actually enforces per-statement resource ceilings for **paid direct SQL**? Candidates are a pooler that pins settings, `temp_file_limit`, connection limits, node capacity management, or accepting monitoring-and-escalation only. This must be decided before direct database access ships in Phase 09.

## Node scheduling

- exact capacity score formula?
- reserve/headroom policy?
- separate node pools from launch or later?
- maximum tenant count safety cap?

## Backups

- physical backup technology?
- WAL archive target?
- logical per-DB backup schedule?
- restore workflow?
- paid retention/PITR tiers?

## Storage

- object-storage provider?
- tenancy/bucket design?
- egress model?

## Billing

- payment provider?
- prices?
- included usage?
- overage vs hard limits?

## MaluDB functionality

Resolved 2026-08-15 — see `docs/MALUDB.md` and ADR-012:

- ~~What is MaluDB — fork, extension, or reimplementation?~~ PostgreSQL 17 plus the `maludb_core` extension.
- ~~Does it support the PostgreSQL semantics this architecture assumes?~~ Yes; it is stock PostgreSQL 17.
- ~~Does it require `shared_preload_libraries` or background workers?~~ No, neither.
- ~~What does it cost per tenant database?~~ ~23 MB and ~2.5 s.

Also resolved:

- ~~Is `maludb_core` installed into every tenant database, or only on opt-in?~~ Every tenant database — ADR-015.
- ~~How do Supabase's cluster-scoped role names coexist with database-per-tenant?~~ Shared privilege-free `NOLOGIN` names plus a per-project authenticator — ADR-016, `specs/tenant-role-model.md`.
- ~~Are PostgreSQL role/database settings sufficient for resource enforcement?~~ No; they are defaults and mostly tenant-overridable — ADR-017.

Still open:

- Does the platform reuse MaluDB's in-database `auth_token_*` functions for project API keys, or keep the separate control-plane `api_keys` design in `specs/control-plane-schema.sql`? Both currently exist.
- ~~Does the platform use MaluDB's in-database secret store for tenant service credentials?~~ No — ADR-023. It would create a bootstrap circularity and put platform secrets in a tenant-adjacent store. It remains a tenant-facing product feature.
- Do `maludb-restd` and `maludb-realtimed` play any role in the Supabase-compatible data path, or do they remain a parallel MaluDB-native surface? `maludb-restd` currently lacks TLS and JWT signature verification.
- How do MaluDB's `current_account_id` account tenancy and the platform's database-per-tenant tenancy compose inside one tenant database? ADR-013 settles the security boundary — the database — but not whether a project maps onto a MaluDB account for product purposes.
- What is the tenant-fleet extension upgrade procedure — ordering, batching, failure isolation, per-tenant version tracking?
- Which dependency versions does provisioning pin? Version drift is already observable (`vector` 0.8.3 in the existing database, 0.8.4 in a database created today).
- exact memory features to expose first?
- SQL surface?
- API/SDK surface?
- compatibility interaction?

## Node configuration

- `max_connections` is still the PostgreSQL default of 100 on the development host. What is the production value, and what is the per-node budget formula relating tenants per node, PostgREST pool size, and direct-connection allowance?
- Is `pgaudit` enabled per tenant database, per node, or not at all? It is preloaded on the development host but not installed into any database.
- Which of `pg_graphql`, `pg_net`, `pg_cron`, `pgjwt`, `uuid-ossp` do platform nodes need? None of the first four are available today, which caps Supabase compatibility — see `docs/MALUDB.md`.

## Migration

- migration CLI vs dashboard first?
- required Supabase features for initial migration launch?
- downtime expectations?
