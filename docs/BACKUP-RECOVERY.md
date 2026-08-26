# Backup and Recovery

## Status

Production backup architecture requires a dedicated design phase. **That phase
is Phase 11, planned 2026-08-26**; see
`plans/active/phase-11-production-resilience.md`. Nothing below has been built
yet, and the tooling is deliberately still unnamed — the choice depends on
measurements Phase 11 slice 0 takes, starting with whether a candidate tool
functions at all against ADR-031's `pg_hba.conf` reject of physical
replication.

This document is rewritten from a placeholder into a record of what was built
as part of that phase.

## Shared-cluster complication

Many tenant databases share one PostgreSQL/MaluDB cluster. Backup strategy must therefore distinguish:

- cluster disaster recovery;
- per-tenant restore;
- point-in-time recovery;
- logical portability/migration.

## Desired capabilities

Free:
- final policy TBD.

Paid:
- scheduled backups;
- clearly documented retention;
- per-project restore where practical;
- PITR on eligible plans later.

## Design requirement

Do not make "restore one project" require replacing the entire shared node in production.

The future design may combine:

- physical node/cluster backups and WAL archiving;
- logical per-database backups;
- recovery into temporary infrastructure followed by tenant extraction.

Exact tooling is TBD.
