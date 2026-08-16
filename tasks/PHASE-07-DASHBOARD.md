# Phase 07 — Customer Dashboard

## Objective

Provide the minimum production UI for account/project operations.

## Scope

- Sign-up, sign-in, sign-out, password reset, MFA enrolment.
- Organization switching, member management, invitations, role changes.
- Session and personal-access-token management with revocation.
- Ownership transfer.
- Create/list project.
- Project status.
- API URL/key management.
- Usage/limits.
- Upgrade entry point.
- Allowed database/admin tooling.
- Audit visibility where appropriate.

## Acceptance criteria

- [x] No privileged DB credentials leak to free users. *(Asserted on every route that
      could break it rather than argued: `test_project_creation.py` and
      `test_api_key_routes.py` and `test_usage_routes.py` each check the exact field set
      and scan the whole response for a DSN, a password, the tenant database name and the
      node's internal hostname. `ProjectOut` omits `node_id` and `database_name` by
      construction, and `api_url` is derived from the public ref rather than stored.)*
- [x] Secret API key material follows one-time/reveal/reset policy. *(Slice 2, and it holds
      because of how the material is stored rather than because a route declines to return
      it: ADR-023 keeps a secret key as a peppered verifier, so there is no plaintext
      anywhere to serve. The test asserts `ciphertext IS NULL` in the table as well as
      `key: null` in the response. Reset is create-then-revoke, two calls, so a rotation
      cannot break every running client between two deployments.)*
- [x] Dashboard uses control-plane APIs rather than direct privileged DB operations. *(The
      frontend repository has no other path: the API covers signup through ownership
      transfer, and ADR-038 puts the one operation that needs node credentials --
      provisioning -- in a worker the internet-facing application cannot reach. The
      import-graph test is what keeps that true.)*

Two scope lines are **not** delivered, and both are decisions rather than omissions:

- **MFA enrolment** — deferred by the repository owner, 2026-08-16, and recorded under
  "Platform MFA" in `docs/OPEN-QUESTIONS.md`. What is open is which factors and whether
  owners may require it of members.
- **Allowed database/admin tooling** — deferred to Phase 08, 2026-08-16. It is a SQL and
  schema surface, which is what Phase 08 is already building tooling around; adding a
  second one here would mean designing the same guard rails twice.
