# Realtime Server Model

What it takes to run upstream `supabase/realtime` against MaluDB's tenancy, and
what it costs. Deliverable of Phase 06 slice 4.

Status: derived from experiments run 2026-08-16 against `supabase/realtime`
v2.110.0 on PostgreSQL 17.10. Every claim below was measured on a running
server, not read from upstream's source. Postgres Changes were delivered to the
official `@supabase/supabase-js` client, through the MaluDB gateway, from a real
tenant database.

Companion to `specs/realtime-replication-model.md`, which specifies what logical
replication costs and exposes underneath. This specifies the server on top, and
the finding that changes the phase: **upstream's replication slot names assume
one tenant per PostgreSQL cluster**, which database-per-tenant on a shared
cluster (ADR-002) does not provide.

## The finding that decides the topology

ADR-031 chose one shared Realtime per node, reasoning in part that upstream is
itself multi-tenant. That is true, and it is true in a way that does not help:
upstream is multi-tenant **across clusters**. On Supabase each project is its own
database server, so a fixed replication slot name is unambiguous. MaluDB puts
many tenants in one cluster, and PostgreSQL replication slot names are
**cluster-unique**.

Measured. Two tenants registered against one shared server. The second:

```
ReplicationSlotBeingUsed: replication slot "supabase_realtime_replication_slot_"
is active for PID 1222378
```

The slot name is `supabase_realtime_replication_slot_<SLOT_NAME_SUFFIX>`, and
`SLOT_NAME_SUFFIX` is a **server-level environment variable**. One Realtime
server can therefore serve exactly one tenant per PostgreSQL cluster for Postgres
Changes.

The failure mode is the dangerous one. The client's channel reports
`SUBSCRIBED` and then receives nothing at all, while the server retries in a
loop. From the application's side it is indistinguishable from a quiet table —
the same silence ADR-032 exists to prevent, arriving by a different route.

**Per-project instances resolve it, and were verified end to end.** Two servers,
each with its own `SLOT_NAME_SUFFIX`, each serving one tenant: both clients
received their own events and only their own.

```
[A rtp00042] EVENT INSERT {"id":11,"body":"per-project-mldb_rtp00042"}
[B rtp00043] EVENT INSERT {"id":3,"body":"per-project-mldb_rtp00043"}
```

## What a Realtime instance costs

The figure ADR-022 has been missing since slice 0, and the reason it was missing
— upstream ships a container image only — is recorded in ADR-033.

| | |
|---|---|
| Memory, cgroup accounting | **~146 MB** per instance, idle with one tenant registered |
| Memory, RSS of the process tree | ~235 MB (counts shared pages; the cgroup figure is the one to plan with) |
| Replication slots | **2 per tenant** (see below) |
| Ports | 2 per instance: HTTP, and gen_rpc |

For scale: ADR-022 measured an entire warm project — PostgREST plus Auth — at
31.8 MB. **A Realtime instance is roughly 4.5× that on its own.** Realtime is by
a wide margin the most expensive thing a project can turn on, which is an
argument for it being opt-in per project rather than provisioned by default, and
an argument for the free tier's `realtime_connections: 0`.

## Two slots per tenant, not one

`specs/realtime-replication-model.md` R1 established one slot per database that
uses Realtime. Measured against the real server, it is two:

| Slot | Plugin | For |
|---|---|---|
| `supabase_realtime_replication_slot_<suffix>` | **wal2json** | Postgres Changes |
| `supabase_realtime_messages_replication_slot_<suffix>` | pgoutput | Broadcast/messages |

Both are created and owned by the Realtime server, not by the platform. Two
consequences:

- **The node's Realtime ceiling halves again.** At PostgreSQL's default
  `max_replication_slots = 10`, with 2 held back for the platform, that is
  4 Realtime projects per node — not 8, and not the 24 warm projects ADR-022
  measured for everything else.
- **The platform must not create a slot of its own.** Phase 06 slice 2 created
  `mldb_<ref>_rt` with the `pgoutput` plugin; Realtime never uses it. It was
  observed doing active harm: with `max_replication_slots = 4`, two dead
  platform slots plus one tenant's two live ones filled the cluster, and the
  second tenant's server failed with `all replication slots are in use`.

`wal2json` is a hard requirement and was not previously known to be one. Without
it, subscriptions succeed and no events are ever delivered:

```
PoolingReplicationPreparationError: could not access file "wal2json":
No such file or directory
```

## Running the migrations without a superuser

Upstream applies 36 migrations **inside the tenant database**, creating the
`realtime` schema and its tables, functions and types. Supabase runs them as
`supabase_admin`, a superuser.

MaluDB cannot. A role holding `LOGIN` plus superuser would make any compromise of
the Realtime server a node compromise — `COPY FROM PROGRAM` is arbitrary code
execution as the PostgreSQL operating-system user — and it would erase the
containment ADR-031 was written to establish.

All 36 migrations were made to run as the ordinary `mldb_<ref>_replicator` role
with four narrowly-scoped grants. Each was found by running the migrations and
reading the failure:

| Requirement | Why | Discovered as |
|---|---|---|
| `realtime` schema, owned by the replicator | The migrations create objects in it | `schema "realtime" does not exist` |
| `GRANT SET ON PARAMETER log_min_messages` | A migration creates a function with `SET log_min_messages`, and that GUC is superuser-only. PostgreSQL 15 added per-parameter grants for exactly this case | `permission denied to set parameter "log_min_messages"` |
| `supabase_realtime_admin` pre-created by the platform, granted to the replicator `WITH INHERIT TRUE, ADMIN OPTION` | Upstream's migration creates the role, then moves object ownership to it and grants it onward | `permission denied to create role`, then `must be owner of table channels` |
| `GRANT CREATE ON DATABASE` | Realtime creates its own publications | `permission denied for database` |

Two details that cost a debugging round each, recorded so they do not cost
another:

- Upstream's role creation is guarded by an `IF EXISTS … ELSE CREATE ROLE`
  block, which is what makes pre-creating the role work: the migration becomes a
  no-op rather than an error.
- PostgreSQL 16 records the inherit option **per grant**, defaulting to the
  member's `rolinherit` *at the time of the grant*. Granting membership before
  setting `INHERIT` on the replicator produces a grant that does not inherit, and
  the ownership checks fail with no indication that inheritance is the problem.
  Grant explicitly: `WITH INHERIT TRUE`.

`supabase_realtime_admin` is `NOLOGIN NOINHERIT NOREPLICATION` and cluster-wide,
which makes it the same category as ADR-016's shared role names: a name carrying
no privilege of its own, whose every privilege attaches to per-database objects.

## Tenant registration

The control plane registers a project with its Realtime server over the admin
API, authenticated with a JWT signed by the server's `API_JWT_SECRET`.

```
POST /api/tenants
Authorization: Bearer <jwt signed with API_JWT_SECRET>
```

Two things that are easy to get wrong:

- **`external_id` is the project ref alone**, not the hostname. Realtime resolves
  a tenant from the first label of the `Host` header, so
  `rtp00042.maludb.local` registered as `external_id` is never found — the
  server logs `TenantNotFound: Tenant not found: rtp00042` while the client sees
  a transport failure.
- **The connection settings name the replicator role**, and its password is the
  Class B credential the platform stores. This is where slice 2's credential is
  finally used.

## The gateway path

Realtime does not serve at its own root, unlike PostgREST and GoTrue. The Phoenix
socket is mounted at `/socket`, so the gateway maps:

```
/realtime/v1/websocket  ->  /socket/websocket
```

Supabase's own edge makes the same substitution, which is why a client written
against Supabase works unchanged. Forwarding the stripped path (`/websocket`)
answers 404; the correct path with no token answers 403, which is how the two
were told apart.

## Required node preparation, added to the replication model's table

| Requirement | Why | Restart? |
|---|---|---|
| `wal2json` output plugin installed | Postgres Changes decode through it; without it, subscriptions succeed and deliver nothing | no |
| A Realtime metadata database, platform-owned | The server keeps its tenant registry in `_realtime`. It must **not** live in a tenant database, where it would be platform state inside a customer's data and reachable from their Data API | no |
| A container runtime (ADR-033) | Upstream ships an image only | no |
| CPU without AVX requirement, or a pinned version | See below | no |

## The CPU constraint

Upstream's **latest** image (v2.128.0) starts the BEAM and then dies with SIGILL:

```
traps: erts_sched_1 trap invalid opcode in
liblumis_nif-v0.7.0-nif-2.15-x86_64-unknown-linux-gnu.so
```

`lumis` is a precompiled Rust NIF, built for a CPU baseline the development and
production nodes do not meet — `QEMU Virtual CPU version 2.5+`, with SSE4.2 and
`popcnt` but no AVX, AVX2, BMI2 or FMA. The BEAM itself runs fine; only the NIF
traps. The dependency is recent: absent from `mix.exs` at v2.110.0, present at
v2.128.0.

**v2.110.0 is therefore the pinned version** (ADR-033), and the CPU requirement is
recorded here so that the pin has a reason attached rather than becoming
folklore.

## Reproducing

`scripts/realtime-test-cluster.sh` builds the PostgreSQL side.
`docs/REALTIME.md` has the operator commands. Every object created by these
experiments is prefixed `mldb_rtp` or named `maludb_realtime*`.
