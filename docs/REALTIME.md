# Realtime

## Status

Phase 06 in progress. The validation this document required before
implementation was run on 2026-08-16 — results in
`specs/realtime-replication-model.md`, decisions ratified as ADR-031 and
ADR-032.

Slice 1 (node preparation and slot safety) is complete: the preconditions are
checked and recorded per node, the slot ceiling is enforced in placement, and an
invalidated slot produces an audit event rather than silence. **No project can
enable Realtime yet** — per-project enablement and the `supabase_realtime`
publication are slice 2, and the `/realtime/v1` surface is slice 3.

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

## Watching the slots

```bash
cp-manage realtime slots [--node n1]     # every slot a node holds
cp-manage capacity report                # slots as a ceiling, beside warm and connections
cp-manage maintenance run                # detects invalidation, writes the audit event
```

An invalidated slot means that project has stopped receiving changes and nothing
in its connection says so. Re-creating the slot resumes from the present and
**does not replay the gap**; every report says so, because the alternative is a
customer who assumes a backfill that never happened.

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
