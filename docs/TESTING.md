# Testing Strategy

## Test layers

### Unit tests

Business logic, validation, plan/entitlement calculations, state transitions.

### Integration tests

- control plane + test Postgres/MaluDB;
- provisioning;
- gateway + project routing;
- API-key validation;
- PostgREST/Auth process management.

### Security/isolation tests

Required negative tests:

- key for project A used against project B hostname;
- tenant A attempts to connect to tenant B database;
- tenant admin attempts owner/superuser operation;
- arbitrary extension attempt;
- free project attempts direct DB access;
- suspended project API request;
- revoked key reuse.

Secret handling (`docs/SECRETS.md`), required and blocking:

- a Class B value re-encrypted under a new key version decrypts correctly, and both versions remain readable mid-rotation;
- a ciphertext moved between rows fails to decrypt, proving AAD row binding;
- API-key verification is constant-time and does not use a memory-hard function;
- a control-plane database dump without the KEK yields no usable recoverable secret;
- no secret appears in logs at any level, including provisioning failure paths and `provisioning_jobs.error_detail`;
- the control plane refuses to start when the KEK source is unavailable.

Platform identity (`docs/ACCOUNTS.md`), required and blocking:

- a user cannot read or act on an organization they do not belong to;
- a `viewer` or `billing` role cannot create, modify, or delete projects;
- a revoked session or personal access token is rejected on the next request;
- an invitation token is single-use, expires, and is only acceptable by the invited address;
- the last `owner` of an organization cannot leave or be demoted;
- a personal access token cannot exceed its user's organization permissions;
- staff support access is recorded in `audit_events` with `actor_type` distinguishing it from customer action.

Because the three Supabase-compatible role names are shared cluster-wide (ADR-016), these are also required and blocking:

- privileges granted to `authenticated` in tenant A are not visible to tenant B;
- `pg_has_role('authenticated', '<any per-tenant role>', 'member')` is false;
- direct login as `anon` / `authenticated` / `service_role` is refused;
- no customer-reachable role is a member of `maludb` or of any `BYPASSRLS` role;
- a new tenant database rejects connections from an unrelated tenant's role — this fails by default until `CONNECT` is revoked from `PUBLIC`, so it must be asserted on every provisioning run, not once.

The full test table with IDs is in `specs/tenant-role-model.md`.

### Supabase compatibility tests

Use `@supabase/supabase-js` where applicable.

Tests should support two targets:

```text
TARGET=supabase
TARGET=maludb
```

Initial Data API tests:

- select;
- insert;
- update;
- delete;
- upsert;
- filters;
- ordering;
- range;
- count;
- RPC;
- RLS behavior.

Then Auth tests.

## Compatibility definition

A feature is marked `supported` in the compatibility matrix only when:

1. its intended behavior is documented;
2. automated black-box tests exist or an explicit exception is recorded;
3. MaluDB passes the tests.

## Provisioning tests

Test:

- happy path;
- retry after each state boundary;
- node selection failure;
- role already exists;
- DB already exists;
- bootstrap partially applied;
- API worker start failure;
- gateway registration failure;
- cleanup of genuinely unused failed project.
