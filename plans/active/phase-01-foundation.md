# Execution Plan: Phase 01 — Foundation

Status: NOT STARTED
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-01-foundation` (not yet created)
Related task: `tasks/PHASE-01-FOUNDATION.md`
Dependencies: none remaining — ADR-021 ratified 2026-08-15

## Objective

Turn the planning repository into a running control-plane skeleton: a chosen
stack, a working development environment, the identity and domain models, real
migrations derived from `specs/control-plane-schema.sql`, and CI.

This is the first phase that writes product code. Nothing before it did.

## Preconditions

- [x] MaluDB defined and its constraints measured (`docs/MALUDB.md`).
- [x] Feasibility proven (`tasks/PHASE-00-FEASIBILITY.md`).
- [x] ADR-021 ratified, so platform identity is built rather than delegated.
- [x] `specs/control-plane-schema.sql` executes cleanly — 18 tables, 21 FKs.
- [ ] Control-plane stack selected. **This is step 1 and needs an ADR.**

## Scope

Per `tasks/PHASE-01-FOUNDATION.md`. Ordered so the riskiest decision comes
first and the identity work lands after it.

## Non-goals

- Tenant database provisioning — Phase 02.
- PostgREST, Auth workers, gateway — Phase 03/04.
- Billing, dashboard UI — Phase 07/09.
- Choosing a production KEK backend. A development file behind the swappable
  interface is sufficient here; production selection is tracked in
  `docs/OPEN-QUESTIONS.md` and blocks launch, not this phase.

## Implementation steps

1. **Select the control-plane stack and record an ADR.** Decision criteria that
   actually matter here, in rough priority order:
   - a mature PostgreSQL driver and migration tool, since the control plane is
     database-heavy and the schema is already written;
   - a credible AEAD/crypto story for ADR-023 envelope encryption;
   - process supervision and subprocess management, since later phases start
     and stop per-project workers;
   - operational familiarity for whoever carries the pager.
   No language is implied by anything decided so far. `maludb-core` is C with
   Python/Node/PHP drivers, and the existing `maludb-python-api-server` is
   FastAPI — prior art, not a constraint.
2. Development environment: local PostgreSQL 17 with `maludb_core`, seed data,
   one documented command to start from a clean checkout.
3. Convert `specs/control-plane-schema.sql` into ordered, versioned migrations.
   The spec stays the human-readable reference; migrations become authoritative.
4. Domain models for plans, nodes, projects.
5. Platform identity per ADR-020/ADR-021 and `docs/ACCOUNTS.md`: users,
   organizations, membership and roles, invitations, sessions, PATs, MFA
   structure.
6. Configuration and secret loading: environment-aware config, the KEK
   interface from ADR-023 with a development file implementation, and the token
   pepper. Fail closed when the KEK source is unavailable.
7. Structured logging with request and project correlation IDs, and the
   redaction rules from `docs/SECURITY.md` and `docs/SECRETS.md`.
8. Tests, linting, CI.
9. Record exact local developer commands in `AGENTS.md`.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-01-FOUNDATION.md`.
- [ ] The identity negative tests in `docs/TESTING.md`.
- [ ] The secret-handling tests in `docs/TESTING.md` — key rotation, AAD row
      binding, constant-time verification, no secrets in logs, fail-closed on
      missing KEK.
- [ ] Migrations apply from empty and are re-runnable.
- [ ] No secrets committed.

## Risks

- **Stack choice is sticky.** It is the hardest decision to reverse in this
  phase. Mitigate by keeping the KEK loader, config, and logging behind narrow
  interfaces.
- **Identity is security-critical and easy to get subtly wrong.** Session
  revocation, the last-owner rule, and cross-organization access are the
  failure points; they are already written as blocking tests.
- **Schema drift.** Once migrations exist, `specs/control-plane-schema.sql` can
  fall out of date. Decide explicitly which is authoritative and note it in the
  ADR — this plan assumes migrations become authoritative and the spec becomes
  reference.
- **Scope creep toward Phase 02.** Provisioning is tempting once the models
  exist. It is out of scope.

## Decision log

- 2026-08-15 — Plan created. Stack selection deliberately left open as step 1.

## Progress log

- 2026-08-15 — Not started.
