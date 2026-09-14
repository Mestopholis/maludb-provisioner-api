# Execution Plan: maludb_core data reaches pg_dump (ADR-078)

Status: IN PROGRESS — registration slices 0–1 done 2026-09-14; maludb-core#28 awaits the owner's review; slice 2 (accept, pin, step aside) follows its release.
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

### Registration slice 2 — Accept, pin, step aside

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
- [ ] The acceptance test passes on the new version and fails on 0.104.0 (control).
- [ ] A move of a tenant with vectors succeeds on the new version with the carry
      stepping aside, and still succeeds on 0.104.0 with the carry.

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
