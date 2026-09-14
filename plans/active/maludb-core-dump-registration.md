# Execution Plan: maludb_core data reaches pg_dump (ADR-078)

Status: IN PROGRESS — ADR-078 accepted 2026-09-14; registration slice 0 (classify every table) is next.
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

### Registration slice 0 — Classify every table, decide the filters

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

### Registration slice 1 — The upstream pull request

- `maludb_core--0.104.0--0.105.0.sql` with the registrations, `default_version`
  bumped, upstream regression expected output updated, CHANGELOG.
- Opened on `maludb/maludb-core` for the owner to review and merge.

### Registration slice 2 — Accept, pin, step aside

- A platform test that installs the candidate version, writes one customer row to
  every registered table, dumps and restores into a fresh database, and asserts
  every row arrives and no installed row is duplicated.
- `extension_data.carry` skips a table present in the source extension's
  `extconfig`.
- The version added to `specs/extension-versions.yaml`, `MALUDB_CORE_REF` bumped,
  nodes moved through the ADR-075 procedure.

## Verification

- [ ] Every table classified with evidence; the owner has decided the master key.
- [ ] Upstream PR opened with registrations and passing upstream tests.
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

- 2026-09-14 — Plan written. No code.
