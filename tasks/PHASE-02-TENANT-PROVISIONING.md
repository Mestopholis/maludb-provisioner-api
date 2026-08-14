# Phase 02 — Tenant Provisioning

## Objective

Create a project as an isolated database and constrained roles on an existing MaluDB node.

## Scope

- Node registry/health.
- Placement reservation.
- Project-ref generation.
- Role/database creation.
- Privilege lockdown.
- Versioned bootstrap migrations.
- Idempotent provisioning job state machine.
- Provisioning smoke test.

## Acceptance criteria

- [ ] Creating a project never provisions a VM/container.
- [ ] Two projects can coexist on one test node.
- [ ] Tenant A cannot connect/access Tenant B.
- [ ] Customer role is not DB owner/superuser.
- [ ] Retry works after simulated failures.
- [ ] Project reaches ACTIVE only after validation.
- [ ] Cleanup never drops a possibly-used DB automatically.
