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
