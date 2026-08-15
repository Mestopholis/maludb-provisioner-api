# Resource Governance

## Objective

Protect shared MaluDB nodes from noisy-neighbor workloads while keeping the product simple enough to operate.

No single mechanism is considered sufficient.

## Enforcement layers

### 1. Gateway

Potential controls:

- requests per time window;
- concurrent API requests;
- project status;
- per-plan payload limits;
- Realtime connection/message limits later;
- write blocking when storage quota is exhausted.

### 2. API/service layer

Potential controls:

- small PostgREST pool sizes;
- bounded worker counts;
- sleeping free project workers;
- request queue length/timeouts;
- limited concurrent database work.

### 3. PostgreSQL/MaluDB role/database settings

Configure per role/database where appropriate:

- connection limits;
- `statement_timeout`;
- `lock_timeout`;
- `idle_in_transaction_session_timeout`;
- `work_mem`;
- `temp_file_limit`;
- `max_parallel_workers_per_gather`;
- other safe plan-specific settings.

All values must come from plan configuration.

**These are defaults, not enforcement.** Verified 2026-08-15; see ADR-017.

Two constraints govern this layer:

1. Role settings apply **at login, to the login role**. Applying them to `authenticated` or `anon` does nothing, because those roles are entered through `SET ROLE` — it fails open with no error. Target `mldb_<ref>_authenticator`, scoped `IN DATABASE`, where the setting applies and survives the later `SET ROLE`.
2. Five of the seven settings above are `context = user` in `pg_settings` — `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `work_mem`, `max_parallel_workers_per_gather` — so any session holding direct SQL can raise or disable them. `SET statement_timeout = 0` succeeds.

Only these bind a hostile client:

| Control | Why it holds |
|---|---|
| `CONNECTION LIMIT` on the role | role attribute, not a session-settable GUC |
| `temp_file_limit` | `context = superuser` |
| `max_connections` | `context = postmaster` |

For the free tier this is not a practical gap: there is no direct SQL and PostgREST is platform-configured (ADR-005), so the tenant never holds a session that can override anything.

For **paid direct SQL it is a real gap**. Per-statement ceilings cannot be enforced at this layer. Paid enforcement must come from connection limits, `temp_file_limit`, pooler-level controls, node capacity management, and the monitoring/escalation path below.

See `specs/tenant-role-model.md` for the exact statements.

### 4. Native MaluDB resource governance

Longer-term research/implementation should consider first-class per-tenant controls for:

- active query concurrency;
- queries/transactions per second;
- CPU time/fairness;
- I/O pressure;
- memory pressure;
- WAL-heavy workloads;
- tenant throttling.

Do not make a young third-party extension a hard production dependency without evaluation.

### 5. Node scheduler

Stop assigning new projects when a node is under pressure.

Measured per-project costs and the planning formula are in `docs/CAPACITY.md`. The scheduler must track **warm** project count separately from total project count: a slept project costs zero RAM and zero connections, while a warm one holds ~32 MB of workers and 4 backends. Connections are the binding constraint, not memory — at default `max_connections` a cluster saturates at roughly 24 warm projects (ADR-022).

Capacity scoring should consider more than database count:

- CPU;
- memory;
- disk usage;
- disk latency/IOPS where available;
- active connections;
- active queries;
- tenant/database count;
- recent resource saturation;
- health/maintenance state.

## Free-tier principles

Accepted:

- API-only external access.
- More aggressive rate/concurrency limits than paid plans.
- API worker can sleep when inactive.
- Direct SQL cannot bypass gateway controls because it is not exposed.

Exact numerical limits are intentionally configurable and not yet product decisions.

## Storage quotas

Measure each database regularly.

Initial enforcement strategy may be:

1. usage warnings before quota;
2. gateway rejection of API writes at/over hard quota;
3. retain read access where safe;
4. require upgrade or cleanup;
5. later add native MaluDB/database-level hard quota enforcement.

Because direct DB access exists on paid tiers, paid storage enforcement cannot rely only on the API gateway.

## Violation handling

Preferred progression:

```text
normal -> throttled -> temporarily restricted -> suspended -> manual review
```

Automated resource enforcement must not automatically destroy customer data.
