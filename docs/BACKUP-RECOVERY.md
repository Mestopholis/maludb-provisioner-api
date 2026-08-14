# Backup and Recovery

## Status

Production backup architecture requires a dedicated design phase.

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
