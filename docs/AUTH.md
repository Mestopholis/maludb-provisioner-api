# Authentication

## Distinct credential concepts

### Project API key

Identifies an application/project component.

### End-user access token

Identifies a signed-in application user and supplies claims used by RLS.

### Internal service credential

Allows PostgREST/Auth/etc. to connect to the correct tenant database.

### Direct database credential

Optional paid feature for PostgreSQL clients/ORMs.

Do not mix these concepts.

## Initial design

Each project has its own Auth configuration/process connected to that project's database and `auth` schema.

Target compatibility begins with:

- sign up;
- password sign-in;
- session refresh;
- user/session retrieval;
- sign out;
- JWT claims usable by PostgREST/RLS.

## Signing keys

Design for asymmetric signing/JWKS-based verification so key rotation and downstream verification do not require sharing a single universal secret across all projects.

The exact key-storage and rotation design must receive a dedicated security review before production.

## Key and session rotation

Owed by Phase 04's acceptance criteria, written against what is actually built
(HS256 per project, ADR-028's decision to defer JWKS) rather than against the
asymmetric design this document targets.

### What exists

Each project has one HS256 signing secret, `project_credentials.jwt_signing`,
envelope encrypted under ADR-023. Three components hold it: PostgREST verifies
end-user tokens with it, GoTrue signs them with it, and the gateway mints
`service_role` tokens with it. That shared dependency is the whole difficulty —
rotating it invalidates every live session and must reach three consumers.

### Rotating a project's signing secret

There is no dual-key verification with a single HS256 secret, so a rotation is a
cutover, not a roll:

1. Generate the new secret and store it as a **new** `project_credentials` row,
   superseding the old one — the same supersede-rather-than-overwrite pattern
   provisioning already uses, so the previous value stays readable for the
   length of the operation.
2. Re-render and restart both workers. PostgREST reads `jwt-secret` at startup;
   GoTrue reads `GOTRUE_JWT_SECRET` at startup. Neither re-reads it.
3. Invalidate the gateway's cached secret. It caches per process with a short
   TTL, so the bounded staleness window applies here as it does to key
   revocation.

**Every existing access token stops verifying at step 2, and every refresh token
with it.** Users are signed out. That is inherent to a shared symmetric secret
and is the strongest argument for the asymmetric design this document targets:
with a key set, a new key can be published and trusted before the old one is
withdrawn, so sessions survive.

Until then, rotation is a deliberate, announced operation, not routine hygiene.
It is appropriate on suspected compromise and inappropriate on a schedule.

### Session lifetime

`GOTRUE_JWT_EXP` defaults to one hour in `auth_workers.AuthSettings`. A refresh
token exchanges for a new access token without re-authentication, so the access
token's lifetime bounds how long a stolen one is useful, while the refresh
token's revocation is what actually ends a session. Signing out revokes the
refresh token in the tenant's own `auth` schema; nothing at the platform level
needs to participate.

### What a security review must settle before production

`docs/AUTH.md` already requires this, and it remains outstanding:

- whether per-project HS256 is acceptable at all, given the secret is held by
  three components and one leak forges tokens for that project indefinitely;
- the migration path to asymmetric signing and JWKS, which is the only way to
  rotate without signing every user out;
- whether the gateway should mint `service_role` tokens at all, or whether that
  privilege belongs behind a separate key.

## Auth migration

Supabase user migration is a later feature. The design must account for:

- password-hash portability/compatibility;
- user IDs;
- identities/providers;
- email confirmation state;
- refresh/session behavior;
- JWT signing changes after cutover.

Do not promise seamless migration until tested.
