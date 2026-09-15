# Execution Plan: memory spaces (ADR-079)

Status: IN PROGRESS — slice 0 (measurement) done 2026-09-14; ADR-079 accepted. The
vector wrapper fence (a prerequisite) is its own pull request.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/memory-pipeline-spike` for slice 0 and the ADR; one branch per slice after
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md`
Dependencies: ADR-074 (wrappers, platform-run refresh), ADR-077 (definer pattern; fenced),
ADR-078 (pipeline data survives moves and restores), ADR-023 (KEK), ADR-038 (worker split)

**Slices are numbered "memory slice N".**

## Objective

A customer sends text to a named memory space with their secret key and, using their
own provider keys, gets searchable memory back — with no superuser-owned code reachable
from a request, per-item results for every write, and per-plan limits enforced before
work is queued.

## Scope

- Memory spaces per project: create, list, delete; opt-in per project; plan limits.
- Provider keys per project: store (write-only), rotate, delete; three fixed providers.
- Ingest of raw text → extraction → embeddings → harvest → searchable, run by a dedicated
  memory worker as a per-project writer.
- Direct ingest of already-embedded edges (the path slice 0 measured).
- Search through a narrow reader wrapper.
- Moves, restores and extension upgrades with spaces present.

## Non-goals

- Space-scoped keys, end-user (JWT) access, cross-space sharing (ADR-079 decision 2).
- Platform-paid models; customer-supplied endpoints (decisions 4, 5).
- The knowledge graph and bitemporal surfaces beyond what search and harvest need.
- MaluDB accounts (decision 1).

## Preconditions

- The ADR-077 fence merged: vector wrappers filter `owner_schema = 'maludb_core'` and
  search by compartment id.
- maludb_core 0.105.x pinned (0.105.1 once maludb-core#32 merges, so embeddings survive a
  dump exactly).

## Implementation steps

### Memory slice 0 — Measure (done 2026-09-14)

`specs/maludb-memory-pipeline-model.md`, `scripts/spike-memory-pipeline.py`.

### Memory slice 1 — The writer role, measured before anything depends on it (done 2026-09-14)

- A per-project `LOGIN` writer with `CREATE` on its spaces and `CONNECT` on its own database
  only (ADR-014): does it pass `maludb_memory_ingest_edge`, `request_extraction` and
  `harvest_extractions`? Which extension role or grants does it need to `EXECUTE` them,
  and what else do those grants reach — other spaces in the database, other databases on
  the cluster (the `maludb_*` roles are cluster-wide)?
- If it cannot be made narrow, stop and reopen ADR-079 decision 6.

**As built/measured** (`specs/maludb-memory-pipeline-model.md` § "Memory slice 1",
`scripts/spike-memory-pipeline.py writer`): decision 6 **holds as written, with one
correction and one addition.** The minimal working set is a `LOGIN` writer
(`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT`), `CONNECT` on
its own database only, `USAGE` on `maludb_core`, `USAGE` + `CREATE` on each space, and
per-object `EXECUTE` on that space's facades — measured end to end (ingest → request →
drain → harvest → search). Correction: the extension's *executor rights are not needed* —
`maludb_memory_executor` membership (cluster-wide, `NOINHERIT`, holding EXECUTE on
auth/secret functions) is strictly wider than the per-object grants and unnecessary; the
guard reads `session_user`, so `SET ROLE` on the node connection would not satisfy it.
Reach is bounded to the one database, the granted space(s), and `maludb_core` only through
the granted facades (0 PUBLIC-executable functions). No escalation found through `CREATE`
on the space (definer paths keep the space off `search_path`; the writer owns nothing it
can trigger/replace/drop) — one latent invariant flagged for slice 2's per-upgrade check:
assert every space-first `SECURITY DEFINER` function references only qualified objects.
Addition: the writer is a new per-project role; slice 2 must add it to `TenantNames`,
create it on a move's target (`prepare_target_roles`) and in `restore.missing_roles` —
a dump/restore round trip confirmed its grants are silently dropped when the role is
absent on the target, exactly like `create_vectors_role` (ADR-077).

### Memory slice 2 — Spaces

Split into three, each reviewable on its own.

#### Memory slice 2a — Create and list spaces (built 2026-09-14)

**As built:**
- `memory_spaces` (migration 0041): the name is reserved as `pending` when asked for,
  under the project's row lock, so `memory_max_spaces` holds under concurrency. The
  schema is derived as `mem_<name>` from a name matching `^[a-z][a-z0-9_]{0,39}$`,
  enforced by the queue (422 in words) and by a CHECK, and is never returned.
- Entitlements `maludb_memory`, `memory_max_spaces`, `memory_max_items`,
  `memory_ingests_per_hour`, owner-confirmed: 1/10k/60, 3/100k/1k, 10/1M/10k.
- `POST /v1/projects/{ref}/maludb/memory/spaces` (manager, 202) and `GET` (member).
- One `memory_spaces` job builds every pending space (`maludb_memory.build_pending`),
  each in its own tenant transaction:
  1. `maludb_core` must be at least 0.105.0 (ADR-078), or the space is refused;
  2. the project's vector wrappers are re-verified, which installs the #146 fence;
  3. a squatted schema is refused;
  4. the platform-owned schema is created and `enable_memory_schema` run;
  5. no customer role may use the schema or execute anything in it, or the
     transaction rolls back.

  A refused space is `failed` with the platform's own sentence, and may be asked for again.
- Nothing is reachable by customers yet; search is slice 3 and ingest slice 5.

#### Memory slice 2b — The writer role, moves, restores and upgrades (built 2026-09-14)

**As built:**
- **The writer.** `TenantNames.memwriter` (`mldb_<ref>_memwriter`) is a login created with
  the project's first space. It holds `CONNECT` on its own database, `USAGE` on
  `maludb_core`, and `USAGE`+`CREATE` plus per-object `EXECUTE` on the seven write facades
  of each space. Its password is sealed as `db_memwriter` after the tenant commit; a run
  that dies before storing it resets the role's password on the next build.
- **Moves.**
  - The writer is on the freeze list.
  - `prepare_target_roles` creates it on the target from the vault before the load.
  - `finish_target_database` grants it `CONNECT`.
  - `roles_refusal` requires it when the project has a built space. Without it the move
    is refused before the freeze, which a test demonstrates.
  - End to end, the writer logs in to the moved database with its stored password and
    ingests beside the memory it wrote before the move.
- **Upgrades.** `extension_upgrade` re-enables every platform-owned space for `maludb_core`,
  re-asserts closure to customer roles, and re-grants the writer. A customer-owned `mem_`
  schema is left alone.
- **The definer check** from slice 1 runs on every build and upgrade:
  - a definer in a space with no pinned `search_path` is refused;
  - a definer that searches the space and is not in `REVIEWED_SPACE_FIRST_DEFINERS` is
    refused. At 0.105.0 that list is exactly `maludb_document_graph_backfill`.
- **Restores** need nothing new: a per-tenant restore loads onto the same cluster, where
  the writer already exists.


- Per slice 1: a per-project writer login with `CONNECT` on its own database only,
  `USAGE` on `maludb_core`, and `USAGE` and `CREATE` on each of its spaces, plus
  per-object `EXECUTE` on each space's facades. Its password lives in the vault; it
  joins `TenantNames`.
- `tenant_movement.prepare_target_roles` and `restore.missing_roles` create and
  require it before the load (measured: without it every grant is dropped).
- Move, restore and extension upgrade with spaces present: objects and data arrive,
  ownership verified. This extends the end-to-end move test.
- The upgrade check from slice 1: every definer with a space on its path references
  only qualified objects.

#### Memory slice 2c — Deleting a space (measured and built 2026-09-15)

Measured first (`specs/maludb-memory-pipeline-model.md`, "Memory slice 2c", PR #160).

**As built:**
- **`DELETE /v1/projects/{ref}/maludb/memory/spaces/{name}`** is manager-only and answers 202.
  - Under ingest admission's lock, the space is marked `deleting` (migration 0047) and its
    pending ingests fail with a reason.
  - The gateway admits, and the worker claims, only ingests for `active` spaces.
  - A space being deleted keeps its name and slot, and its models can't change.
  - A space that never built is just removed.
- **Budget.** Space jobs now count against the hourly MaluDB node-work budget, because
  deletion made a create-delete cycle repeatable superuser work. Only creation is refused
  for it; deleting one's own data is never rationed.
- **Blast radius.** `DROP SCHEMA … CASCADE` is refused when anything outside the space depends
  on it, since CASCADE would drop that too.
- **The job.** The `memory_spaces` job deletes before it builds (`maludb_memory.delete_marked`),
  one tenant transaction per space, under the node lock, in any project state that can be
  disabled and without the entitlement. The steps:
  - drop the registry row;
  - `DROP SCHEMA … CASCADE`;
  - delete keyed rows by `owner_schema`, with the tables read from the catalogue at run time
    and the foreign-key order found by retry;
  - delete object grants either way, and the `malu$enabled_schema` row.
- **Residue check.** `maludb_memory.residue` must be empty before commit: the schema, every
  keyed table, and every `name` column except the reviewed provenance one. A column nobody
  has reviewed that still holds the name refuses the deletion.
- **A schema a customer owns is never dropped.**
- **After the tenant commits:**
  - the control-plane row goes, and its ingests cascade, which frees the slot;
  - `maludb_memory_enabled` and the `maludb` exposure are withdrawn when no space remains
    and no other feature needs them;
  - a `maludb.memory.space_deleted` audit event records the row count.
- **Re-running is harmless:** a deleted space passes its residue check with nothing to delete.

**Still to measure:** deletion at the production ceiling (a cascade of millions of chunk
rows). Until then large spaces delete in one transaction of unmeasured length.

### Memory slice 3 — Search (built 2026-09-14)

**As built:**
- **`maludb.memory_search(space, query, subject, verb, namespace, match_count, metric)`**,
  `SECURITY DEFINER`, owned by `mldb_<ref>_memreader` (NOLOGIN, new in `TenantNames`),
  `EXECUTE` to `service_role` only.
  - The space resolves through the platform-owned `maludb_private.memory_space_registry`:
    an unknown name answers `PT404`, and results are fenced to that space's `owner_schema`.
  - It requires a subject or verb, which bounds one search's cost. The facade also allows
    neither.
- **The reader's grants** are derived from the installed extension (`derive_reach`, now
  parameterised), starting at `exact_vector_search_sql` plus the four tables the wrapper
  reads.
  - **SELECT only.** The body walk also reports writes from an ANN-build branch that exact
    search never takes; those are deliberately not granted.
  - `exercise_reader` proves a real search with exactly these grants.
- **Parity:** the wrapper matches the facade exactly on four queries over 24 memories. A
  search of one space never returns another space's memories.
- **Publishing:** building a space publishes `maludb` on the project's Data API and sets
  `projects.maludb_memory_enabled` (migration 0042).
  - The gateway admits the schema for it.
  - Disabling the data-model graph or vectors no longer withdraws `maludb` while memory
    spaces use it.
- **Moves:** the reader is created on the target before the load. End to end, search on
  the target finds memory from before and after the move, and the wrapper stays owned by
  the reader.
- **Upgrades** re-derive the reader's grants, reinstall the wrapper and re-exercise it.

**Not yet customer-useful on its own:** nothing customers can call writes memories until
slice 5 (ingest), so the feature is not documented in `docs/MALUDB-FEATURES.md` yet.


- `maludb.memory_search(space, query, ...)` owned by a per-project reader, grants derived
  from the installed extension; parity test against the facade on the pinned version.
- `anon`/`authenticated` refused; unknown space 404; another space's rows never returned.

### Memory slice 4 — Provider keys (built 2026-09-14)

**As built:**
- `project_provider_keys` (migration 0043) holds keys sealed under the KEK with AAD binding
  each to its project and provider. There is one live key per provider; replacing one
  revokes the old row. Keys cascade with the project. No customer-facing project deletion
  exists yet, so that path is the cascade alone.
- `PUT` (manager), `GET` (member, metadata only) and `DELETE` (manager) at
  `/v1/projects/{ref}/maludb/memory/provider-keys[/{provider}]`.
  - The key is never returned, logged or audited in full.
  - Validation is shape-only, with no call to the provider from the public app.
- **The gateway role:** `gateway grant` revokes the table from it, and `deploy preflight`
  fails a gateway role that can read it.
- Audit events for key set and removal, and for space creation, are now customer-visible
  with the provider and hint.
- `provider_keys.load_key` is ready for the worker.
- **Not built:** the dashboard form. It lands with the memory panel once ingest (slice 5)
  makes memory usable.


- Per-project secret type; write-only API; KEK-encrypted; never logged; deleted with the
  project; dashboard form.

### Memory slice 5 — Ingest and the worker

**Decided 2026-09-14 by the owner:** ingest is called with the project's **secret key through
the gateway**, the same credential an agent searches with, not a person's access token on the
control plane. Split into three.

#### Memory slice 5a — Embedded edges, the queue and the worker (built 2026-09-14)

**As built:**
- **The gateway** answers `POST /memory/v1/spaces/{space}/ingest` and
  `GET /memory/v1/ingests/{id}` itself. There is no worker behind it.
  - It requires the secret key (403 otherwise) and a project with memory on (404 otherwise,
    saying how to create a space).
  - It applies the request-rate limiter, then `memory_ingest.enqueue`.
- **Admission** (`memory_ingest`), under a per-project advisory lock:
  - items are validated (up to 100; subject, verb, text, finite embedding);
  - `memory_ingests_per_hour` answers 429 with `Retry-After`;
  - `memory_max_items` counts stored plus queued items and answers 409 without naming the
    ceiling;
  - status is visible to its own project only.
- **`memory_ingests`** (migration 0044), gateway own-node policy.
  - `items_json` is customer content, so a CHECK allows it only while the request is
    pending or running; the worker clears it and keeps per-item results.
  - `memory_spaces.item_count` is kept by the worker.
- **The memory worker** (`memory_worker`, `deploy/maludb-memory-worker.service`, its own user
  and environment file):
  - it connects **as the project's memory writer** at the node's internal host, never with a
    node admin credential;
  - each item gets its own transaction (document plus edge) and a result: the statement it
    became, or why not;
  - requests end `succeeded`, `partial` or `failed`, and a paused project's ingest is failed
    without connecting;
  - the unit denies all IP egress except private ranges and localhost, asserted by
    `tests/test_deploy_units.py`.

#### Memory slice 5b — Raw text with the customer's provider keys (built 2026-09-14)

Decided first (ADR-079, "Decisions 3–6, as decided for memory slice 5b"): the worker calls the
models and writes through `ingest_edge`, not upstream's harvest or `ingest_extraction`; egress
goes through a platform CONNECT proxy; model names are free-form per space.

**As built:**
- **Space models** (migration 0045, `maludb_jobs.set_memory_models`,
  `PUT /v1/projects/{ref}/maludb/memory/spaces/{name}/models`, manager-only, audited). Providers
  come from fixed lists, and model names are shape-checked with defaults. There is no endpoint
  column. The embedding model is fixed once the space holds memories or has ingests queued.
- **Text ingests** (`memory_ingest`, gateway):
  - up to 20 `{text, title}` items, never mixed with edges;
  - 409 until the space names its models.
- **Provider calls** (`model_providers`):
  - raw `httpx` to three constant hosts, with `trust_env=False` and no redirects;
  - Anthropic structured outputs and OpenAI JSON mode;
  - retries on 429 and 5xx, honouring `Retry-After` (capped at 30 s);
  - keys scrubbed from provider errors;
  - each edge checked, at most 50 per text.
- **Worker** (`memory_worker.write_text_items`):
  - extract, then embed, then document and edges in one transaction, with a savepoint per edge;
  - a result per item listing stored and skipped edges;
  - fatal provider errors (key, billing, bad model, exhausted limits) stop the ingest;
  - `memory_max_items` held again after extraction;
  - a heartbeat keeps a slow ingest from being reaped;
  - refuses to start in production without `MALUDB_MEMORY_EGRESS_PROXY`.
- **Egress** (`egress_proxy`, `deploy/maludb-egress-proxy.service`):
  - `CONNECT` to the three hosts on 443 only;
  - resolves the name itself, refuses any non-public address, and dials the address it checked;
  - loopback listener only, its own user, no environment file, private ranges denied in the unit.

**Not done here:** embedding a *query* for the customer (search still takes a vector), and a
live call against the real providers. Both are for slice 6.

#### Memory slice 5c — The worker's own control-plane role (built 2026-09-15)

**As built:**
- **An allowlist, not the gateway's denylist** (`memory_worker_grants`). Column grants cover
  exactly the worker's reads (projects, nodes, plans, encryption_keys, credentials, provider
  keys, spaces, ingests) and its writes (the ingest's progress columns and `item_count`). It
  gets no `INSERT` or `DELETE` anywhere.
- **Row policies** (migration 0046, `memory_worker_reach`) are keyed on membership of
  `cp_memory_worker` through `public.is_memory_worker()`:
  - only `db_memwriter` credentials;
  - only live provider keys;
  - no row of any other table with row security.

  Membership rather than a mapping table, because the gateway's denylist can write any table
  and could add itself to one.
- **One role can't be both.** A role that is a gateway and a memory worker would read its
  node's tenant credentials. `cp-manage gateway grant`, `cp-manage memory-worker grant`,
  `deploy preflight` and the worker's startup all refuse it.
- **Startup check.** `memory_worker.assert_narrowed` refuses in production when the role is
  wider, or not a member. Preflight also names any granted column that is missing.
- **Found while testing as the role:** the gateway's own-node policies name no role, so they
  run for the worker too and read `nodes.gateway_role`. Without that column granted, every
  such table answered "permission denied".
- `tests/test_memory_worker_grants.py` runs the worker end to end as the role.

### Memory slice 6a — Search by text (built 2026-09-15)

Decided first (ADR-079, "Search by text, decided 2026-09-15"): an internal embedder on the
control-plane host, authenticated with the customer's own secret key.

**As built:**
- **`memory_embedder`** (`deploy/maludb-memory-embedder.service`, its own user and environment
  file) serves `POST /internal/memory/embed`:
  - it verifies the secret key against the named project with `api_keys.authenticate`;
  - it refuses a publishable key, an unknown space, a space without an embedding model, and a
    missing provider key, in words;
  - it embeds through the egress proxy and never logs the query or the key;
  - it binds only a loopback or private address.
- **`cp_memory_embedder`** (migration 0048, `memory_worker_grants.EMBEDDER_READS`): key hashes
  (never a publishable key's ciphertext), live provider keys, projects, and active spaces'
  models. It holds no writer credential and no ingest.
  - `memory-worker grant` applies both models, and `deploy preflight` checks it.
  - A gateway role can't be a member.
  - The embedder refuses production when its role is wider.
- **Gateway `POST /memory/v1/spaces/{space}/search`:**
  - it validates before paying, then embeds via the embedder with the caller's key;
  - it calls `maludb.memory_search` on PostgREST as `service_role`;
  - it passes the embedder's refusals on, and answers 503 without an embedder.
- `MALUDB_MEMORY_EMBEDDER_URL` must be http(s) to a private or loopback address literal.
  `0.0.0.0` and `::` are refused as a URL and as a bind, since Python counts them as private.

Verified end to end by slice 6's official-client test: a real embedder with a stubbed
provider, the real gateway and real PostgREST.

### Memory slice 6 — Compatibility, docs, launch

**Built 2026-09-15:**
- **Official-client test** (`tests/test_compatibility.py::memory_compat`, `tests/compat/memory.mjs`),
  on the compatibility project. Only the model provider behind the embedder is stubbed.
  1. Before a space exists, search is refused by name and ingest says how to create a space.
  2. Through the platform's routes: create a space (the real provisioner builds it), name its
     models, set a provider key.
  3. Ingest through the gateway with the secret key; a publishable key is refused. The real
     memory worker writes as the memory writer.
  4. Through `supabase.schema('maludb').rpc('memory_search')`:
     - the ingest's status is read back;
     - search by subject and by verb;
     - PT404 and PT400;
     - anon and a signed-in user refused with `42501`;
     - search by text through the real embedder; a publishable key refused;
     - `public` unchanged.
  5. Delete the space; memory is withdrawn and the client is refused by name again.
- **Found by it:** the reader check put its role-existence test and `has_function_privilege`
  in one `AND`, which PostgreSQL may evaluate in either order. A tenant missing a customer role
  failed to build a space with `role … does not exist`. Now a `CASE`.
- **`docs/MALUDB-FEATURES.md` "Memory spaces"** and the `memory_spaces` rows of
  `specs/compatibility-matrix.yaml`.

**Still open:**
- **Plan limits confirmed by the owner.** The docs show the configured values (1/10k/60,
  3/100k/1,000, 10/1M/10,000).
- **Default model names confirmed by the owner.**
- **Production-scale deletion measurement** (2c).
- **Dashboard memory panel.**

## Verification

- [x] Slice 0 measurements recorded with reproduction.
- [x] Writer role passes the facades narrowly (measured slice 1; decision 6 holds, executor
      rights not needed — per-object `EXECUTE` on the space facades is enough).
- [x] **No superuser-owned function reachable from any request role.** Checked across every
      memory schema (`maludb`, `maludb_private`, each `mem_*`) for every customer role:
      `test_memory_verification.py::test_no_request_role_can_execute_a_superuser_owned_definer_in_any_memory_schema`.
      Each space is also closed at build (`test_memory_spaces.py`), and the worker's role is
      narrowed in the control plane (`test_memory_worker_grants.py`).
- [x] **Space isolation.**
  - Search is fenced to its space (`test_search_is_fenced_to_its_space_…`).
  - An ingest into one space leaves another's rows identical, although the writer holds
    `CREATE` on both (`test_an_ingest_into_one_space_changes_nothing_in_the_other`).
  - An ingest naming another project's space is never claimed.
  - Deleting a space leaves its neighbour's search identical (`test_memory_space_deletion.py`).
- [x] **Provider keys never in clear.** The key is set through the route, used by the worker,
      and refused by a provider that echoes it back. Afterwards no row of any control-plane
      table holds it (the scan is shown to find a planted key), no log line does, and no
      response does; the stored result carries `[key]`
      (`test_a_provider_key_set_used_and_refused_is_nowhere_in_the_control_plane_in_clear`).
      Stored ciphertext and provider errors are covered in `test_provider_keys.py` and
      `test_model_providers.py`.
- [x] **Worker egress limited to three hosts, asserted.**
  - The proxy refuses every other host, port, verb, address literal and any private
    resolution (`test_egress_proxy.py`).
  - Both units, and the worker's `IPAddressDeny`, are asserted (`test_deploy_units.py`).
  - The worker refuses to start in production without a loopback proxy
    (`test_the_worker_refuses_production_without_the_egress_proxy_on_loopback`).
- [x] **Move, restore and upgrade with spaces verified end to end.**
  - Move: `test_tenant_movement.py::test_a_tenant_with_a_memory_space_moves_and_its_writer_writes_on_the_target`.
  - Upgrade: `test_extension_upgrade.py::test_a_platform_owned_memory_schema_is_re_enabled` and
    `test_memory_spaces.py::test_an_upgrade_re_verifies_every_space_and_re_grants_the_writer`.
  - Restore: `test_restore.py::test_a_tenant_is_recovered_to_a_point_in_time_…` now builds a
    space and writes a memory before and after the target. Only the first comes back through
    the search wrapper, with the schema superuser-owned, the writer's grants intact and the
    wrapper owned by the reader. Measured locally 2026-09-15.
- [x] **Every write reports skipped items.** 5a has a result per item; 5b a result per edge,
      including edges over 50 and edges the pipeline declines (`test_memory_ingest.py`).
- [x] **Deleting a space leaves nothing behind** (added with 2c): the schema, every keyed row
      and every schema-name column, asserted before commit and again by the test.

## Risks

- **The writer role needs a cluster-wide `maludb_*` grant.** Would widen reach across every
  database on the node; slice 1 measures it before anything is built on it.
- **Search parity drifts on an upgrade.** The wrapper re-implements upstream's query;
  mitigation: parity test in the ADR-075 tested-versions gate.
- **Silent skips.** Upstream succeeds while dropping items; mitigation: per-item results
  compared against what was sent.
- **Queue latency** unmeasured; agents that write then search may not see their write yet.
  Measure in slice 5 and publish it.
- **Egress from the control-plane host** is new; restrict in deployment, not just code.

## Decision log

- 2026-09-14 — ADR-079 accepted: memory spaces; secret key only; wrapper reads, platform
  writes; platform calls models with the customer's keys (revised from customer-supplied
  outputs the same day); OpenAI/Anthropic/Voyage fixed hosts; dedicated worker as a
  per-project writer; every plan with tiered limits.

## Progress log

- 2026-09-14 — Memory slice 5a built: gateway ingest with the secret key, admission against the
  plan, the memory worker writing as each project's writer with per-item results. 5b (provider
  extraction and egress) and 5c (the worker's control-plane role) planned.

- 2026-09-14 — Memory slice 4 built: provider keys, sealed and write-only, out of the
  gateway's reach and checked by preflight.

- 2026-09-14 — Memory slice 3 built: search through a SELECT-only reader wrapper, in
  parity with the facade, fenced per space, published on the Data API, carried by moves
  and re-verified on upgrades.

- 2026-09-14 — Memory slice 2b built: the writer role, carried through moves (demonstrated
  end to end on two clusters) and upgrades, with the space-first definer check enforced.

- 2026-09-14 — Memory slice 2a built: spaces are created and listed within the plan's
  limits (owner-confirmed numbers). Slice 2 split into 2a/2b/2c; deletion is measured
  before it is built.

- 2026-09-14 — Slice 0 measured (spec above). Found ADR-077's vector wrappers unfenced;
  fix raised as a separate pull request before any space can exist.
- 2026-09-14 — Slice 1 measured (`writer` subcommand, spec § "Memory slice 1"). Decision 6
  holds: a narrow per-project writer login runs the whole pipeline through the space facades
  on their definer rights, needing only `CONNECT` (own db), `USAGE` (`maludb_core`),
  `USAGE`+`CREATE` (its spaces) and per-object `EXECUTE` on the space facades — not the
  extension's executor rights. Reach bounded to its own db and granted spaces; no
  escalation through `CREATE`. Corrections carried into slice 2: (1) provision the writer
  role on move/restore targets like `create_vectors_role`; (2) add a per-upgrade assertion
  that space-first `SECURITY DEFINER` functions reference only qualified objects.
- 2026-09-14 — Memory slice 5b built: text ingests extracted and embedded with the customer's
  keys through a three-host egress proxy; three design questions answered and recorded in
  ADR-079 first.
- 2026-09-15 — Memory slice 5c built: the memory worker's own control-plane role, an allowlist
  with membership-keyed row policies, refused in production when wider.
- 2026-09-15 — Memory slice 2c measured: deleting a space is one transaction of about a
  second, complete by catalogue and dump, invisible to the space beside it; build and a
  production-scale measurement remain.
- 2026-09-15 — Memory slice 2c built: space deletion through the provisioner, writes stopped
  first, residue asserted before commit, neighbour untouched, name reusable.
- 2026-09-15 — Verification checklist closed. New assertions: no superuser definer reachable in
  any memory schema, ingest isolation between spaces, no provider key in clear anywhere in the
  control plane, the worker's proxy check, and restore with a memory space end to end.
- 2026-09-15 — Memory slice 6a built: search by text, the query embedded on the control-plane
  host with the customer's own keys, authenticated by the customer's secret key.
- 2026-09-15 — Memory slice 6 official-client test and customer docs: memory spaces end to end
  through supabase-js, the gateway, PostgREST, the worker and the embedder; one ordering bug in
  the reader check found and fixed.
