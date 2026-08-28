# Backup and Recovery

## Status

**Phase 11 slice 1 is built: node backup, scheduled and verified.** The tool is
pgBackRest (ADR-067) and the repository rule is ADR-064. What exists is a node
prerequisite check, a command that takes a backup and records it, and a
maintenance pass that says when a node's backups have stopped arriving.

What does **not** exist yet, and is not claimed anywhere in this document:
per-tenant restore (slice 2), PITR and retention as plan entitlements (slice 3),
object durability and reconciliation (slice 4), control-plane recovery (slice
5), and node failure recovery with a measured RTO (slice 8). See
`plans/active/phase-11-production-resilience.md`.

Measurements behind every number here: `specs/backup-restore-model.md`.

## The one thing this slice does not prove

**A recorded backup is not a verified backup.** `node_backups` rows say what the
platform did and what pgBackRest reported. Only a restore proves recoverability,
and restore is slice 2. There is deliberately no `verified` column and no check
that returns "backups are fine" — a green field with that name is how a backup
system lies to its operator.

The verification that *does* exist is narrower and worth stating exactly: the
maintenance pass checks that a recent, complete backup was recorded, and that a
full backup exists to root the differential chain on. It never opens the
repository.

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

## Desired capabilities

Free:
- policy still TBD, and now costed. Slice 0 measured WAL compressing about 10:1
  in the archive; a tenant writing continuously costs roughly 120 MB of archive
  per hour and a sleeping one costs nothing. Decided in slice 3.

Paid:
- scheduled backups — **built**;
- clearly documented retention — configured, both halves, and asserted;
- per-project restore where practical — slice 2;
- PITR on eligible plans — slice 3.

## Design requirement

Do not make "restore one project" require replacing the entire shared node in
production.

Slice 0 measured the shape that satisfies this: restore the cluster to a
**scratch** cluster at a PITR target, start it on a spare port, and extract the
one database. 187 s end to end, with the live node untouched. Restoring in place
would satisfy nothing.

Two sharp edges recorded there for slice 2: the scratch cluster must have
`archive_mode = off`, or a promoted copy pushes a new timeline into the
repository it was restored from; and a restore onto a node that has never seen
the tenant silently reassigns `auth` and `storage` ownership to whoever ran it —
`postgres` — which is exactly what ADR-059 exists to prevent.
