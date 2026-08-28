# Execution Plan: Phase 11 — Production Resilience

Status: IN PROGRESS — **slices 0, 1 and 2 complete**
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/phase-11-production-resilience`, then one branch per slice
Related task: `tasks/PHASE-11-PRODUCTION-RESILIENCE.md`
Dependencies: Phases 02, 05, 06, 09 and 10 are merged and closed. Nothing in
this phase is blocked on unmerged work.

Plan written 2026-08-26. **Slice 0 complete** the same day; findings in
`specs/backup-restore-model.md`, tooling decision recorded as ADR-067. **Slice
1 complete 2026-08-27**; ADR-064 ratified, and `docs/BACKUP-RECOVERY.md`
rewritten from a placeholder into what was built.

## Objective

Make four sentences true, each of which is currently false:

1. A named tenant can be restored to a named point in time without taking the
   other tenants on its node offline.
2. A node that has been lost can be rebuilt from something other than the node
   itself, and the procedure has been executed rather than described.
3. A project can be moved to another node keeping its `project_ref`, its
   hostname, its keys, and its data.
4. A production project and a free project can be placed on different hardware.

## What is already true

Worth stating, because three of the four objectives are closer than the task
file suggests and one is further away.

**Node pools are plumbed but have no policy.** `nodes` has had a `node_pool`
column since migration 0002, `eligible_nodes` filters on it, and
`cp-manage node register --node-pool` sets it. But `api/projects.py:213` calls
`nodes.reserve_placement(conn, project_id=project_id)` with no pool argument, so
every project on the platform lands in `shared` by the parameter default. The
missing piece is the decision — which pool a plan implies — not the mechanism.

**Drain is a status with no verb.** `node status --status draining` is
accepted, and `PLACEABLE_STATUS = "active"` means a draining node takes no new
projects. That is the whole of it: nothing moves the projects already there, so
a drain never completes. Tenant movement is what turns the status into an
operation.

**The maintenance pass exists and is the right place to hang periodic work.**
ADR-053's `cp-manage maintenance run` already carries billing reconciliation,
worker sleep, storage measurement and job retry, and is explicitly built to be
safe to run concurrently with itself and safe to interrupt. Backup verification
and capacity alerting belong there rather than in a new daemon.

**Physical replication is rejected at every node by design.** ADR-031 requires
`host replication all <cidr> reject` in `pg_hba.conf`, asserted at node
registration and tested with a real `pg_basebackup` against a cluster the test
harness builds. This is the single most important existing fact for this phase,
and it is in tension with the obvious backup tooling — see slice 0.

**A tenant database has a 24 MB floor of which ~15 MB is `maludb_core`**
(ADR-015, `docs/CAPACITY.md`). At `DEFAULT_MAX_PROJECTS = 200` a full node
carries roughly 5 GB of tenant data before any customer bytes. That is small
enough that a full physical backup of a node is not, on its face, expensive —
which is worth confirming before designing around the assumption that it is.

## The questions this phase is blocked on

`docs/OPEN-QUESTIONS.md` has carried a `## Backups` section of five bullets and
a `## Node scheduling` section of four since Phase 01. Unlike Phase 10 — where
all three open questions were answerable from research and were settled in the
plan commit as ADR-055 to ADR-057 — **most of these cannot be answered without
measurement**, and answering them from reputation is how this phase would go
wrong. They are sorted here by whether they are decidable today.

### Decidable now, and proposed for ratification before slice 1

These need judgement, not a benchmark. They are written here as proposals; they
are **not** recorded in `docs/DECISIONS.md` yet, because a plan may not override
that file. Ratify or reject them first.

**Status 2026-08-27: ADR-064 is ratified and recorded.** ADR-065 and ADR-066
remain proposals — they gate slices 6 and 7 rather than slice 1, and nothing
built so far depends on them.

- **A backup repository in the same failure domain as the data is not a
  backup.** Phase 10 put SeaweedFS on the existing Proxmox hardware (ADR-055),
  with the exit stated. Using that same object store as the sole WAL archive and
  backup repository would mean the loss that takes the hardware takes the
  backups with it. The S3 endpoint is the right *interface* — pgBackRest, Barman
  and wal-g all speak it, and Phase 10 already proved the platform can operate
  an S3 target — but the phase must not ship with the repository and the
  cluster on one Proxmox host. Proposed ADR-064.

- **The pool is chosen by entitlement, not by plan name.** `entitlements` is
  already the layer that answers "what does this project get", and
  `DEFAULTS` already carries per-plan values that a deployment can override.
  Adding a `node_pool` entitlement keeps the free/production split
  configuration-driven, which `AGENTS.md` requires, and avoids a second place
  where plan names are hard-coded. Proposed ADR-065.

- **Movement is operator-initiated, never automatic.** `docs/REQUIREMENTS.md`
  lists "automatic cross-node tenant migration" as a deferred requirement, and
  `docs/ARCHITECTURE.md`'s scaling order puts tenant movement at step 6 and
  rebalancing after it. Phase 11 delivers the *move*, with a runbook and a
  measured freeze window. It does not deliver a rebalancer that decides on its
  own to move a customer's data. Proposed ADR-066.

### Not decidable without slice 0

- **Physical backup technology.** pgBackRest, Barman, wal-g, or
  `pg_basebackup` plus archived WAL. The discriminator is not throughput; it is
  the ADR-031 interaction below.
- **WAL archive target.** Interface is settled by the proposal above; the
  location is a hardware question the measurement informs.
- **Logical per-database backup schedule, and whether logical is primary or
  complement.** A per-tenant `pg_dump` is the only thing that restores one
  tenant without touching the others, and it is also the only thing that cannot
  give a point in time between dumps. The answer is probably both, and the
  schedule depends on measured dump cost at realistic tenant counts.
- **Restore workflow.** Whether restore-one-tenant goes through a scratch
  cluster, and what that costs in disk and wall-clock.
- **Paid retention and PITR tiers.** Needs the storage cost of retention, which
  needs measured WAL volume per tenant per day.

## What Phase 10 changed about this phase

**A project is now two data sets, and this phase is the first one that has to
care.** The tenant database holds `storage.objects` rows; the bytes live in the
shared SeaweedFS bucket keyed by object path (ADR-057). Restoring the database
alone to a point in time is not a restore of the project:

- rows return for objects that were deleted after the restore target, so the
  project lists files whose bytes are gone;
- rows vanish for objects uploaded after the target, so the bytes stay in the
  bucket, billed to nobody and reachable by nobody.

`docs/RESOURCE-GOVERNANCE.md:291` already books the second half of this as
Phase 11 work — an upload that wrote bytes and failed to commit its row leaves
an orphan — and notes the error is in the tolerable direction for billing. It
is not in the tolerable direction for restore. Any per-tenant PITR claim this
phase makes must state what it does to objects, and the honest first answer may
be that PITR covers the database and object recovery is separately bounded.

`docs/STORAGE.md:254` likewise defers SeaweedFS erasure coding, replication and
self-healing to "where backups, restore and PITR already live" — meaning here.
Object durability is in scope for this phase whether or not the task file's
scope bullets name it.

## Scope

From the task file, plus two additions argued below.

- Backup and restore implementation for tenant databases.
- WAL archiving and PITR where supported.
- Node pools, with a policy that selects one.
- Drain and maintenance mode.
- Tenant movement between nodes, preserving project identity.
- Disaster-recovery runbooks that have been executed at least once.
- Capacity alerts.
- **Object durability and object/metadata reconciliation** — added because
  Phase 10 deferred it here explicitly, twice, and because per-tenant restore
  is incoherent without it.
- **Control-plane backup and key-material recovery** — added because it is the
  single highest-consequence gap on the platform and it is in no phase's scope
  bullets. `encryption_keys` holds the KEK-wrapped data encryption keys
  (ADR-023). Every node admin DSN, every stored secret and every tenant's
  recoverable material is wrapped by those keys. A node restored without them
  is a node full of databases the platform cannot administer. The test suite's
  own README already records the sharp edge — it never truncates
  `encryption_keys` because a wrong KEK makes existing rows unusable — which is
  the same failure at development scale.

## Non-goals

- **High availability.** No streaming replicas, no automatic failover, no
  quorum. Recovery in this phase is restore-from-backup with a stated RTO, not
  a hot standby. Adding replicas would also reopen ADR-031, which is not a thing
  to do casually in a slice.
- **Cross-region or off-site replication as a product feature.** The
  same-failure-domain proposal above requires a second location for the
  repository; it does not require a multi-region product.
- **Automatic rebalancing.** See proposed ADR-066.
- **Proxmox-level VM backup automation.** ADR-003 keeps node lifecycle with the
  platform administrator. This phase backs up what is inside a node.
- **Customer-facing self-service restore.** A customer may eventually press a
  button; this phase builds the operation and its runbook. Whether it is exposed
  in the dashboard is a later decision.
- **Changing the tenancy model to make backup easier.** Database-per-tenant is
  ADR-002. If per-tenant restore turns out to be expensive, that is a cost to
  document, not a reason to reopen the invariant.

## Preconditions

- A throwaway cluster for backup measurement, built by a script, following the
  `scripts/realtime-test-cluster.sh` and `scripts/storage-test-cluster.sh`
  precedent. It cannot be the development cluster: the measurements involve
  restoring over a data directory and stopping the postmaster.
- Disk headroom equal to roughly twice the measured cluster size on whatever
  host runs slice 0. A restore-to-scratch holds the source and the copy at once.
- The Phase 10 object store, for any measurement that involves an S3 repository
  target or object reconciliation.

## Naming

`maintenance` already means two things in this repository and this phase would
add a third. `services/control_plane/maintenance.py` is the ADR-053 periodic
pass; `node status --status maintenance` is a node lifecycle state. The task
file's "drain/maintenance mode" is a third sense — a node deliberately emptied
for planned work.

Settle it before slice 6 writes a function: the node states stay as they are
(`draining` is the verb-adjacent one and already exists), the periodic pass
keeps the module name, and nothing new in this phase is called `maintenance`
without a qualifier. This is trivial now and expensive after four slices have
imported the wrong thing — the same mistake Phase 10's plan headed off.

## Implementation steps

Nine slices. Each is a branch, a pull request, and a `Security-Review:` trailer
that CI will not let it merge without.

### Slice 0 — Measure the substrate before any slice commits to it — **COMPLETE**

Findings are in `specs/backup-restore-model.md`; the tooling decision is
ADR-067. In brief, and in the order they mattered:

- **ADR-031 costs nothing.** pgBackRest takes a full backup of a cluster on
  which `pg_basebackup` is refused *for the superuser*, because it copies the
  data directory over an ordinary libpq connection between `pg_backup_start()`
  and `pg_backup_stop()` — 0 walsenders during a backup. Shown both ways: the
  `--permissive` cluster, built without the reject, lets `pg_basebackup` take a
  39 MB copy of every database. **No security control has to be narrowed, and
  the front-runner tool was not disqualified.**
- **An untuned backup of an idle cluster waits forever.** pgBackRest's default
  begins after the next *regular* checkpoint, and PostgreSQL skips timed
  checkpoints when no WAL has been written. Measured at 15+ minutes, 0% CPU,
  `num_timed = 0` after forty minutes of uptime. That is precisely the free
  tier's shape, so the nightly backup of a node full of sleeping projects hangs
  rather than fails — no error and nothing in the repository next morning.
- **Per-tenant restore works and the node stays up.** 187 s end to end through a
  scratch cluster for a tenant on a 219.7 MB base with ~720 MB of WAL, with the
  live node's nine tenant databases available throughout, and with a marker row
  proving the copy went *back in time* rather than merely completing.
- **A move silently reassigns ownership to whoever restores.** Onto a node that
  has never seen the tenant, all 164 RLS policies and 268,000 rows arrive and
  `auth` and `storage` change owner from their per-tenant service roles to
  `postgres`. ADR-059 exists to keep that schema away from superuser reach.
  This is the finding slice 7 is built around, and slice 2 inherits it.
- **Backup is cheap in disk and expensive in CPU.** 9.4:1 compression, because
  ADR-015 puts identical `maludb_core` bytes in every tenant database; 105.9 s
  for a 219.7 MB cluster at the default `process-max=1` and 46.9 s at 4.
  Extrapolated to 200 tenants that is ~40 minutes of the node's own cores.

What it deliberately did not measure — SeaweedFS durability, extension version
drift, Barman and wal-g, and anything at node scale — is listed in the spec so
silence is not read as a result.

No production code. Deliverable is `specs/backup-restore-model.md` and enough
evidence to answer the blocked questions above. In rough priority order:

1. **Does the chosen tool survive ADR-031?** This is the first measurement
   because it can disqualify the front-runner. `host replication all <cidr>
   reject` blocks the physical replication protocol, which is what
   `pg_basebackup` and any tool built on it uses. pgBackRest's default backup
   path copies files with a normal `pg_backup_start()`/`pg_backup_stop()`
   session and does *not* need a replication connection; `archive-push` is
   likewise an ordinary connection plus a file copy. If that holds, the reject
   costs nothing. If it does not — or if the tool needs `--backup-standby`, or
   needs `REPLICATION` on its role — then either the tool is disqualified or
   ADR-031's reject must be narrowed from `all` to specific CIDRs, and
   narrowing a control that exists because a role took a 484 MB copy of every
   database on the cluster is an ADR, not a config tweak. **Measure it; do not
   reason about it.** The harness that proved the reject works
   (`tests/test_realtime_node.py`) is the harness that proves this.

2. **Full and incremental backup cost** at a realistic tenant count. Time,
   repository size, and effect on the node while it runs. Use the 24 MB floor
   to build a node with enough tenants to be meaningful rather than one.

3. **WAL volume per tenant per day** under a representative write load. This is
   the input to retention pricing and to the `pg_wal` sizing that ADR-032
   already treats as occupied disk rather than free space.

4. **Restore one tenant, end to end, timed.** Restore the cluster to a scratch
   directory at a PITR target, start it on a spare port, extract the one
   database, load it into the live cluster. Record wall-clock, peak disk, and
   what breaks. Expected sharp edges: the shared `anon`, `authenticated` and
   `service_role` roles (ADR-016) exist on the target and must not be recreated;
   the per-tenant roles are cluster-scoped and are not in a single-database
   dump; the `storage` schema is owned by a platform-internal role (ADR-059)
   and a restore that reassigns ownership breaks its RLS.

5. **Does a tenant database round-trip through `pg_dump`/`pg_restore` at all?**
   Specifically with `maludb_core` installed (ADR-015), a populated `auth`
   schema, RLS policies naming the shared roles, and the Phase 10 `storage`
   schema. Do it as a non-superuser: Phase 10 slice 1 already established that
   upstream migrations complete without the superuser they ask for, and the
   same standard applies here.

6. **Extension version drift across a restore or a move.** `docs/MALUDB.md`
   records that the control plane has no per-project record of installed
   extension or bootstrap versions and says plainly "It needs one." A tenant
   dumped from a node with `maludb_core` 0.104.0 and loaded onto a node with a
   different build is the case that turns that gap into an outage. Measure
   whether it fails loudly or quietly.

7. **Object-side durability options** in the pinned SeaweedFS build:
   replication factor, erasure coding, and what each costs in disk. Phase 10
   deliberately did not measure these.

Slice 0 ends with ADR-067 naming the tooling, and with the retention and PITR
tier shape written down as numbers rather than adjectives.

### Slice 1 — Node backup, scheduled and verified — **COMPLETE**

Repository configuration, WAL archiving turned on as a node prerequisite in the
same shape as ADR-031's `pg_hba` line and ADR-032's WAL bound — asserted at node
registration, not assumed. `cp-manage node backup` to run one and report;
backup metadata in the control plane, which `docs/ARCHITECTURE.md` has reserved
under "backup metadata later" since Phase 01. Migration `0026`.

A backup that has never been restored is not known to be a backup, so
verification is part of this slice and not a later one: the maintenance pass
checks that the most recent backup for each node exists, is complete, and is
within its expected age, and says so.

### Slice 2 — Restore one tenant — **COMPLETE**

The runbook from slice 0's measurement, plus the tooling that executes it, plus
a test that performs a real restore of a real tenant on the throwaway cluster
and asserts the data came back and that the *other* tenants stayed up. Closes
acceptance criterion 1.

### Slice 3 — PITR and retention as entitlements

Retention and PITR windows become plan entitlements with configured defaults,
never hard-coded numbers. Free-tier policy — `docs/BACKUP-RECOVERY.md` still
says "final policy TBD" — is decided here with the measured storage cost in
hand.

### Slice 4 — Objects: durability and reconciliation

Object-store durability configuration from slice 0's measurement. A
reconciliation pass over `storage.objects` versus the bucket that finds orphaned
bytes and dangling rows, reports both, and deletes nothing without an explicit
state check. Whatever this phase can honestly claim about objects at a point in
time gets written into `docs/STORAGE.md` and the compatibility matrix.

### Slice 5 — The control plane's own recovery

Backup of the control-plane database, and a documented, executed procedure for
recovering key material (ADR-023). Includes the case that matters: proving that
a control plane restored from backup can still unwrap a node admin DSN and
administer a node that was never lost.

### Slice 6 — Node pools with a policy

Wire the entitlement from proposed ADR-065 through `reserve_placement`, so a
production project is placed in a production pool and a free project is not.
Include the migration path for projects already placed in `shared`, and what
happens when a plan change implies a different pool than the project is on —
which is tenant movement, and therefore a dependency on slice 7 for anything
beyond refusing.

### Slice 7 — Drain and tenant movement

Move a project to another node preserving `project_ref`, hostname, API keys and
data. Reuse ADR-044's measured write freeze rather than inventing a second
freeze mechanism — Phase 08 already built and measured one for cutover, and a
move is the same problem with both ends inside the platform. Turn `draining`
into an operation that completes. Closes acceptance criterion 3.

### Slice 8 — Node failure recovery, DR runbooks, capacity alerts

Rebuild a lost node from backup onto fresh hardware and time it, producing an
RTO figure rather than an intention. Capacity alerting on the ceilings
`docs/CAPACITY.md` already names — connections, warm projects, replication
slots, disk — through the maintenance pass. Closes acceptance criteria 2 and 4,
moves the plan to `plans/completed/`, and answers the `## Backups` and
`## Node scheduling` sections of `docs/OPEN-QUESTIONS.md` in place.

## Verification

- [x] `tests/test_backup.py` — repository configuration, backup metadata,
      verification pass, and the node-prerequisite assertion. 32 tests; three
      need the measurement cluster and one of those is the walsender count.
- [x] `tests/test_restore.py` — a real per-tenant restore on a throwaway
      cluster, asserting both that the tenant came back and that its neighbours
      were never interrupted. 31 tests; the end-to-end one asserts that *only*
      the pre-target write returned, which is the difference between recovering
      data and copying it.
- [ ] `tests/test_tenant_movement.py` — identity preserved across a move:
      same `project_ref`, same hostname, same keys, data intact, old node clean.
- [ ] `tests/test_placement.py` extended for pool policy, including the
      already-placed and plan-change cases.
- [ ] Object reconciliation tested against a real object store, both
      directions.
- [x] `scripts/backup-test-cluster.sh` builds and drops the measurement
      cluster, and — following the precedent of the Realtime script — can build
      a deliberately *unprotected* one, so a check that has never returned
      unsafe is not mistaken for a working check. Delivered in slice 0; CI now
      builds it and asserts both halves.
- [x] `MALUDB_REQUIRE_BACKUP_REPO=1` in CI, so an absent repository is a failed
      run rather than a skipped test, and a banner line locally when it is
      absent. This repository's recurring failure mode is a green run that
      verified nothing; four existing `MALUDB_REQUIRE_*` variables exist because
      of it, and this phase adds the fifth rather than repeating the mistake.
- [ ] `docs/BACKUP-RECOVERY.md` rewritten from a 37-line placeholder into what
      was built. `docs/CAPACITY.md` gains backup's disk and WAL terms.
      `docs/OBSERVABILITY.md` gains the alert set.
- [ ] `docs/OPEN-QUESTIONS.md` `## Backups` and `## Node scheduling` answered
      in place, in the style Phase 10 used for `## Storage`.
- [ ] A `Security-Review:` trailer on every slice.

## Risks

- **ADR-031 disqualifies the front-runner tool.** Mitigated by making it slice
  0's first measurement rather than something slice 1 discovers. If the reject
  must be narrowed, that is an ADR with a security review, not a config change.

- **Per-tenant restore turns out to require a full cluster restore every
  time.** This is the shape the design must not have —
  `docs/BACKUP-RECOVERY.md` says so directly: "Do not make 'restore one
  project' require replacing the entire shared node in production." Restoring
  to a *scratch* cluster and extracting one database satisfies that; restoring
  in place does not. If the scratch path is too slow to be operationally real,
  the answer is more frequent logical per-database backups, not a cluster-wide
  restore.

- **Disk.** A restore-to-scratch needs room for a second copy of the cluster.
  On a node near its disk ceiling there is none, which makes free disk a
  restore prerequisite and therefore a capacity term, not just a placement one.
  Slice 8's alerting must account for it.

- **The two data sets diverge and the phase quietly ships PITR that only
  covers one.** Mitigated by requiring slice 3 to state explicitly, in the
  customer-facing text, what a point-in-time restore does to objects.

- **Extension version drift makes a move fail after the freeze has started.**
  Mitigated by measuring it in slice 0 and by recording per-project extension
  versions before slice 7 moves anything — the record `docs/MALUDB.md` already
  says is needed.

- **Movement races the maintenance pass.** A project being moved is a project
  whose workers may be slept, whose storage may be measured, and whose billing
  may be applied, all by a pass explicitly designed to be safe to interrupt but
  not designed for the row underneath it changing nodes. Slice 7 needs a state
  the other passes respect.

- **Scope.** Nine slices with two additions to the task file's scope is a large
  phase, and the additions are load-bearing rather than optional. If it needs
  splitting, the natural line is after slice 5: everything before it is
  durability, everything after it is placement and movement.

## Decision log

- 2026-08-26 — Plan written. Three decisions proposed for ratification before
  slice 1 (repository failure domain, pool by entitlement, movement is
  operator-initiated) and deliberately **not** written into
  `docs/DECISIONS.md`, because a plan may not override that file and because
  ratifying them is the owner's call. Proposed numbers ADR-064 to ADR-066, with
  ADR-067 reserved for slice 0's tooling decision.
- 2026-08-26 — Unlike Phase 10, the open questions are **not** answered in the
  plan commit. Four of the five backup questions depend on measurements nobody
  has taken, and answering them from tool reputation is the specific way this
  phase would go wrong.
- 2026-08-26 — Two scope additions beyond the task file: object durability and
  reconciliation, which Phase 10 deferred here explicitly; and control-plane
  backup with key-material recovery, which is in no phase's scope and is the
  highest-consequence gap on the platform.
- 2026-08-26 — High availability is a non-goal. Recovery here is
  restore-from-backup with a measured RTO. Replicas would reopen ADR-031.
- 2026-08-26 — **ADR-067 accepted: pgBackRest**, and ADR-031 stays exactly as
  written. The tension the plan opened on turned out not to exist, which is the
  outcome slice 0 was ordered first to find cheaply. Barman and wal-g were not
  examined — a deliberate stop, recorded in the ADR, rather than an oversight.
- 2026-08-26 — Slice 0 answered two of the five `## Backups` open questions in
  place. The three that remain need product input (retention tiers) or a
  ratified ADR-064 (repository location), not another measurement.

## Progress log

- 2026-08-26 — Phase 10 closed and merged (PR #88). Plan written on
  `plan/phase-11-production-resilience`. No code. Slice 0 is next and needs
  none; it needs a throwaway cluster and disk.
- 2026-08-26 — **Slice 0 complete** on `feat/phase-11-slice-0`, stacked on the
  plan branch. Delivered `specs/backup-restore-model.md`,
  `scripts/backup-test-cluster.sh`, `scripts/bench-backup.py` and ADR-067;
  `docs/CAPACITY.md` gained backup's disk, CPU and WAL terms and
  `docs/OPEN-QUESTIONS.md` two answers. No production code, no schema, no route.
- 2026-08-26 — The disk precondition was real and nearly stopped the slice. The
  host was at 95% with 1.6 GB free, against a plan that asks for room for a
  second copy of a cluster. The cause was not tenant data: `pgaudit.log =
  'read, write, ddl, role, function'` with `log_catalog = on` is set
  cluster-wide in the vendor MaluDB install, `logging_collector` is off, and
  logrotate runs weekly — 12.9 GB in one file, ~215 MB/day, on a development
  box. `docs/OPEN-QUESTIONS.md` still asks whether pgaudit should be on per
  tenant, per node, or not at all; on this node the answer has been "on, for
  every SELECT, into one file" since June. **It is a node-availability path with
  no relationship to tenant activity and it belongs in slice 8's alerting**,
  which is where a disk term that fills without customers is somebody's
  problem.
- 2026-08-26 — Two bugs found in slice 0's own harness by using it, both of the
  kind the phase is about. `--drop` left the stanza section in
  `/etc/pgbackrest.conf`, so a rebuild produced two `[maludb-bk]` blocks and
  pgBackRest refused to start; and `RETENTION_FULL` was declared and never
  written, which is why every run warned that the repository may run out of
  space. Cleanup that is never exercised is not cleanup.
- 2026-08-27 — **Slice 1 complete** on `feat/phase-11-slice-1`. Migration 0026,
  `services/control_plane/backup.py`, three `cp-manage node` commands, a
  `backups` pass in the maintenance run, and `tests/test_backup.py`. ADR-064
  ratified with a severity split — production refuses a co-located repository,
  development warns — and both directions verified end to end against the
  measurement cluster rather than only in unit tests.
- 2026-08-27 — The walsender assertion is the one worth keeping. ADR-067's claim
  is that pgBackRest opens **no** replication connection, which is why ADR-031's
  reject costs nothing; slice 0 measured it once, and a test that samples
  `pg_stat_replication` during a real backup is what stops it quietly ceasing to
  be true if an option like `--backup-standby` is ever added. Measured 0 again
  here. CI asserts both halves in the build step too: that `pg_basebackup` is
  refused on that cluster, and that `pgbackrest check` passes on it.
- 2026-08-27 — **A gap the plan did not anticipate: pgBackRest has to run as the
  cluster's owner**, and it says so badly. It reads the data directory and a
  0600 `/etc/pgbackrest.conf`, so any other user gets `unable to open file
  '/etc/pgbackrest.conf' for read: [13] Permission denied` — an error naming a
  config file rather than the actual cause. Found by running the tests as the
  ordinary user. Handled with `MALUDB_BACKUP_RUN_AS`/`--run-as` and an annotated
  error, rather than by defaulting to a silent `sudo -u postgres`: a control
  plane that is not on the node has no business shelling out to sudo, and a
  hidden one is worse than an error that says what to do. Same shape as the
  root-has-no-DAC-override note already in `scripts/backup-test-cluster.sh`.
- 2026-08-27 — Backup readiness is recorded on the node and **deliberately not
  wired into `rejection_reason`**. A node with no backup has a real problem and
  is still a perfectly good node for the projects already on it; refusing
  placement would strand capacity to punish an operator for something a report
  can tell them. `maintenance.check_backups` raises it instead, and counts it as
  *failed* rather than merely noted. Report before enforcing — Phase 05's
  lesson, applied in the other direction.
- 2026-08-27 — No `verified` column, and that was a deliberate omission rather
  than an oversight. This slice can say a backup was recorded; only a restore
  says one is recoverable, and that is slice 2. A green field named `verified`
  is how a backup system lies to its operator, so `docs/BACKUP-RECOVERY.md`
  states the limit in its own section instead.
- 2026-08-27 — **Slice 2 complete** on `feat/phase-11-slice-2`, stacked on slice
  1. Migration 0027 (`tenant_restores`), `services/control_plane/restore.py`,
  a `cp-manage restore` group, the runbook in `docs/BACKUP-RECOVERY.md`, and
  `tests/test_restore.py`. **Acceptance criterion 1 is closed** and ticked in
  the task file.
- 2026-08-27 — The restore is non-destructive by construction rather than by
  confirmation. Recovered data lands in a database beside the live one;
  `restore activate` renames both ways and drops neither, so the previous
  database survives as `<db>_pre_restore_<ts>` and a wrong activation is
  reversible with two `ALTER DATABASE`s. There is no code path in this slice
  that destroys a tenant's data, which is a stronger property than "requires an
  explicit state check" and costs only disk.
- 2026-08-27 — Slice 0's ownership finding is now a gate rather than a note.
  `load_into_target` refuses when the target cluster lacks the tenant's roles —
  failing *before* the load, because slice 0 measured what happens after it:
  `pg_restore` carries on past eleven "role does not exist" errors, leaves every
  row and all 164 policies in place, exits 1, and hands back a database whose
  `auth` and `storage` are owned by the superuser. Ownership is then verified on
  the loaded copy and recorded, and **activation refuses without it** (ADR-059).
- 2026-08-27 — **Three bugs found by running it, and the second was the
  dangerous one.** (1) pgBackRest's `--target` rejects ISO-8601: the `T`
  separator fails with an error naming neither it nor the field. Slice 0 never
  hit it because its harness passed `now()::text` through. (2) `pg_restore` was
  invoked without a port, so it addressed the *live* cluster on 5432 rather than
  the node under restore — it surfaced only as `database "..." does not exist`,
  and on a node running both clusters that is the worst failure this module
  could have. The port now comes from the connection that created the database.
  (3) A `%` in a LIKE literal was read as a placeholder.
- 2026-08-27 — A fourth found by running the suite twice: the end-to-end test
  was not isolated. Tenant databases live on the backup cluster, which the
  control-plane fixture's TRUNCATE does not reach, so the marker table
  accumulated rows across runs and the central assertion started failing for a
  reason unrelated to the change. Fixed by dropping the tenant first — the
  drop-then-create rule the cluster scripts already follow, for exactly this
  reason. Confirmed by running the file twice in a row.
- 2026-08-27 — Guarding `pg_dropcluster` needed more than a validated name. A
  name cannot check itself, so cluster creation writes a marker into the
  *configuration* directory — not the data directory, which pgBackRest rewrites
  during a restore, so a marker there would be gone at exactly the moment it is
  needed — and nothing is dropped without it. The live `data_directory` is read
  from the node and compared as well: two checks that must agree, the pattern
  `realtime` already uses for `pg_hba.conf`.
- 2026-08-27 — **Objects are still not covered, and the docs now say so where a
  customer-facing claim would be made.** A restored tenant gets its
  `storage.objects` rows back at the target time; the bytes in the shared bucket
  are present-day and unversioned. That is slice 4's work, and slice 3 must
  state the limit in customer-facing text rather than leaving PITR to imply more
  than it covers.
