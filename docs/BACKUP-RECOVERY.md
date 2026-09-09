# Backup and Recovery

## Status

**Phase 11 slices 1 to 7 are built.** Slices 1-3 cover a node's database,
slice 4 covers objects, slice 5 covers the control plane itself, slice 6 covers
node pools, and slice 7 turns drain into an operator-driven tenant movement
runbook. See "The control plane's own recovery" below — it is the one whose
absence would have made all the others useless.

The tool is pgBackRest (ADR-067), the repository rule is ADR-064, the recovery
windows are ADR-068, object durability is ADR-069, control-plane recovery is
ADR-070, node pools are ADR-065, and movement is ADR-066. What exists is a node
prerequisite check, a command that takes a backup and records it, a maintenance
pass that says when a node's backups have stopped arriving, a restore that
recovers one tenant to a point in time while the rest of the node keeps
serving, a per-plan window that both the restore and the node are checked
against, and an operator command that moves one stopped tenant to another node
without changing its public identity.

What does **not** exist yet, and is not claimed anywhere in this document: node
failure recovery with a measured RTO (slice 8). See
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
  point-in-time database with present-day objects is what this delivers today.
  Stated in the customer-facing terms below rather than left here; slice 4
  addresses reconciliation.

## When a node is lost

**There is no failover.** No standby, no leader election: a node outage is an
outage for every tenant on it until an operator rebuilds. That is the honest
position and the runbook says so rather than implying otherwise.

Recovery is a restore onto fresh hardware, then reconnecting the control plane.

### 1. Stop placing work on it

```bash
cp-manage node status --name node-01 --status unhealthy
```

Placement only considers `active` nodes, so this stops new projects landing on a
machine that cannot take them. Do it first: everything after it takes minutes.

### 2. Build the replacement and restore into it

Build a node the way `docs/DEPLOYMENT.md` section 2 describes — PostgreSQL 17,
`maludb_core`, `wal2json`, the `pg_hba.conf` reject — then restore the lost
node's stanza onto it, as the cluster owner:

```bash
sudo -u postgres pgbackrest --stanza=maludb-node-01 restore
sudo pg_ctlcluster 17 main start
```

The stanza is on the lost node's row, which is why that row is never deleted:

```bash
cp-manage node list          # shows the stanza recorded for each node
```

### 3. Register it, then reconnect the control plane

```bash
cp-manage node register --name node-02 --hostname node-02.example.com \
  --internal-host 10.0.0.21
cp-manage node rebuild --source-node node-01 --target-node node-02
```

`rebuild` refuses a target that already carries projects — rebuilding onto a
serving node turns one outage into two — checks that **every** restored tenant
still owns its own `auth` and `storage` schemas, and only then repoints
`projects.node_id`.

That order is the whole point. ADR-059: a restore that cannot find a tenant's
roles completes with `pg_restore` exiting 1 and "errors ignored", every row
present, and those schemas silently owned by whoever ran it. Repointing is what
sends customer traffic at the result, so it happens after the check.

A tenant that does not verify is **left pointing at the lost node**. That is
deliberate: a project pointing at a node that is gone is visibly broken and an
operator fixes it; a project pointing at a database whose `auth` schema belongs
to the superuser is not visibly anything.

### 4. Start the workers, and check

```bash
cp-manage node health --name node-02 ...
cp-manage deploy preflight
```

Then a real request against a real project, because that is the only check that
covers the gateway, DNS and TLS together.

### What it takes

`cp-manage node rebuild` prints its own elapsed time. **This is unmeasured
against a production-sized node**: the figure below is what the test cluster
produced, and it is recorded here as a measurement rather than a target so that
nobody quotes it as an RTO it has not earned.

| measured | tenants | data | control-plane reconnect |
|---|---|---|---|
| *not yet measured on real hardware* | — | — | — |

Fill this in the first time a real rebuild happens, with the date and the data
size. An RTO true only of a 50 MB node is worse than no RTO.

## When a node is degraded but still serving

Do not rebuild. A node that answers is a node whose tenants have their data, and
a restore replaces good data with older data.

```bash
cp-manage node status --name node-01 --status draining
cp-manage project drain-report --node node-01
cp-manage project move --ref abcd0001 --source-node node-01 --target-node node-02
```

Draining stops new placement; moving is per project and operator-initiated
(ADR-066). Each move is downtime for that one project and no others.

## When a restore produced the wrong point in time

The restored database lands beside the original and activation renames, so the
original is still there until you say otherwise:

```bash
cp-manage restore list --ref abcd0001
```

Restore again at a different `--target-time`. Nothing has been destroyed:
`restore.activate` renames the live database aside rather than dropping it, and
a move retains its source as `<db>_pre_move_<timestamp>`. Reclaiming that disk
is always a separate, deliberate act.

## Capacity, before it becomes an incident

`cp-manage maintenance run` includes a `capacity` pass that names any node within
80% of a ceiling `docs/CAPACITY.md` tracks — projects, warm projects, connections
— and any node whose free disk is near its floor or unreported.

It **reports and never repairs**. ADR-066 makes movement operator-initiated, and
an alert that relieved itself by moving projects would be the data-moving
control plane that decision exists to prevent. The output is a list for a person
to act on, with `cp-manage project move`.

Reaching a ceiling is too late to be told: at that point placement is already
refusing, which a customer experiences as a failed project creation. The
threshold is a parameter for the same reason `docs/CAPACITY.md` leaves per-node
targets open — they depend on hardware nobody has specified yet.

## Moving one tenant to another node

ADR-066 makes this an operator operation, not a maintenance repair. Capacity
reports, pool drift reports and drain reports may name work to do; they do not
move customer data. A move is named by project and target node:

```bash
cp-manage project move --ref abcd0001 --source-node n1 --target-node n2
```

The command preserves the control-plane identity: `project_ref`, hostname,
database name, API keys, subscription state, plan and storage namespace stay the
same. What changes is `projects.node_id`, after the target database has loaded
and the tenant-owned schemas have passed the same ownership verification that
gates restore activation.

### What it refuses

- **An implicit source.** `--source-node` is optional only for emergency use; in
  a normal drain it should be passed so the command refuses if the project moved
  after the operator made the plan.
- **A serving project.** API, Auth and Realtime worker states must already be
  `STOPPED`. The command sets project status to `MOVING`, which keeps the
  gateway from serving it while the freeze, dump, load and verification run.
- **Realtime-enabled projects.** Realtime carries node-local replication and
  metadata state today, so slice 7 does not claim to move it.
- **An unfit target.** The target node must be active, freshly healthy, in the
  pool the plan entitles, and within the same capacity checks placement uses.
- **A suspicious database name.** The recorded database must still be the name
  derived from `project_ref`; otherwise the command refuses to copy or drop
  anything.
- **A destination that is physically the source.** Source and target are
  compared by `pg_control_system()` system identifier, not by their `nodes`
  rows: two rows can address one cluster through a copy-pasted DSN or a node
  re-registered under a new name, and a move that loaded there would write the
  tenant's dump over the tenant's own live database.

Every one of these is checked **before the freeze**, because the project is
offline from the freeze onward and a move that fails on a missing role has spent
that downtime for nothing.

### The freeze, and what happens when a move fails

The freeze is `REVOKE CONNECT` plus terminating the tenant's backends (ADR-071),
so no session of any kind survives it -- not a `DELETE`, not a `TRUNCATE`, not a
`SECURITY DEFINER` side effect. The platform owner keeps `CONNECT`, which is what
lets `pg_dump` run at all. The project is refusing connections for the length of
the copy, and `cp-manage project move` prints how long it held.

A failed move **gives the tenant back**: every failure path is raised before the
control-plane row is repointed, so the destination has never served traffic and
the source is still the only live copy. The command restores the project's
status and its `CONNECT` grants, and exits non-zero.

The exception is a release that itself fails -- the node went away mid-move,
typically. Then the tenant is offline until an operator finishes it:

```bash
cp-manage node release-freeze --name n1 --database mldb_abcd0001
```

`move-history` flags that row as still frozen so it cannot be missed. The
command reads what to give back from the catalogue rather than from the move
record, so it is safe to run twice and safe on a tenant that was never frozen.

### The source is retained, never dropped

After the target verifies and the control-plane row points at it, the source
database is **renamed** to `<db>_pre_move_<timestamp>` and left on the old node.
It is not dropped, and neither are its roles -- they are what the retained
database's `auth` and `storage` schemas are owned by, so dropping them would
leave the retained copy unrestorable (ADR-059).

The retained database stays **frozen**. Its roles still exist on the old node,
so an unfrozen copy would answer a customer's old DSN with a writable stale
database while the live tenant serves from the new node.

That makes a move reversible in three steps rather than two:

```bash
psql -c 'ALTER DATABASE "mldb_abcd0001_pre_move_20260909T120000" RENAME TO "mldb_abcd0001"'
cp-manage node release-freeze --name n1 --database mldb_abcd0001
# then repoint the project back at n1
```

`cp-manage project move` prints the retained name and
`cp-manage project move-history --ref abcd0001` records it, so neither the name
nor the grants have to be reconstructed. Reclaiming the disk is a separate,
later, deliberate act -- the same choice `restore.activate` makes, for the same
reason.

### Draining a node

Drain is a runbook:

```bash
cp-manage node status --name n1 --status draining
cp-manage project drain-report --node n1
cp-manage project move --ref abcd0001 --source-node n1 --target-node n2
cp-manage project drain-report --node n1
```

`draining` already prevents new placements because only `active` nodes are
placeable. Slice 7 adds the missing second half: a report that names remaining
projects and a move command that removes them one by one. Nothing in
`cp-manage maintenance run` drains a node on its own.

## What each plan gets, and what "point in time" means here

ADR-068. Two numbers per plan, both resolved through `entitlements.py` and both
overridable per deployment in `plans.config_json` — `AGENTS.md` forbids
hard-coding production plan limits, and a recovery window is a plan limit in
exactly the way a storage quota is. `cp-manage node backup-policy` prints the
table a deployment is actually running.

| plan | retention | point-in-time |
|---|---|---|
| free | 7 days | none — restores to the state of a backup |
| starter | 14 days | within 7 days |
| production | 30 days | within 30 days |

**Retention is a promise, not a repository setting.** A pgBackRest repository
retains per stanza and a stanza is a whole node, so nothing anywhere makes one
tenant's bytes outlive another's on the same cluster — ADR-002 puts all 200 of
them in one backup set. What a plan buys is how far back the platform will
honour a request, which is why the same number is also what a *node* is held to:
`cp-manage node backup-check` fails a node whose repository keeps less than the
longest promise any offered plan makes.

**Free is backed up.** Its bytes are in the node backup whether or not anyone
sells them, and slice 0 measured the cost at about 2.5 MB of repository per
tenant at the 24 MB floor, after the measured 9.4:1 compression. Selling free a
retention of zero would have been a fiction. What free does not get is a second
of its choosing: PITR's cost is the archive and the per-request restore, not the
backup.

### What a point-in-time restore does not cover

**Objects are recovered as rows, not as bytes.** A restored tenant gets its
`storage.objects` table back at the target time; the bytes those rows name live
in a shared bucket (ADR-057) that is neither versioned nor restored. So after a
restore to a moment last Tuesday:

- an object **deleted since** Tuesday has its row back and its bytes gone — the
  project lists a file that cannot be downloaded;
- an object **uploaded since** Tuesday has its bytes in the bucket and no row —
  it is unreachable, and billed to nobody.

This is the honest limit of what Phase 11 delivers today and it must not be
described as anything narrower. Reconciliation — finding both directions and
reporting them — is slice 4. Until it exists, a restore of a project that uses
Storage needs the object consequences considered by hand.

**Nothing else about the project is rolled back.** API keys, the hostname, the
plan, the node it sits on and its workers' configuration all live in the control
plane, not in the tenant database, and a tenant restore does not touch them.
That is usually what an operator wants; it is stated because "restore the
project to Tuesday" and "restore the project's database to Tuesday" are
different sentences and only the second is true.

### Asking for more than a plan grants

`cp-manage restore run` refuses a target outside the plan's window and says
which of the two bounds refused it — the plan, or the repository. They have
opposite answers: a plan refusal is fixed by changing the plan, and a repository
refusal means the data is gone and no plan change reaches it.

`--beyond-entitlement` restores anyway and logs that it did. An incident is a
real reason to restore a project past what it was sold, and a control that
cannot be overridden during one is a control that gets deleted rather than used.

## Design requirement

Do not make "restore one project" require replacing the entire shared node in
production.

Satisfied by the procedure above. Both of slice 0's sharp edges are handled in
code rather than in prose: the scratch cluster is created with `archive_mode =
off`, so a promoted copy cannot push a new timeline into the repository it was
restored from; and ownership is verified after every restore, because a load
onto a cluster that has never seen the tenant silently reassigns `auth` and
`storage` to whoever ran it.


## The control plane's own recovery

ADR-070. Slices 1 to 4 make a *node* recoverable. This is the thing that
administers nodes, and until slice 5 nothing backed it up.

### Recovery needs two artefacts and neither is sufficient

**A backup of the control-plane database is not a backup of the control plane.**
ADR-023 keeps the KEK out of that database on purpose — "a dump of it must be
useless alone" — so restoring the dump gives a database full of ciphertext and
no way to read any of it. The KEK is a second artefact, stored somewhere else by
design, and a recovery needs both. Every backup this platform takes says so on
its own output rather than leaving it to this page.

Both failure modes are total and symmetric: the dump without the KEK is
unreadable, the KEK without the dump has nothing to read.

### Taking one

```bash
cp-manage control-plane backup --path /secure/cp-$(date +%F).sql
```

`pg_dump`, not a physical backup. The control plane is one small database on
ordinary PostgreSQL rather than a node, so a physical backup would mean owning
archiving and a pgBackRest stanza for a cluster whose whole content restores in
seconds from a file that can be copied anywhere. **What that gives up is
point-in-time recovery between dumps: the RPO is the dump interval.** Stated
here rather than left to be discovered during a recovery.

The command **exits non-zero on a dump with no `encryption_keys` rows**, because
such a dump cannot restore a working platform. It checks by parsing the dump's
`COPY` blocks rather than by trusting the exit code — `--exclude-table-data`
produces exactly that file, silently, with status 0.

The file is written mode 0600. It carries every node's admin DSN as ciphertext.

### Restoring one — and the flag that is not optional

```bash
createdb maludb_control_plane_restored
psql -v ON_ERROR_STOP=1 -f /secure/cp-2026-08-28.sql maludb_control_plane_restored
```

**`ON_ERROR_STOP=1` is load-bearing.** `nodes.admin_key_version` and
`project_credentials.key_version` are foreign keys into `encryption_keys`, so a
dump that lost that table cannot restore those constraints and psql reports an
ERROR for each. Without the flag psql prints them and carries on, leaving a
database that holds every secret, is missing the constraints that objected, and
has no keys — which used to be enough for the control plane to start and mint a
new key, making the loss permanent. ADR-070's guard now refuses that, but the
flag is what stops the restore where the problem actually is.

### Proving it worked

A restore is **not** verified by the control plane starting. Before ADR-070 a
control plane with no key material started perfectly well and could administer
nothing.

```bash
MALUDB_CONTROL_PLANE_DATABASE_URL=...restored cp-manage control-plane verify --reach-nodes
```

```
1 data encryption key(s) loaded
  nodes.admin_ciphertext: unwrapped 1 of 1
  project_credentials.ciphertext: unwrapped 2 of 2
  administered 1 node(s) with the recovered credential: bkdev

this control plane can read its own secrets.
and it administered a live node with a recovered credential.
```

Two different claims, kept apart deliberately: decrypting a credential proves
the key material survived, and connecting with it proves the credential is still
true. A node that is down is reported as a node that is down, not as a key
failure — conflating them would send an operator hunting for a KEK problem
during an unrelated outage.

### If the KEK is lost

```bash
cp-manage control-plane break-glass
```

Printed by a command as well as written in `docs/SECRETS.md`, because the moment
it is needed is an incident and nobody is reading documentation. The summary, in
ascending order of harm: node and object-store credentials are regenerable by an
operator; publishable API keys still work but can no longer be displayed;
SMTP and hook secrets are customer-supplied and must be re-entered; **per-project
JWT signing keys are not recoverable, so every end user of every project is
signed out**; and platform-user TOTP seeds are unrecoverable, which is the entry
that decides whether the operators can still get into their own dashboard.
