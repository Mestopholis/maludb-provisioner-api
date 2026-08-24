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

**The control plane's own memory is a shared resource, and ADR-039's SQL surface
is the first thing that lets a tenant spend it.** Every other layer on this list
governs what a *node* gives a tenant; a response the control plane assembles is
paid for in a process every tenant shares. Slice 1's limits bounded rows,
statements and seconds and none of them bounded bytes: a hundred rows of a
megabyte each is inside the free tier's row cap, reports itself untruncated, and
costs ~200 MB — measured 2026-08-19, closed by ADR-046's `sql_console_max_bytes`.

Two things generalise past that fix, and both apply to any future route that
reads tenant-shaped data:

- **A row count is not a size.** It is the right cap only where the rows have a
  shape the platform chose. Wherever a value is customer-authored text — a
  result column, a function body, a comment — the cap has to be in bytes.
- **A limit bounds the process only where the process is the one allocating.**
  libpq buffers a whole result set before the platform can refuse a byte of it,
  so `sql_console_max_bytes` bounds what is *held* while a response is written
  and not the transient spike on the way in. That residual is
  `docs/OPEN-QUESTIONS.md`'s, and until it is closed the API needs an
  operational memory limit — which this repository does not yet assert.

**A plan's settings only bind the roles they are written to, and a plan change
only reaches what re-reads it.** Phase 09 slice 0 measured both halves.

Entitlements resolved per request — the gateway's rate and concurrency limits,
the storage quota, the console's ceilings, `max_projects`, Realtime capacity —
change the moment a project's plan row changes. Entitlements written into the
node during provisioning — role GUCs, the admin role's `LOGIN`, `CONNECTION
LIMIT` — did not change at all until something re-applied them, and nothing
did. `cp-manage project direct-sql` exists because of that and says so in its
own help text.

`cp-manage plans drift` now reports which projects' nodes disagree with their
plans and which way each difference points, and `cp-manage project plan-apply`
corrects one. The maintenance pass reports and does not correct: an operator
revoking a paid project's access during an incident looks identical to an
upgrade that never landed, and a reconciler on a timer would undo the first
within the hour.

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

- No connection credentials and no reachable database port; external access is
  the API plus the platform-mediated SQL surface (ADR-039).
- More aggressive rate/concurrency limits than paid plans.
- API worker can sleep when inactive. The SQL surface talks to PostgreSQL rather
  than to PostgREST, so using it must not wake a slept project.
- Mediated SQL cannot bypass the platform's controls because the platform holds
  the connection and cancels out of band — **not** because no path exists. The
  distinction is load-bearing: ADR-017 established that `statement_timeout` and
  four of its neighbours are `context = user` and can be raised by the session,
  so an exposed connection would have no per-statement ceiling while a mediated
  one does. Do not relax this on the old reasoning that free "is not exposed".

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

**Nor can free's, once ADR-039's SQL surface exists.** Free was fully enforced
only because it had no path to the admin role; the mediated SQL surface creates
one. **ADR-040 therefore extends the revoke to `mldb_<ref>_admin`**, so the API,
the console and paid direct SQL are covered by one mechanism and no path carries
a special case.

Read that as a default rather than a control. A role that owns a table holds
`GRANT OPTION` on it implicitly, so a customer can grant `INSERT` back to
themselves and write on the next statement — probed, and asserted in
`tests/test_sql_console.py` so the admission stays true. It stops accidental
writes and honest clients, it makes the escape auditable rather than automatic,
and the maintenance pass re-applies it on every pass — which it did not do until
ADR-041 found the revoke sitting inside the state-transition branch, where a
single re-grant held until the project dropped below quota. It does not stop a
determined customer.
That is ADR-017's finding one layer up, and the reason this list has five other
layers on it.

`DELETE` and `TRUNCATE` are untouched, deliberately: a project that cannot
shrink cannot recover, and on the free tier the console is the only way in.

`service_role` **is no longer an exception** (ADR-041). Phase 05 could leave it
out of the revoke because the only route to it was the gateway, where writes are
already refused at quota. Slice 3's impersonation is a second route the gateway
never sees — and the role named in a request cannot be the control, because
`SET ROLE` is authorized against the session user, so a request asking for `anon`
reaches `service_role` in one line of its own SQL. The revoke covers all three
now. The exemption's purpose survives anyway: it removes `INSERT` and `UPDATE`
only, and a cleanup job needs `DELETE`.

## Object storage and egress (ADR-056)

Two more ceilings, on every tier including free, and the enforcement shape is
**not** the one above. Everything in the section before this revokes something
inside the tenant, because a database grows through connections the platform
does not mediate. Object bytes do not: every one of them arrives and leaves
through the Storage API, so both ceilings are enforced where the request is and
the tenant database is untouched.

That is why the state is called `exceeded` rather than `restricted`. A reader
who saw the same word on both would reasonably expect a revoke behind both, and
there is none here — the state means "the next upload should be refused", not
"uploads are now impossible".

| | `object_storage_bytes` | `egress_bytes_per_month` |
|---|---|---|
| what it bounds | bytes held | bytes served |
| how it is counted | **measured** by a maintenance pass, from the tenant's own `storage.objects` metadata | **counted as it passes**, at the gateway |
| where the figure lives | `projects.object_bytes` | `project_egress`, one row per project per UTC month |
| enforced by | the Storage API refusing an upload | the Storage API refusing a download |

The asymmetry is deliberate. Polling is right for a quantity that is a property
of the world rather than of a request: self-correcting, and a missed pass costs
accuracy rather than truth. Egress has nowhere to be read back from afterwards,
so it has to be counted as it happens — which puts it on the path ADR-026
published a throughput number for, so the caller accumulates in process and
flushes a total rather than writing per request.

**Neither is a meter.** ADR-050 makes both hard: refused at the ceiling, never
accumulated into a charge, never reported to a payment provider. `project_egress`
looks like the start of a metering pipeline and is not one — no invoice reads
it, and a wrong value there refuses a download rather than billing for one.

Egress is also the platform's first resource a customer can have consumed *for*
them: a public bucket is served to whoever has the URL, and the project pays the
ceiling either way. That is the free tier's exposure, and it is why the ceiling
exists on free rather than only above it.

### The held-bytes figure is customer-writable, and re-measuring does not fix it

**A customer who can reach `service_role` can under-report their object storage
usage, and the platform will believe them.** Measured 2026-08-24 and asserted in
`tests/test_object_storage_accounting.py`.

The quota is measured from `storage.objects.metadata->>'size'`, which is the
tenant's own record. `service_role` holds `ALL` on that table (upstream
migration 0046) and carries `BYPASSRLS`, and
`services/control_plane/api/tenant_access.py` already records that the session
user on an impersonating connection is the authenticator — a member of all three
shared names — so a request can `SET ROLE service_role` in one line of its own
SQL. ADR-039 puts that surface on **every tier**. One `UPDATE` sets every
object's recorded size to zero.

`anon` and `authenticated` cannot: they hold the same grants and not
`BYPASSRLS`, so row-level security with no policy in place stops them.

This is ADR-040's admission in a new place, and **worse in one specific way**.
ADR-040's hole is a loop: a customer re-grants `INSERT`, the next maintenance
pass revokes it again, and the escape has to be repeated. This one is not a
loop — re-measuring re-reads the same forged column and gets the same answer
forever.

What closes it is a figure taken from the object store, which is not
customer-writable. No code can take one until a storage worker has an endpoint
to ask, so it is carried to Phase 10 slice 3 rather than fixed here, and the
interim figure is treated as what it is: the tenant's claim about itself, good
enough to bound an honest project and not a control against a determined one.
ADR-009's layering is the answer to it not being sufficient alone — node
capacity management is what bounds the disk either way.

Egress is unaffected. It is counted at the gateway from bytes actually served
and is never read back from the tenant, so there is nothing in a customer's
reach to rewrite.

The measured figure is the tenant's metadata, not a query against the object
store, and the two can drift — an upload that wrote bytes and failed to commit
its row leaves an object nobody is billed for and nobody can reach.
Reconciliation is Phase 11's, alongside backups and restore. The error is in the
tolerable direction: the platform under-counts rather than over-charging for
bytes a customer's metadata does not show.

## Violation handling

Preferred progression:

```text
normal -> throttled -> temporarily restricted -> suspended -> manual review
```

Automated resource enforcement must not automatically destroy customer data.
