# Execution Plan: Vector compartments (ADR-077)

Status: IN PROGRESS — compartments slices 0–1 built 2026-09-13; slice 2 (wrappers and limits) is next.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/adr-077-vector-compartments`, then one branch per slice
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md`
Dependencies: ADR-075 (pinning) and ADR-076 (extension function grants), both
complete; ADR-074's opt-in machinery (`maludb.py`, `maludb_jobs`), merged.

**Slices are numbered "compartments slice N"**, so they are not confused with the
data-model graph's, the pinning plan's or the grants plan's.

## Objective

Make one sentence true that is currently false:

> A project that enables vector compartments can create one, insert embeddings and
> search it from its own server through `supabase.schema('maludb').rpc(...)`,
> within its plan's limits, and still has every vector after the platform moves
> or restores it.

## What ADR-077 decided, so this plan does not relitigate it

1. MaluDB's vector compartments, not pgvector parity or the retrieval planner.
2. `service_role` only.
3. Platform-owned `SECURITY DEFINER` wrappers in `maludb`, owned by a narrow
   per-tenant platform role — never the superuser.
4. Every plan; maximum vectors, dimension and compartments per plan, enforced by
   the wrappers.
5. Exact search first; ANN only after it is measured.
6. A per-feature opt-in; `maludb` served while any MaluDB feature is on.
7. Embeddings as pgvector `vector`.
8. Nothing ships until a move and a per-tenant restore carry compartments.

## What is already true

- `maludb.py` enables the data-model graph: squat refusals, a platform-owned
  `maludb` schema, `pgrst.db_schemas` on the authenticator, `maludb_jobs` for
  customer requests, and `disable` withdrawing it. It exposes `maludb` only while
  `maludb_datamodel_enabled` — decision 6 turns that into "any feature".
- The gateway answers "the MaluDB data-model graph is not enabled for this
  project" (`services/gateway/app.py`).
- `entitlements` carries `maludb_datamodel` and `datamodel_refreshes_per_hour` per
  plan; `specs/plans-and-limits.yaml` seeds them.
- `extension_upgrade.upgrade_tenant` verifies an enabled tenant inside the
  transaction (ADR-074 decision 5).
- Moves (`tenant_movement.dump_from_source` → `restore.load_into_target`) and
  per-tenant restores (`restore.dump_from_scratch` → `load_into_target`) are both
  `pg_dump`, which carries **no** `maludb_core` table data (ADR-077 context).

## What is not known, and is why compartments slice 0 comes first

- **The definer role's exact grants.** The wrappers write
  `malu$vector_compartment`, `_chunk`, `_subject`, `_verb`, probably
  `_tombstone`, `_index_status` and their sequences, and read through
  `exact_vector_search_sql`. What a non-superuser owner needs, and whether any
  upstream function assumes a superuser or a MaluDB role, is unmeasured.
- **`owner_schema` under a definer.** It defaults to `current_schema()`. With the
  wrapper's `search_path` pinned, every compartment lands in one owner schema;
  whether upstream functions filter on it when reading is unverified, and it
  decides whether a customer-created schema could see or shadow compartments.
- **Exact search cost** at realistic sizes (1k / 10k / 100k vectors × 384 / 1536
  dimensions), per implementation (`_sql`, `_c`, `_parallel_c`), on the node
  under test. This is what sets decision 4's free-plan numbers.
- **ANN cost** (decision 5): `ann_build` time, peak memory and locks, and
  `maludb_ann_search_c` per-query cost with a graph stored as one `bytea`.
- **How to carry the rows** (decision 8): `COPY` of the tenant's `malu$vector_*`
  rows out of the source and into the target after `pg_dump`'s load, with
  sequences advanced and identifiers kept, versus anything upstream offers.

## Scope

- Measurement, with a reproducing script, as pinning slice 0 did.
- The move and restore fix, before anything customer-facing.
- Enablement, wrappers, limits, gateway and upgrade verification.
- Docs, compatibility matrix, customer-facing feature page.

## Non-goals

- ANN (until slice 0's numbers and a follow-up decision allow it).
- Visibility for `authenticated` or `anon`.
- Platform-generated embeddings, model registry, provider keys.
- The retrieval planner, query hints, the memory pipeline.
- A `/maludb/v1` gateway endpoint.

## Implementation steps

### Compartments slice 0 — Measure, and make moves and restores carry compartments (done 2026-09-13)

**As built**: findings in `specs/vector-compartments-model.md`, reproduced by
`scripts/spike-vector-compartments.py`. `services/control_plane/extension_data.py`
carries the seven vector tables by column list in one transaction, moves their
sequences past what it carried, compares counts before committing, and refuses a
non-empty target, a source column the target lacks, and any row linked through a
foreign key that leaves the carried set (read from the catalogue). A move reads
the source over a connection; a restore reads the scratch cluster through `psql`
as its owner. Both call it after the load and before ownership is verified, so a
move fails before repointing. The stop condition was not met.

- A spike script against real provisioned tenants, recording in
  `specs/vector-compartments-model.md`: the definer role's minimum grants (found by
  starting from none), `owner_schema` behaviour, exact search timings, ANN build and
  query timings with peak memory, and `pg_dump` coverage.
- The carry: move and per-tenant restore copy the tenant's `malu$vector_*` rows
  beside the dump and verify counts, so a tenant with compartments arrives with
  them. Tests move and restore such a tenant and **search** afterwards, with a
  control: removing the carry makes the test fail.
- File the upstream `maludb-core` report: data tables not registered with
  `pg_extension_config_dump`.
- **Stop condition:** if a non-superuser definer cannot run exact search, or the
  carry cannot be made exact, stop and bring it back as an ADR-077 amendment.

### Compartments slice 1 — The definer role and enablement (done 2026-09-13)

**As built**, with three departures from the text below, each for a reason found
while building it:

- **Grants are derived at enable time** from the installed `maludb_core`'s
  function bodies (`maludb_vectors.derive_reach`), starting at the five entry
  points slice 2's wrappers will call and following every function they name,
  every table they read or write, and the `CHECK` constraints, column defaults
  and triggers of those tables. Fenced: table grants only on `malu$vector_*` and
  `malu$ann_*`, and `EXECUTE` only on functions that are not `SECURITY DEFINER` —
  reaching anything else fails enablement. Then the role is **exercised** twelve
  times on one connection inside a rolled-back savepoint. Building it proved the
  exercise necessary twice: an upsert's `ON CONFLICT DO UPDATE` needs `UPDATE`,
  and the chunk table's `CHECK` calls `octet_length`; reading bodies alone found
  neither.
- **The "holds no more" check is `maludb_vectors.assert_definer`**, run on every
  enablement, rather than in `tenant_bootstrap.verify`, which runs for every
  tenant and cannot know whether vectors are on. Slice 3 adds it to the upgrade
  run's per-tenant verification.
- **The gateway's schema check moved here from slice 2**: with exposure keyed to
  any feature, a vectors-only project would otherwise be refused `maludb`.

The customer route through `maludb_jobs` is not in this slice; enablement is
`cp-manage project maludb enable --feature vectors` until slice 2.


- Migration: `maludb_vectors_enabled`, `maludb_vectors_enabled_at`.
- The per-tenant definer role, created at enable time with slice 0's grants;
  carried by moves and restores like the tenant's other roles;
  `tenant_bootstrap.verify` refuses it holding more.
- `maludb.py`: enable/disable for vectors; `maludb` exposed while any feature is
  on; disabling one withdraws only its own objects. `cp-manage` first, then the
  `maludb_jobs` route, as the data-model graph went.

### Compartments slice 2 — Wrappers and limits

- Wrappers in `maludb`: create compartment, insert chunks, exact search, delete
  chunk and compartment, explain. `vector` in, `malu_vector` inside, pinned
  `search_path`, `EXECUTE` to `service_role` only.
- Entitlement `maludb_vectors` and limits `vector_max_count`,
  `vector_max_dimension`, `vector_max_compartments` in `entitlements` and
  `specs/plans-and-limits.yaml`, from slice 0's numbers; wrappers refuse past them
  with a stable error code.
- Gateway: the "not enabled" answer names the feature.

### Compartments slice 3 — Upgrades, docs and compatibility

- `extension_upgrade.upgrade_tenant` calls the wrappers on a vectors-enabled
  tenant inside the transaction.
- Official-client compatibility tests: enable, insert, search as `service_role`;
  refused as `anon` and `authenticated`; refused on a project not enabled; `public`
  OpenAPI unchanged.
- `docs/MALUDB-FEATURES.md`, `specs/compatibility-matrix.yaml`, `docs/MALUDB.md`.

## Verification

- [x] Slice 0 measurements recorded, with a reproducing script.
- [x] A tenant with compartments is restored to a point in time and a search
      returns the chunk written before the target and not the one after
      (`tests/test_restore.py`); the carry between real tenants searches
      identically (`tests/test_extension_data.py`), with the control that the
      same target without it has no compartment. The move's own orchestration
      test asserts the carry's place in the order; there is no end-to-end move
      test in the suite, before or after this slice.
- [x] The definer role holds exactly the vector grants, derived and exercised;
      a grant added by hand is refused on the next enable, and a grant the
      derivation misses fails enablement with nothing recorded or published
      (`tests/test_maludb_vectors.py`).
- [ ] `service_role` can create, insert and search; `anon` and `authenticated`
      cannot; a project not enabled gets the gateway's answer.
- [ ] Each plan limit refuses past its value and accepts at it.
- [ ] An extension upgrade that breaks a wrapper rolls the tenant back.
- [ ] Existing suites unchanged, compatibility included; `public` OpenAPI
      identical before and after enabling.
- [ ] Security review recorded on every slice.

## Risks

- **The carry is new code on the two paths that exist for disasters.** A bug
  there loses data at the worst moment. Mitigation: count-verified, tested with a
  control, and the move keeps its source (ADR-066's retained copy) until verified.
- **A definer wrapper is an escalation surface.** Mitigation: the owner is a
  narrow role, not the superuser; `search_path` pinned and tested with a
  shadowing object, as `gateway_node_id()`'s was.
- **Upstream changes table shapes** between pinned versions. Mitigation: the
  upgrade run's in-transaction wrapper call, and the carry reads column lists from
  the catalogue rather than hard-coding them.
- **Exact search on a shared node.** Mitigation: per-plan limits from measured
  numbers, not guesses.

## Decision log

- 2026-09-13 — ADR-077 accepted: eight questions decided one at a time by the
  repository owner. Question 8 was raised by a finding made while preparing the
  plan, not beforehand: `pg_dump` carries no `maludb_core` table data.

## Progress log

- 2026-09-13 — **Compartments slice 1 built.** Migration 0038
  (`maludb_vectors_enabled`), the `maludb_vectors` entitlement on every tier,
  `mldb_<ref>_vectors` (NOLOGIN, no attributes, no memberships), grants derived
  from the installed extension and exercised before commit, `maludb` served
  while either feature is on, the gateway admitting either, and moves creating
  the role before `pg_restore` — a dump carries the grants, and drops one to an
  absent role. Tests with controls: removing one derived function grant fails
  enablement and leaves nothing; a hand-added grant is refused; a reachable
  definer function or non-vector table is refused; a dump restored onto a
  cluster with the role keeps a working owner.

- 2026-09-13 — **Compartments slice 0 built.** Measured on the development node:
  a non-superuser definer works and needs table, sequence **and function**
  grants (bootstrap 011 revokes `EXECUTE` in every schema); grants cannot be
  found by calling a wrapper once, because a cached PL/pgSQL plan reaches
  branches a first call does not; exact search is 10–18 ms per million stored
  dimensions; **ANN's p95 is worse than exact search at every size measured**
  and one build of 20k × 1536 peaked at 951 MB, so ANN stays off; exact search
  ignores tombstones. The carry is in move and restore, tested against real
  tenants and a real point-in-time restore. Upstream report drafted, not filed.

- 2026-09-13 — Plan written. No code.
