# Phase 02 — Tenant Provisioning

## Objective

Create a project as an isolated database and constrained roles on an existing MaluDB node.

## Scope

- Node registry/health.
- Placement reservation.
- Project-ref generation.
- Role/database creation per `specs/tenant-role-model.md`.
- Privilege lockdown (`REVOKE CONNECT ... FROM PUBLIC`).
- `CREATE EXTENSION maludb_core CASCADE` in every tenant database (ADR-015).
- Recording installed extension and bootstrap versions per project.
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
- [ ] `CONNECT` is revoked from `PUBLIC` on every provisioned database, asserted per run.
- [ ] `maludb_core` is present in every tenant database, with its version recorded against the project.
- [ ] No per-tenant role is granted to `anon`, `authenticated`, or `service_role`.
- [ ] No customer-reachable role is a member of `maludb` or of any `BYPASSRLS` role.
- [ ] Plan resource settings are applied to the authenticator login role, scoped `IN DATABASE`, and verified to take effect through `SET ROLE`.
- [ ] The negative tests in `specs/tenant-role-model.md` pass.
- [ ] Generated tenant credentials and the project JWT signing key are persisted encrypted in `project_credentials`, never written to a file or returned to a caller.
- [ ] A failed provisioning run leaks no credential into `provisioning_jobs.error_detail` or any log.

## Blocked on

Nothing. ADR-013 was ratified 2026-08-15: the database is the tenancy
boundary, and a project maps to a database rather than to a MaluDB account or
schema. Phase 01 is complete.
