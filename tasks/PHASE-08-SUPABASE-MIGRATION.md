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
