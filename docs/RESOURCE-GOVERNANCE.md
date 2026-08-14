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
