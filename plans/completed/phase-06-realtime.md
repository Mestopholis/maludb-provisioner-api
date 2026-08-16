# Execution Plan: Phase 06 — Realtime

Status: **COMPLETE** — slices 0-5, 2026-08-16. A real Realtime server serves
Postgres Changes to the official client end to end, which reversed ADR-031's
topology and corrected three things in slices 1-3. Findings in
`specs/realtime-server-model.md` and `specs/realtime-replication-model.md`.

ADR-031 and ADR-032 ratified on opening slice 1; **ADR-033 and ADR-034** added by
slice 4, the second superseding ADR-031's topology while leaving its security
analysis intact; **ADR-035 and ADR-036** added by slice 5 -- how a Realtime
container is contained on a node, and what the gateway has to do to a channel
frame for an opaque key to work with the official client.

Slice 5 built the per-project workers, tenant registration, the gateway's
per-project upstream lookup and the compatibility test. Its own measurements are
under the slice, and one of them — that the container must not be able to reach
the node's loopback — is a node prerequisite the phase did not previously have.

What remains open is named under Verification: RLS over Postgres Changes is
still unproven, and Broadcast and Presence are deliberate non-goals.

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
- [x] **What a Realtime process costs.** Left unmeasured here -- upstream ships a
      container image only and the host had no runtime -- and answered by slices
      4 and 5: ~146 MB per instance, two replication slots, one host port, one
      metadata database, and 9.0 s to start. All of it is in `docs/CAPACITY.md`.

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

### Slice 5 — Per-project Realtime workers — **DONE**

What slice 4 drove by hand, as code. Opened by re-deriving the server's contract
against a real instance rather than from the spike's notes, which found five
things the notes do not contain. They are recorded here first because three of
them change the shape of the work.

**Measured before implementing, 2026-08-16, against `supabase/realtime`
v2.110.0 on the Realtime test cluster:**

- **The container must not be given access to the node's loopback.** Podman
  rootless reaches a host service either through `slirp4netns`'s
  `allow_host_loopback` or not at all, and with it on, the instance reached
  `127.0.0.1:5432` — *the other cluster*, carrying every tenant not on the
  Realtime node — and by the same route every loopback-bound worker on the node.
  A tenant's PostgREST serves anonymous reads through `db-anon-role` to anything
  that can open the port, so that arrangement hands a compromised Realtime
  container the anon-visible data of every project on the node, past the gateway
  and past ADR-028's keys entirely.

  Measured containment: with `allow_host_loopback` **off**, the container reaches
  no host loopback service (`curl` exit 7), and still reaches a **non-loopback
  address on the node** (exit 0). So Realtime gets a dedicated data address —
  a private address on its own interface, which PostgreSQL also listens on — and
  nothing else on the node is reachable from it. That is node preparation, in
  the same category as `wal_level`, and it needs ADR-031's physical-replication
  reject extended to the new address: the existing reject names `127.0.0.1/32`
  and `::1/128`, and an address added without one re-opens exactly the hole
  ADR-031 closed. Proven end to end in this arrangement — the official protocol,
  a real tenant, Postgres Changes delivered.

- **One host port per instance, not two.** Slice 4 recorded two, because it ran
  the container on the host's network. With a network namespace per instance and
  `-p 127.0.0.1:<port>:4000`, gen_rpc's port stays inside the namespace, is
  identical in every instance, and needs no allocation. Port allocation is
  therefore the machinery `workers.allocate_port` already has, with one more
  column.

- **The metadata database is per project, and the reason is not tidiness.**
  `CLUSTER_STRATEGIES` defaults to `POSTGRES` in a production release, which
  discovers peers *through the metadata database*. Instances sharing one would
  form a single distributed Erlang cluster spanning every tenant on the node —
  gen_rpc between the processes that read each tenant's WAL. One metadata
  database per project, and `CLUSTER_STRATEGIES=NONE` besides, because a
  one-node cluster should not be a cluster.

- `METRICS_JWT_SECRET` is `fetch_env!` in a production release: absent, the
  server does not boot. Not in the spike's notes, and not optional.

- The `_realtime` schema must exist **before** the server starts:
  `DB_AFTER_CONNECT_QUERY` sets `search_path` to it and the migrator does not
  create it (`no schema has been selected to create in`). The platform creates
  the schema, the server migrates inside it.

- Registration is genuinely idempotent, which `AGENTS.md` asks of provisioning
  operations: `POST /api/tenants` answers 201 then 200, and `DELETE` answers 204
  whether or not the tenant is there. So enable and disable can both be retried.

- The environment file is **unquoted**, unlike the GoTrue one. `podman
  --env-file` takes the rest of the line literally and keeps quotes as
  characters, where systemd strips them; a quoted password would be wrong by two
  bytes and would fail as an authentication error rather than as a syntax one.

Work:

- [x] Migration 0013: the port, worker state, activity clock and registration
      stamp, in the same shape as the API and Auth workers.
- [x] `realtime_workers.py`: settings, environment rendering, the metadata
      database and its role, the derived per-instance secrets, readiness,
      registration and deregistration, start/stop/teardown.
- [x] `deploy/maludb-realtime@.service`, and a test that the unit and the code
      agree on the container arguments rather than drifting apart.
- [x] The gateway's per-project upstream lookup, the wake, and an activity clock
      so the sleep policy can reclaim 146 MB.
- [x] The maintenance pass sleeping idle Realtime workers, on an hour rather than
      fifteen minutes.
- [x] `cp-manage project realtime-worker --start|--stop|--status`, and disable
      stopping the worker before dropping the slots the server holds open.
- [x] The node's Realtime data address, built by
      `scripts/realtime-test-cluster.sh` and required by the worker.
- [x] A compatibility test with the official client, through the gateway, and CI
      running a real instance. `postgres_changes` is promoted to `supported` on
      the strength of that test and nothing else.

**Three things only a running client and a running container could find**, each
now an ADR and a test:

- **The client sends its key inside the channel frame.** `access_token` in every
  `phx_join`, where upstream expects a JWT and ADR-028's keys are opaque, so the
  socket connects and every channel fails with `MalformedJWT`. The gateway now
  translates that one field, in both Phoenix serialisers -- the official client
  defaults to the array form an object-only implementation would have missed.
  ADR-036.
- **A wake is longer than the client's patience.** Nine seconds against the ten
  the official client waits, so holding the socket fails the same connection
  more slowly. A sleeping project is closed with 1013 and woken in the
  background; phoenix.js reconnects and the next attempt lands on a ready
  instance. ADR-036, and the reason Realtime's idle window is an hour.
- **`--cap-drop ALL` stops the server booting.** Its entrypoint drops to
  `nobody` through sudo, so it fails at `setresuid` reporting `no valid sudoers
  sources found` -- which reads like a broken image rather than a capability the
  platform removed. `SETUID` and `SETGID` stay; the containment is the user and
  network namespaces.

And one the platform had already got right, confirmed only when a real server
ran the tenant migrations: dropping the replicator needs the grants it *made*
gone first, because upstream's migrations end with `GRANT supabase_realtime_admin
TO postgres` executed as the replicator and PostgreSQL refuses to drop a grantor
while its grants stand. `drop_replicator_role` handles it; the enablement
tests' own teardown did not, and now does.

## Non-goals

- Broadcast and Presence. `docs/REALTIME.md` names them as later, and Postgres
  Changes is the compatibility target that matters first.
- Realtime for free projects. `realtime_connections` is `0` on free and should
  stay there until the slot ceiling is understood in production.
- Raising `max_replication_slots` beyond what slice 1 can bound safely.

## Verification

- [x] Every acceptance criterion in `tasks/PHASE-06-REALTIME.md`.
- [x] A security review per slice.
- [x] The slot ceiling enforced, not merely measured — Phase 05's lesson.
- [x] A stalled consumer demonstrated **not** to fill the disk.
- [ ] **RLS over Postgres Changes**, which is the one claim this phase does not
      make. The replicator reads every table past policies, so the Realtime
      server is the only thing that can enforce them; the compatibility test
      shows a subscriber receiving changes from a table its role may select, and
      nothing yet shows one being refused rows a policy hides. Carried forward
      rather than ticked, and the matrix says `postgres_changes` is supported
      rather than that RLS over it is.

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

- 2026-08-16 — **The wake worked only on the runtime it was developed on, and
  CI found it.** With the plugin problems fixed, the two compatibility tests
  still failed in CI while passing here. The difference was Node: CI pins 22,
  this host runs 24. Same client version, same gateway, same project, 9.7s wake
  — `@supabase/supabase-js` made four socket attempts on Node 24 and connected,
  two on Node 22 and gave up.

  ADR-036 had closed a sleeping project's socket with 1013 and relied on
  phoenix.js reconnecting until the instance was up. That is not a property the
  platform owns: how long a client retries depends on the runtime the customer
  chose. A customer on Node 22 whose project had been idle an hour would have
  got a dead socket and an error naming nothing.

  Amended, on the repository owner's decision: **the gateway accepts the socket
  and holds it while the instance boots**, bounded by `WAKE_HOLD_SECONDS`, with
  the client's `phx_join` waiting in the receive queue until upstream can answer
  it. Verified on both runtimes — Node 22 passes for the first time, Node 24
  still passes.

  Three rounds of this were spent inferring rather than measuring, and the thing
  that ended it was making the failure legible: the gateway logged nothing on
  the path that was refusing, so every refusal looked identical to every other
  from outside. It now logs each one with its reason — uniform on the wire,
  distinct in the log, which is the distinction that was missing. Two of the
  wrong turns came from reading a client-side symptom as a server-side cause;
  phoenix logs `push phx_join` for a *buffered* push, which reads exactly like
  an open socket and is not one.

- 2026-08-16 — **CI could not have delivered a single event, and said so only
  by timing out — and the reason was a node prerequisite nobody knew existed.**
  The first CI run of the phase failed the three tests that assert Postgres
  Changes arrive, while the same tests passed on the development host. The
  first answer was the obvious one: the workflow never installed
  `postgresql-17-wal2json`. Installing it, and asserting the plugin loads
  rather than trusting the package list, moved the failure — and the second
  answer is the one worth keeping:

  ```
  ERROR:  library "wal2json" may not be used as an output plugin
  HINT:  ... add it to "output_plugin_libraries" and reload
  ```

  **PostgreSQL 17.11 added `output_plugin_libraries`**, an allowlist of the
  libraries a replication connection may load. CI installs 17.11; this host is
  on 17.10, where the setting does not exist. So the package was installed on
  both and the plugin loaded on only one — a node prerequisite that arrives
  with a *minor* upgrade, on a node that was prepared correctly when it was
  built.

  **And then the fix broke two plugins to fix one.** `output_plugin_libraries`
  *replaces* the default rather than adding to it, so a cluster told to permit
  `wal2json` stopped permitting `pgoutput` — every Realtime project's second
  slot (ADR-034) — and `test_decoding`, which four of the node assertions use.
  CI caught it because those assertions exist; the script now appends to the
  running value rather than writing one, and the CI probe covers all three
  plugins rather than the one somebody was thinking about. A check that proves
  one plugin loads proves nothing about the others.

  **And then the append applied and still matched nothing.** It was written
  with `ALTER SYSTEM SET output_plugin_libraries = 'pgoutput, test_decoding,
  wal2json'`, and this is a list GUC whose elements are quoted — like
  `shared_preload_libraries`. Given one quoted string, such a variable takes a
  single element *named* `pgoutput, test_decoding, wal2json`. It applies, it
  shows in `pg_settings`, and it permits no library at all. The config-file
  form splits on the commas, which is why every `shared_preload_libraries`
  example in the wild is written that way.

  Confirmed on 17.10, which has no `output_plugin_libraries` but has
  `search_path`, a list GUC of the same kind: `ALTER SYSTEM` yields
  `"aa, bb"`, one element; the same value in `postgresql.conf` yields `aa, bb`,
  two. Three wrong answers in a row about a setting this host cannot run is
  what finally bought a test on a version it can.

  The script now writes the line and reloads — and then **probes all three
  plugins itself** before declaring the cluster up. Every way of getting this
  wrong so far, an absent package, a replaced default, and a list stored as one
  element, fails identically from a client: subscribe, then nothing. Three
  distinct causes, one symptom, and only a created slot tells them apart.

  Three things came out of it. `scripts/realtime-test-cluster.sh` sets the GUC
  when the version has it, detected from the binary rather than assumed,
  because older minors refuse to start with an unknown setting. The spec tables
  gain the requirement. And `probe_wal2json` — which classified this as
  "could not run", the answer that means *say nothing* — now separates a
  plugin the server **refuses to load** from a probe that could not be
  performed. That mattered more than the CI fix: unclassified, a node running
  17.11 with wal2json installed would have passed its readiness check, been
  given Realtime projects, and delivered nothing to any of them. The check
  designed to catch a silent failure had a silent failure in it.

- 2026-08-16 — Slice 5's security review found three things, all in slice 5's
  own code and all fixed before merge. Recorded because two of them generalise.

  **A refusal that returns while holding a resource leaks it.** The new 1013
  path acquired a socket slot and then returned without releasing it, so a
  client reconnecting through a wake -- which is exactly what the platform asks
  it to do -- would spend one of its own connections per attempt and eventually
  be refused with 4029 for the life of the gateway process. Fixed by checking
  the worker's state *before* counting the connection. Slice 3 had written "in a
  finally, always" about this same limiter and it still happened, because the
  new code returned above the `try`.

  **PostgreSQL grants CONNECT to PUBLIC on every new database.** Each project's
  Realtime metadata database was created without revoking it, so one project's
  Realtime role could open another's -- where it would find that project's
  replicator credential, sealed under a key it does not have but present all the
  same. ADR-014's lockdown exists for tenant databases; platform state needs it
  too.

  **Sixteen hex characters are 64 bits, not 128.** `DB_ENC_KEY` is used by
  upstream as AES key material and is sixteen *characters* long, so deriving it
  as hex halved the key that encrypts the replicator password inside the
  metadata database. Base64 now, for 96.

- 2026-08-16 — **Slice 5 complete.** `services/control_plane/realtime_workers.py`
  and `deploy/maludb-realtime@.service` are new; migration 0013 adds the port,
  the worker state, the activity clock and the registration stamp; the gateway
  looks the port up per project, wakes a sleeping instance in the background and
  refuses with 1013 while it does; the maintenance pass sleeps an idle one after
  an hour. ADR-035 (the container reaches PostgreSQL and nothing else on its
  node) and ADR-036 (the key inside the frame, and 1013) were decided here.
  **Postgres Changes reach the official client through the gateway, in a test**
  -- `tests/test_realtime_compat.py` -- which is Phase 06's first acceptance
  criterion and the only basis on which the matrix was promoted.
  Six more assertions run against a real container in
  `tests/test_realtime_server.py`, including the one that matters most: from
  inside the instance, every loopback address on the node refuses.
  Measured here and recorded in `docs/CAPACITY.md`: a wake costs 9.0 s, and an
  instance one host port rather than the two slice 4 assumed.
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
