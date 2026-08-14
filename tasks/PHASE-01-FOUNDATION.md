# Phase 01 — Foundation

## Objective

Create the executable repository foundation without prematurely choosing unsupported product behavior.

## Scope

- Select and document control-plane implementation stack.
- Add development environment.
- Implement core project/node/plan domain models.
- Implement configuration/secret loading.
- Establish tests, linting, CI.
- Establish structured logging/correlation IDs.
- Convert logical schema/specs into implementation migrations.

## Acceptance criteria

- [ ] Project builds/starts locally.
- [ ] CI executes tests/lint.
- [ ] No committed secrets.
- [ ] Project/node/plan models exist.
- [ ] Configuration supports multiple environments.
- [ ] Architecture decision for control-plane stack added to `docs/DECISIONS.md`.
- [ ] Exact local developer commands added to `AGENTS.md`.

## Non-goals

- Tenant DB provisioning.
- PostgREST/Auth.
- Billing/dashboard.
