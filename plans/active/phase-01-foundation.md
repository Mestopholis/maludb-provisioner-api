# Execution Plan: Phase 01 — Foundation

Status: READY TO START — all preconditions met
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-01-foundation` (not yet created)
Related task: `tasks/PHASE-01-FOUNDATION.md`
Dependencies: none — ADR-021 ratified and ADR-024 recorded, both 2026-08-15

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
- [x] Control-plane stack selected — ADR-024: Python 3.12, FastAPI, psycopg3, no ORM.

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

1. ~~Select the control-plane stack.~~ **Done — ADR-024.** Python 3.12,
   FastAPI, uvicorn, psycopg3, no ORM. The gateway is explicitly not decided
   here; it belongs to Phase 03 with Envoy as the leading candidate.
2. Development environment: local PostgreSQL 17 with `maludb_core`, seed data,
   one documented command to start from a clean checkout. Use `uv`, which is
   already installed on the development host.
3. Convert `specs/control-plane-schema.sql` into ordered, versioned `.sql`
   migrations applied by a minimal runner. Per ADR-024 migrations become
   authoritative and the spec becomes reference — no ORM or migration DSL is
   introduced to restate SQL that already executes.
4. Domain models for plans, nodes, projects.
5. Platform identity per ADR-020/ADR-021 and `docs/ACCOUNTS.md`: users,
   organizations, membership and roles, invitations, sessions, PATs, MFA
   structure.
6. Configuration and secret loading: environment-aware config, the KEK
   interface from ADR-023 with a development file implementation, and the token
   pepper. Fail closed when the KEK source is unavailable.
7. Structured logging with request and project correlation IDs, and the
   redaction rules from `docs/SECURITY.md` and `docs/SECRETS.md`.
8. Tests, linting, CI. CI must also regenerate `specs/control-plane-api.yaml`
   from the FastAPI app and fail on drift (ADR-024).
9. Gate the FastAPI documentation routes (`/docs`, `/redoc`, `/openapi.json`)
   behind configuration so they are not publicly reachable in production.
10. Record exact local developer commands in `AGENTS.md`.

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
- **Drift between generated and hand-written artefacts.** Settled by ADR-024:
  migrations are authoritative over `specs/control-plane-schema.sql`, and the
  FastAPI app is authoritative over `specs/control-plane-api.yaml`, with CI
  enforcing the latter. The residual risk is CI not being wired up early
  enough, so do it in the same change that introduces the first route.
- **Scope creep toward Phase 02.** Provisioning is tempting once the models
  exist. It is out of scope.

## Decision log

- 2026-08-15 — Plan created. Stack selection deliberately left open as step 1.
- 2026-08-15 — ADR-024: Python 3.12 / FastAPI / psycopg3, no ORM. Control plane
  only; the gateway is deferred to Phase 03. Migrations and the FastAPI app are
  authoritative over their hand-written specs. FastAPI documentation routes are
  configuration-gated.

## Progress log

- 2026-08-15 — Preconditions complete, stack chosen. Ready to begin
  implementation at step 2.
