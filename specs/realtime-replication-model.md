# Realtime Replication Model

What logical replication costs and exposes on a shared MaluDB node, and what a
Realtime consumer role may hold.

Status: derived from experiments run 2026-08-16 against PostgreSQL 17.10 on an
isolated cluster. Every claim below was measured; the IDs map to the negative
tests at the end. Deliverable of Phase 06 slice 0.

Companion to `specs/tenant-role-model.md`, which specifies the roles a tenant
already has. This specifies the one it does not yet have, and why that role is
more dangerous than any of them.

## What was not tested, and why

Upstream `supabase/realtime` is distributed as a container image only — latest
v2.128.0, no release binaries — and **Docker is not installed on the development
host**. The Realtime server itself was therefore not exercised.

What was exercised is the layer underneath it: the PostgreSQL logical
replication protocol, through `pg_recvlogical` and the slot SQL functions. That
is the same interface Realtime consumes, and it is where every question the plan
asked actually lives — slot arithmetic, database binding, and what credentials a
consumer needs. Those are answered.

One question is **not** answered and must not be treated as though it were:
*what a Realtime process costs*, which the plan wanted in order to extend
ADR-022's density numbers rather than guess at them. That needs the real server.
Until it is measured, no node capacity figure should assume a value for it.

## Method

`wal_level` cannot be changed without restarting the cluster, and restarting the
development cluster would have dropped every connection on it. So the
experiments ran on a throwaway cluster instead:

```
pg_createcluster 17 spike --port 5433 -- --auth-local=peer
wal_level = logical, max_replication_slots = 4, max_wal_senders = 4
```

`max_replication_slots` was set deliberately low so the ceiling could be reached
in four statements rather than ten. Two tenant-shaped databases, `mldb_rt0001`
and `mldb_rt0002`, each with the ADR-014 `CONNECT` lockdown applied. The cluster
was dropped afterwards. **The live cluster was not modified** and remains
`wal_level = replica`.

## Findings

### R1 — A logical slot is bound to exactly one database

```
rt0001_slot -> database=mldb_rt0001 type=logical
rt0002_slot -> database=mldb_rt0002 type=logical
```

Using one from the other database is refused:

```
ERROR:  replication slot "rt0001_slot" was not created in this database
```

Confirms what the plan assumed. **One slot per tenant database that uses
Realtime.** There is no multiplexing available.

### R2 — At the ceiling, slot creation fails loudly

```
ERROR:  all replication slots are in use
HINT:  Free one or increase "max_replication_slots".
```

This is the good case. The failure lands at *enablement* time, on the operation
that asked for it, not at runtime on a project that thought it had Realtime. It
is reportable and retryable, which is what slice 1's capacity accounting needs
in order to refuse the enablement rather than half-perform it.

### R3 — An unconsumed slot pins WAL without limit

With `max_slot_wal_keep_size = -1`, the production default:

| | `pg_wal` | WAL retained by the slot |
|---|---|---|
| One idle slot, no traffic | 17 MB | 304 bytes |
| After 200,000 rows of ~1 KB | **225 MB** | **206 MB** |

A `CHECKPOINT` did not release any of it. Nothing about this self-limits: the
consumer does not have to be malicious, only absent — a crashed worker, a
partitioned network, a project the platform put to sleep. The disk fills, and
PostgreSQL on a full disk stops accepting writes **for every tenant on the
node**.

This is the sharpest cross-tenant availability failure in the project so far,
and one project's *inactivity* is enough to cause it.

### R4 — Bounding retention converts that into a lost slot

With `max_slot_wal_keep_size = 64MB` and more traffic than the bound:

```
rt0001_slot wal_status=lost
ERROR:  can no longer get changes from replication slot "rt0001_slot"
DETAIL:  This slot has been invalidated because it exceeded the maximum reserved size.
```

`pg_wal` then **plateaued at 433 MB** — a further 300,000 rows did not grow it.

Two things follow, and the second is easy to miss:

1. The bound works. The disk stops growing, and the failure is contained to the
   one project whose consumer stalled.
2. **The bound caps growth; it does not reclaim.** `pg_wal` did not shrink back.
   PostgreSQL recycles segments for reuse rather than deleting them, so the
   high-water mark persists. Node capacity must budget for the bound as space
   that will be occupied, not space that might be.

The cost of the bound is that the project silently stops receiving changes. The
consumer gets a clear error; the *customer* gets nothing at all, which is why
this needs detection rather than only configuration.

### R5 — Logical decoding requires the `REPLICATION` attribute

A role with `LOGIN` and `CONNECT` but no `REPLICATION` is refused on both paths.
Via the SQL functions:

```
ERROR:  permission denied to use replication slots
DETAIL:  Only roles with the REPLICATION attribute may use replication slots.
```

And on the replication protocol:

```
FATAL:  permission denied to start WAL sender
DETAIL:  Only roles with the REPLICATION attribute may start a WAL sender process.
```

There is no lesser grant that buys logical decoding. A Realtime consumer role
**must** hold `REPLICATION`, which makes the next finding unavoidable rather
than a configuration mistake to be avoided.

### R6 — `REPLICATION` alone reads every database on the cluster

This is the finding that shapes the phase.

`rt_consumer` was created `LOGIN REPLICATION`, granted `CONNECT` on
`mldb_rt0001` **only**, and explicitly denied `mldb_rt0002` — the ADR-014
lockdown, correctly applied. Its logical replication connection to the database
it does not hold `CONNECT` on was properly refused:

```
FATAL:  permission denied for database "mldb_rt0002"
DETAIL:  User does not have CONNECT privilege.
```

So logical replication *is* bound by `CONNECT`. That much is good news, and
shared Realtime depends on it.

But physical replication names no database, so nothing scopes it. As the same
non-superuser role:

```
$ pg_basebackup -U rt_consumer -D ./bb
  -> 484 MB, containing base/ for:
     template0, template1, postgres, mldb_rt0001, mldb_rt0002
```

**A role with `CONNECT` on one tenant database took a byte-level copy of every
tenant database on the cluster.** The `CONNECT` lockdown that Phase 02 verified,
and that `specs/tenant-role-model.md` calls mandatory, does not constrain this at
all — it operates on a layer the physical replication protocol never reaches.

The exposure is not specific to a shared Realtime. It is a property of the
`REPLICATION` attribute, so *any* topology that grants it — one consumer per
project included — inherits it. Per-project isolation does not fix R6.

### R7 — `pg_hba.conf` can reject physical replication while permitting logical

`pg_hba.conf`'s `replication` keyword in the database column matches **only**
physical replication connections. Logical replication names a real database and
matches ordinary database rules. So the two can be separated:

```
host    replication     all     127.0.0.1/32    reject
```

With that line in place, as the same role:

| Operation | Result |
|---|---|
| `pg_basebackup` | `FATAL: pg_hba.conf rejects replication connection for host "127.0.0.1", user "rt_consumer"` |
| Create a logical slot on its own database | succeeded |
| Decode changes from that slot | succeeded |
| Logical connection to another tenant | still refused by `CONNECT` |

This is the containment. It reduces `REPLICATION` from *read the entire cluster*
to *decode the databases you could already connect to*, which is the property
the tenancy model needs and assumed it already had.

It is **node configuration**, not tenant provisioning. A node missing this line
hands a cluster-wide reader to the first project that enables Realtime.

### R8 — The consumer reads every table in its database, past grants and RLS

`secret_t` had all privileges revoked from `PUBLIC` and row-level security
enabled. `rt_consumer` holds no grant on it:

```sql
SELECT note FROM secret_t;
-- ERROR:  permission denied for table secret_t
```

The same role, through its slot, reads the row contents out of the WAL:

```
table public.secret_t: INSERT: id[integer]:2 note[text]:'rls-canary-visible-in-wal'
```

Logical decoding reads the write-ahead log, which is written before any
privilege or policy is consulted. So the consumer role is, within its own
database, an unrestricted reader — equivalent to `BYPASSRLS` plus `SELECT` on
everything, present and future.

Two consequences:

- **All RLS enforcement for Postgres Changes happens in the Realtime server**,
  not in PostgreSQL. Whatever the platform ships must be trusted to do it, and
  the compatibility suite in slice 4 has to test that it does — a policy that
  filters a REST read but not a Realtime subscription is a data leak wearing a
  feature's clothes.
- The consumer credential is a **Class B secret of the highest value in the
  system**: it reads one tenant completely and ignores that tenant's own
  policies.

### R9 — A `REPLICATION` role can still pin WAL despite R7

The `pg_hba` reject blocks physical replication *connections*. It does not block
creating a physical slot through SQL over an ordinary connection:

```sql
SELECT pg_create_physical_replication_slot('dos_slot2', true);
-- slot created, type=physical, wal_status=reserved, restart_lsn=0/3049FDC0
```

A reserved physical slot with no consumer pins WAL exactly as R3 describes, and
PostgreSQL has no per-role slot quota — only the cluster-wide
`max_replication_slots`.

So `max_slot_wal_keep_size` is not merely advisable, it is **the only backstop**
against a role that holds `REPLICATION` for legitimate reasons pinning the
node's WAL, deliberately or by accident. R7 and R4 are both required; neither
substitutes for the other.

## What this means for the two open decisions

### Shared versus per-project Realtime

The plan recommended shared, with the concentration of credentials as the
objection the spike was meant to test. The spike changes the shape of that
objection:

- The cluster-wide exposure that made concentration frightening (R6) is **not a
  property of sharing**. It is a property of `REPLICATION`, and per-project
  topology inherits it identically. It is closed at the node, by R7, or it is
  not closed at all.
- With R7 applied, the difference between the two is blast radius on credential
  compromise: a shared server holds N tenant credentials, a per-project server
  holds one.
- Per-project costs a BEAM VM per project, which ADR-022 does not cover and R-nothing
  measured, because Docker was absent.

**Recommendation: shared, conditional on R7 being a node prerequisite.** It is
what upstream builds and tests, `AGENTS.md` prefers upstream behaviour, and the
concentration that remains is the same category the control plane already
occupies — it, too, holds credentials for every tenant it has provisioned, and is
held to ADR-023 for exactly that reason. The Realtime credential store must meet
the same bar.

The honest caveat: this recommendation rests on a cost comparison whose
per-project side is unmeasured. If per-project Realtime turns out to be cheap,
the blast-radius argument favours it and this should be revisited.

### What an invalidated slot should do

R4 makes invalidation a certainty rather than an edge case — it is the designed
outcome of a consumer that stops, and Phase 05 sleeps projects on purpose.

The customer-visible symptom is an application that stops receiving events with
no error anywhere they can see. **Treat invalidation as a project-visible
incident**: detected by the Phase 05 maintenance pass, recorded as an audit
event, and surfaced. Recovery is re-creating the slot, which resumes from the
present and **does not replay what was missed** — so the report has to say that,
or a customer will assume a gap was backfilled when it was not.

## Required node preparation

Every item is node-level (ADR-003), and every one of them must be true before a
node accepts its first Realtime project.

| Setting | Value | Why | Restart? |
|---|---|---|---|
| `wal_level` | `logical` | R1; nothing works below it | **yes** |
| `max_slot_wal_keep_size` | bounded, recorded per node | R3, R9 — the only backstop | no, reload |
| `max_replication_slots` | ≥ Realtime project ceiling | R2 | **yes** |
| `max_wal_senders` | ≥ concurrent consumers | R2 | **yes** |
| `pg_hba.conf` | `host replication all <cidr> reject` | R6, R7 — else cluster-wide read | no, reload |

Three of these need a restart, which is an outage for every tenant on the node.
They belong in node build, before the node takes its first project. A node
prepared afterwards costs downtime.

## The consumer role

| | |
|---|---|
| Name | `mldb_<ref>_replicator` |
| Attributes | `LOGIN`, `REPLICATION`, password, `CONNECTION LIMIT` |
| Granted | `CONNECT` on its own database only |
| Never granted | `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS`, membership in any shared role |
| Effective read | **every table in its own database, past grants and RLS** (R8) |
| Contained by | `CONNECT` (R6 logical path) plus the `pg_hba` reject (R6 physical path) |

It must not be `mldb_<ref>_admin`, and it must not be the authenticator. Both are
customer-reachable on paid plans, and `REPLICATION` on either would hand a
customer R6 against every tenant on the node — the exact escalation
`specs/tenant-role-model.md` lists as prohibited.

## Required negative tests

Blocking for Phase 06 slice 1, in the shape `specs/tenant-role-model.md` uses.

| ID | Test | Expected |
|---|---|---|
| R1 | Use tenant A's slot from tenant B's database | `ERROR: replication slot ... was not created in this database` |
| R2 | Create one slot past `max_replication_slots` | `ERROR: all replication slots are in use`, enablement refused |
| R4 | Stall a consumer past `max_slot_wal_keep_size` | slot `wal_status=lost`, `pg_wal` plateaus, incident raised |
| R5 | Non-`REPLICATION` role creates a logical slot | `permission denied to use replication slots` |
| R6a | Replicator connects logically to another tenant | `FATAL: permission denied for database` |
| R6b | **Replicator runs `pg_basebackup`** | `FATAL: pg_hba.conf rejects replication connection` |
| R8 | Replicator holds no direct `SELECT` on tenant tables | `permission denied for table` |
| — | `mldb_<ref>_admin` and `_authenticator` hold `REPLICATION` | false, always |

R6b is the one that matters most and the one most likely to be dropped as
awkward to write, because it needs a node whose `pg_hba.conf` is under test. It
is the difference between the lockdown holding and only appearing to.

## Reproducing

Recreate the throwaway cluster from the Method section. Every object is prefixed
`mldb_rt` or `rt_` and the cluster is dropped at the end. Do not run any of this
against a node carrying customer data — R6 produces a readable copy of every
database on the cluster it runs against.
