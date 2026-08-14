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

## Auth migration

Supabase user migration is a later feature. The design must account for:

- password-hash portability/compatibility;
- user IDs;
- identities/providers;
- email confirmation state;
- refresh/session behavior;
- JWT signing changes after cutover.

Do not promise seamless migration until tested.
