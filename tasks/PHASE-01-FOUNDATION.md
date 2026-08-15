# Phase 01 — Foundation

## Objective

Create the executable repository foundation without prematurely choosing unsupported product behavior.

## Scope

- Select and document control-plane implementation stack.
- Add development environment.
- Implement platform identity: users, organizations, membership, roles, sessions, personal access tokens (`docs/ACCOUNTS.md`).
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
- [ ] User/organization/membership models exist; a project cannot be created without an owning organization.
- [ ] Signup creates a personal organization with the user as `owner`.
- [ ] The last `owner` of an organization cannot leave or be demoted.
- [ ] Sessions and personal access tokens are revocable, and revocation takes effect immediately.
- [ ] Token and session material is stored non-reversibly; passwords use a memory-hard hash.
- [ ] Envelope encryption is implemented per ADR-023: KEK loaded from a swappable backend, wrapped DEKs versioned, AEAD with associated data binding each ciphertext to its row.
- [ ] The control plane fails closed when the KEK source is unavailable.
- [ ] A user cannot read or act on an organization they do not belong to.
- [ ] Configuration supports multiple environments.
- [ ] Architecture decision for control-plane stack added to `docs/DECISIONS.md`.
- [ ] Exact local developer commands added to `AGENTS.md`.

## Non-goals

- Tenant DB provisioning.
- PostgREST/Auth.
- Billing/dashboard.
