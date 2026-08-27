# Backup and Recovery

## Status

**Phase 11 slices 1 and 2 are built: node backup, and per-tenant restore.**
The tool is pgBackRest (ADR-067), the repository rule is ADR-064. What exists is
a node prerequisite check, a command that takes a backup and records it, a
maintenance pass that says when a node's backups have stopped arriving, and a
restore that recovers one tenant to a point in time while the rest of the node
keeps serving.

What does **not** exist yet, and is not claimed anywhere in this document:
PITR and retention as plan entitlements (slice 3),
object durability and reconciliation (slice 4), control-plane recovery (slice
5), and node failure recovery with a measured RTO (slice 8). See
`plans/active/phase-11-production-resilience.md`.

Measurements behind every number here: `specs/backup-restore-model.md`.

## The one thing the backup slice does not prove

**A recorded backup is not a verified backup.** `node_backups` rows say what the
platform did and what pgBackRest reported. Only a restore proves recoverability,
and restore is slice 2. There is deliberately no `verified` column and no check
that returns "backups are fine" — a green field with that name is how a backup
system lies to its operator.

The verification that *does* exist is narrower and worth stating exactly: the
maintenance pass checks that a recent, complete backup was recorded, and that a
full backup exists to root the differential chain on. It never opens the
repository.

Slice 2 changes what is *provable* rather than what is claimed. A restore that
completes, comes back with the data as it was at the target time, and passes the
ownership check **is** evidence — for that backup, at that moment. Running one
is how a backup becomes known to be a backup, which is why the procedure below
is a runbook rather than a design note.

## Shared-cluster complication

Many tenant databases share one PostgreSQL/MaluDB cluster. Backup strategy
therefore distinguishes:

- cluster disaster recovery;
- per-tenant restore;
- point-in-time recovery;
- logical portability/migration.

A pgBackRest stanza owns a whole cluster, so backup is **per node**. Per-tenant
granularity is a *restore* concern, not a repository one: slice 0 measured
restoring one tenant through a scratch cluster at 187 s with the live node's
other nine tenant databases available throughout.

## Node prerequisites

Backup preconditions are node configuration, in the same shape as ADR-031's
`pg_hba.conf` reject and ADR-032's WAL bound — **asserted, not assumed**:

```bash
cp-manage node backup-check --name n1 --stanza maludb-n1
```

Non-zero exit means the node is not ready, so a node-build script fails rather
than printing a reason into a log nobody reads. It records the result on the
node row, so `cp-manage node backups` and the maintenance pass can answer
without opening a connection to the node.

What it refuses, ordered by how expensive the fix is:

| Condition | Why it is fatal |
|---|---|
| `archive_mode` not `on`/`always` | No WAL archive, so no point in time and nothing restorable past the moment of the backup. **Postmaster context** — fixing it restarts the cluster and takes every tenant on the node down |
| `wal_level` below `replica` | The WAL a replay needs is not written at all. Also postmaster context |
| `archive_command` empty | Archiving is on and nothing is archiving |
| Archiver has failed and never archived | WAL is accumulating in `pg_wal` and no backup taken here is restorable. **Invisible from the cluster** — every tenant is served normally |
| `pgbackrest check` fails | The repository is configured and does not work |
| `repo1-retention-full` unset | The repository grows without bound; the only symptom is a warning |
| `repo1-retention-archive` unset | WAL outlives every backup it belongs to. Both halves, or expiry is half-done |
| Repository co-located with the data | ADR-064 — **production only**; a warning elsewhere |

And what it warns about without refusing: `archive_timeout = 0` (on a cluster
nobody is writing to, no segment is closed and the recoverable point in time
stops advancing — ADR-022 makes that the free tier's normal state); an archiver
with past failures that is working now; and a repository that could not be
inspected from here at all.

That last one matters. pgBackRest runs on the node. A control plane somewhere
else cannot see the repository, and the check says so rather than passing it:
**an unexamined repository is not a healthy one.**

## Taking one

```bash
cp-manage node backup --name n1 --type full --process-max 4
```

`--start-fast` is passed on every run and there is no way to turn it off.
ADR-067 measured what its absence does: on an idle cluster, pgBackRest waits for
a regular checkpoint that PostgreSQL never schedules, because it skips timed
checkpoints when no WAL has been written. 15+ minutes at 0% CPU, `num_timed = 0`
after forty minutes of uptime. A node full of sleeping free-tier projects is
exactly that cluster, so the nightly backup would hang rather than fail.

pgBackRest must run as the cluster's owner — it reads the data directory and
`/etc/pgbackrest.conf` is mode 0600. Set `MALUDB_BACKUP_RUN_AS` or run the
command as that user; root is not sufficient without `CAP_DAC_OVERRIDE`. A
permission failure names the config file rather than the cause, so the error is
annotated with what to actually do.

`--process-max` is the only lever that matters for wall clock — 2.26x measured
between 1 and 4 — and those cores come from the node the tenants are running on.
Backup scheduling is a capacity question as well as a durability one.

## What the platform believes

```bash
cp-manage node backups
```

Reports every node, including ones nobody has prepared — a query that returned
only prepared nodes would answer "all healthy" on a platform with no backups at
all. Exits non-zero when anything is wrong.

The same checks run in `cp-manage maintenance run` (ADR-053) as the `backups`
pass, and they are all questions about the **record**:

- the node has no stanza — nobody prepared it;
- no backup has ever been recorded;
- the most recent backup failed, with its reason;
- the most recent backup is older than the node allows
  (`nodes.backup_max_age_hours`, defaulting to 26 — configuration, not a
  constant, because hourly and weekly are both legitimate);
- a backup has been `running` for over six hours, which is the ADR-067 hang;
- there is no completed *full* backup to root the chain on.

**Silence is read as failure, never as "still going".** That is the whole design
of the pass: the failure it exists to catch produces no error, no exit code and
nothing in the repository.

## Restoring one tenant

The requirement is a constraint on shape, not on speed: a shared node carries up
to 200 tenants, so restoring in place to recover one of them is an outage for
the other 199 — caused by somebody else's mistake, which is the worst kind a
platform can serve.

So the restore never touches the running node's data directory. It builds a
**scratch cluster**, restores the repository into it at a point in time,
promotes it, extracts the one database, and loads it *beside* the live one.

```bash
cp-manage restore run --ref abcd0001 --target-time 2026-08-27T09:15:00+00:00
```

Omit `--target-time` for the latest consistent state in the repository. **The
offset is required** — pgBackRest reads a naive timestamp as the node's local
time, which on a node in another zone silently picks a different moment in the
customer's history.

What it prints is what it cost, and one line that matters more than the rest:

```
abcd0001: restored to mldb_abcd0001_restore_20260827091500 in 187.0s
  scratch restore        148.7s
  recovery to promoted    31.2s
  extract one tenant       3.4s  1.03 MB
  load beside the live     6.5s
  other tenants answering throughout: 9
  ownership            every tenant-owned schema is owned by its per-tenant role

  The live database mldb_abcd0001 was not touched.
```

### What it refuses to do

**It never writes over a live database.** The recovered data lands in a new
database next to the original. There is no code path that destroys a tenant's
data — a stronger property than "asks for confirmation", and it costs only disk.

**It never drops a cluster it did not create.** `pg_dropcluster` on the wrong
name destroys a node and every tenant on it, and a name cannot check itself. So
creation writes a marker into the cluster's *configuration* directory —
pgBackRest rewrites the data directory during a restore, so a marker there would
be gone exactly when it is needed — and nothing is dropped without it. The live
cluster's `data_directory` is read from the node and compared as well.

**It never reports a restore as complete without checking who owns it.** See
below.

### Ownership, and why it gates activation

This is slice 0's sharpest finding and it produces no error at all. Restoring a
tenant into a cluster that has never seen it moves `auth` and `storage` from
their per-tenant service roles to whoever ran the restore — the platform
superuser — while all 164 RLS policies and every row arrive intact and
`pg_restore` exits 1 with "errors ignored".

ADR-059 puts the `storage` schema under a per-tenant role *specifically* so it
is not owned by something with superuser reach, and ADR-061 tells customers they
cannot author policies there on that basis. **A tenant restored the naive way
arrives with a different security posture from the one that was backed up, and
nothing about the database says so.**

So the load refuses outright when the target cluster lacks the tenant's roles —
failing before the load is cheaper than diagnosing after it — and the restore
records whether the schemas came back correctly owned. Activation refuses
without it.

### Activating a restore

```bash
# Stop the project's workers first: a database with open connections
# cannot be renamed.
cp-manage restore activate --ref abcd0001
```

Renames both ways and drops neither. The database that was live is retained as
`mldb_abcd0001_pre_restore_<timestamp>`, so an activation that turns out to have
been the wrong call is reversible with two `ALTER DATABASE ... RENAME TO`.

`cp-manage restore list` shows what has been restored and what became of it.

### Prerequisites and limits

- **Disk is the real constraint, not time.** A scratch restore holds a second
  copy of the whole cluster. That makes free disk a *restore* prerequisite and
  not only a placement one — `nodes.DEFAULT_MIN_FREE_DISK_BYTES` is a placement
  floor, not a restore budget, so a node can be comfortably placeable and unable
  to restore. Checked before the cluster is built.
- **It runs on the node**, because it creates and destroys a PostgreSQL cluster
  (root) and reads a pgBackRest repository (the cluster owner).
- **It is necessarily a platform operation.** Not policy: `CREATE EXTENSION
  maludb_core` requires superuser because `maludb_core` is not trusted
  (ADR-015), so there is no non-superuser path that produces a working tenant
  database. "Let the customer restore their own project" is not an option that
  was rejected; it is one that does not exist.
- **Objects are not covered.** A tenant's `storage.objects` rows are restored;
  the bytes in the shared bucket are not, and are not versioned. A
  point-in-time database with present-day objects is what this delivers today —
  slice 4 addresses reconciliation, and slice 3 must state the limit in
  customer-facing text.

## Desired capabilities

Free:
- policy still TBD, and now costed. Slice 0 measured WAL compressing about 10:1
  in the archive; a tenant writing continuously costs roughly 120 MB of archive
  per hour and a sleeping one costs nothing. Decided in slice 3.

Paid:
- scheduled backups — **built**;
- clearly documented retention — configured, both halves, and asserted;
- per-project restore where practical — **built**;
- PITR on eligible plans — slice 3.

## Design requirement

Do not make "restore one project" require replacing the entire shared node in
production.

Satisfied by the procedure above. Both of slice 0's sharp edges are handled in
code rather than in prose: the scratch cluster is created with `archive_mode =
off`, so a promoted copy cannot push a new timeline into the repository it was
restored from; and ownership is verified after every restore, because a load
onto a cluster that has never seen the tenant silently reassigns `auth` and
`storage` to whoever ran it.
