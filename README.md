# MaluDB Platform

MaluDB Platform is a managed database platform designed as a lower-cost, production-oriented alternative for applications currently built on Supabase.

The initial product strategy is:

1. Provide a Supabase-compatible developer surface.
2. Make migration from Supabase low-friction.
3. Run each customer project as its own PostgreSQL/MaluDB database on an existing shared MaluDB node.
4. Add MaluDB-specific memory/database capabilities without breaking Supabase compatibility.

## Status

This is an implementation repository. It began as planning and scaffolding; the
stack, the gateway, provisioning, the Data API, Auth and resource governance are
now code with tests behind them.

| Phase | State |
|---|---|
| 00 — Feasibility spike | Complete |
| 01 — Foundation | Complete |
| 02 — Tenant provisioning | Complete |
| 03 — Supabase-compatible Data API | Complete |
| 04 — Auth and RLS | Complete |
| 05 — Resource governance | Complete |
| 06 — Realtime | Complete — Postgres Changes, with the official client, in a test |
| 07 — Customer dashboard API | Complete — account, project, keys, usage, abuse controls |
| 08–12 | Not started (`docs/ROADMAP.md`) |

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

- **Tenant provisioning.** A project is a database plus constrained roles on an
  already-running cluster — never a VM. Retryable state machine, `CONNECT`
  lockdown, a per-project authenticator, `maludb_core` per database, versioned
  tenant bootstrap including the ADR-018 extension-function revoke, and a
  cleanup path that refuses to drop a database holding customer objects.
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

`specs/compatibility-matrix.yaml` is the authoritative answer to what is
supported. A feature moves off `planned` only when a test drives it with the
official client, through the real gateway, against a provisioned tenant.

## What is not built yet

- **Self-service project creation.** The projects API is read-only, and there is
  no `cp-manage project create`. Creating a project today means inserting the
  row and reserving placement before `cp-manage project retry` has anything to
  provision. This is the largest gap between the completed phases and a usable
  platform.
- **RLS over Postgres Changes.** The replicator reads past grants and policies,
  so the Realtime server is the only thing that can enforce them, and nothing
  automated yet shows a subscriber being refused rows a policy hides. Broadcast
  and Presence are later by design.
- The dashboard **interface**. Phase 07 built the API it consumes -- accounts,
  projects, keys, usage, abuse controls -- and ADR-025 puts the web frontend in
  its own repository. Platform MFA and customer database/admin tooling are
  deferred (`docs/OPEN-QUESTIONS.md`, and Phase 08 respectively).
- Supabase migration tooling (Phase 08), billing and paid direct database access
  (Phase 09), Storage (Phase 10), backups/PITR/tenant movement (Phase 11).
- A connection pooler, which ADR-022 says is required: connections, not memory,
  bound warm density.
- A scheduler. `cp-manage maintenance run` is a command, not a daemon.

## Running it

`AGENTS.md` is the canonical setup: dependencies, development key material, the
control-plane database, migrations, and the two processes — the control plane
(`services.control_plane.main:create_app`, port 8111) and the gateway
(`services.gateway.main:build`, port 8110). Read the testing section there
before trusting a green run: without a node admin DSN, a Realtime node DSN and a
container runtime, the suite **skips** the isolation assertions and prints a
`security properties not verified` banner rather than failing.

Operations go through `cp-manage`: node registration, health and
`realtime-check`; project provisioning, cleanup, email mode, direct SQL,
Realtime enablement and storage; API key issue, list, reveal and revoke;
maintenance passes; capacity and replication-slot reports.

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

That now works for the Data API and Auth against a provisioned tenant. It is not
yet a claim of Supabase compatibility, and `AGENTS.md` forbids making one until
the matrix and the automated tests support it.

## Still undecided

Deliberately, and tracked in `docs/OPEN-QUESTIONS.md`: where the production KEK
lives, the billing provider, the object-storage provider, whether rate-limit
state moves to Redis, and final production plan limits. `docs/DECISIONS.md`
records what has been settled.
