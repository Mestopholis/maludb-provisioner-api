# Backup and Restore Model

What it takes to back up a MaluDB node, restore one tenant out of it, and move
a tenant to another node — and what each costs. Deliverable of Phase 11 slice 0.

Status: derived from experiments run 2026-08-26 against pgBackRest 2.59.1 and
PostgreSQL 17 on the development host (6 cores, 3.9 GB RAM). Every claim here
was measured on a running cluster built by `scripts/backup-test-cluster.sh`,
with tenants provisioned through the platform's own provisioning module —
`maludb_core` (ADR-015), all seven per-tenant roles, the ADR-014 lockdown, and
the versioned bootstrap with its `auth` and `storage` schemas. The harness is
`scripts/bench-backup.py`.

Companion to `plans/active/phase-11-production-resilience.md`, which records
what the phase intends to build, and to `specs/realtime-replication-model.md`,
whose finding R7 is the constraint this document had to test first.

## The finding that unblocks the phase

**pgBackRest takes a full backup of a cluster on which `pg_basebackup` is
refused, including for the superuser.** ADR-031 requires every node to carry
`host replication all <cidr> reject`, because a non-superuser holding
`REPLICATION` took a 484 MB physical copy of every database on a cluster. That
reject is exactly what a base-backup tool needs, so the phase's first question
was whether the platform's own security control makes the platform unable to
back itself up.

It does not, and the reason is mechanical rather than lucky. pgBackRest copies
the data directory between `pg_backup_start()` and `pg_backup_stop()` over an
**ordinary libpq connection**. It opens no replication connection, and
`archive-push` is likewise a normal connection plus a file copy.

| On the same cluster, same moment | Result |
|---|---|
| `pgbackrest stanza-create` | OK |
| `pgbackrest check` | OK — proves `archive_command` works through the reject |
| `pgbackrest backup --type=full` | OK — 22.1 MB, 963 files, 10.6 s (empty cluster) |
| `pg_basebackup -h 127.0.0.1 -p 5434 -U postgres` | `FATAL: pg_hba.conf rejects replication connection for host "127.0.0.1", user "postgres"` |
| `pg_stat_replication` during the backup | **0 walsenders** |

And the control, because a check that has never returned "unsafe" has not been
tested. `scripts/backup-test-cluster.sh --permissive` builds the same cluster
**without** the ADR-031 line:

| On the permissive cluster | Result |
|---|---|
| `pg_basebackup` | **succeeds** — 39 MB copy of every database |
| `pgbackrest backup --type=full` | still fine — it never wanted the protocol |

So the reject is what blocks the base backup, not some other property of the
cluster, and pgBackRest is indifferent to it in both directions.

**ADR-031 needs no amendment, and none of its reasoning has to be reopened.**
Recorded as ADR-067.

## Two ways an untuned pgBackRest fails quietly

Both were found by running it rather than reading about it, and both are
configuration rather than defects.

### A backup of an idle cluster waits forever

pgBackRest's default is `start-fast=n`, which means "begin after the next
regular checkpoint completes". PostgreSQL **skips a timed checkpoint when no
WAL has been written since the last one**. On a cluster that is up but idle,
there is no next checkpoint, and the backup waits indefinitely.

Measured: a full backup sat at `execute backup start: backup begins after the
next regular checkpoint completes` for over 15 minutes at 0% CPU, on a cluster
with `checkpoint_timeout = 300`. `pg_stat_checkpointer` explained it —
**`num_timed = 0`** after forty minutes of uptime.

This is the free tier's shape exactly. ADR-022 puts free-tier economics on
projects that sleep, so a node full of sleeping projects is a node writing no
WAL, and its nightly backup would hang rather than fail — no error, no exit
code, no alert, and nothing in the repository the next morning. `--start-fast`
forces an immediate checkpoint and costs one checkpoint's I/O.

**Any scheduled backup this platform runs must pass `--start-fast`, and the
verification pass must treat "no new backup" as a failure rather than waiting
for one.**

### Retention is unset, so nothing is ever expired

Out of the box pgBackRest warns on every run that `repo1-retention-full` is not
set and that "the repository may run out of space", and separately that
`repo1-retention-archive` is not set so "archive logs will not be expired". Both
are true and neither is fatal, which is the problem: the repository grows
without bound and the only symptom is a warning nobody reads.

`repo1-retention-archive` must be set alongside `repo1-retention-full`, or WAL
outlives every backup it belongs to.

## What a backup costs

Eight tenants provisioned through the real path: **24.4 MB each, 194.9 MB
total**, 2.1–3.0 s each. That matches `docs/CAPACITY.md`'s measured 24 MB floor
exactly, ~15 MB of which is `maludb_core` in every tenant database (ADR-015).

| | wall clock | repository |
|---|---|---|
| Full, empty cluster (22.1 MB, 963 files) | 10.6 s | 2.8 MB |
| **Full, 8 tenants (219.7 MB)**, `process-max=1` | **105.9 s** | **23.3 MB** |
| Full, same cluster, `process-max=4` | **46.9 s** | 23.3 MB |
| Differential, no changes | 2.8 s | +5.3 MB |
| Incremental, no changes | 2.7 s | +5.3 MB |

Two things follow.

**Compression is 9.4:1, and it is structural rather than lucky.** 219.7 MB of
cluster becomes 23.3 MB of repository, because ADR-015 puts the same ~15 MB of
`maludb_core` in every tenant database and identical bytes compress to nothing.
Density makes MaluDB nodes *cheaper* to back up per tenant than a general
PostgreSQL fleet would be.

**Parallelism is the only lever that matters, and it is 2.26x here.** At
`DEFAULT_MAX_PROJECTS = 200` a node holds ~4.9 GB at the floor, which
extrapolates to roughly **40 minutes** for a full backup at `process-max=1` and
**17 minutes** at 4, plus customer data. On a six-core node those minutes come
out of the node the tenants are running on, so backup scheduling is a capacity
question and not only a durability one.

## What WAL costs

Measured with 8 tenants writing continuously for 60 s — 2,144,000 rows of a
~200-byte payload:

| | |
|---|---|
| WAL generated | **721.2 MB** |
| Archive grew on disk | **72.4 MB** |
| Compression in the archive | **≈10:1** |
| Per 1,000 rows | 0.34 MB of WAL, 0.034 MB archived |

The per-tenant-day figure this extrapolates to (126 GB) is **not a planning
number** — it is eight tenants writing flat out with no think time, which no
real tenant does. The two numbers to plan retention against are the per-row
figure and the ~10:1 archive compression. A tenant writing continuously costs
about 120 MB of archive per hour; a sleeping free tenant costs nothing, which
is the same shape as every other cost in `docs/CAPACITY.md`.

`archive_timeout` bounds how stale a PITR target can be on an idle cluster —
set to 60 s here — and it does so by forcing a segment switch, so a node full
of idle tenants still writes one segment per tenant-less minute. That is a floor
on archive volume that has nothing to do with tenant activity.

## Restoring one tenant

`docs/BACKUP-RECOVERY.md` sets the requirement in one sentence: "Do not make
'restore one project' require replacing the entire shared node in production."
The path that satisfies it is a restore into a **scratch cluster** followed by
extraction of the one database.

Measured end to end, PITR to a timestamp, on a 219.7 MB base with ~720 MB of
WAL to replay:

| Step | |
|---|---|
| `pgbackrest restore --type=time --target-action=promote` into a scratch data directory | **148.7 s** |
| Recovery replayed and cluster promoted | **179.9 s** cumulative |
| `pg_dump` of the one tenant | **3.4 s**, 1.03 MB |
| **Total** | **187.0 s** |

Two assertions matter more than the timings.

**The restore genuinely went back in time.** A marker row was written before the
PITR target and a second after it. Only `before-target` was present in the
restored copy. A restore that merely completed would have shown both.

**The live cluster never stopped.** Its nine tenant databases were queried
throughout and were continuously available. This is the acceptance criterion the
whole path exists to satisfy, and it holds because nothing in the procedure
touches the running node's data directory.

The scratch cluster **must have `archive_mode = off`**. It is a copy of a
cluster whose `archive_command` names a stanza that is not its own, and a
promoted copy pushing a new timeline into the live repository is how a restore
exercise damages the backups it was testing.

Disk is the real constraint, not time: the scratch restore needs room for a
second copy of the cluster on whatever host performs it. **Free disk is
therefore a restore prerequisite, not only a placement term** — and
`nodes.DEFAULT_MIN_FREE_DISK_BYTES` is 20 GB, which is a placement floor rather
than a restore budget.

## Moving a tenant to another node — the finding for slice 7

A per-database `pg_dump`/`pg_restore` round trip **on the same cluster** is
clean: 2.0 s to dump, 6.5 s to restore, **zero errors**, all six extensions
including `maludb_core` 0.104.0, all six namespaces (`auth`, `maludb_core`,
`maludb_platform`, `mc2db`, `public`, `storage`), and all **164 RLS policies**.

Onto a cluster that has **never seen that tenant**, it is not clean, and the way
it fails is the point:

```
pg_restore: warning: errors ignored on restore: 11
  4x  role "mldb_bk000001_admin" does not exist
  6x  role "mldb_bk000001_auth" does not exist
  1x  role "mldb_bk000001_storage" does not exist
```

The data all arrived — 164 policies, 6 extensions, 268,000 rows — and
`pg_restore` exited **1**. What did not arrive was the privilege structure, and
what happened instead is worse than an omission:

| Schema | Owner on the source | Owner after the move |
|---|---|---|
| `auth` | `mldb_bk000001_auth` | **`postgres`** |
| `storage` | `mldb_bk000001_storage` | **`postgres`** |
| `maludb_platform` | `postgres` | `postgres` |
| `public` | `pg_database_owner` | `pg_database_owner` |

**Ownership silently falls back to whoever ran the restore, and that is the
platform superuser.** ADR-059 puts the tenant `storage` schema under a
per-tenant service role *specifically* so it is not owned by something with
superuser reach, and leaves its RLS deliberately unforced on that basis. ADR-061
tells customers they cannot author policies there because the owner is a
platform-internal role. A tenant moved this way arrives with a different
security posture from the one that was backed up, and nothing about the database
says so.

Three requirements for slice 7 follow, and they are not optional:

1. **Recreate the per-tenant roles on the target before restoring data**, not
   after. Cluster-scoped roles are not in a single-database dump and never will
   be; `pg_dumpall --roles-only` dumps every role on the source node, which is
   every other tenant's as well, so the mover has to synthesise them from the
   control plane's own record.
2. **Verify ownership after the move**, object by object, against what the
   source had. Checking `pg_restore`'s exit code is not enough — it is 1 here,
   but a mover that ignores it gets a database that looks complete.
3. **The shared `anon`, `authenticated` and `service_role` names must already
   exist on the target** (ADR-016). They did in this test, which is why 164
   policies restored without complaint; on a node without them the policy
   restore is where it would fail instead.

A per-tenant restore is also necessarily a **platform** operation rather than a
customer one, for a reason that is not about policy: `CREATE EXTENSION
maludb_core` requires superuser, because `maludb_core` is not a trusted
extension. There is no non-superuser path that produces a working tenant
database, so "restore as the customer" is not a design option.

## What slice 0 did not measure

Named here so a later reader does not mistake silence for a result.

- **Object durability.** SeaweedFS replication factors and erasure coding were
  not measured. Phase 10 deferred them here (`docs/STORAGE.md`), and they
  belong with slice 4's reconciliation work, which needs a running object store
  rather than a PostgreSQL cluster.
- **The two-data-sets problem, quantified.** That a per-tenant PITR of the
  database alone leaves `storage.objects` rows disagreeing with the bucket is
  argued in the phase plan and is structurally obvious; how far apart they drift
  in practice was not measured.
- **Extension version drift across a move.** `docs/MALUDB.md` records that the
  control plane has no per-project record of installed extension versions and
  says it needs one. Only `maludb_core` 0.104.0 is available on this host, so a
  cross-version restore could not be attempted. The gap is real; the failure
  mode is unmeasured.
- **Barman and wal-g.** Not examined. pgBackRest answered the question that
  gates the phase on the first attempt, and a two-way comparison would have cost
  slice 0's remaining budget to choose between tools that both work.
- **A node-scale backup.** Everything here is 8 tenants. The 200-tenant figures
  are extrapolations from a linear-looking measurement and are labelled as such.
- **The dump sizes are not representative.** The load generator writes
  `repeat('x', 200)`, which is why a 96 MB tenant database dumps to 1 MB. Backup
  and WAL figures are byte-level and unaffected; the *logical dump* figures
  would be several times larger for real customer data.

## Reproducing

```bash
sudo apt-get install -y pgbackrest
scripts/backup-test-cluster.sh                    # prints the exports
export MALUDB_BACKUP_NODE_DSN=...
export MALUDB_BACKUP_STANZA=maludb-bk
scripts/bench-backup.py provision --count 8
scripts/bench-backup.py backup --types full diff incr --process-max 4
scripts/bench-backup.py load --seconds 60 --tenants 8
scripts/bench-backup.py roundtrip --ref bk000001
scripts/bench-backup.py restore-tenant --ref bk000001
scripts/backup-test-cluster.sh --drop
```

`--permissive` builds the same cluster without the ADR-031 reject, which is how
the control in the first section was taken.

Both scripts are spike artefacts in the class of `scripts/bench-gateway.py`:
nothing imports them, and they are committed so these figures can be
reproduced rather than believed.
