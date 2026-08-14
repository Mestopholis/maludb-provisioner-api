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

## Acceptance criteria

- [ ] Scanner identifies unsupported blockers before cutover.
- [ ] Migration is repeatable in a test project.
- [ ] Source is not modified unexpectedly.
- [ ] Post-migration official-client compatibility suite passes for supported features.
