# Phase 04 — Auth and RLS

## Objective

Provide the initial Supabase-compatible password authentication surface and use Auth JWTs with PostgREST/RLS.

## Scope

- Per-project Auth process/config.
- Tenant `auth` schema bootstrap.
- Signing/JWKS design.
- Signup/password sign-in.
- Session refresh/user/signout.
- RLS integration tests.

## Acceptance criteria

- [ ] User signs up through official client.
- [ ] User signs in and receives valid session.
- [ ] Authenticated request reaches Data API with correct claims.
- [ ] RLS distinguishes two end users.
- [ ] User in project A cannot authenticate/access project B.
- [ ] Key/session rotation design documented.
