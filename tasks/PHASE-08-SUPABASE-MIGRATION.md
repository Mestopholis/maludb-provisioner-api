# Phase 08 — Supabase Migration

## Objective

Make an existing Supabase project analyzable and migratable to MaluDB for a defined compatibility subset.

## Scope

- Compatibility scanner.
- Schema/data migration.
- RLS/functions/triggers/indexes.
- Supported extensions.
- Initial Auth migration where proven.
- Validation report.
- Cutover runbook.
- Allowed database/admin tooling for customers -- the SQL and schema surface a
  dashboard offers. **Deferred here from Phase 07** (2026-08-16): it is the same
  guard-rail problem this phase already solves for migration, and building it
  twice would mean deciding twice what a customer may run against their own
  database.

## Acceptance criteria

- [ ] Scanner identifies unsupported blockers before cutover.
- [ ] Migration is repeatable in a test project.
- [ ] Source is not modified unexpectedly.
- [ ] Post-migration official-client compatibility suite passes for supported features.
- [ ] Every tier can create and alter its own schema without a database
      credential, and the surface that allows it cannot escalate beyond what its
      plan entitles it to, escape its tenant, outlive its statement timeout, or
      write while the project is storage-restricted. *(ADR-039. Without this
      criterion the phase can meet the four above while the surface every one of
      them applies through is unbuilt or unsafe.)*
- [ ] A migrated schema that installs an allowlisted extension succeeds, or the
      scanner reports it as a blocker before cutover. *(Today it can do neither:
      `mldb_<ref>_admin` cannot `CREATE EXTENSION` at all — negative test H —
      while Supabase's free tier installs from a 60+ allowlist through
      `supautils`. A migration that fails on `create extension if not exists
      "uuid-ossp"` fails on line one.)* **Answered 2026-08-17 by ADR-045: it
      succeeds, for what `specs/extension-allowlist.yaml` carries, and the
      scanner blocks on everything else. The installer lands in the
      schema-migration slice.**

Scope and shape settled 2026-08-17, before the migration slices began — ADR-042
(a CLI the customer runs, so the platform never holds their Supabase
credential), ADR-043 (initial launch covers exactly what
`specs/compatibility-matrix.yaml` marks `supported`; everything else is a
scanner blocker naming its phase) and ADR-044 (a controlled write freeze with a
measured, published window).
