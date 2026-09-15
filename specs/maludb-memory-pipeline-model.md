# MaluDB Memory Pipeline Model

What it takes to put `maludb_core`'s memory pipeline in front of a customer as
named **memory spaces**, and whether the leading delivery candidate for the draft
ADR-079 works: writes run by the platform, reads through a narrow per-project
wrapper. Deliverable of memory pipeline slice 0.

Status: derived from experiments run 2026-09-14 against `maludb_core` 0.105.0,
PostgreSQL 17.10 and PostgREST 14.17 on the development host (6 cores, 3.8 GB
RAM, with another test session running). Every tenant measured was provisioned
through the platform's own provisioning module and `tenant_bootstrap`, so
ADR-016's roles, the ADR-014 lockdown and the ADR-018/076 bootstrap are the
platform's own. The harness is `scripts/spike-memory-pipeline.py`; it reproduces
every table below in one ~35 s run.

Companion to the data-model spike (`specs/maludb-datamodel-model.md`), whose
guard finding this document extends from two facades to the whole pipeline, and
to ADR-077 (`specs/vector-compartments-model.md`), whose definer pattern it
tests for reads — and whose shipped wrappers it finds need a fence before memory
spaces ship (finding 1e).

Nothing here is decided. The options are at the end.

## Summary

| Question | Answer, measured |
|---|---|
| 1. Search without superuser code | **Yes.** A `NOLOGIN` definer holding `SELECT` on four vector tables and `EXECUTE` on eight invoker functions returns the facade's results exactly (40 queries, 400 rows, 0 differences), through PostgREST as `service_role`, at the facade's latency. The facade itself fails on that path, whoever owns the wrapper. |
| 2. Ingest by the platform | 8.2 ms median / 10.6 ms p95 per embedded edge; 15.8 / 17.9 ms per extraction document. **Read-after-write is immediate**: 50/50 searches from another session, right after commit, found the new memory at rank 1. Nothing has to run first. But only embedded edges are searchable — `ingest_extraction` output never is. |
| 3. Memory spaces | ~0.5–0.7 s, ~0.8–1.0 MB and 165 objects per space. Spaces isolate through both the facade and the wrapper (0 foreign rows either way). The hazards are real and demonstrated: RLS on the graph tables follows the caller's `search_path`, and an owner-rights view reads its space whatever the path. |
| 4. Guard inventory | 84 functions per space, all owned by the node superuser; 27 `SECURITY DEFINER`, 28 reach the guard. Every pipeline facade is a definer that reaches it. The search path's leaves (`exact_vector_search_sql`, `register_vector_chunk`) are invoker and unguarded. |
| 5. Extraction worker | **Works end to end** over the node superuser connection with no MaluDB role granted, and also as a narrow non-`BYPASSRLS` role holding five grants. Harvest needs the guard, so it is the platform's. Upstream's own in-database stub cannot be used: it fails on `owner_schema`, and its output is not harvestable. |

## Finding 1 — search without superuser code

### 1a. What `maludb_memory_search` executes

`<space>.maludb_memory_search` is a one-line `SECURITY DEFINER` SQL function
pinned to `search_path = pg_catalog, maludb_core, pg_temp` that calls
`maludb_core._memory_search_for_schema('<space>', ...)`. That function, also a
superuser-owned definer:

1. calls `_memory_schema_assert_manageable('<space>')` — the session-user guard;
2. selects compartments from `malu$vector_compartment` joined to
   `malu$vector_subject` and `malu$vector_verb`, **filtered on
   `owner_schema = '<space>'`**, namespace, and subject and/or verb;
3. for each, `CROSS JOIN LATERAL maludb_core.exact_vector_search_sql(compartment_id, query, limit, metric)`;
4. ranks across compartments by distance and joins `malu$vector_chunk` for
   `statement_id` and `document_id`.

**It reads only vector tables**, and those four have **no row-level security
enabled at all** (not merely not forced). It reads nothing from the svpor graph,
episodes, claims or facts. There is no recall or episode read facade in the
pipeline beyond `maludb_episode_get` and `maludb_document_get`, which are
invoker functions over the RLS-protected graph tables (finding 3c).

### 1b. The candidate wrapper, and exactly what it needed

`maludb.memory_search(space, query vector, subject, verb, namespace, match_count, metric)`:
`SECURITY DEFINER`, owned by `mldb_<ref>_memread` (`NOLOGIN`, no members, member
of nothing), `search_path = maludb_core, public, pg_temp`. It resolves `space`
against a platform-owned registry table (`maludb_private.memory_spaces`),
refuses anything unregistered, and runs the facade's query with the registry's
value as the `owner_schema` filter. `EXECUTE` to `service_role` only.

Starting from `USAGE` on `maludb_core`, `public` and `maludb_private` and
`SELECT` on the registry, it was called as `service_role` from an authenticator
session and granted exactly what each refusal named. Ten refusals, in order:

| Refusal | Granted |
|---|---|
| `permission denied for function vector_out` | `EXECUTE` on `public.vector_out(vector)` |
| `permission denied for function malu_vector_in` | `EXECUTE` on `maludb_core.malu_vector_in(cstring)` |
| `permission denied for table malu$vector_chunk` | `SELECT` |
| `permission denied for table malu$vector_compartment` | `SELECT` |
| `permission denied for table malu$vector_subject` | `SELECT` |
| `permission denied for table malu$vector_verb` | `SELECT` |
| `permission denied for function exact_vector_search_sql` | `EXECUTE` |
| `permission denied for function vector_dims` | `EXECUTE` on all three overloads (`malu_vector`, `vector`, `halfvec`) |
| `permission denied for function vector_normalize` | `EXECUTE` on `maludb_core.vector_normalize(malu_vector)` |
| `permission denied for function exact_vector_search_c` | `EXECUTE` |

**None of the eight functions is `SECURITY DEFINER`**, and no table needed more
than `SELECT`. Fifteen further calls on the same connection, alternating
subject-only and subject-plus-verb past the generic-plan switch, produced no
further refusal (ADR-077's finding 2 did not recur here). The `USAGE` grants are
not optional in a quieter way: without `USAGE` on `maludb_core` the pinned path
silently skips the schema and the error is `relation "malu$vector_compartment"
does not exist`, not a denial.

Compared on the same tenant with ADR-077's `mldb_<ref>_vectors` installed beside
it, the set is a subset of what that role already holds, except the registry and
the `vector`/`halfvec` overloads of `vector_dims` — which the harness grants
because a function denial names no signature, and which may not be needed.

### 1c. Same results, same cost, real PostgREST path

| Check | Result |
|---|---|
| Wrapper vs facade-as-platform, 40 queries (subject, subject+verb) | **0 differing result sets, 400 rows compared** — chunk, statement, document, text, distance to 1e-9, rank, subject, verb |
| Facade as platform | median 6.0 ms, p95 8.4 ms |
| Wrapper as `service_role`, authenticator session | median 5.9 ms, p95 8.3 ms |
| PostgREST (`render_config` output, `db-schemas = "public, maludb"`, the ADR-076 pre-request), `POST /rpc/memory_search`, JWT `role: service_role` | HTTP 200, same chunk ids in the same order as the facade |
| HTTP round trip, 30 calls | median 9.8 ms, p95 12.6 ms |
| Same request with an `anon` JWT | HTTP 401, `permission denied for schema maludb` |
| `space: "maludb_core"` (not registered) | HTTP 404, `no memory space maludb_core` |
| `POST /rpc/maludb_memory_search` with `Content-Profile: space_a` | HTTP 406, `PGRST106` — a space schema is never in `db-schemas` |

Data: 1,000 embedded edges (384 dimensions) in `space_a` across 20 (subject, verb)
compartments, 250 in `space_b` with the same names.

### 1d. The facade on that path fails, whoever owns the wrapper

Both from the authenticator session after `SET ROLE service_role`
(`session_user / current_user` = `mldb_mps00001_authenticator / service_role`):

| Wrapper over `space_a.maludb_memory_search` | Result |
|---|---|
| Owned by the node superuser | `enable_memory_schema: mldb_mps00001_authenticator lacks CREATE on schema space_a` |
| Owned by the reader, after granting each refusal (`permission denied for schema space_a`; `permission denied for function maludb_memory_search`, a superuser-owned `SECURITY DEFINER`) | the same guard error |

So the data-model finding holds across the pipeline, and granting a narrow role
`EXECUTE` on the facade does not help: it only moves the refusal from a denial to
the guard, having first handed that role superuser-owned code.

### 1e. What those grants expose, and a hazard in shipped code

**Database-local rows only.** The four tables live in the tenant database; none
holds cluster-wide rows. **But every space's rows.** The reader's `SELECT` alone,
without the wrapper, sees `{space_a: 21, space_b: 20}` compartments — and would
see ADR-077's customer compartments (`owner_schema = 'maludb_core'`) too. Space
isolation is entirely the wrapper's `owner_schema` filter; the tables contribute
none. The role is reachable only through wrapper code (no login, no members), so
this is the same posture as ADR-077's definer, not a new exposure — but it means
the filter is load-bearing and must be tested per wrapper.

**The same fact cuts the other way, in code that has shipped.** ADR-077's
wrappers find compartments by namespace, subject and verb **without an
`owner_schema` filter** (`vector_compartment_find`, `vector_compartments`, and
`search_memory_filter` underneath `vector_search`). Installed through
`maludb_vectors._build` in a database that also has two memory spaces, with **no
customer compartment created**:

| As `service_role` | Result |
|---|---|
| `maludb.vector_compartments()` | **41 rows** — every memory-space compartment |
| `maludb.vector_search('default', 'subject-01', 'owns', …)` | **50 rows, all `space_a` memories** |
| `maludb.vector_compartment_delete('default', 'subject-01', 'owns')` (rolled back) | returned 50: **deleted a memory-space compartment** and its chunks |
| Vector quota (`max_compartments`, `max_count`) | counts memory-space rows, since both count the whole table |

**Not exploitable today**: nothing the platform ships writes
`malu$vector_compartment` in a tenant except ADR-077's own wrappers — the data
model graph's refresh was checked and writes none (0 compartments after a
refresh in `maludb_memory`). It becomes a cross-surface defect the day memory
spaces write embedded edges. The fence is one predicate
(`owner_schema = 'maludb_core'`) in the ADR-077 wrappers and helpers, and it has
to land **before** memory spaces do. It is intra-project — every party is the
same project's `service_role` — but it lets one surface silently destroy
another's data, and makes the vector plan limits count memories.

## Finding 2 — ingest by the platform

Run as the node superuser over a direct connection, where `session_user` passes
the guard.

### 2a. Cost and rows

| Facade | Latency | Rows written |
|---|---|---|
| `maludb_upload_document` (n=50) | median 4.8 ms, p95 6.8 ms | 1 `malu$document`, 1 `malu$source_package` per call |
| `maludb_memory_ingest_edge`, caller-supplied 384-dim embedding (n=1,000) | **median 8.2 ms, p95 10.6 ms** | 1 `malu$vector_chunk` per call; per new (subject, verb): 1 `malu$vector_compartment`, and per new name 1 `malu$vector_subject` / `malu$vector_verb` / `malu$svpor_subject` / `malu$svpor_verb`; 1 `malu$svpor_statement` + 1 `malu$svpor_attribute` per distinct (source, verb, subject); `malu$embedding_dirty` rows (45 for the batch) |
| `maludb_memory_ingest_extraction`: document, 4 subjects (1 dated event), 2 verbs, 4 edges, 1 relationship (n=50) | **median 15.8 ms, p95 17.9 ms** | per call ~1 document, 1 source package, 1 episode, 2.8 statements, ~1 subject, 3.8 `embedding_dirty`; **0 vector chunks** |

Two properties of the write path that an API contract has to reflect:

- **Statements upsert on identity.** 1,000 edges from one document over 20
  (subject, verb) pairs produced 20 statements (keyed source → verb → subject)
  and 1,000 chunks. Chunks accumulate; the graph deduplicates.
- **Partial success is silent.** `ingest_extraction` catches every per-item
  error and reports it in `skipped`: 30 of the 50 payloads' relationships were
  skipped with `conflicting key value violates exclusion constraint
  "malu$svpor_subject_relationship_edge_…"`, and the call still succeeded. A
  wrapper or job must surface `skipped`, or callers lose data without an error.

### 2b. Read-after-write

| Check | Result |
|---|---|
| `ingest_edge` committed, then `maludb_memory_search` from **another session** immediately | **50/50 found, 50/50 at rank 1** |
| `malu$embedding_dirty` rows those 50 ingests added | 3 — a reindex queue search never reads |
| Same transaction sees its own uncommitted ingest | yes |
| Another session sees it before commit | no |

**Nothing has to run between ingest and search**: no reindex, no harvest, no
`embedding_dirty` drain. Search is exact over the chunk table, so visibility is
plain MVCC. For async ingest, the delay an agent sees is exactly the job's
queue-to-commit time, plus ~8 ms per edge.

### 2c. Extraction output is not searchable by `memory_search`

`ingest_extraction` takes no embeddings and writes the svpor graph and episodes,
never a vector chunk. A search for a subject it created (`importer`) returned 0
rows. Only two paths make a memory searchable: `ingest_edge` with an embedding,
and `harvest_extractions` over a model response whose edges carry an
`embedding` array (finding 5). A "memory" API that accepts extraction JSON and
promises search over it would be wrong.

## Finding 3 — memory spaces

### 3a. Cost per space

| Space | Enable (`CREATE SCHEMA` + `enable_memory_schema`) | Database growth | Objects |
|---|---|---|---|
| `space_a` | 0.58 s | 0.90 MB | 165 (74 relations, 84 functions) |
| `space_b` | 0.67 s | 0.79 MB | 165 |
| `space_c` | 0.46 s | 0.83 MB | 165 |
| `space_d` | 0.58 s | 0.93 MB | 165 |
| `space_e` | 0.51 s | 1.00 MB | 165 |

Baseline tenant 25.8 MB. Flat per space: no sign that the fifth costs more than
the first. Each space also adds 158 `malu$enabled_schema_object` rows, 6
`malu$svpor_verb` and 6 `malu$embedding_dirty` rows in `maludb_core`. The
catalogue cost is what bounds the count: 165 objects × spaces in every
`pg_dump`, schema-cache load and re-enable after an extension upgrade (the data
model spike's finding that `ALTER EXTENSION` does not rebuild facades applies per
space). Not measured: behaviour beyond five spaces.

### 3b. Isolation, as the platform would use it

`space_a` and `space_b` held edges with **identical** namespace, subject and verb
names, distinguishable only by a marker in the span text.

| Path | Search of `space_a` for a subject both spaces hold | Search of `space_b` |
|---|---|---|
| (a) facade as platform | 0 `space_b` rows | 13 `space_b` rows, 0 `space_a` |
| (b) candidate wrapper as `service_role` | 0 `space_b` rows | 0 `space_a` rows |
| (b) wrapper, space `public` | refused: `no memory space public` | — |

### 3c. The hazards, demonstrated

**`current_schema()` follows `search_path`, and the unpinned functions store it.**

| As the platform, after `SET search_path = space_b, maludb_core, public` | Stored `owner_schema` |
|---|---|
| `space_a.maludb_register_episode(...)` | `space_a` — the facade pins `search_path=space_a, maludb_core, pg_temp` |
| `maludb_core.register_episode(...)` | **`space_b`** |

So the space facades are not exposed to this (55 of 84 pin their path); anything
that calls `maludb_core` directly — a platform job included — writes into
whichever space its session path names. Upstream's own model stub fails the same
way (finding 5).

**RLS on the graph tables isolates by the caller's path, not by grant.** A probe
role (`NOLOGIN`, not `BYPASSRLS`) granted `USAGE` on both spaces, `SELECT` on
`space_b.maludb_svpor_statement` (a `security_invoker` view), on
`space_b.maludb_subject` (an owner-rights view), and on the base
`malu$svpor_statement`. `space_b` truly holds 20 statements and 21 subjects.

| Probe's `search_path` | `space_b.maludb_svpor_statement` (invoker) | `space_b.maludb_subject` (owner-rights) | base `malu$svpor_statement` |
|---|---|---|---|
| `space_b` | 20 | 21 | `{space_b: 20}` |
| `space_a` | **0** | 21 | **`{space_a: 161}`** |
| `maludb_core` | 0 | 21 | — |
| `''` | 0 | 21 | — |

- An **invoker view** returns its own space only when the caller's path names that
  space, and nothing otherwise.
- An **owner-rights view** returns its space regardless of path: it runs as the
  superuser owner (RLS bypassed) and filters on a literal. `SELECT` on it *is*
  access to that space.
- The **base table** returns whichever space the caller's path names. A role with
  `SELECT` on `malu$svpor_statement` reads every space, one `SET search_path` at
  a time. RLS is enabled, not forced, on every graph table; the vector tables
  have none.

Consequence for any wrapper over the graph (not the vector search): its
isolation comes from its pinned path or an explicit `owner_schema` predicate,
never from RLS — and the reader of finding 1 should never be granted base graph
tables.

## Finding 4 — guard inventory

`enable_memory_schema('space_a')` creates 84 functions and 74 relations; the
harness walks every function body and the `maludb_core` functions it calls, to a
fixed point, for the guard (`_memory_schema_assert_manageable`) and for definer
reach.

| Property | Count |
|---|---|
| Functions in the space | 84 |
| Owner | node superuser, all 84 |
| `SECURITY DEFINER` / invoker | 27 / 57 |
| Reach the session-user guard | 28 (26 definers + invoker `maludb_chat_finalize`, `maludb_quick_add_note`) |
| Definers that do **not** reach it | 1: `maludb_register_subject_type` |
| Pin `search_path` to the space | 55 |
| Invoker, unguarded, body or direct callee reads `current_schema()` | 45 |
| Views: `security_invoker` / owner-rights | 52 / 22 |
| `EXECUTE` on `maludb_memory_search` | node superuser, `maludb_memory_admin`, `maludb_memory_executor`, `maludb_memory_auditor` — no customer role |

The pipeline, specifically (all owned by the node superuser):

| Function | Definer | Guard |
|---|---|---|
| `<space>.maludb_memory_search`, `_ingest_edge`, `_ingest_extraction`, `_request_extraction`, `_harvest_extractions`, `_set_model_config`, `_model_config`, `maludb_upload_document` | yes | reached, not called: each is a one-line call to its `_for_schema` twin |
| `maludb_core._memory_search_for_schema`, `_memory_ingest_edge_for_schema`, `_memory_ingest_extraction_for_schema`, `_memory_request_extraction_for_schema`, `_memory_harvest_extractions_for_schema`, `_upload_document_for_schema`, `_memory_set_model_config_for_schema`, `_vector_compartment_for_svpor` | yes | **called directly** |
| `maludb_core.exact_vector_search_sql`, `exact_vector_search_c`, `register_vector_chunk`, `register_episode`, `episode_get` | no | no |
| `maludb_core._memory_schema_assert_manageable` | no | — |

The owner-rights views are `maludb_claim`, `maludb_fact`, `maludb_memory`,
`maludb_memory_detail`, `maludb_subject`, `maludb_verb`, `maludb_person`,
`maludb_project`, `maludb_stakeholder`, `maludb_source_package`, `maludb_prompt`,
`maludb_prompt_render`, `maludb_llm_model`, `maludb_llm_provider`,
`maludb_llm_request`, `maludb_llm_response`, `maludb_model_alias`,
`maludb_model_provider`, `maludb_subject_type`, `maludb_subject_verb`,
`maludb_svpor_relationship`, `maludb_verb_type`. The full per-function table is in
the harness output.

## Finding 5 — the extraction worker

### 5a. End to end, over the node superuser connection

| Step | Run as | Result |
|---|---|---|
| `register_model_provider('mps-stub', 'stub', …)`, `register_model_alias('mps-extract', …)` | platform, `search_path = space_a, maludb_core` | stored with `owner_schema = space_a` (both default to `current_schema()`; the alias must be in the space's schema or `request_extraction` refuses it) |
| `space_a.maludb_memory_set_model_config('mps-extract', NULL, 'stub-384', 'default')` | platform | ok |
| `space_a.maludb_memory_request_extraction('document', doc, chunk, 'default')` ×10 | platform | median 5.6 ms, p95 15.2 ms; rows `owner_schema = space_a`, **`account_id` NULL**, `pending` |
| Worker: `SELECT … FOR UPDATE SKIP LOCKED` → `running` → `INSERT malu$model_response` (`output_json` = `candidate_edges` with per-edge 384-dim `embedding`) → `succeeded`, one transaction per request | node superuser: `rolbypassrls = false`, member of no `maludb_llm_*` role | 7 drained, median 15.2 ms, p95 27.2 ms per request |
| `space_a.maludb_memory_harvest_extractions(100, NULL)` | platform (guard) | 14 harvested, 28 edges, 0.22 s |
| `maludb_memory_search` for a harvested subject, immediately | platform | found at rank 1 |

The superuser needs no MaluDB role: it bypasses RLS as superuser, not through
`BYPASSRLS`. The worker never touches the guard — only harvest does.

### 5b. A narrow role is enough too

A `NOLOGIN` role, not `BYPASSRLS`, started with nothing and was granted what each
refusal named, then each table privilege was revoked again and kept revoked if a
fresh request still drained:

| Refusal | Needed |
|---|---|
| `permission denied for schema maludb_core` | `USAGE` |
| `permission denied for function current_account_id` | `EXECUTE` — **a superuser-owned `SECURITY DEFINER`**, called by the RLS policy |
| `permission denied for table malu$model_request` | `SELECT`, `UPDATE` |
| `permission denied for table malu$model_response` | `INSERT` (escalation granted `SELECT` and `UPDATE`; both proved unnecessary) |
| `permission denied for sequence malu$model_response_response_id_seq` | `USAGE` |

The RLS the research summary expected to require `BYPASSRLS` does not bite on
this path: `request_extraction` writes `account_id` NULL, and the policy admits
`account_id IS NULL` for every role. `current_account_id()` reads a session GUC
first, so it is no boundary either. A narrow worker therefore sees **every
space's** pending requests in the database — which is fine for a per-database
platform worker and is not a customer control.

### 5c. What a worker must do that upstream's stub does not

| Check | Result |
|---|---|
| `maludb_core.mc_stub_process(request)` with `search_path = maludb_core, public` | `insert or update on table "malu$model_response" violates foreign key constraint "malu$model_response_owner_request_fk"` — `Key (owner_schema, request_id)=(maludb_core, 1) is not present` |
| Same with `search_path = space_a, maludb_core, public`, then harvest | response written; harvest marks it `failed`: `invalid input syntax for type json` |

- **Write `owner_schema` explicitly on the response.** It defaults to
  `current_schema()` and is part of a foreign key to the request.
- **Answer in the harvest's JSON** (`candidate_edges[]` with `subject_text`,
  `verb_text`, `predicate`, `source_span`, `embedding`). The contract's stub
  output (`MALUDB_STUB_REPLY:<hash>`) is text and cannot be harvested.
- **Embeddings come from the worker.** Harvest writes a vector chunk only for an
  edge carrying an `embedding` array; the platform worker is where an embedding
  model has to be called.
- The chunk text reaches the worker only inside `rendered_prompt` (after
  `CHUNK:\n` in the default template); a custom prompt template changes that.
- One worker per tenant database, or a sweep across them: the request tables are
  per database. Not measured: polling cost across many databases.

## Where the research summary was wrong or incomplete

- **Vector tables have no RLS at all**; the graph tables have RLS keyed on
  `current_schema()`. Search reads only the former.
- **Only 22 of 74 facade views are owner-rights**; 52 are `security_invoker`.
- **Facades do not call the guard themselves**; each calls a `_for_schema`
  function that does. The effect on a wrapper is the same.
- **`BYPASSRLS` / `maludb_llm_admin` are not needed** to drain extraction
  requests (finding 5b).
- **Most facades pin `search_path` to their space**, so the `current_schema()`
  hazard applies to direct `maludb_core` callers and to base-table readers, not to
  facade callers.
- **The named facades write no claims, facts or memories.** `malu$claim`,
  `malu$fact` and `malu$memory` stayed empty through the whole run; they are
  written by separate invoker functions (`register_claim`, `register_fact`, …)
  keyed on `current_schema()`. "source → claim → fact" is not what
  `ingest_extraction` or `harvest` produce in 0.105.0: they produce svpor
  statements, episodes and vector chunks.
- **Caller-supplied embeddings reach search only through `ingest_edge` and
  harvest**, not `ingest_extraction` (finding 2c).

## Facts that constrain the decision

1. The guard is inside every pipeline path's `_for_schema` function. No wrapper of
   any owner can call a facade on PostgREST's path (1d).
2. Search needs nothing superuser-owned: four `SELECT`s, eight invoker functions,
   parity proven, same latency (1b, 1c).
3. The search wrapper **re-implements** upstream's ~40-line query. Parity holds for
   0.105.0; every extension upgrade has to re-prove it, as ADR-077 re-derives
   grants.
4. Space isolation on the vector tables is solely an `owner_schema` predicate, in
   upstream's code and in ours (1e, 3b). RLS contributes nothing to vectors and
   only path-dependent isolation to the graph (3c).
5. ADR-077's shipped wrappers lack that predicate and would list, search, delete
   and count memory-space data (1e). They need fencing before any space holds an
   embedded edge.
6. Ingest is cheap and synchronous-safe: ~8 ms per embedded edge, ~16 ms per
   extraction document, visible to search on commit (2).
7. Writes silently skip items (2a); a delivery must return `skipped`.
8. Extraction requires a platform-run worker that calls an LLM and an embedding
   model, writes `owner_schema` explicitly, and answers in harvest JSON; harvest
   itself requires the guard (5).
9. A space costs ~0.6 s and ~1 MB, and 165 catalogue objects that every upgrade
   re-enables (3a).

## Options for delivery, given the guard

Recorded for the owner to decide.

**A. Grant the authenticator `CREATE` on every space.** Every facade then works
from a request, reads and writes alike, today. The data-model spike's objections
apply per space and are larger here: the guard then admits every role the
authenticator can become, superuser-owned definers doing writes are one
`EXECUTE` grant from each request, and the login role behind the API holds DDL on
N platform schemas. Measured cost: none added; measured risk: 26 superuser-owned
definers per space become request-reachable.

**B. Platform writes, asynchronously; narrow reader wrapper for search.** (The
leading candidate.) Writes are queued (like ADR-074's refresh) and run by the
platform over the node connection; search is `maludb.memory_search` owned by
`mldb_<ref>_memread` (or folded into ADR-077's definer, whose grants are a
superset). Measured: search parity 0/400 differences, 5.9 ms in-database, 9.8 ms
over HTTP; writes 8–16 ms plus queue delay; read-after-write immediate once the
job commits. Costs: a job queue and per-plan limits at enqueue; `skipped` and
failures reported back through job status, since the caller is gone; a
re-implemented search query to re-verify on each upgrade; ADR-077's wrappers
fenced first. Agents that write and then immediately search see their write only
after the job, which is the latency this option adds and which slice 0 did not
measure.

**C. Platform writes, synchronously, mediated.** The same platform execution as
B, but the control plane runs the facade inside the customer's request and
returns its result, including `skipped`. Read-after-write becomes exact from the
caller's view, and there is no queue. It reopens the routing question ADR-074
declined (a `/maludb/v1` or RPC path forwarding to the control plane), and a
customer request steering a superuser session into upstream PL/pgSQL with
customer-supplied JSON is a capability that needs its own review. Reads can stay
as in B.

**D. Narrow writer wrappers as well.** A per-project definer that writes vector
and svpor rows through upstream's invoker, unguarded functions
(`register_vector_compartment`, `register_vector_chunk`,
`register_svpor_statement`, …) with a pinned path. No superuser code on any
request. **Not measured:** `_memory_ingest_edge_for_schema` is itself guarded and calls
another guarded definer (`_vector_compartment_for_svpor`), so this means re-implementing ~200 lines of upstream write logic and its
upserts, and granting a request-reachable role write access to graph tables whose
RLS isolates only by path (3c). Highest divergence risk.

**E. Wait for upstream** to check `current_user`. It would not make wrappers over
the facades narrow: the facades are superuser-owned definers, so a wrapper still
reaches superuser code from a request, and each space would have to grant
`CREATE` to whatever role the guard then checks. It improves A, not B.

Extraction is platform-run under every option (finding 5): request enqueue can
follow the chosen write path, but draining and harvest belong to a platform
worker.

## What slice 0 did not measure

- Queue-to-commit latency of an async write job, which is what B adds.
- More than five spaces in one database, or spaces carrying more than 1,250
  chunks; search cost at scale is ADR-077's finding 5 (the same exact search).
- Upgrading `maludb_core` with spaces enabled and wrapper parity afterwards.
- A real LLM or embedding model; the worker's stub answered deterministically.
- Polling `malu$model_request` across many tenant databases.
- Concurrency between ingest, harvest and search in one space.
- Whether upstream considers the guard's `session_user` check deliberate.

## Reproducing

```bash
set -a; . ./.dev/test.env; set +a    # MALUDB_NODE_ADMIN_DSN, MALUDB_PLATFORM_OWNER, MALUDB_POSTGREST_BIN
scripts/spike-memory-pipeline.py run            # questions 1-5, ~35 s
scripts/spike-memory-pipeline.py run --keep     # leave mldb_mps00001 behind for inspection
scripts/spike-memory-pipeline.py run --no-postgrest --edges 200   # quicker, no HTTP path
```

It provisions one disposable tenant (`mps00001`), enables five spaces, and drops
the database and every `mldb_mps00001_*` role afterwards unless `--keep`. It
starts PostgREST on `127.0.0.1:3997` (`--port`). Point it only at a disposable
node: it grants probe roles privileges on `maludb_core` tables on purpose,
including `EXECUTE` on superuser-owned facades.

# Memory slice 1 — the writer role

Measures the one thing ADR-079 decision 6 said was **not yet measured**: that the
memory worker can connect to a tenant database as a *per-project writer login
with `CREATE` on that project's spaces only* and run the pipeline facades, that
this reaches nothing beyond the space, and that granting the login `CREATE` on a
superuser-owned schema is not a privilege-escalation primitive. Same host and
versions as slice 0 (`maludb_core` 0.105.0, PostgreSQL 17.10). Reproduced by
`scripts/spike-memory-pipeline.py writer`, which provisions two disposable
tenants (`mws00001`, `mws00002`), builds the writer, and drops both databases and
the role unless `--keep`.

## Summary

| Question | Answer, measured |
|---|---|
| 1. Does a narrow writer pass the facades? | **Yes, with `EXECUTE` on the space facades alone** — no `maludb_memory_executor` membership, no table/sequence grant. All pipeline work runs on the facades' `SECURITY DEFINER` rights. `CREATE` on the space is required (the guard), `USAGE` on the space and on `maludb_core` are required, nothing else is. |
| 2. What that grant set reaches | The one database (CONNECT to a second tenant is refused, ADR-014), the one space (a second space with no grant is refused through the facade, the `_for_schema` twin, and the base tables), and `maludb_core` **only through the granted facades** — no direct table, no ungranted function, no PUBLIC surface (0 PUBLIC-executable functions). |
| 3. Escalation through `CREATE` | **None found.** The granted facades pin `search_path` away from the space (`pg_catalog, maludb_core, pg_temp`), so a writer-created object in the space is never resolved by superuser code; `pg_temp` shadows lose to `maludb_core`; the writer owns nothing it can trigger, replace, or drop. One latent invariant flagged. |
| 4. Move / restore | The writer role **must be created on the target before the load**, exactly like `create_vectors_role` (ADR-077). Its grants are in the dump; with the role absent, `pg_restore` errors on every one and restores none. |

**Verdict: ADR-079 decision 6 holds as written, with one correction and one
addition.** A per-project `LOGIN` writer, `NOSUPERUSER NOCREATEDB NOCREATEROLE
NOREPLICATION NOBYPASSRLS NOINHERIT`, with `CONNECT` on its own database only,
`USAGE` on `maludb_core`, `USAGE` + `CREATE` on each of its spaces, and `EXECUTE`
on that space's pipeline facades, runs ingest, extraction request, harvest and
search and reaches nothing else. The correction: decision 6 says the writer needs
`CREATE` *and the extension's executor rights*; the executor rights are **not
needed** — per-object `EXECUTE` on the space facades is enough, and it is strictly
narrower than `maludb_memory_executor` membership (which is cluster-wide and
carries EXECUTE on the search facade and the auth/secret functions, ¶1). The
addition: the writer is a new per-project role and slice 2's move/restore work
must provision it on the target (¶4).

## 1 — a narrow writer passes the facades

### 1a. `CREATE` on the space is required; that is the guard

Every `_for_schema` function on the pipeline (ingest_edge, ingest_extraction,
request_extraction, harvest, search, upload_document, set_model_config) calls
`_memory_schema_assert_manageable`, whose sole check is
`has_schema_privilege(session_user, <space>, 'CREATE')`. `SET ROLE` does not
satisfy it — the guard reads `session_user`, not `current_user` — so the worker
cannot use the node superuser connection narrowed by a `SET ROLE` (confirmed from
the guard body; it is why the worker connects *as* the writer). Measured: with
`USAGE` + `EXECUTE` on the facades but `CREATE` revoked, every facade fails with
`enable_memory_schema: mldb_mws00001_memwriter lacks CREATE on schema space_a`.
Re-granting `CREATE` makes them pass.

### 1b. The minimal working grant set, and what it does *not* need

The narrowest set that runs the whole pipeline (option (a) — per-object grants,
per-database objects, no role membership):

```sql
CREATE ROLE mldb_<ref>_memwriter LOGIN PASSWORD '…'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
GRANT CONNECT ON DATABASE mldb_<ref> TO mldb_<ref>_memwriter;   -- its own db only
GRANT USAGE ON SCHEMA maludb_core TO mldb_<ref>_memwriter;
GRANT USAGE, CREATE ON SCHEMA <space> TO mldb_<ref>_memwriter;
GRANT EXECUTE ON FUNCTION
    <space>.maludb_upload_document(…), <space>.maludb_memory_ingest_edge(…),
    <space>.maludb_memory_request_extraction(…), <space>.maludb_memory_harvest_extractions(…),
    <space>.maludb_memory_set_model_config(…), <space>.maludb_register_model_provider(…),
    <space>.maludb_register_model_alias(…)
    TO mldb_<ref>_memwriter;   -- and maludb_memory_search(…) if the writer searches
```

Measured, as that login, end to end in one space: register a stub provider and
alias → `set_model_config` → `upload_document` → `ingest_edge` (searchable
immediately, rank 1) → `request_extraction` → the model worker drains the request
(over the node connection, as decision 6 keeps it) → `harvest_extractions` (2
edges) → search finds the harvested subject. Every step **OK**.

What it does **not** need, all confirmed unnecessary:

- **No `maludb_memory_executor` membership.** Option (b) was not reached because
  (a) works. This matters: `maludb_memory_executor` is a cluster-wide role
  (¶2b), it is `NOINHERIT` upstream so it would need `SET ROLE` per statement,
  and it holds `EXECUTE` on the search facade and on `auth_token_*`,
  `__secret_master_key*` and `current_account_id` — far more than a writer needs.
  Per-object `EXECUTE` on the space's facades is both sufficient and narrower.
- **No table, view or sequence grant.** The facades are `SECURITY DEFINER` owned
  by the node superuser and do all their table work as the definer; the writer
  touches no `malu$*` table directly (contrast the model *worker* of slice 0 ¶5b,
  which reads/writes `malu$model_request`/`_response` directly and does need those
  grants — that is the drain step, run over the node connection, not the writer's
  facade path).
- **No `BYPASSRLS`, no superuser, no `CREATEROLE`/`CREATEDB`.**

`maludb_register_model_provider`/`_alias` are space facades and are grantable to
the writer; the bare `maludb_core.register_model_provider`/`_alias` are granted
only to `cp_ci` and are **not** reachable by the writer (nor need to be).

## 2 — what the grant set reaches beyond its space

| Reach probe, as the writer | Result |
|---|---|
| (a) second space `space_b` (no grant): facade `ingest_edge` / `search` | `permission denied for schema space_b` |
| (a) `maludb_core._memory_ingest_edge_for_schema('space_b', …)` (the twin) | `permission denied for function _memory_ingest_edge_for_schema` |
| (a) base `maludb_core."malu$vector_compartment"` directly | `permission denied for table malu$vector_compartment` |
| (a) base table after `SET search_path = space_b, maludb_core` (RLS-path game) | `permission denied for table malu$vector_compartment` |
| (b) `CONNECT` to the other tenant database `mldb_mws00002` | refused: `FATAL: permission denied for database` (ADR-014 lockdown) |
| (c) ungranted `maludb_core.register_claim(…)` | `permission denied for function register_claim` |
| (c) `pg_authid` | `permission denied for table pg_authid` |
| (c) `CREATE TABLE maludb_core.…` | `permission denied for schema maludb_core` |

The writer's `USAGE` on `maludb_core` is **not** a reach: `maludb_core` grants
`EXECUTE` to no PUBLIC and holds **0** functions with a default (PUBLIC) ACL, so
`USAGE` on the schema opens nothing the writer was not explicitly granted. Its
reach is exactly: its own database, its own space(s), and `maludb_core` *solely
through the facades it was granted*. A second space in the same database is as
closed to it as another tenant's database — the platform must grant the writer
`CREATE`/`USAGE`/`EXECUTE` per space, and a space it was not granted is
unreachable through every path tried.

## 3 — privilege escalation through `CREATE` (the critical one)

`CREATE` on a space schema lets the writer create objects in a schema whose other
objects are superuser-owned and whose superuser-owned `SECURITY DEFINER` code
runs there. Every attempt to turn that into superuser execution **failed**:

| Attempt | Outcome |
|---|---|
| Create `<space>."malu$vector_chunk"` and `<space>.register_vector_chunk(…)` shadows, then `ingest_edge` | Objects created (the writer has `CREATE`), but ingest wrote to the **real** `maludb_core` table and search found the new row — the shadows were never consulted |
| Create `pg_temp.vector_dims` / `pg_temp.vector_normalize`, then `ingest_edge` | Ingest succeeded using `maludb_core`'s functions — `maludb_core` precedes `pg_temp` in the definer path, so `pg_temp` never wins |
| `CREATE TRIGGER … ON maludb_core."malu$vector_chunk"` | `permission denied for table` (not the owner) |
| `CREATE TRIGGER … INSTEAD OF INSERT ON <space>.maludb_memory` | `permission denied for view` (not the owner) |
| `CREATE OR REPLACE FUNCTION <space>.maludb_memory_search(…)` | `must be owner of function maludb_memory_search` |
| `DROP FUNCTION <space>.maludb_memory_ingest_edge` | `must be owner of function` |

**Why it holds.** All 26 pipeline definers in a space pin
`search_path = pg_catalog, maludb_core, pg_temp` — the space is *not* on their
path — so an object the writer creates in the space is invisible to them. The
`_for_schema` bodies call into `maludb_core` by qualified or path-resolved names
that land in `maludb_core` before `pg_temp`. The writer owns none of the existing
schema objects (all node-superuser), so it can neither trigger, replace, nor drop
them; `CREATE` grants only the right to add *new* objects, which nothing
privileged reads.

**One latent invariant, flagged not exploited.** Exactly one space definer pins
the space **first**: `<space>.maludb_document_graph_backfill()`
(`search_path = <space>, maludb_core, pg_temp`). Today it is safe — its body
calls only `maludb_core._document_graph_backfill_for_schema(current_schema())`,
fully qualified, and it is not among the facades the writer is granted. But it is
the shape that *would* be exploitable: a space-first definer that referenced an
unqualified name a `CREATE`-holding writer could shadow. **Slice 2's
per-upgrade re-verification should assert that every `SECURITY DEFINER` function
whose `search_path` names the space before `maludb_core` references only
schema-qualified objects.** This is the residual cost of the `CREATE`-based guard
model and should be recorded as a test, the ADR-075 way.

## 4 — moves and restores

One `pg_dump -Fc` / `pg_restore` round trip (with ADR-078's
`session_replication_role = replica`, as `restore.pg_restore_argv` uses):

| Case | Result |
|---|---|
| Target **has** the writer role | restore exit 0; all 8 function `EXECUTE` grants restored; `CREATE` on the space restored |
| Target **lacks** the writer role | 10 × `role "mldb_<ref>_memwriter" does not exist`; **0** of its grants restored |

The writer's grants are per-database objects in the dump (schema `USAGE`/`CREATE`
and function `EXECUTE`), and — being a cluster-scoped role never in a
single-database dump — the role itself is not. This is the ADR-077 vectors
finding exactly: a load onto a cluster without the role silently drops every
grant, leaving a database whose worker cannot write. So slice 2 must add
`mldb_<ref>_memwriter` to `provisioning.TenantNames`, create it on a move's
target in `tenant_movement.prepare_target_roles` (conditional on the project
having memory enabled, as `create_vectors_role` is on vectors), and — since
`restore.missing_roles` today checks only admin/auth/storage/authenticator — add
it there so a restore refuses before the load rather than diagnosing after.

## Where this meets or corrects slice 0

- Consistent with slice 0 ¶1d/¶4: the guard is `session_user`-based and on every
  pipeline path. Slice 0 measured that a *request-path* role (authenticator →
  service_role) cannot pass it without `CREATE` on the space; slice 1 measures
  that a *platform writer login* granted `CREATE` passes it and nothing about
  that grant leaks — which is the posture ADR-079 chose (writes off the request
  path, run by the platform).
- Consistent with slice 0 ¶5b: the model-worker *drain* step needs direct table
  grants and runs over the node connection; it is distinct from the writer's
  facade path, which needs none. Decision 6's "writer login" and slice 0's
  "narrow worker role" are two roles for two steps, not one.
- Correction to decision 6's wording: the extension's *executor rights* are not
  required; per-object `EXECUTE` on the space facades is sufficient and narrower.

## Reproducing

```bash
set -a; . ./.dev/test.env; set +a
scripts/spike-memory-pipeline.py writer          # slice 1, ~40 s, self-cleaning
scripts/spike-memory-pipeline.py writer --keep   # leave both tenants and the role behind
```

It provisions `mws00001` (spaces `space_a`, `space_b`) and `mws00002`, builds the
writer with the minimal grant set, and reports Q1a (guard needs `CREATE`), Q1b
(end to end), Q2 (reach), Q3 (escalation) and Q4 (a real dump/restore round trip
into two scratch databases it drops). Point it only at a disposable node.

---

# Memory slice 2c — deleting a space

Measured 2026-09-15 on maludb_core 0.105.0, node superuser connection, one tenant
(`mds00001`) with two spaces: `space_keep` and `space_gone`. Both were filled
through every write path the platform uses or could use:
- embedded edges (`ingest_edge`);
- rich extraction (`ingest_extraction`);
- a registered model provider, alias and config;
- extraction requests answered by the stub worker and harvested;
- entity-card embeddings completed.

Then `space_gone` was deleted. Numbers below are from the larger run (3,000 edges,
200 extractions, 20 requests per space); a 200-edge run agreed on every property.

## Summary

**A complete, verifiable deletion is cheap and does not disturb the space beside it.**
It is a single transaction, with the steps in this order:
1. `DROP SCHEMA <space> CASCADE`.
2. `DELETE … WHERE owner_schema = <space>` on every `maludb_core` table carrying
   `owner_schema`, in foreign-key order.
3. The space's row in `malu$enabled_schema`, whose objects cascade.

It took 1.1 s. Afterwards nothing in the database or a data dump names the space,
the other space's search results are identical, and a space re-created under the
same name starts empty. Upstream still has no teardown; the platform owns this
sequence, as it owns the build.

## 1 — what a space writes

| Check | Result |
|---|---|
| `maludb_core` tables carrying `owner_schema` | 112 |
| Tables the second space wrote to | 20 |
| …of those, without `owner_schema` | **`malu$vector_chunk` only** (+3,040 rows), keyed by `compartment_id`, and `ON DELETE CASCADE` from `malu$vector_compartment` |
| Rows naming the space before deletion | 2,570 |
| Other columns that can name a schema | `malu$enabled_schema.schema_name`, `malu$enabled_schema_object.schema_name`, `malu$object_grant.granted_by_schema` / `granted_to_schema`, `malu$payload_schema.schema_name`, `malu$skill_package.source_owner_schema` |
| Foreign keys inside `maludb_core` | 230; the ones on the delete path mix `cascade`, `set null`, `restrict` and `no action` |
| `DELETE` triggers on the touched tables | 5: `_episode_subject_cleanup` and four `_embedding_dirty_*_tg` |

`enable_memory_schema` itself writes rows keyed to the space before anything is
ingested: 8 attribute templates, 10 document types, 10 episode types, 6 verbs and
6 embedding-queue rows, plus 1 `malu$enabled_schema` row with 158
`malu$enabled_schema_object` rows. A deletion that removed only ingested data would
leave these behind.

## 2 — the delete

| Step | Result |
|---|---|
| `DROP SCHEMA … CASCADE` | 0.09 s |
| Ordered delete by `owner_schema` | **1.00 s**; 2,171 rows from 20 tables, plus 3,040 chunks by cascade |
| `malu$enabled_schema` by `schema_name` | 1 row; its 158 objects cascade |
| Embedding-queue rows the `DELETE` triggers added | **0 net**: a trigger's row for a deleted object is keyed to the same space and removed in the same pass |
| Order | Found by retrying any table a foreign key refused. The `restrict` edges (`svpor_statement → svpor_verb`, `vector_compartment → vector_subject` / `vector_verb`, `skill_file` / `verbatim_archive → source_package`) are what force an order; `model_alias` and `model_provider` come last. |

## 3 — the space beside it

| Check | Result |
|---|---|
| `space_keep` search while the delete was uncommitted | ran in 0.02 s, not blocked (`lock_timeout` 3 s) |
| `space_keep` ingest while the delete was uncommitted | ran in 0.02 s, not blocked |
| `space_keep` search after commit | identical to before the second space was filled |
| Row counts after, against before the second space was filled | differ only by the second space's enablement rows (removed, as above) and the one row the concurrent ingest wrote |

The spaces share tables but not rows, so the delete takes row locks on the deleted
space's rows only.

## 4 — verification

| Check | Result |
|---|---|
| Rows naming the space, in `owner_schema` or any schema-name column | **none** |
| Rows in any `owner_schema` table keyed to a schema that no longer exists | **none** |
| Embedding queue, by owner | only `space_keep` |
| Schema | gone |
| Data-only `pg_dump --schema=maludb_core`, lines mentioning the deleted space's marker | **0** |
| Re-created under the same name | search returns 0 rows; the only rows naming it are a fresh enablement's own |

## What a build must also do

The measurement answers the tenant-database half. Deleting a space as a product
operation needs the rest:

- **Run as the provisioner** (ADR-038). `DROP SCHEMA` on superuser-owned facades and
  `DELETE` on `maludb_core` tables need the node superuser, as the build does.
- **Stop writes first.** Mark the space `deleting` in the control plane in the same
  transaction that queues the job. The gateway then refuses ingests and searches for
  it, and pending ingests fail with a reason, before the job runs. A worker
  transaction still open against the schema only delays `DROP SCHEMA`, which waits
  for it.
- **Derive the table list at run time**, from the catalogue (`owner_schema` columns
  and the schema-name columns above), rather than from a constant. A release that
  adds a keyed table is then covered. Assert "no rows name the space" before commit,
  the way the build asserts closure, and re-run the deletion test on every pinned
  version (ADR-075), since the foreign-key order and triggers are upstream's.
- **Include `malu$object_grant`.** It was empty here because sharing is not offered,
  but a grant to or from the deleted space must go with it.
- **The platform's own rows:**
  - `maludb_private.memory_space_registry`;
  - the writer's and reader's grants, which `DROP SCHEMA` removes;
  - the control-plane `memory_spaces` row, whose ingests cascade, and which releases
    the plan slot;
  - an audit event.
- **Moves and restores:** take the node's advisory lock as the build does, so a
  deletion never interleaves with a move of the same tenant.
- **Backups still hold the data** until they age out. Customer documentation says
  so, as it does for project deletion.

## Not measured

- **Deletion at production scale.** The 1.0 s here is dominated by 112 table scans,
  not by row count: 200 edges took 0.58 s. A space at the production ceiling
  (1,000,000 memories) is a delete of millions of chunk rows by cascade. Measure it
  before offering large spaces; batching by compartment is the likely remedy.
- A deletion concurrent with an extension upgrade or a move (the lock should
  serialise them; not demonstrated).

## Reproducing

```bash
set -a; . ./.dev/test.env; set +a
scripts/spike-memory-pipeline.py delete --dump                                        # ~1 min
scripts/spike-memory-pipeline.py delete --dump --edges 3000 --extractions 200 --requests 20
```

It provisions `mds00001`, fills two spaces, deletes one, and drops the tenant
afterwards unless `--keep`. `--dump` needs `pg_dump` on the path. Point it only at a
disposable node.
