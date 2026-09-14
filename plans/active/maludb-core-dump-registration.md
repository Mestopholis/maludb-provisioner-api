# Execution Plan: maludb_core data reaches pg_dump (ADR-078)

Status: IN PROGRESS — slices 0–2 done 2026-09-14; 0.105.0 pinned in the tested list and CI. What remains is operator rollout: each node moved to 0.105.0 through the ADR-075 procedure (`docs/MALUDB.md`). Stays in `active/` until a node carries it.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/adr-078-maludb-core-dump`; upstream work on a branch of `maludb/maludb-core`
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md`
Dependencies: ADR-075 (the tested list), ADR-077 (`extension_data.carry`), maludb-core#27.

**Slices are numbered "registration slice N".**

## Objective

Make one sentence true that is currently false:

> A tenant moved or restored arrives with every row `maludb_core` stores for it,
> carried by `pg_dump` itself, and no installed row duplicated or lost.

## Scope

- Classify all 157 `maludb_core` tables and decide each one's dump filter.
- The upstream change: a new `maludb_core` version registering them.
- The platform's acceptance test on the tested-versions list, the carry stepping
  aside, the pin.

## Non-goals

- The knowledge graph and the memory pipeline themselves (each needs its own ADR).
- Retiring `extension_data.carry` before a fixed version is pinned on every node.

## Implementation steps

### Registration slice 0 — Classify every table, decide the filters (done 2026-09-14)

**As built**: `specs/maludb-core-dump-registration.md`. 140 tables registered
without a filter; 12 with one (`owner_schema <> 'maludb_core'` for seven,
`NOT system_defined` for five, three of which gain the column upstream); five not
registered — three superuser-only catalogues and the two per-database secrets,
**which the owner decided are not registered** (a dump never holds a key; MaluDB's
secret store and auth tokens do not survive a dump). Triggers that fire during
`pg_restore` are to be avoided platform-side with
`session_replication_role=replica`, verified before the upstream PR states it.


- For each of the 157 tables: **catalogue** (installed rows only, not registered),
  **customer data** (registered, no filter), **mixed** (registered with a filter
  that excludes installed rows — by `system_defined`, `owner_schema`, a key range,
  or a marker the upgrade adds), or **derived** (rebuilt on demand, not
  registered). Recorded in `specs/maludb-core-dump-registration.md` with the
  evidence for each.
- Every sequence behind a registered table registered too.
- The triggers that fire on `COPY` during `pg_restore`: which are harmless and
  which must be disabled or made restore-aware upstream.
- **`malu$secret_master_key` brought to the owner as its own question** before
  anything is written.

### Registration slice 1 — The upstream pull request (done 2026-09-14)

**As built**: maludb/maludb-core#28, `feat/dump-registration`. Verified in a
disposable PostgreSQL 17 container (the development node's extension files are
shared by every cluster, so a candidate version cannot be installed there without
changing what every node under test provides):

- upstream `installcheck`: 97 of 98 pass; `governance_audit` needs `pgaudit`,
  absent from the container, unrelated;
- a real `pg_dump`/`pg_restore`: identical per-table counts, no installed row
  duplicated, vector search and fresh ids working;
- **a plain `pg_restore` re-fired the SVPOR subject trigger and failed
  `malu$embedding_dirty`'s `COPY` on a duplicate key; the same restore with
  `session_replication_role=replica` had no error** — so slice 2 must make the
  platform's restores use it;
- an in-place upgrade from 0.104.0 with customer rows marks exactly the built-ins.

The new regress test found a real bug: the SVPOR type tables default
`system_defined` to true, so a directly inserted type would never be dumped; the
default changes to false.


- `maludb_core--0.104.0--0.105.0.sql` with the registrations, `default_version`
  bumped, upstream regression expected output updated, CHANGELOG.
- Opened on `maludb/maludb-core` for the owner to review and merge.

### Registration slice 2 — Accept, pin, step aside (done 2026-09-14)

**As built**: maludb-core#28 and #30 merged; pinned at `2f2acac` (0.105.0).

- `tests/test_maludb_core_dump.py` fills **all 152 non-secret tables** from the
  catalogue — types, enumerated CHECK values read from the constraint definitions,
  parents before children, 20 tables with hand-written values for constraints that
  relate columns — and round-trips through `restore.pg_restore_argv`. Per registered
  table, the rows its filter passes arrive byte for byte and the whole table matches
  (timestamps aside: `CREATE EXTENSION` rewrites the installed rows with a new
  `created_at`, ids and bodies identical). Catalogue rows stay behind; the target
  keeps its own master key and pepper; registered sequences arrive at the source's
  values. **Controls**: on 0.104.0 every customer row is lost; on 0.105.0 without
  replica mode `pg_restore` exits 1 and the extension's triggers write rows of their own.
- `extension_data.carry` asks the **source** what it registered and skips those
  tables (`CarryReport.by_dump`); a carried table registered with a filter is refused.
  Its existing tests now provision 0.104.0 tenants, the source it still exists for.
- `restore.pg_restore_argv` — the one load both restores and moves use — passes
  `options='-c session_replication_role=replica'` in the connection string, because
  `sudo` would drop `PGOPTIONS`.

- A platform test that installs the candidate version, writes one customer row to
  every registered table, dumps and restores into a fresh database, and asserts
  every row arrives and no installed row is duplicated.
- `extension_data.carry` skips a table present in the source extension's
  `extconfig`.
- **`restore.load_into_target` runs `pg_restore` with
  `PGOPTIONS='-c session_replication_role=replica'`** — measured in slice 1:
  without it a restore re-fires extension triggers and fails a table's `COPY`.
- The version added to `specs/extension-versions.yaml`, `MALUDB_CORE_REF` bumped,
  nodes moved through the ADR-075 procedure.

## Verification

- [x] Every table classified with evidence; the owner has decided the master key.
- [x] Upstream PR opened with registrations and passing upstream tests (maludb-core#28).
- [x] The acceptance test passes on the new version and fails on 0.104.0 (control).
- [x] A tenant with vectors survives on the new version with the carry stepping
      aside, and the carry still copies a 0.104.0 source. **As verified, not as
      written:** there is no end-to-end move test on real clusters —
      `test_tenant_movement.py` mocks the load and the carry. What runs for real is
      `test_restore.py`'s point-in-time restore of a tenant with a vector compartment
      (the same `load_into_target` a move uses, the carry through `ScratchSource`), and
      `test_extension_data.py`'s step-aside and 0.104.0 carries through
      `ConnectionSource`, the move's source. The end-to-end move gap is closed by
      `tests/test_tenant_movement.py::test_a_tenant_with_vectors_moves_between_two_real_clusters`.
- [ ] A node moved to 0.105.0 in production (operator).

## Risks

- **A wrong filter is silent loss or duplication.** Mitigation: the acceptance
  test writes to every table, not a sample.
- **Upstream seed rows added by later upgrade scripts** may collide with customer
  rows restored into a newer version. Mitigation: slice 0 classifies by marker
  rather than by key range wherever a marker exists.

## Decision log

- 2026-09-14 — ADR-078 accepted: fix upstream and pin; the platform drafts the PR
  and gates the pin on a dump-coverage test.

## Progress log

- 2026-09-14 — **Registration slice 2 done.** maludb-core#28/#30 merged; 0.105.0
  pinned. The acceptance test writes a customer row to every table and proves the
  restore carries all of them; 0.104.0 and a plain `pg_restore` are its controls.
  The carry steps aside for registered tables; restores run in replica mode.

- 2026-09-14 — **Registration slice 1: maludb-core#28 opened.** 0.105.0 registers
  152 tables and their sequences, adds `system_defined` to three tables and fixes
  its default on two, with a regress test. Proven in a container by upstream's
  suite, a real dump and restore, and an in-place upgrade. Found that a plain
  `pg_restore` fails on extension triggers; the platform's restore must use
  `session_replication_role=replica` (slice 2).

- 2026-09-14 — **Registration slice 0 done.** Inventory of all 157 tables from a
  fresh 0.104.0 install: installed rows, markers, writer functions and grants,
  triggers. Found a second per-database secret (`malu$auth_pepper`) beside the
  master key; the owner decided neither is registered.

- 2026-09-14 — Plan written. No code.
