# Requirements

## Functional requirements — MVP

### Accounts/projects

- A user can create a MaluDB project.
- The control plane assigns the project to a healthy existing MaluDB node.
- The project receives a stable project reference.
- The platform creates a tenant database and constrained PostgreSQL roles.
- The project receives a public API URL and project-scoped API keys.
- The project can be suspended, resumed, upgraded, and eventually deleted through explicit state transitions.

### Tenancy

- One customer project maps to one PostgreSQL/MaluDB database.
- Multiple customer databases share one already-running MaluDB/PostgreSQL cluster.
- PostgreSQL roles are globally unique within a cluster.
- The platform retains database ownership.
- Tenants cannot access another tenant database.

### API

- Project requests use a URL shaped like `https://<project-ref>.maludb.com`.
- Gateway validation must confirm that an API key belongs to the requested project.
- The first data API target is Supabase/PostgREST-compatible `/rest/v1`.
- The official Supabase JavaScript client should be used for compatibility testing.

### Free tier

- Free projects are API-only.
- No direct PostgreSQL connection string is exposed to free projects.
- Free projects can use sleeping/on-demand API worker processes.
- Resource limits are plan-configured and enforced at multiple layers.

### Paid tier

- Upgrading normally keeps the same tenant database.
- Paid plans may expose direct PostgreSQL connectivity.
- Paid plans may enable higher limits, backups, PITR, Realtime, and other capabilities.

## Non-functional requirements

- Strong tenant isolation.
- Idempotent provisioning.
- Auditable project lifecycle transitions.
- No plaintext secret API keys in logs.
- Health-aware node scheduling.
- Configuration-driven plan limits.
- Black-box compatibility testing.
- Ability to add additional MaluDB nodes without changing the tenant abstraction.
- Design for later separation into free/production node pools.

## Deferred requirements

These are important but not all required for the first proof:

- automated Supabase migration;
- Realtime;
- Storage;
- billing;
- production dashboard;
- PITR;
- MaluDB-native client/API;
- automatic cross-node tenant migration;
- native MaluDB resource governor.
