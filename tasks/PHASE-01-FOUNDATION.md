# Phase 01 — Foundation

Status: **COMPLETE** (2026-08-15). Delivered in two slices; see
`plans/completed/phase-01-foundation.md`.

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

- [x] Project builds/starts locally.
- [x] CI executes tests/lint.
- [x] No committed secrets.
- [x] Project/node/plan models exist.
- [x] User/organization/membership models exist; a project cannot be created without an owning organization.
- [x] Signup creates a personal organization with the user as `owner`.
- [x] The last `owner` of an organization cannot leave or be demoted.
- [x] Sessions and personal access tokens are revocable, and revocation takes effect immediately.
- [x] Token and session material is stored non-reversibly; passwords use a memory-hard hash.
- [x] Envelope encryption is implemented per ADR-023: KEK loaded from a swappable backend, wrapped DEKs versioned, AEAD with associated data binding each ciphertext to its row.
- [x] The control plane fails closed when the KEK source is unavailable.
- [x] A user cannot read or act on an organization they do not belong to.
- [x] Configuration supports multiple environments.
- [x] Architecture decision for control-plane stack added to `docs/DECISIONS.md`.
- [x] Exact local developer commands added to `AGENTS.md`.

## Non-goals

- Tenant DB provisioning.
- PostgREST/Auth.
- Billing/dashboard.
