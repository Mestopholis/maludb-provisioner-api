# MaluDB Platform

MaluDB Platform is a managed database platform designed as a lower-cost, production-oriented alternative for applications currently built on Supabase.

The initial product strategy is:

1. Provide a Supabase-compatible developer surface.
2. Make migration from Supabase low-friction.
3. Run each customer project as its own PostgreSQL/MaluDB database on an existing shared MaluDB node.
4. Add MaluDB-specific memory/database capabilities without breaking Supabase compatibility.

## Status

This is an implementation repository. It began as planning and scaffolding;
every phase in `docs/ROADMAP.md` is now code with tests behind it, and what is
left before a production launch is mostly decisions rather than features (see
"Still undecided").

| Phase | State |
|---|---|
| 00 — Feasibility spike | Complete |
| 01 — Foundation | Complete |
| 02 — Tenant provisioning | Complete |
| 03 — Supabase-compatible Data API | Complete |
| 04 — Auth and RLS | Complete |
| 05 — Resource governance | Complete |
| 06 — Realtime | Complete — Postgres Changes, with the official client, in a test |
| 07 — Customer dashboard API | Complete — account, project creation, keys, usage, abuse controls |
| 08 — Supabase migration | Complete — scan, schema, data, Auth users, verify, cutover runbook |
| 09 — Billing | Complete — Stripe Checkout, hard plan limits, paid direct SQL, grace period |
| 10 — Storage | Complete — buckets and objects through the official client, SeaweedFS |
| 11 — Production resilience | Complete — pgBackRest, per-tenant PITR restore, tenant moves, node rebuild |
| 12 — MaluDB-native features | First surface complete — the data-model graph; extension version pinning in progress |

Execution plans for each are in `plans/completed/`; `plans/active/` holds what is
still moving.

Phase 06's state is worth stating precisely, because "Realtime" is easy to
over-read. `@supabase/supabase-js` subscribes over the gateway, a row is written
to a tenant table, and the change arrives — in a test, against a real
`supabase/realtime` instance and a tenant the platform provisioned, which is
what promoted `postgres_changes` to `supported` in
`specs/compatibility-matrix.yaml`. Each Realtime project runs its own instance
(ADR-034), woken on demand and slept after an idle hour.

What is **not** claimed: RLS over Postgres Changes. The replicator reads every
table past policies, so the Realtime server is the only thing that can enforce
them, and nothing automated yet shows a subscriber being refused rows a policy
hides. Broadcast and Presence are deliberately later. See
`plans/completed/phase-06-realtime.md`.

The control-plane stack is Python 3.12, FastAPI and psycopg3 with no ORM
(ADR-024). The gateway is a Python ASGI proxy for the MVP, on a measured
throughput number (ADR-026). The web frontend lives in its own repository
(ADR-025); this one is backend-only.

## What works today

- **Project creation.** `POST /v1/organizations/{org_id}/projects` (owners and
  admins, idempotent) places the project on a node and queues it; a provisioner
  process builds it, so the internet-facing API never holds a node credential
  (ADR-038).
- **Tenant provisioning.** A project is a database plus constrained roles on an
  already-running cluster — never a VM. Retryable state machine, `CONNECT`
  lockdown, a per-project authenticator, `maludb_core` per database, versioned
  tenant bootstrap, and a cleanup path that refuses to drop a database holding
  customer objects.
- **Data API.** The gateway resolves the project from the hostname, validates a
  project-scoped key, checks that hostname and key agree, wakes the worker, and
  proxies to a per-project PostgREST supervised by a systemd template unit
  (ADR-027). CRUD, RPC, filters, ordering, ranges, counts and RLS are verified
  with the official `@supabase/supabase-js` client through the real gateway.
- **Auth.** Per-project GoTrue, also a systemd template unit. Signup, sign-in,
  refresh, get-user and sign-out are verified the same way, with email
  confirmation on — and an end-user JWT drives `auth.uid()` in RLS policies.
- **Resource governance.** One entitlement resolver answers "what is this
  project entitled to"; gateway rate and concurrency limits, PostgreSQL per-role
  settings, storage accounting and quota enforcement, worker sleep/wake, and
  node capacity enforced rather than merely measured.
- **Realtime.** Postgres Changes reach `@supabase/supabase-js` through the
  gateway. Each project runs its own `supabase/realtime` instance under systemd
  and Podman (ADR-033, ADR-034), woken on demand and slept after an idle hour --
  an instance is ~146 MB, four times an entire warm project. The container
  reaches this node's PostgreSQL and nothing else on it (ADR-035).
- **Realtime node safety.** `wal_level`, `wal2json`, a bounded
  `max_slot_wal_keep_size` and a `pg_hba.conf` reject of physical replication
  are checked node preconditions (ADR-031, ADR-032). Replication slots are a
  third placement ceiling. A stalled consumer is demonstrated to lose its slot
  rather than the node losing its disk.
- **Storage.** Buckets, upload, download, list, delete, signed URLs and public
  URLs reach `@supabase/supabase-js` through the gateway, with RLS on
  `storage.objects` deciding what a caller may read. Bytes live in SeaweedFS
  (ADR-055) and demonstrably not in the tenant database; one shared
  `supabase/storage-api` per node serves every tenant (ADR-058), and one
  platform bucket holds every tenant's objects, so isolation is a property of
  metadata and credential scoping and is tested as a denial across two real
  projects (ADR-057). Available on every tier, bounded by held-byte and
  monthly-egress ceilings that refuse rather than bill (ADR-056, ADR-060). A
  customer cannot yet author a storage policy (ADR-061).

- **Extension functions.** pgvector search, `uuid_generate_v4()` defaults and
  `crypt()` in triggers work for `anon`, signed-in users and `service_role`, as on
  Supabase, while extension functions stay off `/rpc` behind a platform-owned
  PostgREST pre-request check (ADR-076, which replaced ADR-018's revoke). Verified
  with the official client against a migrated schema. Existing tenants get it from
  `cp-manage extension grants --node <n>`.
- **Migration from Supabase.** `maludb-migrate scan | apply [--with-data] |
  verify`, run by the customer against their own source, writing through the
  public API: schema with RLS, functions, triggers and indexes; rows; and email
  and password Auth users. `docs/CUTOVER-RUNBOOK.md` is the cutover; measured at
  about 9 minutes per GiB. Every tier also gets platform-executed SQL,
  introspection and role impersonation for RLS debugging instead of a database
  credential (ADR-039). OAuth, magic-link, MFA and SSO users are migration
  blockers.
- **Billing.** Stripe hosted Checkout, with merchant of record through Stripe
  Managed Payments (ADR-049); hard plan limits, no overage and no metering
  (ADR-050). The webhook records what was paid for and the maintenance pass
  applies it (ADR-053). Paid plans get direct SQL through a separate client role
  (ADR-047). A lapsed subscription keeps 14 days of unchanged service, then
  falls back to the free plan with writes restricted and nothing deleted
  (ADR-051).
- **Backup and recovery.** pgBackRest per node (ADR-067), with backup age and
  completeness checked by the maintenance pass and retention and PITR as plan
  entitlements (ADR-068). `cp-manage restore run` recovers one tenant to a point
  in time into a scratch cluster beside the live database while its neighbours
  keep serving (measured at 187 s). `cp-manage node rebuild` restores a lost node
  (measured RTO: 279 s for 22 tenants, 1.6 GB). The control plane has its own
  backup and a break-glass procedure for lost key material (ADR-070).
- **Tenant movement.** `cp-manage project move` moves a stopped project between
  nodes, keeping its ref, hostname, keys and data; the source is frozen by
  revoking `CONNECT` and renamed aside, never dropped (ADR-071). Moves are
  operator-initiated only (ADR-066).
- **Gateway narrowing.** The internet-facing gateway has its own database role
  that cannot read node admin credentials and sees only its own node's rows
  (ADR-072).
- **MaluDB data-model graph.** Opt in per project, refresh on request within a
  per-plan hourly budget, and read a copy of the database's own structure —
  relations, their descriptions and how they connect — through the official
  client with a secret key (ADR-074, `docs/MALUDB-FEATURES.md`). Enabling it
  demonstrably leaves the `public` API unchanged, and it can be switched off
  without deleting anything.
- **Extension version pinning (in progress).** `vector` and `maludb_core` are
  pinned per node to versions CI has tested (`specs/extension-versions.yaml`),
  and a node whose packages disagree takes no new projects, restores or moves
  (ADR-075). Installing the pin at provisioning and extending the upgrade run to
  `vector` are the remaining slices.

`specs/compatibility-matrix.yaml` is the authoritative answer to what is
supported. A feature moves off `planned` only when a test drives it with the
official client, through the real gateway, against a provisioned tenant.

## What is not built yet

- **RLS over Postgres Changes.** The replicator reads past grants and policies,
  so the Realtime server is the only thing that can enforce them, and nothing
  automated yet shows a subscriber being refused rows a policy hides. Broadcast
  and Presence are later by design.
- The dashboard **interface**. Phase 07 built the API it consumes -- accounts,
  projects, keys, usage, abuse controls -- and ADR-025 puts the web frontend in
  its own repository. Platform MFA is deferred (`docs/OPEN-QUESTIONS.md`).
- **Customer-authored storage policies.** RLS on `storage.objects` is enforced
  and a customer cannot write one: `CREATE POLICY` needs ownership of the table
  and the owner is a platform-internal role. Deferred deliberately (ADR-061) —
  the grant that would fix it is owner-level bypass of every storage policy.
  Signed *upload* URLs, resumable uploads, image transformation and the S3
  protocol endpoint are deferred with it; `docs/STORAGE.md` has each reason.
- A connection pooler, which ADR-022 says is required: connections, not memory,
  bound warm density. Which pooler, and whether per node or central, is open.
- A scheduler. `cp-manage maintenance run` is a command, not a daemon, and
  `deploy/` ships no timer for it; the deployment runbook says to schedule it.
- High availability: no replicas or automatic failover, no cross-region
  replication, no automatic rebalancing between nodes, and no alert delivery for
  the capacity and backup reports (all deferred by Phase 11).
- **Further MaluDB features.** The memory pipeline needs a decision on how a
  project maps onto MaluDB accounts first; vector search waits on version
  pinning; the SVPOR knowledge graph is not scoped.

## Running it

`AGENTS.md` is the canonical setup: dependencies, development key material, the
control-plane database, migrations, and the two processes — the control plane
(`services.control_plane.main:create_app`, port 8111) and the gateway
(`services.gateway.main:build`, port 8110). Read the testing section there
before trusting a green run: without a node admin DSN, a Realtime node DSN and a
container runtime, the suite **skips** the isolation assertions and prints a
`security properties not verified` banner rather than failing.

Operations go through `cp-manage`: node registration, health,
`realtime-check`, `backup-check`, extension pins and `extension-check`; project
provisioning, cleanup, plans, email mode, direct SQL, Realtime enablement,
storage, moves and restores; API key issue, list, reveal and revoke; billing
prices; extension upgrades and grants; maintenance passes; capacity,
replication-slot and drift reports.

Deploying it — the two machines, their systemd units in `deploy/`, and the order
of operations — is `docs/DEPLOYMENT.md`.

## Repository purpose

This repository is intentionally structured so that human developers, OpenAI Codex, and Claude Code can all work from the same project knowledge.

- `AGENTS.md` — canonical agent working agreement, and the setup/testing reference.
- `CLAUDE.md` — thin Claude Code adapter that imports `AGENTS.md`.
- `PLANS.md` — execution-plan rules.
- `docs/` — product and architecture decisions, including `DECISIONS.md` (ADRs) and `OPEN-QUESTIONS.md`.
- `specs/` — machine-readable or implementation-oriented specifications.
- `tasks/` — phased implementation scopes and acceptance criteria.
- `plans/` — active/completed execution plans.
- `services/` — the control plane and the gateway.
- `tests/` — the Python suite; `tests/compat/` is the black-box suite driven by the official Supabase client.
- `deploy/` — systemd template units for the per-project workers.
- `scripts/` — operational and test-environment helpers.

## Product north star

An existing Supabase application should eventually be able to switch its project URL and API key to MaluDB and continue operating with minimal or no application-code changes for supported features.

```javascript
import { createClient } from '@supabase/supabase-js'

const client = createClient(
  'https://<project-ref>.maludb.com',
  '<maludb-publishable-key>'
)
```

That now works for the Data API, Auth, Realtime Postgres Changes and Storage
against a provisioned tenant, including one whose schema was migrated in from a
Supabase-shaped source. It is not yet a blanket claim of Supabase compatibility —
`AGENTS.md` forbids one until the matrix and the automated tests support it, and
the matrix names what is deferred or intentionally different.

## Still undecided

Tracked in `docs/OPEN-QUESTIONS.md`, and the real critical path to a production
launch:

- **Where the production KEK lives** (Vault, systemd credentials or hardware).
  ADR-072 keeps it on the node; a key file is acceptable for development only.
- **What enforces per-statement resource ceilings for paid direct SQL**, since a
  customer can override role settings.
- **Final plan limits** — request rates, statement timeout, `work_mem`, storage
  quotas — and the capacity targets and hardware they rest on.
- **Which connection pooler**, and whether it runs per node or centrally.
- The final domain and TLS strategy, platform MFA, and whether per-project JWT
  signing moves to asymmetric keys before general availability.

Settled since this list was first written: the billing provider is Stripe
(ADR-049), the object store is SeaweedFS (ADR-055), and rate-limit state stays
gateway-local until a second gateway exists (ADR-030). `docs/DECISIONS.md`
records everything that has been decided.
