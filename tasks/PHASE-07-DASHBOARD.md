# Phase 07 — Customer Dashboard

## Objective

Provide the minimum production UI for account/project operations.

## Scope

- Create/list project.
- Project status.
- API URL/key management.
- Usage/limits.
- Upgrade entry point.
- Allowed database/admin tooling.
- Audit visibility where appropriate.

## Acceptance criteria

- [ ] No privileged DB credentials leak to free users.
- [ ] Secret API key material follows one-time/reveal/reset policy.
- [ ] Dashboard uses control-plane APIs rather than direct privileged DB operations.
