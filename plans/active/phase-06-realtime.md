# Execution Plan: Phase 06 — Realtime

Status: IN PROGRESS — slice 0 (spike) complete 2026-08-16; findings in
`specs/realtime-replication-model.md`, proposals in ADR-031 and ADR-032
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-06-slice-*`, one per slice
Related task: `tasks/PHASE-06-REALTIME.md`
Dependencies: Phase 05 complete (merged 2026-08-16).

## Objective

Supabase-compatible Postgres Changes, delivered to the official client, without
one project's Realtime consumer being able to take down the node it shares.

That second clause is the phase. `docs/REALTIME.md` asks for the upstream
topology to be validated against database-per-project **before** implementation,
so this plan opens with what that validation already found.

## What was measured before planning

Four facts from the development node, 2026-08-16. Each changes the shape of the
work, and none of them is a detail.

### `wal_level` is `replica`, so Postgres Changes cannot work at all today

```
ERROR:  logical decoding requires "wal_level" >= "logical"
```

Changing it requires a **cluster restart**, which is every tenant on the node
losing their connections at once. It is therefore node preparation (ADR-003),
not tenant provisioning, and it has to happen before a node can host a Realtime
project. A node built without it cannot be fixed without an outage.

### A logical replication slot is bound to one database

`pg_replication_slots` has a `database` column, and logical decoding is
per-database by construction. So Realtime needs **one slot per tenant database**
that uses it. There is no multiplexing to be clever about.

### Slots are a cluster-wide resource, and there are 10

`max_replication_slots = 10` and `max_wal_senders = 10` against ADR-022's warm
ceiling of roughly 24 projects. **Realtime's ceiling is under half the node's
project ceiling**, and it is a different resource from the connections Phase 05
learned to count. Raising it is possible; raising it without bounding the risk
below is not.

### `max_slot_wal_keep_size = -1`, which is unbounded

This is the one that matters. A logical slot whose consumer stops — a crashed
Realtime worker, a slept free project, a network partition — **pins WAL
indefinitely**. The disk fills, and PostgreSQL on a full disk stops accepting
writes for **every tenant on the node**, not just the one whose consumer died.

That is a cross-tenant availability failure caused by one project's inactivity,
which is a sharper version of exactly what ADR-009 and Phase 05 exist to
prevent. PostgreSQL 13+ can bound it: `max_slot_wal_keep_size` invalidates a slot
rather than letting it consume the disk. Setting it is not optional here, and the
consequence — an invalidated slot means that project silently stops receiving
changes until something notices — is itself a thing the platform has to detect
and report.

## Decisions needed before slice 1

**Both were answered by the spike on 2026-08-16 and are proposed as ADR-031 and
ADR-032, pending ratification.** The analysis below is what the plan reasoned
before measuring; the spike changed the shape of the first one. It is kept
because the difference between the two is the point.

The spike also found something neither decision anticipated: **the `REPLICATION`
attribute reads every database on the cluster**, past ADR-014's `CONNECT`
lockdown, via physical base backup — and that is a property of the attribute, so
per-project isolation does not fix it. It is closed at the node with a
`pg_hba.conf` reject, or not at all. See `specs/realtime-replication-model.md`
finding R6.

### 1. Whether Realtime is per-project or shared

ADR-007 permits a process per project for PostgREST and Auth, and ADR-022
measured that as affordable. Realtime is a different shape: an Elixir/BEAM
process is heavier than either, and upstream `supabase/realtime` is itself
multi-tenant — it holds a tenants table and fans out to many databases from one
server.

| Option | For | Against |
|---|---|---|
| **One shared Realtime per node**, upstream's own model | One process, one place to bound slots and connections, matches how upstream is built and tested | A bug or overload in it affects every Realtime project on the node; it needs credentials for many tenant databases, which is a new concentration of secrets |
| **One per project**, matching ADR-007 | Blast radius is one tenant; reuses the systemd machinery from ADR-027 and the sleep/wake work from Phase 05 | Heavier per project than PostgREST and Auth combined; ADR-022's density numbers do not cover it, so it would need measuring before it could be sized |

**Recommendation: shared, and measure it.** It is what upstream builds and tests,
and the compatibility rule in `AGENTS.md` prefers upstream behaviour over a
MaluDB-specific arrangement. The concentration-of-secrets objection is real and
is why the spike exists: if a shared Realtime needs broad database credentials,
that is a finding worth having before the design is committed rather than after.

### 2. What happens when a slot is invalidated

Once `max_slot_wal_keep_size` is set, a stalled consumer loses its slot instead
of filling the disk. The project then receives no changes, and nothing about the
connection says so.

Recommendation: treat an invalidated slot as a **project-visible incident** —
detected by the Phase 05 maintenance pass, recorded as an audit event, and
surfaced. The alternative is a customer whose application silently stops
receiving events, which is worse than an error.

## Slices

Sequential, with a security review between each.

### Slice 0 — Spike: does upstream Realtime fit database-per-project? — **DONE**

Findings: `specs/realtime-replication-model.md`. Proposals: ADR-031, ADR-032.

- [x] Slot arithmetic — one per database (R1), hard error at the ceiling (R2),
      and the failure lands at enablement rather than at runtime.
- [x] Credentials — `REPLICATION` is required and has no lesser substitute (R5);
      it is *not* constrainable to less than cluster-wide read by grants alone
      (R6); it **is** constrainable by `pg_hba.conf` (R7).
- [x] The stalled-consumer risk measured end to end: 206 MB pinned by one idle
      slot (R3), and the bounded behaviour that replaces it (R4).
- [ ] **What a Realtime process costs — NOT MEASURED.** Upstream ships as a
      container image only and Docker is absent from the development host, so
      the server itself was never run. ADR-022's density numbers still have no
      Realtime term, and no capacity figure may assume one until this is done.

The topology fits, with a node-level precondition it did not previously have.

### Slice 1 — Node preparation and slot safety

The parts that must exist before any tenant can use Realtime, and which are
node-level rather than per-tenant.

- `wal_level = logical` as a node prerequisite, asserted at node registration so
  a node that cannot host Realtime says so rather than failing at provisioning.
- **The `pg_hba.conf` physical-replication reject (ADR-031), asserted the same
  way.** Added by the spike, and the most important item in the slice: without
  it the first project to enable Realtime holds a readable copy of every tenant
  on the node. Asserting it is awkward — it means testing a node's
  `pg_hba.conf`, not just its settings — and that is exactly why it is named
  here rather than left to node build to remember.
- `max_slot_wal_keep_size` bounded, with the value recorded per node, and
  budgeted as space that will be occupied: the bound caps WAL growth but does
  not reclaim it (R4).
- Slot accounting in `nodes.capacity_of`, in the same shape as the connection
  headroom from Phase 05 slice 4 — Realtime's ceiling is a third resource
  alongside projects and connections, and it is the tightest of the three.
- Detection of invalidated slots in the maintenance pass, with an audit event.

### Slice 2 — Per-project enablement and the `supabase_realtime` publication

- Entitlement-driven: `realtime_connections` is already `0` on free.
- The publication created per tenant in a bootstrap file, alongside the slot.
- Enabling and disabling as an operator command and as a plan consequence, the
  same shape as direct SQL.

### Slice 3 — `/realtime/v1` routing and project authorisation

- The gateway surface, reusing the `Surface` table from Phase 04 slice 2.
- **WebSockets, which the gateway has never proxied.** The Phase 03 proxy is
  request/response; upgrade handling is new code on the security-critical path.
- Cross-project isolation, tested the way Phase 03 slice 3 tested it: a key for
  one project must not open a socket for another, and the test must fail if the
  check is removed.
- Connection limits from the entitlement, enforced per project.

### Slice 4 — Compatibility

- Postgres Changes through `@supabase/supabase-js`, against a real tenant
  through the real gateway, in the shape Phase 03 slice 4 established.
- Matrix entries promoted only for what the suite covers.

## Non-goals

- Broadcast and Presence. `docs/REALTIME.md` names them as later, and Postgres
  Changes is the compatibility target that matters first.
- Realtime for free projects. `realtime_connections` is `0` on free and should
  stay there until the slot ceiling is understood in production.
- Raising `max_replication_slots` beyond what slice 1 can bound safely.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-06-REALTIME.md`.
- [ ] A security review per slice.
- [ ] The slot ceiling enforced, not merely measured — Phase 05's lesson.
- [ ] A stalled consumer demonstrated **not** to fill the disk.

## Risks

- **One project's stalled consumer can take down every tenant on the node.**
  The sharpest cross-tenant risk in the project so far, and the reason slice 1
  is node safety rather than features.
- **The slot ceiling is tighter than the connection ceiling**, so a node's
  capacity now depends on which projects have Realtime enabled. Placement gets
  a third dimension.
- **WebSocket proxying is new code on the path that authenticates every
  request.** ADR-026 accepted Python in the data path on a measurement; a
  long-lived socket is a different cost profile from a request, and the
  measurement does not carry over.
- **`wal_level` needs a cluster restart**, so enabling Realtime on an existing
  node is an outage for every tenant on it. Node preparation must get this right
  before a node takes its first project, or the fix costs downtime.

## Decision log

- 2026-08-16 — Plan created after measuring the node. Two decisions surfaced:
  shared versus per-project Realtime, and what an invalidated slot should do.
- 2026-08-16 — Spike answered both. **ADR-031** (proposed): shared Realtime,
  conditional on a node-level `pg_hba.conf` reject of physical replication,
  because `REPLICATION` otherwise reads every database on the cluster and
  per-project topology does not change that. **ADR-032** (proposed):
  `max_slot_wal_keep_size` mandatory, invalidation is a project-visible
  incident, and recovery does not replay the gap.

## Progress log

- 2026-08-16 — Plan created, five slices, opening with a spike. Not started.
- 2026-08-16 — Slice 0 complete on an isolated PostgreSQL 17 cluster; the live
  cluster was not modified and remains `wal_level = replica`. Nine findings in
  `specs/realtime-replication-model.md`, of which R6 (cluster-wide read through
  physical replication) changed slice 1's scope. One deliverable outstanding:
  the Realtime process cost was **not** measured, because upstream ships only a
  container image and Docker is not installed.
