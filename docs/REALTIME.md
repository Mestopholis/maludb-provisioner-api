# Realtime

## Status

Phase 06 in progress. The validation this document required before
implementation was run on 2026-08-16 — results in
`specs/realtime-replication-model.md`, decisions ratified as ADR-031 and
ADR-032.

Slice 1 (node preparation and slot safety) and slice 2 (per-project enablement)
are complete. A project can hold a replication slot and a `supabase_realtime`
publication, and the platform accounts for both. **Nothing serves the events to
a client yet** — the `/realtime/v1` gateway surface and the Realtime server
itself are slice 3.

## Preparing a node

A node cannot host Realtime until it has been prepared and checked. Three of the
five settings need a cluster restart, which is an outage for every tenant
already on it, so this belongs in node build — a node prepared afterwards costs
downtime.

```bash
cp-manage node realtime-check --name n1
```

It reads the node, records the answer on the node row, and exits non-zero if the
node is not ready, so a build script fails rather than printing a reason into a
log nobody reads. Placement reads the recorded answer: an unchecked node refuses
Realtime projects and keeps accepting every other kind, because the slot ceiling
should not strand capacity ADR-022 measured as usable.

The settings themselves are in `specs/realtime-replication-model.md` under
"Required node preparation". `scripts/realtime-test-cluster.sh` builds a
throwaway cluster carrying all of them, which is what the test suite runs
against.

## Enabling a project

```bash
cp-manage project realtime --ref abcd1234 --enable
cp-manage project realtime --ref abcd1234 --disable
```

Entitlement first, then node capacity, then the node itself — ordered so the two
refusals a customer can actually hit ("your plan does not include this", "this
node is full") never require touching the tenant database to discover.
`realtime_connections` is `0` on free, which is what makes free projects refuse.

Enabling creates the project's `mldb_<ref>_replicator` role — the one tenant role
holding `REPLICATION` — stores its credential, and takes one of the node's
replication slots. Disabling drops the slot, drops the role, and revokes the
credential. Turning a capability off reduces what exists, not just what is
reachable.

A plan downgrade takes Realtime away on the next provisioning run, the same way
direct SQL works. An *upgrade* does not turn it on: enabling creates a role
holding `REPLICATION`, which should be somebody's decision rather than a side
effect of a billing change.

**Custom plans must state `realtime_connections` explicitly.** Entitlement
resolution falls back to the free tier for any plan code it does not recognise,
and free is `0` — so a bespoke plan whose `config_json` omits the limit resolves
to "not entitled", and the next provisioning run will remove Realtime from
projects on it. That is the safe direction for a limit and the wrong surprise to
discover from a customer. The maintenance pass reports projects in that state
before a provisioning run acts on it:

```
rte00003: plan no longer includes Realtime, but the slot is still held;
          the next provisioning run removes it
```

### Choosing what replicates

Every tenant gets an empty `supabase_realtime` publication from bootstrap 009,
owned by the tenant admin role. A paid customer adds tables to it exactly as
they would on Supabase:

```sql
ALTER PUBLICATION supabase_realtime ADD TABLE your_table;
```

A table that is not in the publication produces no events. That is upstream's
behaviour, and it is not an error the client can see.

Note what the publication is *not*: it is not the authorisation boundary. The
replicator reads every table in the database past grants and RLS regardless, so
removing a table from the publication is choosing not to broadcast it, not
securing it. RLS for Postgres Changes is enforced in the Realtime server, and
slice 4 has to prove it is.

## Watching the slots

```bash
cp-manage realtime slots [--node n1]     # every slot a node holds
cp-manage capacity report                # slots as a ceiling, beside warm and connections
cp-manage maintenance run                # detects invalidation, writes the audit event
cp-manage project realtime-recover --ref abcd1234   # re-create an invalidated slot
```

An invalidated slot means that project has stopped receiving changes and nothing
in its connection says so. Re-creating the slot resumes from the present and
**does not replay the gap**; every report says so, because the alternative is a
customer who assumes a backfill that never happened.

Recovery is deliberately a command a person runs, not something the maintenance
pass does. ADR-032 makes invalidation a project-visible incident, and a platform
that repaired it quietly would turn a reportable failure back into a silent one
— which is the outcome the whole design exists to avoid.

## Compatibility target

Initial target: Supabase-compatible Postgres Changes.

Later:

- Broadcast;
- Presence.

## Resource concerns

Realtime has persistent connections and different economics from REST.

Controls must include:

- enabled/disabled state by project/plan;
- max simultaneous connections;
- message/change rates;
- payload limits;
- idle behavior;
- per-node/service capacity.

Do not automatically allocate unlimited Realtime resources to every free project.

## Architecture question — answered 2026-08-16

Before implementation, validate the upstream Realtime multi-tenancy model against the MaluDB database-per-project architecture. Prefer using upstream protocol/software before inventing a new client protocol.

The upstream model fits, with one precondition the architecture did not previously have. Logical decoding requires the PostgreSQL `REPLICATION` role attribute, and a role holding it can take a physical base backup of **every database on the cluster** — the ADR-014 `CONNECT` lockdown does not reach that path. Containing it is node configuration (a `pg_hba.conf` reject of physical replication), not tenant provisioning, and it is required whether Realtime runs shared or per-project. See `specs/realtime-replication-model.md` and ADR-031.

Two further properties belong here rather than in the implementation. A replication consumer reads every table in its database past grants and row-level security, so **RLS for Postgres Changes is enforced by the Realtime server, not by PostgreSQL** — the compatibility suite must prove it is. And a stalled consumer pins WAL until the disk fills, taking down every tenant on the node, so bounded retention is mandatory and an invalidated slot is a reportable incident (ADR-032).

Not yet answered: what a Realtime process costs. Upstream ships as a container image only and Docker is absent from the development host, so ADR-022's density numbers have no Realtime term yet.
