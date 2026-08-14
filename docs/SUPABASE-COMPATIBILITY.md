# Supabase Compatibility

## Goal

MaluDB should support an intentionally defined subset of Supabase behavior and expand that subset over time.

Compatibility must be verified, not assumed.

## Primary client target

`@supabase/supabase-js`

Target shape:

```javascript
const client = createClient(
  'https://<project-ref>.maludb.com',
  '<publishable-key>'
)
```

## Initial API target

### Data API

The first target is PostgREST-compatible:

```text
/rest/v1/*
```

Initial behaviors:

- select;
- insert;
- update;
- delete;
- upsert;
- filtering;
- ordering;
- range/limit;
- count;
- RPC/database functions;
- RLS-aware access.

### Auth

Next target:

```text
/auth/v1/*
```

Planned initial auth scope:

- sign up;
- sign in with password;
- refresh session;
- get user/session;
- sign out;
- Auth user records in each tenant database's `auth` schema;
- JWTs usable by the Data API/RLS.

OAuth, MFA, magic links, enterprise SSO, and other auth modes are later compatibility items unless explicitly promoted.

### Realtime

Later target:

```text
/realtime/v1/*
```

Initial planned scope:

- Postgres Changes.

Broadcast and Presence may follow.

### Storage

Later target:

```text
/storage/v1/*
```

Storage must be designed so object bytes can live outside the tenant database while authorization/metadata remain compatible.

## API keys

MaluDB should support a project-level client-safe publishable key and a server-side secret key.

The gateway translates/validates the project key and passes the correct internal authorization context to downstream services.

Legacy Supabase-style key compatibility may be added if required by migration testing, but new MaluDB projects should not be designed around obsolete key assumptions.

## Compatibility matrix

The machine-readable source of truth is:

`specs/compatibility-matrix.yaml`

## Test philosophy

For each supported surface, black-box tests should be runnable against:

- a reference Supabase project;
- a MaluDB project.

The tests should compare behavior that matters to applications rather than only HTTP status codes.

## Compatibility rule

MaluDB-specific features must use separate extensions, SQL features, namespaces, endpoints, or client methods where necessary. They must not silently redefine the behavior of a Supabase-compatible method.
