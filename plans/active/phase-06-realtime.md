# Execution Plan: Phase 06 — Realtime

Status: IN PROGRESS — slices 0-3 complete 2026-08-16. Slice 4's **spike is
complete and its build is not**: a real Realtime server now serves Postgres
Changes to the official client end to end, and doing so reversed ADR-031's
topology and corrected three things in slices 1-3. Findings in
`specs/realtime-server-model.md` and `specs/realtime-replication-model.md`.

ADR-031 and ADR-032 ratified on opening slice 1; **ADR-033 and ADR-034** added by
slice 4, the second superseding ADR-031's topology while leaving its security
analysis intact.

Next: slice 5, which builds what the slice 4 spike drove by hand — per-project
Realtime workers, tenant registration, the gateway's per-project upstream
lookup, and an automated compatibility test.
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

### Slice 1 — Node preparation and slot safety — **DONE**

The parts that must exist before any tenant can use Realtime, and which are
node-level rather than per-tenant. Nothing in it enables Realtime for anybody;
that is slice 2.

- [x] `wal_level = logical` as a node prerequisite. Checked and **recorded on the
      node row** by `cp-manage node realtime-check`, so placement can refuse a
      Realtime project without opening a connection to the node. Registration
      itself has no admin DSN, so the check is a bring-up step; a node nobody has
      checked reads as not ready rather than as ready.
- [x] **The `pg_hba.conf` physical-replication reject (ADR-031).** Checked two
      ways, because the static and the empirical answers fail differently:
      `pg_hba_file_rules` is parsed to *name* the offending line, and libpq's
      `replication=true` is used to actually attempt a physical replication
      connection. Only the second decides. A refusal for any other reason —
      node down, wrong credential — records as unknown, and unknown is not ready.
- [x] `max_slot_wal_keep_size` bounded, recorded per node, with a floor
      (`MIN_SLOT_WAL_KEEP_MB`) below which the setting would invalidate slots
      during ordinary traffic rather than during a stall. Budgeted as occupied
      space in `docs/CAPACITY.md`: the bound caps growth and does not reclaim (R4).
- [x] Slot accounting in `nodes.capacity_of`, in the same shape as the connection
      headroom from Phase 05 slice 4, and **enforced in `reserve_placement`** —
      the Phase 05 lesson. Kept separate from `rejection_reason()` on purpose: a
      node out of slots is still a good node for the many projects that do not
      want Realtime, and folding the ceilings together would strand capacity
      ADR-022 measured as usable.
- [x] Detection of invalidated slots in the maintenance pass, with an audit event
      on the transition rather than on the state. Three outcomes distinguished —
      `lost`, `missing`, and a slot no project claims — because they need
      different responses. Every event carries `replayed_on_recovery: false`.

Deliberately **not** done here: re-creating an invalidated slot. Recovery skips
the gap, so silently repairing it would convert a reportable incident back into a
silent one. That belongs with slice 2, where there is a surface to tell the
customer what was lost.

**Verified against a real cluster, not a mock.** `scripts/realtime-test-cluster.sh`
builds a throwaway PostgreSQL 17 cluster carrying the five node settings; the
suite in `tests/test_realtime_node.py` runs R1, R2, R4, R5, R6a, R6b and R8
against it, including a real `pg_basebackup`. The script also builds a
deliberately unprotected cluster, which was used to confirm the check reports
*unsafe* — a control that has never failed has not been tested. A stock Debian
cluster turns out to ship `host replication all 127.0.0.1 trust`.

### Slice 2 — Per-project enablement and the `supabase_realtime` publication — **DONE**

- [x] Entitlement-driven, off `realtime_connections`, which Phase 05 slice 1
      already set to `0` on free. No new flag: the number that says how much
      Realtime a plan includes is the same number that says whether it includes
      any.
- [x] The publication created per tenant in bootstrap 009 — for **every** tenant,
      not on enablement, which is the opposite of how the slot is handled and
      deliberately so. An empty publication is a catalogue row that reserves no
      WAL; the slot is the scarce and dangerous resource. It is owned by the
      tenant admin, so a paid customer runs `ALTER PUBLICATION ... ADD TABLE`
      exactly as they would on Supabase.
- [x] Enabling and disabling as an operator command, and disabling as a plan
      consequence in the provisioning run — the same shape as direct SQL.
      **Only the removing half is automatic.** An upgrade does not silently
      start replicating a customer's tables, because enabling creates a role
      holding `REPLICATION` and that should be a decision rather than a side
      effect of a billing change.
- [x] The replicator role, created on enablement and **dropped** on disablement.
      Not left `NOLOGIN` the way the admin role is: a dormant admin role holds
      nothing until enabled, while a dormant role holding `REPLICATION` is one
      `pg_hba.conf` regression away from reading the cluster.
- [x] The slot claimed under the node row lock, so two enablements racing for
      the last of ten cannot both win, and released on any failure — a claim
      without a slot would hold capacity forever and be reported as `missing`
      by every maintenance pass thereafter.
- [x] Recovery of an invalidated slot, which slice 1 deliberately left here. Run
      by a person, never by the maintenance pass, and its audit event carries
      the start of the gap and `replayed_on_recovery: false`.

Verified against a real tenant on the Realtime cluster, provisioned through
`jobs.provision` rather than assembled by the fixture: 16 tests in
`tests/test_realtime_enablement.py`, including a replicator authenticating with
its own stored credential and being refused another tenant's database.

### Slice 3 — `/realtime/v1` routing and project authorisation — **DONE**

- [x] The gateway surface — though **not** reusing the `Surface` table as the
      plan expected, and the reason is worth recording. `Surface` describes a
      per-project worker with a port, a lifecycle state and an activity clock to
      sleep it by. Realtime has none of those: ADR-031 makes it one shared server
      per node, so there is nothing to wake, nothing to sleep, and no port to
      look up. Forcing it into that shape would have meant three columns that
      exist to be ignored. `realtime_enabled` from slice 2 is its gate instead.
- [x] **WebSockets, which the gateway had never proxied.** New code on the
      security-critical path, so the authentication *order* is deliberately
      identical to the request path and the refusals are deliberately uniform:
      every pre-authentication rejection closes 1008, exactly as the HTTP path
      answers a uniform 401.
- [x] Cross-project isolation, tested the way Phase 03 slice 3 tested it: a key
      for one project cannot open a socket for another, and the test fails if the
      check is removed.
- [x] Connection limits from the entitlement, enforced per project, in a limiter
      of their own. A socket is counted, not rated — a connection held for an
      hour spends one token and then costs nothing, which is the wrong model in
      both directions.

Three things this slice found that were not in the plan:

- **A key on a WebSocket must be accepted from the query string.** A browser
  cannot set headers on a handshake, so a header-only gateway works from Node
  and fails from every browser. It is what upstream defined and what the official
  client sends.
- **`Host` cannot be passed as an extra header** on the upstream connection: the
  library already derives one from the URI, so it appends a *second*, leaving the
  header that identifies the tenant ambiguous. Caught by the stub upstream
  refusing to read a duplicate key, and fixed by carrying the hostname in the URI
  with the socket redirected to loopback.
- **An ordinary disconnect raised out of the close path**, logging a traceback
  for every normal session end. Found by the benchmark, not by the tests.

**The ADR-026 measurement the plan asked for is recorded** in ADR-026 and
`docs/CAPACITY.md`: ≈204 kB of RSS per socket (both ends), 8.8 ms handshake,
1.6 ms frame round trip, via `scripts/bench-gateway-sockets.py`.

Not done here, because nothing in slice 3 needs it: **there is still no real
Realtime server.** The upstream in every test and in the benchmark is a stub.
See the note under slice 4.

### Slice 4 — A real Realtime server, and what it corrected — **SPIKE DONE, BUILD OUTSTANDING**

Postgres Changes reached the official `@supabase/supabase-js` client, through the
gateway, from a real tenant database, via upstream `supabase/realtime`. Findings
in `specs/realtime-server-model.md`; decisions in ADR-033 and ADR-034.

- [x] A container runtime, and a version that runs on these nodes (ADR-033).
      Podman rootless; pinned to v2.110.0 because latest dies with SIGILL in a
      precompiled Rust NIF on a CPU with no AVX.
- [x] **The topology reversal.** Upstream's slot names are server-level and
      PostgreSQL's are cluster-unique, so a shared server serves one tenant per
      cluster and the second silently receives nothing. ADR-034 makes Realtime
      one instance per project, verified with two instances and two tenants.
- [x] All 36 of upstream's tenant migrations running as a **non-superuser**,
      with four narrow grants. The part of this slice most worth keeping.
- [x] The three corrections to slices 1-3 (gateway path, the platform's unused
      slot, two slots per project rather than one) landed in code and tests.
- [x] ADR-022's Realtime density term: ~146 MB per instance.
- [x] `wal2json` as a checked node prerequisite.

**What is not built.** The spike drove everything by hand; none of the following
exists as code, and slice 5 is where it goes:

- **Per-project Realtime workers.** A systemd unit template wrapping the Podman
  container, per-project `SLOT_NAME_SUFFIX`, HTTP port and gen_rpc port
  allocation, and start/stop/sleep in the shape ADR-027 already uses for
  PostgREST and Auth. Realtime's cost makes the sleep policy matter more here
  than anywhere else.
- **Tenant registration** with the project's server on enable, and
  deregistration on disable, over its admin API. The replicator credential from
  slice 2 is what it carries.
- **The gateway's upstream lookup.** It currently reads one `realtime_port` from
  configuration, which was right for a shared server and is wrong now. This is
  the `Surface` shape after all -- a per-project port and worker state -- so
  slice 3's decision not to use it should be revisited rather than worked around.
- **The compatibility test.** The end-to-end proof exists only as a scratch
  script; it needs to be a real test in `tests/compat`, and CI needs to run a
  Realtime instance.
- Matrix entries stay `planned` until that test exists.

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

- 2026-08-16 — **Slice 4 spike complete; ADR-031's topology reversed.** Podman
  installed, `supabase/realtime` v2.110.0 running, tenant registered, and the
  official client receiving Postgres Changes through the gateway. Then: a second
  tenant on the same server subscribed and silently received nothing, because
  upstream's replication slot names are server-level and PostgreSQL's are
  cluster-unique. ADR-034 makes Realtime per-project; ADR-033 records the
  runtime and the CPU-driven version pin.
  Three corrections landed in code: the gateway maps `/realtime/v1` to
  `/socket`, the platform no longer creates a slot the server does not read, and
  a Realtime project costs two slots rather than one.
  Not built: per-project workers, tenant registration, the gateway's
  per-project upstream lookup, the automated compatibility test.
- 2026-08-16 — Plan created, five slices, opening with a spike. Not started.
- 2026-08-16 — **Slice 1 complete.** ADR-031 and ADR-032 ratified from Proposed
  to Accepted on opening it, since the code encodes them as mandatory node
  preconditions and leaving them Proposed would have made the implementation the
  decision. Migration 0012 adds the slot columns; `services/control_plane/realtime.py`
  is new; `nodes.py` gains the third ceiling and a `needs_realtime` placement
  path; `maintenance.check_replication_slots` is wired into `run_all`.
  Full suite 468 passed / 33 skipped with a node admin DSN, including 12
  Realtime node assertions against a purpose-built cluster.
  One thing worth recording because it was not expected: a stock Debian
  `pg_createcluster` cluster ships `host replication all 127.0.0.1 trust`, so
  the unprepared default is not merely "no reject" but an open one.
- 2026-08-16 — Slice 1's security review found a real bypass **in slice 1's own
  code** and it was fixed before merge: the physical-replication probe runs as
  one role, and `pg_hba.conf` matches on the user, so a file rejecting the
  platform's role and admitting every other one passed the check. Readiness now
  requires the parsed rules and the probe to agree. Recorded here because the
  lesson generalises: an empirical check of a control that is *per-principal*
  only tests the principal it ran as.
- 2026-08-16 — **Slice 3 complete.** `services/gateway/sockets.py` is new;
  `Gateway.handle_websocket` authenticates in the same order the request path
  does; `limits.SocketLimiter` counts connections rather than rating them.
  20 new tests in `tests/test_gateway_realtime.py` against a recording
  WebSocket upstream. ADR-026's socket measurement recorded: ≈204 kB RSS per
  socket, 8.8 ms handshake, 1.6 ms frame round trip.
  Two defects found by running things rather than reading them — a duplicate
  `Host` header on the upstream connection, and a traceback on every ordinary
  disconnect. Both fixed here.
  The plan expected this slice to reuse the `Surface` table; it does not, and
  the reason is in the slice notes.
- 2026-08-16 — **Slice 2 complete.** Bootstrap 009 adds the `supabase_realtime`
  publication for every tenant; `realtime.enable/disable/recover_slot/apply_plan`
  and the `mldb_<ref>_replicator` role are new; `jobs` applies the plan's
  Realtime entitlement in the validate step. `TenantNames` gained a fifth name.
  Nothing serves events to a client yet.
- 2026-08-16 — Slice 0 complete on an isolated PostgreSQL 17 cluster; the live
  cluster was not modified and remains `wal_level = replica`. Nine findings in
  `specs/realtime-replication-model.md`, of which R6 (cluster-wide read through
  physical replication) changed slice 1's scope. One deliverable outstanding:
  the Realtime process cost was **not** measured, because upstream ships only a
  container image and Docker is not installed.
