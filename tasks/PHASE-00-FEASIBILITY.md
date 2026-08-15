# Phase 00 — Feasibility Spike

## Objective

Prove, before choosing a control-plane stack or writing platform code, that the
Supabase compatibility wedge is achievable at all: that stock PostgREST and
stock Supabase Auth (GoTrue) run unmodified against a MaluDB tenant database,
and that the official `@supabase/supabase-js` client works against the result.

If this fails, ADR-001 needs rethinking and every later phase changes shape.
It is deliberately ordered before Phase 01.

## Status

**Complete — executed 2026-08-15. The wedge holds.**

Run against MaluDB 0.104.0 / PostgreSQL 17.10 on the development host, using a
tenant database provisioned per `specs/tenant-role-model.md`.

| Component | Version | Result |
|---|---|---|
| PostgREST | 14.17 (upstream static binary) | works unmodified |
| Supabase Auth (GoTrue) | 2.195.0 (upstream binary) | works unmodified |
| `@supabase/supabase-js` | 2.112.3 | **16 / 16 passed** |

## What was proven

Provisioning followed the spec exactly and worked end to end: shared
`anon` / `authenticated` / `service_role` names, a per-project
`mldb_<ref>_authenticator` login role, `REVOKE CONNECT ... FROM PUBLIC`,
`CREATE EXTENSION maludb_core CASCADE`, and plan limits applied to the login
role `IN DATABASE`. The `statement_timeout` set per ADR-017 was observed active
in the PostgREST session, confirming that guidance live.

Official-client coverage, all passing: `select`, `insert`, `update`, `upsert`,
`delete`, `.eq()`, `.ilike()`, `.in()`, `order`, `limit`, `range`, exact
`count`, `rpc`, PostgREST error shape `{code, message, details, hint}`, and
gateway rejection of a wrong API key.

Tenant isolation held throughout. User B saw none of user A's rows, a
cross-user `update` affected zero rows, and `anon` was refused.

**Full auth integration**: GoTrue migrations created 23 tables in the tenant's
`auth` schema; signup, password sign-in, session refresh, get-user, and signout
all worked; and a **GoTrue-issued JWT presented to PostgREST resolved through
`auth.uid()` into the correct `owner_id`**, with RLS filtering to that user's
rows. That is the complete Supabase-shaped request path working on MaluDB.

A ~40-line Node proxy stood in for the gateway, mapping `/rest/v1/*` to
PostgREST and validating the API key. Its behavior is the seed of the Phase 03
gateway.

## Findings that change later phases

Seven issues surfaced. None blocks the wedge; all must be handled in bootstrap
or provisioning, and each has been verified fixed except the first.

### 1. `maludb_core` cannot have its dependencies relocated — upstream blocker

Supabase installs extensions into a dedicated `extensions` schema to keep them
off the public API surface. That is **not possible** with `maludb_core` 0.104.0:
installing `vector` / `pg_trgm` / `pgcrypto` / `btree_gist` into an `extensions`
schema and then `CREATE EXTENSION maludb_core` fails with

```text
ERROR: function public.gen_random_bytes(integer) does not exist
```

The extension's install script hard-codes `public.` qualification for its
dependencies. Per `AGENTS.md`, this is the case where implementation discovers a
constraint and must document rather than work around it silently.

The upstream fix is to schema-qualify via `@extschema@` or a controlled
`search_path`. Until then, mitigation 2 below is required. **Raise as an issue
against `maludb-core`.**

### 2. Extension functions leak onto the public Data API

`CREATE EXTENSION maludb_core CASCADE` puts **373 functions** into `public`.
PostgREST exposes the callable subset as RPC endpoints, and `anon` could invoke
them — `/rpc/gen_salt`, `/rpc/armor`, `/rpc/dearmor`, `/rpc/pgp_key_id`,
`/rpc/show_trgm` and others were live on the public API. A call to
`/rpc/gen_salt` as `anon` reached the function body.

**Verified mitigation**, required in tenant bootstrap:

```sql
-- revoke EXECUTE on every extension-owned function in the exposed schema
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT p.oid::regprocedure AS sig
             FROM pg_proc p
             JOIN pg_namespace ns ON ns.oid = p.pronamespace
             JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
            WHERE ns.nspname = 'public'
  LOOP EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', r.sig);
  END LOOP;
END $$;
NOTIFY pgrst, 'reload schema';
```

After this the exposed RPC surface was exactly the tenant's own function, `anon`
could no longer reach `gen_salt`, and tenant CRUD, RPC, and `maludb_core`
itself all still worked.

### 3. Schema cache reload is required after tenant DDL

PostgREST caches the schema. A table created after startup returned
`PGRST205: Could not find the table 'public.notes' in the schema cache` until
`NOTIFY pgrst, 'reload schema'` was issued.

Any customer schema change — dashboard SQL editor, migration, ORM push — must
trigger a reload. Supabase uses an event trigger for this. Phase 03 needs an
explicit mechanism; it cannot be left to worker restarts.

### 4. GoTrue writes bookkeeping into the exposed schema by default

GoTrue's first migration attempts `CREATE TABLE schema_migrations` under the
connection's default `search_path`, landing it in `public` — where PostgREST
would expose it. It also fails outright without `CREATE` on that schema.

**Verified fix**: `ALTER ROLE mldb_<ref>_auth IN DATABASE <db> SET search_path
= auth, public`. Bookkeeping then lands in `auth` and nothing leaks into
`public`.

### 5. GoTrue migrations require ownership of the auth helper functions

If the platform pre-creates `auth.uid()` owned by the platform role, GoTrue's
migration fails: `ERROR: must be owner of function uid`. Either let GoTrue's
migrations run first, or create the helpers owned by the project's auth role.

### 6. `GENERATED ALWAYS AS IDENTITY` breaks `.upsert()`

An upsert supplying an explicit `id` against a `GENERATED ALWAYS` column fails
with `428C9 / cannot insert a non-DEFAULT value`. Switching the column to
`GENERATED BY DEFAULT AS IDENTITY` made `.upsert()` pass. Tenant bootstrap
templates, documentation, and migration tooling must use `BY DEFAULT`.

### 7. The `anon` grant posture is a visible behavioral choice

With no grant to `anon`, a public read returns `42501 permission denied for
table`, not an empty array. Supabase convention is to grant and let RLS return
empty. The difference is visible to application error handling, so bootstrap
must choose deliberately and the compatibility matrix must record it.

## Note on `service_role` and `BYPASSRLS`

The spike gave the shared `service_role` the `BYPASSRLS` attribute, matching
Supabase. This looks alarming next to ADR-016 — `BYPASSRLS` is a role attribute
and applies in every database the role can reach.

It is safe **only** because of the surrounding controls: `service_role` is
`NOLOGIN` and unreachable except via `SET ROLE` from a tenant's own
authenticator, and that session is already bound to a single tenant database by
the ADR-014 `CONNECT` lockdown. Remove either control and this becomes a
cross-tenant RLS bypass. It must be called out in the Phase 02 security review.

## Not covered

- Realtime and Storage.
- Email delivery. `GOTRUE_MAILER_AUTOCONFIRM=true` was used to avoid SMTP
  entirely, which confirms that a transactional email provider is a hard
  dependency for any real signup flow — see the email gap in
  `docs/OPEN-QUESTIONS.md`.
- OAuth, MFA, magic links.
- Asymmetric JWT signing. The spike used a shared HS256 secret; `docs/AUTH.md`
  targets asymmetric/JWKS, which remains unproven.
- Density, concurrency, and performance under load.
- Worker sleep/wake and cold start.

## Acceptance criteria

- [x] Stock PostgREST serves a MaluDB tenant database unmodified.
- [x] Stock GoTrue migrates and runs against a MaluDB tenant database.
- [x] The official `@supabase/supabase-js` client performs CRUD and RPC.
- [x] RLS isolates two end users through the official client.
- [x] A GoTrue-issued JWT drives `auth.uid()` in RLS via PostgREST.
- [x] Provisioning per `specs/tenant-role-model.md` succeeds end to end.
- [x] Findings recorded, with mitigations verified where one exists.
- [ ] Upstream issue raised for the `maludb_core` dependency-schema blocker.

## Reproducing

`scripts/spike-provision-tenant.sh` provisions a throwaway tenant database and
prints the follow-on commands. `tests/compatibility/supabase-js-crud.mjs` is the
official-client suite. Both create scratch objects prefixed `mldb_` and are safe
only on a development node — never run them against a node carrying customer
data.
