-- Hand the tenant's `auth` schema to the project's auth role, so GoTrue can
-- migrate it.
--
-- Bootstrap 002 creates the auth schema and the four helper functions as the
-- platform superuser, which leaves the project's own auth role with nothing:
-- no ownership of the functions, and not even USAGE on the schema. Stock GoTrue
-- 2.195.0 then fails three separate ways, all verified during the Phase 00
-- spike and re-confirmed against the shipped migrations:
--
--   1. `00_init_auth_schema` creates tables in `auth`. Without CREATE on the
--      schema the migration fails outright.
--   2. `20211202183645_update_auth_uid` issues `create or replace function
--      auth.uid()`. Replacing a function you do not own raises
--      `ERROR: must be owner of function uid`.
--   3. Without a search_path the connection's default puts GoTrue's
--      `schema_migrations` bookkeeping in `public`, where PostgREST exposes it
--      on the customer's Data API (Phase 00 finding 4, required by ADR-018).
--
-- 002 is immutable once applied and existing tenants need this too, so it lands
-- here rather than as an edit.
--
-- Letting GoTrue own these is safe and is the upstream-preferred arrangement.
-- The auth role is a platform-internal service credential (`docs/AUTH.md`); it
-- is never issued to a customer, and schema ownership is per-database so it
-- confers nothing outside this tenant.
--
-- Note on the claim key, which is the reason 002 pre-created the helpers at
-- all: GoTrue's *first* migration defines uid()/role() reading only the legacy
-- `request.jwt.claim.sub`, which returns NULL against PostgREST 14 and fails
-- every policy closed. Its three later migrations coalesce both forms and end
-- at a correct definition. So a fully migrated tenant is fine and a
-- half-migrated one is not -- which is why `tenant_bootstrap.verify()` probes
-- the behaviour rather than trusting that migrations ran.

DO $$
DECLARE
    auth_role text;
BEGIN
    -- Derived, not passed in, because bootstrap files are static SQL applied to
    -- every tenant. TenantNames builds the database as mldb_<ref> and the auth
    -- role as <database>_auth, from a project_ref validated against a strict
    -- alphabet before either name is constructed.
    auth_role := current_database() || '_auth';

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = auth_role) THEN
        -- Loud rather than skipped. A tenant without its auth role is
        -- misprovisioned, and silently continuing would leave the schema owned
        -- by the platform and GoTrue unable to start -- diagnosed much later,
        -- as a confusing migration failure.
        RAISE EXCEPTION 'no auth role % for this tenant; cannot hand over the auth schema', auth_role;
    END IF;

    EXECUTE format('ALTER SCHEMA auth OWNER TO %I', auth_role);

    EXECUTE format('ALTER FUNCTION auth.jwt()   OWNER TO %I', auth_role);
    EXECUTE format('ALTER FUNCTION auth.uid()   OWNER TO %I', auth_role);
    EXECUTE format('ALTER FUNCTION auth.role()  OWNER TO %I', auth_role);
    EXECUTE format('ALTER FUNCTION auth.email() OWNER TO %I', auth_role);

    -- ADR-018 and Phase 00 finding 4. Scoped IN DATABASE because a bare
    -- ALTER ROLE ... SET is cluster-wide, and this role's search_path is a
    -- property of its work in this tenant only (ADR-017 makes the same point
    -- about resource settings).
    EXECUTE format(
        'ALTER ROLE %I IN DATABASE %I SET search_path = auth, public',
        auth_role, current_database()
    );
END $$;

-- CONNECT is granted in provisioning; USAGE on public is what lets the auth
-- role reach the helpers' dependencies and anything a policy references.
DO $$
DECLARE
    auth_role text := current_database() || '_auth';
BEGIN
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', auth_role);
END $$;
