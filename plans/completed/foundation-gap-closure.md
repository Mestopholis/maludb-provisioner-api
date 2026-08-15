# Execution Plan: Foundation Gap Closure

Status: COMPLETE — merged to `main` 2026-08-15 via PR #1 (`bdb23cd`)
Human owner: repository owner
Agent: Claude Code
Branch: `docs/foundation-gap-closure` (merged, deleted)
Related task: `tasks/PHASE-00-FEASIBILITY.md`, and prerequisites for Phases 01–05
Dependencies: none

## Objective

Close the blocking gaps identified in a review of the brainstormed planning
repository, before any implementation phase begins. Replace assumptions with
measurements taken against the real MaluDB install wherever possible.

## Scope

Seven gaps, in dependency order:

1. MaluDB was undefined — what it is, what it costs, what it constrains.
2. Supabase role names collide with cluster-scoped roles on a shared node.
3. No feasibility spike before Phase 01.
4. Transactional email absent, and a hard dependency of Auth.
5. Platform accounts, organizations, and control-plane identity absent.
6. Density and unit economics unvalidated while cost is the market claim.
7. Secret storage conflated hashing with encryption.

## Non-goals

- Any control-plane implementation code. Stack selection remains Phase 01.
- Promoting `specs/compatibility-matrix.yaml`. Evidence came from a prototype
  gateway with no CI; promotion belongs to Phase 03.
- The medium-tier review items: deletion/retention, legal and compliance,
  regions, node operations and major-version upgrades, data export,
  load testing, admin tooling, LICENSE, CI, glossary.

## Verification

- [x] Findings verified against the running MaluDB 0.104.0 / PostgreSQL 17.10
      install rather than asserted from documentation.
- [x] Stock PostgREST 14.17 and Supabase Auth 2.195.0 exercised end to end.
- [x] `@supabase/supabase-js` 2.112.3 suite passes 16/16.
- [x] `specs/control-plane-schema.sql` executes: 18 tables, 21 foreign keys.
- [x] All YAML specs parse.
- [x] No secrets committed; all scratch databases, roles, and processes removed.
- [ ] Compatibility matrix promotion — deferred to Phase 03 by design.

## Risks

- Measurements come from one small development host. Per-project figures should
  generalize; absolute node totals should not.
- `maludb_core` hard-codes `public.gen_random_bytes`, blocking the
  extensions-schema mitigation. Upstream issue not yet raised.
- ADR-021 (control-plane identity off tenant infrastructure) remains Proposed.

## Decision log

- 2026-08-15 — ADR-012 through ADR-023 recorded. ADR-013 ratified by the owner.
  ADR-015 (`maludb_core` in every tenant database) decided by the owner.
  ADR-019 fixed the mail relay as the email transport.
- 2026-08-15 — ADR-021 left Proposed pending owner ratification.

## Progress log

- 2026-08-15 — Gaps 1–7 closed. Added `docs/MALUDB.md`, `docs/EMAIL.md`,
  `docs/ACCOUNTS.md`, `docs/CAPACITY.md`, `docs/SECRETS.md`,
  `specs/tenant-role-model.md`, `tasks/PHASE-00-FEASIBILITY.md`, and the spike
  harnesses. Twelve ADRs recorded and propagated into the existing docs, specs,
  and phase acceptance criteria.

## Carried forward

This plan's scope is complete. The following are **not** closed by it and are
tracked in `docs/OPEN-QUESTIONS.md` rather than left implicit here:

- ADR-021 remains Proposed — whether control-plane identity stays off tenant
  infrastructure.
- The `maludb_core` dependency-schema defect has not been raised upstream. It
  is the one unchecked box in `tasks/PHASE-00-FEASIBILITY.md`.
- Where the KEK lives is undecided. Blocking production, not Phase 01.
- The medium-tier review items listed under Non-goals.

The next execution plan should cover Phase 01, whose first act is selecting the
control-plane stack and recording it as an ADR.
