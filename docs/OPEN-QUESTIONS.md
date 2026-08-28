# Open Questions

These items do not block creation of the planning repository, but each should be explicitly decided before the associated implementation is productionized.

## Public API surface and self-serve signup

Both surfaced 2026-08-15 while deciding repository topology (ADR-025). Neither blocks Phase 01; both are cheaper to answer before routes exist.

Resolved 2026-08-16 — see ADR-037 and ADR-038:

- ~~Which control-plane endpoints are internet-reachable?~~ Two applications built from the same routers on separate listeners, a route internal until something mounts it publicly, and a test asserting the public route set. `/v1/plans` stays authenticated: it is an entitlement catalogue rather than a price list, and publishing `work_mem_mb` and the statement timeouts anonymously would hand an abuser every threshold to sit under.
- ~~Where do node superuser credentials live once a customer can create a project?~~ Not in the internet-facing application. Provisioning is enqueued and run by a worker (ADR-038).

**What controls a self-serve free tier?** **Decided 2026-08-16 by the repository owner: signup is public at launch**, not invite-only. That settles the question this one was waiting on and makes the rest Phase 07 scope rather than something deferrable to Phase 09 — a public free tier attracts crypto mining, spam, and throwaway-account farming, all landing on shared nodes alongside paying tenants, and the only abuse controls currently written down are email-specific (`docs/EMAIL.md`).

Two things are already in place and are worth naming, because they narrow what is left. Phase 05's entitlements give the free tier a deliberately tight envelope — an 8-second statement timeout, 4 MB `work_mem`, 10 connections, 500 MB of storage, no direct database access, no Realtime, and a worker that sleeps when inactive — so "what may an untrusted project do" is largely answered by configuration that already exists. And free projects require email confirmation.

What is **not** in place, and what Phase 07 must therefore carry:

- **The control plane has no rate limiting of any kind.** The limiters in `services/gateway/limits.py` front tenant traffic; nothing throttles `/v1/auth/signup` or `/v1/auth/signin`. That is a credential-stuffing surface independent of free-tier policy, and it is the first thing a public launch needs.
- signup velocity limits per source. **A CAPTCHA challenge is required from day one** (repository owner, 2026-08-16) rather than added once abuse appears, so the choice of provider and its failure mode — what happens to signup when the challenge service is down — is a Phase 07 slice 5 decision;
- account-farming defences, given one user may hold multiple organizations and each organization may hold projects;
- detection and response for mining or spam workloads, and who reviews it;
- the acceptable-use policy this enforces, which remains unresolved under legal and compliance and is not an engineering decision.

## Platform MFA

Deferred 2026-08-16 by the repository owner, on drafting the Phase 07 plan.

`tasks/PHASE-07-DASHBOARD.md` lists MFA enrolment in scope; nothing implements it, and it is self-contained enough to add later without reworking what Phase 07 builds. It is deferred rather than dropped, and the open part is *which* factors — TOTP alone, or WebAuthn as well — and whether organization owners can require it of members, which is the version that matters for a platform holding other people's databases.

Not to be confused with `mfa` in `specs/compatibility-matrix.yaml`, which is a *tenant* Auth feature for a customer's own end users. This entry is about platform users signing in to the dashboard.

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
- ~~Redis/distributed cache or gateway-local cache first?~~ Resolved by ADR-030 for the
  rate limiter, and by Phase 03's key cache for key material: gateway-local first, with the
  N-gateways multiplication recorded rather than glossed. Revisit when a second gateway is
  deployed.
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
- **~~Break-glass procedure if the KEK is lost: which secrets are regenerable by re-provisioning and which represent unrecoverable state.~~** **Answered 2026-08-28 by Phase 11 slice 5** (ADR-070), classified per column in `docs/SECRETS.md` and printed by `cp-manage control-plane break-glass`. Node and object-store credentials are regenerable by an operator; publishable API keys keep working but can no longer be displayed; SMTP and hook secrets are customer-supplied and re-entered by the customer; **per-project JWT signing keys are not recoverable**, so every end user of every project is signed out; and platform-user TOTP seeds are unrecoverable, which is the entry that decides whether operators can still reach their own dashboard.

  Slice 5 also found that the question had a sharper edge than "what is lost". A control plane restored from a dump missing `encryption_keys` used to **start successfully** and mint a replacement key, making the loss permanent and occupying the version the real keys needed. That is now refused.

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

**Partly settled by the Phase 11 plan, 2026-08-26.** The pool question is no
longer "from launch or later" — `nodes.node_pool` has existed since migration
0002 and `eligible_nodes` filters on it, but `api/projects.py` reserves
placement without passing a pool, so every project on the platform is in
`shared` by a parameter default. What is missing is the policy, not the
mechanism, and Phase 11 slice 6 proposes making it an entitlement so the
free/production split stays configuration-driven. The scoring formula and
headroom policy remain open; Phase 11 slice 8 has the capacity terms in hand
and is the natural place to close them.

## Backups

**Phase 11 is planned as of 2026-08-26 and these are its blocking questions.**
Unlike the Storage questions below, four of the five are **not** answerable
without measurement, so they are deliberately still open after the plan was
written: the discriminator between backup tools here is not throughput but
whether the tool works at all against ADR-031's `pg_hba.conf` reject of
physical replication, and that is a thing to test rather than to read about.
Phase 11 slice 0 answers them in `specs/backup-restore-model.md`. See
`plans/active/phase-11-production-resilience.md`.

**Two were answered 2026-08-26 by Phase 11 slice 0**, by measurement rather
than by research; the evidence is in `specs/backup-restore-model.md`. **A third
was answered 2026-08-27 by slice 1** — that one needed judgement rather than a
benchmark, and it was ratified as ADR-064 rather than settled in a plan. **A
fourth was answered 2026-08-28 by slice 3** (ADR-068), on the storage cost slice
0 measured. One remains open: the logical per-database backup schedule.

- **~~physical backup technology?~~** **Answered: pgBackRest** (ADR-067). The
  discriminator was never throughput. It was whether any base-backup tool can
  work at all on a node carrying ADR-031's `host replication all <cidr> reject`,
  and pgBackRest can, because it copies the data directory over an ordinary
  libpq connection between `pg_backup_start()` and `pg_backup_stop()` and opens
  no replication connection — 0 walsenders during a backup, while
  `pg_basebackup` on the same cluster is refused for the superuser. Measured
  both ways: on a cluster built without the reject, `pg_basebackup` succeeds.
  **ADR-031 needs no amendment.** Barman and wal-g were not examined.
- **~~restore workflow?~~** **Answered: restore to a scratch cluster, then
  extract the one database.** Measured end to end at **187 s** for a tenant on a
  219.7 MB base with ~720 MB of WAL to replay — and, more importantly, with the
  live node's nine tenant databases continuously available throughout, which is
  the acceptance criterion. Restoring in place would have satisfied nothing:
  `docs/BACKUP-RECOVERY.md` forbids making "restore one project" replace the
  node. The scratch cluster must have `archive_mode = off`, or a promoted copy
  pushes a new timeline into the repository it was restored from.

- **~~WAL archive target?~~** **Answered: a pgBackRest repository, and not in
  the node's own failure domain** (ADR-064, ratified 2026-08-27 in Phase 11
  slice 1). The interface was already settled by ADR-067; the location is now a
  rule that production enforces rather than a preference. `cp-manage node
  backup-check` refuses a repository co-located with the data directory when
  `MALUDB_ENV=production` and warns everywhere else — the split is what keeps
  the measurement cluster in `scripts/backup-test-cluster.sh` usable, since it
  puts both on one development box on purpose. The check is a path and `st_dev`
  comparison, so it catches the default mistake and **cannot** see an NFS mount
  on the same SAN or an S3 endpoint on the same Proxmox host; ADR-064 records
  that limit rather than leaving a green check to be misread as proof.
- logical per-DB backup schedule? — still open, now costed. A tenant dumps in
  2.0 s and restores in 6.5 s on the same cluster, so frequency is affordable;
  what it cannot give is a point in time between dumps.
- **~~paid retention/PITR tiers?~~** **Answered: retention and PITR are plan
  entitlements, and retention is a promise rather than a repository setting**
  (ADR-068, ratified 2026-08-28 in Phase 11 slice 3). A pgBackRest repository
  retains per stanza and a stanza is a whole node, so no setting anywhere makes
  one tenant's bytes outlive another's on the same cluster — which means the
  number a plan sells is how far back the platform will *honour a request*, and
  the same number is what a node's `repo1-retention-full` is held to. Shipping
  defaults: free 7 days and no point in time, starter 14 days with a 7-day
  window, production 30 days with a 30-day window, all overridable in
  `plans.config_json`.

  **Free is backed up and gets no point in time.** Its bytes are in the node
  backup regardless, at about 2.5 MB of repository per tenant after the measured
  9.4:1 compression, so a retention of zero would have been a fiction told for a
  pricing reason. PITR is the half with a marginal cost — the archive, plus a
  ~3-minute scratch-cluster restore per request.

  One sharp edge came out of it, and it is the reason the check is honest rather
  than green: `repo1-retention-full` is a **count of full backups** unless
  `repo1-retention-full-type=time` is set, and a count cannot be compared with a
  window in days without knowing the backup schedule. So the node check has
  three outcomes — kept, not kept, and *not checkable* — and the third is
  reported as a warning naming the option that would fix it.

Two things slice 0 found that this section never thought to ask, both recorded
in ADR-067 because they are how a backup system fails without saying so:

- **An untuned pgBackRest backup of an idle cluster waits forever.** Its default
  is to begin after the next regular checkpoint, and PostgreSQL skips timed
  checkpoints when no WAL has been written. Measured: 15+ minutes at 0% CPU,
  `num_timed = 0` after forty minutes of uptime. That is the free tier's exact
  shape, so the nightly backup of a node full of sleeping projects hangs rather
  than fails. Every scheduled backup passes `--start-fast`.
- **Moving a tenant to another node silently reassigns schema ownership to
  whoever ran the restore** — the platform superuser. `auth` and `storage` go
  from their per-tenant service roles to `postgres`, while all 164 RLS policies
  and every row arrive intact. ADR-059 puts the `storage` schema under a
  per-tenant role precisely so it is not owned by something with superuser
  reach. A move must recreate the per-tenant roles first and verify ownership
  after; `pg_restore`'s exit code is not enough.

One question the section did not have, added while planning Phase 11:

- **what backs up the control plane, and what recovers its key material?**
  `encryption_keys` holds the KEK-wrapped data encryption keys (ADR-023), and
  every node admin DSN on the platform is unwrapped through them. A node
  restored without them is a node full of databases the platform cannot
  administer. This is in no phase's scope bullets and is now Phase 11 slice 5.

And one the Storage phase left here rather than answering, restated because
Phase 11 is where it lands: **a project is two data sets.** Restoring the
tenant database to a point in time without the objects it references produces
rows whose bytes are gone and bytes no row can reach. Phase 11 must state what
a point-in-time restore does to objects rather than leaving it implied.

## Storage

**All three were answered 2026-08-22 while planning Phase 10 and recorded as
ADR-055 to ADR-057, and Phase 10 closed on 2026-08-25 having built on them.**
They are kept here with their answers because the reasoning is what a later
reader needs and the ADRs assume it; `docs/STORAGE.md` is what was actually
built.

**One Storage question is open and was opened by the phase rather than
answered by it:** a customer cannot author an RLS policy on `storage.objects`,
because `CREATE POLICY` requires ownership and the owner is a
platform-internal role. ADR-061 records why the Supabase-shaped answer —
granting the tenant admin membership in that role — is not available here, and
leaves the mechanism undecided. The shape that gives authoring safely is a
platform-mediated surface that validates what it creates.

- **~~Object-storage provider?~~** **Answered 2026-08-22: SeaweedFS, with S3 as
  the boundary and the endpoint as configuration** (ADR-055). The platform
  writes no provider abstraction of its own: `storage-api` already has one
  (`STORAGE_BACKEND`, `STORAGE_S3_ENDPOINT`), so `docs/STORAGE.md`'s
  vendor-decoupling requirement is met by configuration rather than by code we
  would then own. MinIO was the obvious answer and was withdrawn on checking its
  current state rather than its reputation — community edition archived
  25 April 2026, no binaries, no security patches, which is disqualifying for
  the component holding every customer's files. SeaweedFS is Apache 2.0 rather
  than AGPL, which matters for a commercial platform and is also what rules out
  Garage. Ceph RGW would be right if the Proxmox cluster already ran Ceph and is
  disproportionate otherwise.

  Bytes live on the existing Proxmox hardware for now, not a dedicated storage
  box, with the exit stated in the ADR. What makes that exit cheap is an
  invariant rather than discipline: ADR-035 already forbids a container from
  reaching node loopback, so the object store is addressed as though remote even
  while it is local.

- **~~Tenancy/bucket design?~~** **Answered 2026-08-22: one platform bucket,
  tenancy in metadata** (ADR-057). `STORAGE_S3_BUCKET` is singular upstream — a
  customer "bucket" is a row in the tenant's `storage.buckets`. Keeping that is
  not really a choice: bucket-per-project would put an object-store API call in
  the provisioning path, coupling the tenant lifecycle to the vendor, which is
  the one thing `docs/STORAGE.md` requires this phase not to do.

  The half worth carrying forward: **tenant isolation for objects is not
  provided by the object store.** One bucket holds every tenant's bytes, so
  "cross-project object access is denied" is a property of code this platform
  writes and must be tested as a denial against a real second project.

- **~~Egress model?~~** **Answered 2026-08-22: hard ceilings on every tier,
  including free** (ADR-056). `object_storage_bytes` and
  `egress_bytes_per_month` are entitlements enforced at the point of use under
  ADR-050 — refused when exceeded, never a charge, never reported to a provider.
  The platform still has no metering pipeline and this does not build one.

  Free gets Storage because Storage over the gateway is API access, which is
  inside ADR-005 rather than against it, and because `services/migrate` turns
  away every Supabase project using Storage today — the customer this
  compatibility work exists to reach. The paid line stays where ADR-039 put it:
  the **S3 protocol endpoint** is a credential and a reachable port, and is
  deferred to its own decision rather than inherited.

## Billing

Expanded 2026-08-19 while planning Phase 09, because four words each understated
what they decide. **All four were answered 2026-08-20 and recorded as ADR-049
to ADR-052.** Nothing in `plans/completed/phase-09-billing.md` is blocked on this
section any more. The questions are kept here with their answers, because the
reasoning is what a later reader needs and the ADRs assume it.

Slice 3 came out of the blocked set earlier, under ADR-048, and the reason
generalises: subscription state is the part of billing that does not depend on
who takes the money. It records what has been paid for in MaluDB's own
vocabulary and reconciles it through `plan_change`, so it would have survived
any answer below. That is what "provider-shaped but not provider-specific"
was asking for, and the reason the slice was worth doing before the answers
arrived.

- **~~Which provider, and is it a merchant of record?~~** **Answered
  2026-08-20: Stripe, with merchant-of-record status as configuration rather
  than code** (ADR-049). The question assumed processor and MoR were different
  vendors; they are not any more. **Stripe Managed Payments** is Stripe's own
  MoR — Stripe becomes the legal seller and registers, files and remits sales
  tax, VAT and GST in 80-plus countries including the US, the EU 27 and the UK
  — and it is the same API as plain Stripe, enabled per account and per
  Checkout Session. Stripe Tax, by contrast, calculates and monitors
  thresholds and leaves MaluDB the seller: it does not register, file, or take
  liability. So the tax posture is a deployment decision and the integration is
  built once. What it does constrain: Managed Payments works only with hosted
  Checkout and Payment Links, so slice 4 must not use Elements, and a
  subscription cannot be created outside Checkout. The recommendation is to
  launch with it on — 3.5% on top of processing, against a free tier where
  most projects never generate a tax event and a failure mode of back-VAT plus
  penalties whose size somebody else chooses.
- **~~Overage, or hard limits?~~** **Answered 2026-08-20: hard limits**
  (ADR-050). Entitlements are ceilings, refused at the point of use; no usage
  quantity is ever reported to a provider and no metering pipeline exists. The
  choice reinforces ADR-049 rather than merely coexisting with it: Managed
  Payments cannot bill overage at all — no invoice items on a subscription, no
  one-off invoices outside the billing period — so adding metered billing later
  would be a decision to leave merchant-of-record status, not an incremental
  feature.
- **~~What does a failed payment do, and for how long?~~** **Answered
  2026-08-20: fourteen days of unchanged service, then ADR-040's storage
  restriction, and never deletion** (ADR-051). `past_due` keeps its plan for
  the grace period; at its end the subscription is `canceled`, `reconcile`
  hands the default plan to `plan_change`, writes stop and reads do not. The
  database, `project_ref`, API keys and rows all survive, per ADR-006. Fourteen
  days is configuration, not a constant in application logic — a grace period
  is a plan limit and the development rules forbid hard-coding those.
- **~~Prices in the repository, or only in the provider?~~** **Answered
  2026-08-20: only in the provider** (ADR-052). The platform stores
  `plan_code` -> Stripe price id plus the product tax code ADR-049 requires,
  and stores no amount, no currency, and no logic that computes what a customer
  owes. Two sources of truth for a charged number is the drift that becomes a
  refund. `specs/plans-and-limits.yaml` stays an entitlement catalogue, which
  is what `plans.router` already calls it.
- **~~Does a paid customer receive `mldb_<ref>_admin`'s password, or a role of
  their own?~~** **Answered 2026-08-19: a role of their own** (ADR-047,
  shipped in Phase 09 slice 2). Kept here for the reasoning, which is why:
  the admin role is what the platform's own mediated SQL enters (ADR-039),
  what maintenance uses, and what `specs/tenant-role-model.md` bounds — so
  handing out its password would make rotation a platform outage, make
  revoking direct access indistinguishable from breaking the SQL console, and
  put the identity the platform acts under into a customer's `.env`.
  `mldb_<ref>_client` holds the same grants through membership and is
  revocable and rotatable on its own.

Still open, and narrower than the question it came out of:

- **Is a project restricted for a very long time ever reclaimed?** ADR-051
  settles that a failed payment never destroys data, and accepts indefinite
  retention as the cost of that. It deliberately does not settle whether a
  project sitting restricted for, say, a year is eventually removed after
  explicit and *delivered* notice. Nothing in Phase 09 depends on the answer;
  it is recorded so that the eventual answer is a decision rather than the
  outcome of a storage bill. Whatever it is, it needs a notice mechanism that
  can show delivery, which the platform does not have today.

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

## Control-plane memory under tenant-shaped responses

Raised 2026-08-19 by ADR-046, which bounds half of it.

- **How is the transient half of a tenant result bounded?** libpq buffers a
  whole result set before the platform can refuse a byte of it, so
  `sql_console_max_bytes` bounds what is *held* while a response is serialised
  and read, and not the ~200 MB spike a `SELECT repeat(...)` produces on its
  way in. Measured: 202 MB peak with a 100 MiB budget and 203 MB with a 2 MiB
  one, against 100.0 MB and 2.0 MB retained. Three candidate answers, none
  costed: single-row mode through `pgconn` below psycopg's `stream`, which
  would mean building rows from `PGresult` by hand; fetching in a process whose
  memory can be capped and killed; or a per-request guard that abandons a
  connection once the process crosses a threshold. Streaming through
  `Cursor.stream` is not one of them -- it is the extended protocol and this
  route takes multi-statement text.

- **Should the control plane assert its own memory limit?** ADR-046's residual
  is bounded operationally rather than in code, and this repository does not
  currently say what that limit is or where it is set. A deployment that runs
  the API without one inherits the whole of the residual.

## Node configuration

- `max_connections` is still the PostgreSQL default of 100 on the development host. What is the production value, and what is the per-node budget formula relating tenants per node, PostgREST pool size, and direct-connection allowance?
- Is `pgaudit` enabled per tenant database, per node, or not at all? It is preloaded on the development host but not installed into any database.
- Which of `pg_graphql`, `pg_net`, `pg_cron`, `pgjwt`, `uuid-ossp` do platform nodes need? None of the first four are available today, which caps Supabase compatibility — see `docs/MALUDB.md`.

## Migration

Resolved 2026-08-17 by the repository owner, before Phase 08 slice 5 — see
ADR-042, ADR-043, ADR-044 and ADR-045:

- ~~migration CLI vs dashboard first?~~ **A CLI the customer runs.** The
  deciding argument is custody rather than developer experience: a scanner must
  read the *source* Supabase project, and a dashboard-driven one means the
  control plane accepting, storing and using a third party's production
  credential — a secret class `docs/SECRETS.md` does not have, whose blast
  radius is somebody else's platform and whose revocation path we do not own.
  Run from the customer's machine it never leaves it. The CLI drives the same
  slice 1-3 API a dashboard would, with no privileged path of its own.
- ~~required Supabase features for initial migration launch?~~ **Exactly what
  `specs/compatibility-matrix.yaml` marks `supported`**: the database, email and
  password Auth users, and Realtime Postgres Changes. Storage, OAuth/magic
  link/MFA/SSO identities, broadcast, presence and Edge Functions are scanner
  *blockers* naming the phase that will carry them. The matrix stays the
  authority, so promoting a surface grows the migration scope without amending
  the ADR.
- ~~downtime expectations?~~ **A controlled write freeze, with a published
  window measured by slice 8's validation runs** rather than estimated.
  Zero-downtime stays a later objective and is claimed nowhere until it is
  implemented and tested. The platform cannot enforce the freeze — the source is
  Supabase — so the validation step compares row counts and names any table that
  moved during the migration.
- ~~may a customer install an allowlisted extension themselves?~~ **Yes**, from
  `specs/extension-allowlist.yaml`, through a `SECURITY DEFINER` installer that
  refuses anything else. ADR-010 forbids only *arbitrary* extensions and the
  implementation was stricter than the decision. The security half already
  exists: bootstrap 005 revokes the new functions from `anon` on the same
  `CREATE EXTENSION` and aborts the install if the revoke fails.

Still open, and raised by the answers above:

- **May a migration be driven from the dashboard, and if so where does the
  customer's Supabase credential live?** ADR-042 defers rather than forbids it.
  Answering it means either a credential class in `docs/SECRETS.md` for
  third-party production secrets, or a browser-side design where the credential
  never reaches the control plane. Not blocking: the CLI covers the case.
- **Is ~1.9 MiB/s an acceptable cutover rate, or does migration need a
  transport of its own?** Measured by Phase 08 slice 8's validation runs and
  published in `docs/CUTOVER-RUNBOOK.md`: 1,000,000 rows, 160.5 MiB, 84.5s --
  roughly nine minutes per GiB, so 10 GiB is a ninety-minute write freeze and
  50 GiB is most of a working day. It falls directly out of ADR-042's custody
  argument: rows move as multi-row `INSERT` through the same public SQL route
  every other caller uses, because the migration has no privileged path. The
  question is whether that is acceptable for the customers being targeted at
  launch, and it is better answered before someone books an outage around it
  than after. A faster transport is not a small change -- it is a second way
  into a tenant database, which is the thing ADR-039 and ADR-042 were careful
  to avoid creating.

- **Does the extension allowlist change a node's capacity model?** Extensions
  are per-database and some are large. `docs/CAPACITY.md`'s per-project cost is
  measured with the provisioning set, and ADR-045 lets a customer add to it.
  PostGIS is absent from the allowlist for this reason rather than a security
  one.
