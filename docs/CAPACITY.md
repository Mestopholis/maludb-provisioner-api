# Capacity and Density

What a project actually costs, how many fit on a node, and which resource runs
out first.

`docs/MARKET-POSITIONING.md` claims more predictable and lower production cost
than Supabase. ADR-007 accepted one PostgREST and one Auth process per active
project "for MVP", to be revisited "after measuring real usage". This document
is that measurement.

Measured 2026-08-15 on the development host: 6 cores, 3.9 GB RAM, PostgreSQL
17.10, MaluDB 0.104.0, three concurrently running projects each with its own
PostgREST 14.17 and Auth 2.195.0. Figures are **PSS** (proportional set size),
which apportions shared binary pages correctly across processes — RSS
double-counts them and overstates density cost badly here.

This is one small dev box, not a production node. The per-project figures
should generalize; the absolute totals should not.

## Measured per-project cost

| Component | PSS | RSS |
|---|---|---|
| PostgREST worker | 14.2 MB | 23.2 MB |
| Auth (GoTrue) worker | 17.6 MB | 35.0 MB |
| **Workers subtotal** | **31.8 MB** | 58.2 MB |
| PostgreSQL backend, idle | ~4 MB | ~20 MB |
| PostgreSQL backend, after serving queries | ~15–17 MB | ~35–39 MB |
| Tenant database on disk | 24 MB | — |

Two observations that matter more than the totals.

**The Auth worker is larger than the data worker.** For a project that does not
use Auth, starting GoTrue wastes the single largest per-project allocation.
Worker startup should be per-service and demand-driven, not "start both when
the project wakes".

**Backends grow as they work.** An idle backend is ~4 MB PSS; one that has
served queries settles around 15 MB, because it builds its own relcache and
catcache. Those caches are **per-database**, so with database-per-tenant they
are not shared between tenants. This is the intrinsic memory cost of ADR-002
versus a schema-per-tenant design, and it is the dominant term at scale.

## The binding constraint is connections, not memory

With a PostgREST pool of 3, each project held **4 backends** under light load
(3 data + 1 auth).

```text
max_connections            100
superuser_reserved           3
usable                      97
backends per warm project    4
                            --
warm projects per cluster  ~24
```

Memory on the same box would have allowed roughly 40 warm projects, and disk
far more. **Connections run out first, and they run out early.**

Scaling the ceiling is expensive, because raising `max_connections` costs
backend memory:

| Warm projects | Connections needed | Backend memory at ~10 MB PSS |
|---|---|---|
| 25 | 100 | ~1 GB |
| 100 | 400 | ~4 GB |
| 250 | 1,000 | ~10 GB |

Adding worker memory on top, 250 warm projects is roughly 10 GB of backends
plus 8 GB of workers before `shared_buffers`, the OS, or any actual query work.

### Replication slots are a third ceiling, and a tighter one

Measured 2026-08-16 (`specs/realtime-replication-model.md`). A logical
replication slot is bound to exactly one database, so a project using Realtime
needs **one slot of its own** — there is no multiplexing available. Slots are a
cluster-wide resource, and `max_replication_slots` defaults to 10.

```text
max_replication_slots       10
platform allowance           2
usable by tenants            8      against a warm ceiling of ~24 projects
```

So **Realtime's ceiling is under half the node's**, on a different axis from
both memory and connections, and a node's capacity now depends on *which* of its
projects have Realtime enabled rather than only on how many it has. Placement
counts it separately for that reason: a node out of slots still accepts projects
that do not want Realtime.

Two consequences that are easy to miss. Raising `max_replication_slots` needs a
restart, so it is node-build work like `wal_level`. And the WAL bound ADR-032
requires is space that **will** be occupied, not space that might be:
`pg_wal` plateaus at the bound rather than shrinking back, because PostgreSQL
recycles segments for reuse. Budget it as occupied disk.

### A Realtime socket costs the gateway, not the database

Measured 2026-08-16 with `scripts/bench-gateway-sockets.py`, 200 concurrent
sockets: **≈204 kB of RSS per connection**, and that figure covers both ends
because the benchmark client shares the process, so the gateway's own share is
smaller. Handshake 8.8 ms at p50; a frame round trips in 1.6 ms once open.

This is a different budget from everything above it. A subscriber holds a socket
on the gateway and nothing on the tenant's database — the database side is the
one replication slot the project already has. So Realtime scales the *gateway*
with subscriber count and the *node* with project count, and the two do not
trade against each other.

At the measured upper bound, ten thousand concurrent subscribers is roughly 2 GB
in one gateway process. RSS also does not fall when sockets close (the allocator
keeps the pages), so a gateway sized for a peak stays that size.

**Still unmeasured: what a Realtime server process costs.** Upstream ships as a
container image and the development host has no container runtime, so ADR-022
has no Realtime density term and no capacity figure here may assume one.

### What this means for the pooler

`docs/OPEN-QUESTIONS.md` asks "chosen pooler and when introduced?". The answer
is now quantifiable: **a transaction-mode pooler is required before roughly 25
warm projects per node at default settings**, and is the only way to reach
triple-digit warm density at sane memory.

One important caveat specific to this architecture: poolers key their server
pools by (user, database). With database-per-tenant there is no cross-tenant
multiplexing — 250 tenant databases means at least 250 pools. The win is not
that tenants share backends; it is that **idle-but-warm tenants stop holding
backends at all**. Pool capacity should therefore be sized from *concurrently
active* projects, not total projects, and real workloads are bursty enough that
this ratio is large.

## Free tier economics depend entirely on sleep

ADR-007 allows free project workers to sleep. Measured, that is worth far more
than it might appear:

| State | RAM | Connections | Disk |
|---|---|---|---|
| Warm project | ~32 MB workers + backends | 4 | 24 MB |
| **Slept project** | **0** | **0** | **24 MB** |

A sleeping free project costs only disk. Free-tier density is therefore bounded
by storage, not by RAM or connections: a 500 GB volume holds roughly 20,000
sleeping projects at the 24 MB floor, before any customer data.

That floor is not negligible and is fixed by ADR-015 — `maludb_core` is
installed in every tenant database, contributing ~15 MB of the 24 MB. Free-tier
storage quotas must be defined net of it.

### Cold start makes sleep viable

Time from process spawn to serving:

| Service | Measured |
|---|---|
| PostgREST — process responding | 28–67 ms |
| PostgREST — schema cache loaded (actually serving data) | ~320 ms |
| Auth (GoTrue) — `/health` responding | 175–268 ms |

Sub-second wake for both. The user-visible cost of sleeping a free project is
small enough that aggressive sleep policy is clearly the right default.

Two caveats. The schema-cache figure is for a trivial one-table tenant; that
query scales with schema size, so a real application's cold start will be
longer and should be re-measured against a representative schema. And the
"process responding" figure is not "serving data" — PostgREST answers with
`503 PGRST002` until its cache loads, so wake orchestration must wait for
readiness rather than for the port to open, or the first request after wake
fails.

## Planning formula

For a node with `R` GB of usable RAM and `C` configured connections:

```text
warm_projects   <= (C - reserved) / backends_per_project
warm_projects   <= R / (worker_footprint + backends_per_project * backend_footprint)
total_projects  <= disk / (24 MB + mean_customer_data)
```

Measured inputs: `worker_footprint` 32 MB (16 MB without Auth),
`backend_footprint` 10 MB mean / 15 MB active, `backends_per_project` 4 at
pool size 3.

`docs/RESOURCE-GOVERNANCE.md` requires capacity scoring to consider more than
database count. These are the terms it should score on, and the node scheduler
must track **warm** project count separately from total project count — they
have entirely different cost profiles.

## Is ADR-007 still the right call?

Yes, for now, with conditions.

At 32 MB of worker PSS per project, the per-project process model is not the
binding constraint — connections are, and those would be needed by any
architecture that gives each tenant its own database. The measured cost does
not justify building a custom multi-tenant PostgREST or Auth, which
`AGENTS.md` prohibits without an explicit decision anyway.

The conditions that would force a revisit:

- warm density targets above a few hundred per node, where worker memory starts
  to rival backend memory;
- Auth workers running for projects that never use Auth;
- cold start growing beyond a second or so for representative schemas.

## Open items

Numbers this document cannot supply without product input:

- target warm and total projects per node;
- production node hardware profile, and therefore `max_connections`;
- free-tier inactivity threshold before sleep;
- pooler selection and deployment topology;
- cost per project in currency, which needs hardware pricing.

These are recorded in `docs/OPEN-QUESTIONS.md`.

## Reproducing

The harness is a spike artefact, not committed. It provisions N tenants with
`scripts/spike-provision-tenant.sh`, starts a PostgREST and an Auth worker for
each, and reads `/proc/<pid>/smaps_rollup` for PSS. Measure PSS, never RSS —
with three copies of the same binary running, RSS overstated worker cost by
roughly 80%.
