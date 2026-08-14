# Open Questions

These items do not block creation of the planning repository, but each should be explicitly decided before the associated implementation is productionized.

## Control-plane implementation

- Programming language/framework?
- Control-plane database?
- Background job mechanism?
- Redis/distributed cache or gateway-local cache first?
- API gateway implementation choice?

## Domain/DNS

- Final public domain?
- Wildcard TLS/DNS strategy?
- Project ref format/length?
- Custom domains later?

## API workers

- systemd template units vs another supervisor?
- separate API worker hosts vs colocated on DB nodes?
- inactivity duration for free workers?
- cold-start target?

## API keys/JWT

- exact MaluDB key format?
- asymmetric signing-key hierarchy?
- per-project key pairs vs managed key service?
- legacy Supabase key compatibility requirements?

## Database connectivity

- chosen pooler and when introduced?
- direct DB endpoint architecture for paid users?
- password vs short-lived credential model later?

## Resource limits

Exact initial values remain TBD:

- API requests/time window;
- concurrent API requests;
- active DB queries;
- PostgREST pool size;
- statement timeout;
- work_mem;
- temp_file_limit;
- parallel query limit;
- storage quota;
- Realtime limits.

## Node scheduling

- exact capacity score formula?
- reserve/headroom policy?
- separate node pools from launch or later?
- maximum tenant count safety cap?

## Backups

- physical backup technology?
- WAL archive target?
- logical per-DB backup schedule?
- restore workflow?
- paid retention/PITR tiers?

## Storage

- object-storage provider?
- tenancy/bucket design?
- egress model?

## Billing

- payment provider?
- prices?
- included usage?
- overage vs hard limits?

## MaluDB functionality

- exact memory features to expose first?
- SQL surface?
- API/SDK surface?
- compatibility interaction?

## Migration

- migration CLI vs dashboard first?
- required Supabase features for initial migration launch?
- downtime expectations?
