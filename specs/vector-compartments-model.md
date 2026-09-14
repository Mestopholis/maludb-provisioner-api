# Vector compartments — what MaluDB's own vector store needs, and costs, on this platform

Compartments slice 0 of `plans/completed/phase-12-vector-compartments.md` (ADR-077).
Measured 2026-09-13. Reproduce with `scripts/spike-vector-compartments.py`; its
docstring has the invocation.

## Where it was measured

The development node: PostgreSQL 17 on a 3.8 GB, 2-vCPU VM, `maludb_core`
0.104.0, `vector` 0.8.4, with nothing else busy. Two tenants were provisioned
through `services/control_plane/provisioning.py` and `tenant_bootstrap`, so
ADR-016's roles and the ADR-018/076 bootstrap are the platform's own. Probe
wrappers were `SECURITY DEFINER` functions in `maludb`, owned by a `NOLOGIN` role
that started with no grants, called as `service_role`.

Timings are one node, one run, twenty searches each. They set orders of
magnitude for plan limits; they are not a benchmark.

## Findings

### 1. A non-superuser definer works, and needs more than the scratch database suggested

Create, insert and exact search all run through wrappers owned by a role holding
nothing but grants — **the node superuser is not needed**. Found by starting from
no grants and adding exactly what each denial named:

| Object | Privileges |
|---|---|
| schema `maludb_core` | `USAGE` |
| `malu$vector_subject`, `malu$vector_verb`, `malu$vector_compartment` | `SELECT`, `INSERT`, `UPDATE` |
| `malu$vector_chunk` | `SELECT`, `INSERT` |
| `malu$ann_index` | `SELECT` (search checks for an index) |
| their four `_id_seq` sequences | `USAGE` |
| `register_vector_compartment`, `register_vector_subject`, `register_vector_verb`, `register_vector_chunk`, `search_memory_exact`, `exact_vector_search_sql`, `exact_vector_search_c` | `EXECUTE` |
| `vector_dims`, `vector_norm`, `vector_normalize`, `octet_length` (MaluDB's own overloads) | `EXECUTE` |

A scratch database with the extension alone needed only the table grants: there,
`PUBLIC` holds `EXECUTE` on the extension's functions. **On a provisioned tenant
it does not** — bootstrap 011 revokes it in every schema, and ADR-076's grants
name customer roles, not a platform definer. So the definer's function grants
are real requirements, and a test against a bare extension would miss them.

ANN adds: `SELECT, INSERT, UPDATE` on `malu$ann_index`; `SELECT, INSERT, UPDATE,
DELETE` on `malu$ann_delta`; `SELECT` on `malu$vector_tombstone`; `EXECUTE` on
`ann_build`, `maludb_ann_build_c`, `maludb_ann_search_c`, `vector_dot_product`,
`vector_l2_squared`. The table privilege levels were found by escalation
(`SELECT`, then `INSERT`, …) because the error names the table and not the
operation, so a level can be one higher than strictly needed.

`anon` and `authenticated` calling a wrapper are refused.

### 2. A privilege a first call does not need, a later call can

The ANN search passed discovery on its first call and then failed on a later one
with `permission denied for function vector_l2_squared`. PL/pgSQL checks a
function's privilege when a plan reaches it, and a cached plan reaches `CASE`
branches the first execution did not. **So the definer's grants cannot be
derived by calling each wrapper once** — slice 1 grants every function the
wrapped code can reach, read from the function bodies, and its test calls each
wrapper repeatedly on one connection.

### 3. `owner_schema` is `maludb_core`, not `maludb`

`owner_schema` defaults to `current_schema()`, which is the first schema on the
path the *current role* can use. The definer has no `USAGE` on `maludb`, so every
compartment lands under `maludb_core`. Harmless on a one-tenant database — no
customer role can reach `maludb_core` — but slice 1 sets it deliberately rather
than inheriting it, since `search_memory_exact` finds a compartment by namespace,
subject and verb **without** filtering on `owner_schema`.

### 4. Upstream calls its own functions unqualified

`register_vector_compartment` calls `register_vector_subject(...)` with no schema,
so it works only with `maludb_core` on the `search_path`. The wrappers pin it; any
platform code calling upstream directly must too.

### 5. Exact search cost

Through the wrapper as `service_role`, on one held connection:

| Vectors × dimensions | Median | p95 |
|---|---|---|
| 1,000 × 384 | 11 ms | 12 ms |
| 10,000 × 384 | 45 ms | 84 ms |
| 50,000 × 384 | 192 ms | 228 ms |
| 1,000 × 1536 | 45 ms | 48 ms |
| 20,000 × 1536 | 562 ms | 855 ms |

Roughly linear in vectors × dimensions: **10–12 ms per million stored dimensions
at 384, about 18 ms at 1536**, single-threaded. Storage is **5.5 bytes per stored dimension** including indexes
and source text (304 MB for 82,002 vectors), so a 1536-dimension vector is about
8.5 KB.

### 6. ANN is not faster where it would matter, and is expensive to build

Timed on a second build after grant discovery (discovery repeats the build after
each denial, which is why the first run's 10k figure was 39.5 s). Peak memory is
the building backend's `VmHWM`:

| Vectors × dimensions | Build | Backend peak | Graph (`bytea`) | Search median / p95 | Exact median / p95 |
|---|---|---|---|---|---|
| 10,000 × 384 | 5.7 s | 209 MB | 16 MB | 12 / 100 ms | 42 / 66 ms |
| 50,000 × 384 | 43.5 s | 652 MB | 80 MB | 132 / 473 ms | 192 / 281 ms |
| 20,000 × 1536 | 42.8 s | 951 MB | 124 MB | 290 / 695 ms | 503 / 614 ms |

- **The tail is worse than exact search at every size measured.** The median
  improves, the p95 does not — consistent with each query passing the whole graph
  `bytea` to `maludb_ann_search_c`, so a query that misses the buffer cache reads
  tens of megabytes before it starts.
- **One build of a 20k × 1536 compartment took a quarter of this node's memory**
  in a single backend, synchronously, inside the caller's statement.
- The graph is stored inline in the tenant database, so it also counts against the
  database storage quota: 124 MB for 124 MB of vectors.

Recall was not measured; with the tail as it is, it did not need to be to decide.
**ANN stays off** (ADR-077 decision 5): nothing here allows offering it on shared
nodes. pgvector HNSW, which already works for customer tables, is the answer for a
project that needs approximate search at scale.

### 7. `pg_dump` carries none of it

A plain `pg_dump` of the provisioned tenant has no `COPY` for any of
`malu$vector_subject`, `_verb`, `_compartment`, `_chunk`, `_tombstone`,
`malu$ann_index` or `malu$ann_delta`, and no chunk text anywhere. `maludb_core`
registers no table with `pg_extension_config_dump`.

*Fixed in 0.105.0* (ADR-078, maludb-core#28): the data tables are registered and
`pg_dump` carries them. `tests/test_maludb_core_dump.py` asserts it for every
table; the carry below remains for sources that predate it.

### 8. Carrying the rows works, and is cheap

Binary `COPY` of those seven tables from the source tenant into a freshly
provisioned target, then `setval` on each sequence to its column's maximum:
**16.6 s for 82,002 vectors (304 MB)**. Every row count matched; the same search
returned the same chunks in the same order on the target; an insert on the target
afterwards took a fresh id (82005) rather than colliding.

Binary `COPY` needs identical column layouts. That holds for a move (ADR-075
refuses a move between different pins) but **not necessarily for a restore**: a
point-in-time restore can read a tenant from before an extension upgrade, while
`pg_restore` creates the extension at the node's current version. Slice 0's carry
therefore copies by explicit column list, and refuses a source column the target
lacks rather than dropping it.

*Correction, 2026-09-14.* **The carry as built uses text `COPY`, not binary** --
`extension_data.carry` copies by column list in text format -- and so does
`pg_dump`. Before maludb_core 0.105.1 that is lossy for embeddings:
`malu_vector`'s text output kept six significant digits, so every carried or
dumped vector was rounded (maludb-core#31, measured there: every one of 2,000
1536-d vectors changed, cosine distances by up to 5.5e-8, near-duplicate order
changed). 0.105.1 prints the shortest exact form; `tests/test_maludb_core_dump.py`
asserts the bytes.

`malu$vector_chunk.statement_id` references `malu$svpor_statement`, which is not
carried. Wrappers never set it; the carry refuses a non-null one rather than
failing on the foreign key halfway through.

### 9. Exact search does not honour tombstones

`malu$vector_tombstone` is filtered only on the ANN path. A tombstoned chunk is
still returned by `search_memory_exact`. So a delete wrapper must delete the chunk
row (the tombstone and delta rows cascade), not tombstone it.

## What this changes in the plan

- Slice 1's definer grants come from function bodies, not from calling once
  (finding 2), and include function `EXECUTE` (finding 1).
- The wrappers set `owner_schema` explicitly (finding 3) and pin `search_path`
  (finding 4).
- Plan limits are sized from finding 5.
- The carry uses column lists, not binary layout (finding 8).
- ANN is not offered; no follow-up decision is needed unless upstream stores the
  graph differently (finding 6).
- Deleting a chunk deletes its row (finding 9).
