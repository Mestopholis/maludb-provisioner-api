# Tenant Role Model

Concrete role, grant, and lockdown specification for a tenant database on a
shared MaluDB node.

Status: derived from experiments run 2026-08-15 against MaluDB 0.104.0 on
PostgreSQL 17.10. Every claim below was tested; the test names map to the
negative tests in `docs/TESTING.md`. See ADR-013, ADR-014, ADR-015, ADR-016,
ADR-017.

## The problem this solves

Two facts collide:

1. Supabase-compatible RLS policies name roles literally. A policy migrated from
   Supabase reads `... TO authenticated`, `... TO anon`, `... TO service_role`,
   and calls `auth.uid()`. Rename those roles and every migrated policy breaks.
2. PostgreSQL roles are **cluster-scoped**. On a node hosting many tenant
   databases there can be exactly one role named `authenticated`, shared by all
   of them.

`docs/TENANCY.md` requires generated role names to be globally unique, which is
correct for per-project roles but cannot apply to the three Supabase-compatible
role names. This spec resolves that split.

## Role inventory

### Shared, cluster-wide — created once per node

| Role | Attributes | Purpose |
|---|---|---|
| `anon` | `NOLOGIN` | Unauthenticated request role named by migrated policies |
| `authenticated` | `NOLOGIN` | Signed-in end-user role named by migrated policies |
| `service_role` | `NOLOGIN` | Server-side elevated role named by migrated policies |

These are **names only**. They carry no privilege of their own. Every privilege
they hold is granted per database on per-database objects, which is what makes
sharing them safe.

All three are `NOLOGIN`. **Verified (TEST G)**: a direct connection attempt as
`authenticated` is refused. They are reachable only via `SET ROLE` from a
tenant's own authenticator, inside a session already bound to that tenant's
database.

### Per-tenant, globally unique — created per project

| Role | Attributes | Purpose |
|---|---|---|
| `mldb_<ref>_authenticator` | `LOGIN`, password, `CONNECTION LIMIT` | The only role that logs in. PostgREST connects as this and `SET ROLE`s to `anon` / `authenticated` / `service_role` per request. |
| `mldb_<ref>_auth` | `LOGIN`, password | Auth service connection, owns the tenant `auth` schema |
| `mldb_<ref>_admin` | `NOLOGIN` | Tenant-admin-like role for paid direct SQL. Not database owner, not superuser. |

The platform role owns the database. Customers receive no ownership and no
superuser (ADR-004).

## Why sharing the three names is safe

**Verified (TEST D)**: privileges granted to `authenticated` inside tenant 1 do
not appear in tenant 2. Tenant 2's `authenticated` got
`ERROR: permission denied for schema app`.

The reason: `GRANT ... ON SCHEMA/TABLE ... TO authenticated` attaches the
privilege to a **per-database object**, not to the role. Only the *name* is
shared. Combined with per-database `CONNECT` lockdown (ADR-014), a session can
never be in two tenants' databases at once, so the shared role never spans a
boundary.

**Verified (TEST A/B)**: a Supabase-shaped policy works unmodified.

```sql
CREATE FUNCTION auth.uid() RETURNS text LANGUAGE sql STABLE AS
  $$ SELECT nullif(current_setting('request.jwt.claims', true)::json->>'sub','') $$;

CREATE POLICY own_rows ON app.items FOR SELECT TO authenticated
  USING (owner = auth.uid());
```

With `request.jwt.claims` set to `{"sub":"user-a"}` the session saw only
user-a's row; with `{"sub":"user-b"}` only user-b's. This is the PostgREST
mechanism, working as-is on MaluDB.

## The one thing that does leak: role membership

**Verified (TEST F)**: role membership *is* cluster-global.

```sql
GRANT mldb_t1_admin TO authenticated;   -- issued in tenant 1's context
-- checked from tenant 2's database:
SELECT pg_has_role('authenticated','mldb_t1_admin','member');  -- => t
```

Granting any per-tenant role **to** a shared role makes every tenant's
`authenticated` a member of that tenant's role. This is the single most
dangerous operation in the model.

**Rule: grants involving the shared roles are one-directional.**

```sql
GRANT anon, authenticated, service_role TO mldb_<ref>_authenticator;  -- REQUIRED
GRANT mldb_<ref>_admin TO authenticated;                              -- FORBIDDEN
GRANT <any per-tenant role> TO anon | authenticated | service_role;   -- FORBIDDEN
```

Provisioning must never issue the forbidden form, and a periodic assertion
should verify no per-tenant role is a member-granted parent of a shared role.

## Provisioning sequence

Node build, once:

```sql
CREATE ROLE anon          NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role  NOLOGIN;
```

Per project:

```sql
-- 1. per-tenant roles
CREATE ROLE mldb_<ref>_authenticator LOGIN PASSWORD '<generated>' CONNECTION LIMIT <plan>;
CREATE ROLE mldb_<ref>_auth          LOGIN PASSWORD '<generated>' CONNECTION LIMIT <plan>;
CREATE ROLE mldb_<ref>_admin         NOLOGIN;

-- 2. one-directional membership
GRANT anon, authenticated, service_role TO mldb_<ref>_authenticator;

-- 3. database, owned by the platform role
CREATE DATABASE mldb_<ref> OWNER <platform_role>;

-- 4. MANDATORY lockdown (ADR-014) - without this every role on the node can connect
REVOKE CONNECT ON DATABASE mldb_<ref> FROM PUBLIC;
GRANT  CONNECT ON DATABASE mldb_<ref>
  TO mldb_<ref>_authenticator, mldb_<ref>_auth;

-- 5. MaluDB, in every tenant database (ADR-015)
CREATE EXTENSION maludb_core CASCADE;   -- ~23 MB, ~2 s, superuser only

-- 6. per-login-role resource defaults, database-scoped (ADR-017 - defaults, not limits)
ALTER ROLE mldb_<ref>_authenticator IN DATABASE mldb_<ref>
  SET statement_timeout = '<plan>', idle_in_transaction_session_timeout = '<plan>';
```

Identifiers are generated from `project_ref`, which uses a restricted character
set and must still be quoted correctly in generated SQL (`docs/TENANCY.md`).

## Where resource settings actually apply

**Verified**: role-level `SET` applies at **login**, to the **login role**. It
does not apply to a role entered via `SET ROLE`.

| Statement | Effect on a PostgREST session |
|---|---|
| `ALTER ROLE authenticated IN DATABASE d SET statement_timeout='3s'` | **none** — target is reached by `SET ROLE`, not login |
| `ALTER ROLE mldb_<ref>_authenticator IN DATABASE d SET statement_timeout='7s'` | applies, and **survives** the subsequent `SET ROLE authenticated` |

So every per-plan database setting must target the **authenticator/login role**,
scoped `IN DATABASE`. Targeting `authenticated` silently does nothing — it
fails open, with no error.

`IN DATABASE` scoping is mandatory regardless, because a bare
`ALTER ROLE <role> SET ...` applies on every database that role can reach, and
the shared roles are reachable from every tenant.

## Prohibited grants

Never grant to any customer-reachable role:

| Role | Why |
|---|---|
| `maludb` | **Superuser** on the current install, despite upstream documenting `GRANT maludb TO <role>` as a `maludb_user` alias |
| `maludb_memory_admin`, `maludb_memory_auditor` | `BYPASSRLS` |
| `maludb_llm_admin`, `maludb_llm_auditor` | `BYPASSRLS` |
| `maludb_modeld`, `maludb_mc2dbd` | `BYPASSRLS`, and are service logins |

`BYPASSRLS` is a role attribute, so it applies in every database the role can
reach — a single mistaken grant defeats RLS for every tenant on the node.

The correct MaluDB grants for tenant use are `maludb_read`, `maludb_user`, and
`maludb_admin`, all **verified** non-superuser.

## Required negative tests

Blocking for Phase 02. Test IDs match the probe that established them.

| ID | Test | Expected |
|---|---|---|
| A | Tenant authenticator `SET ROLE authenticated`, reads own rows under a JWT claim | own row only |
| B | Same session, different `sub` | RLS filters to the other row |
| C | Tenant B's authenticator connects to tenant A's database | `FATAL: permission denied for database` |
| D | Tenant B's `authenticated` reads tenant A-granted objects | `permission denied` |
| F | `pg_has_role('authenticated','mldb_<other>_admin','member')` | false, always |
| G | Direct login as `anon` / `authenticated` / `service_role` | refused (`NOLOGIN`) |
| H | Customer role attempts `CREATE EXTENSION` | `permission denied` |
| I | Customer role is member of `maludb`, or any `BYPASSRLS` role | false, always |
| J | Free-tier project has no login role reachable from outside the gateway | no route |

## Reproducing

The probe scripts that produced these results are throwaway; recreate them from
the sequences above. Every test creates scratch databases and roles prefixed
`mldb_` and drops them at the end. Do not run them against a node carrying
customer data.
