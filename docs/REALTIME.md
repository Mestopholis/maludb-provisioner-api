# Realtime

## Status

Phase 06 in progress. The validation this document required before
implementation was run on 2026-08-16 — results in
`specs/realtime-replication-model.md`, decisions proposed as ADR-031 and
ADR-032.

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
