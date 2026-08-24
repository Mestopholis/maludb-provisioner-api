-- Phase 10 slice 1: the tenant's `storage` schema, under platform ownership.
--
-- Upstream `storage-api` ships 63 tenant migrations and expects to run them as
-- a superuser that may invent roles. `.env.sample` says so directly:
-- `DB_INSTALL_ROLES=true`, `DB_SUPER_USER=postgres`, `DB_ANON_ROLE=anon`,
-- `DB_SERVICE_ROLE=service_role`. Every one of those collides with something
-- already decided here. ADR-004 gives customers no superuser and keeps
-- database ownership with the platform. ADR-016 makes `anon`,
-- `authenticated` and `service_role` **shared cluster-wide** -- so a component
-- that believes it may create them is a component that believes it is alone on
-- the cluster, and on a shared node it is wrong in a way that reaches every
-- other tenant.
--
-- So the platform does the parts upstream would otherwise do for itself, and
-- `DB_INSTALL_ROLES=false` turns off the parts it must not. This file is that
-- arrangement. It runs at provisioning, before `storage-api` has ever
-- connected; the schema it creates is empty until the worker (slice 3)
-- registers the tenant and upstream's migrations populate it.
--
-- ## What was measured, and what it changed
--
-- `specs/storage-server-model.md` recorded slice 0's remaining unknown as the
-- one "most likely to produce an unwelcome surprise": upstream's migrations
-- were measured with the migrating role as a **superuser**, so whether they
-- complete under a constrained owner was untested. Measured here, 2026-08-24,
-- against `supabase/storage-api:v1.70.6`'s 63 tenant migrations applied as a
-- `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION` role owning
-- nothing but this schema:
--
--   * **62 of 63 pass unchanged.** The one failure is
--     `0011-add-trigger-to-auto-update-updated_at-column.sql`, and it fails
--     with `permission denied for schema public`.
--
-- That failure is the finding, and the tempting fix is the wrong one. The
-- migration opens with an **unqualified** `CREATE OR REPLACE FUNCTION
-- update_updated_at_column()`. Upstream lands it in `storage` because its own
-- migration 0002 does `ALTER USER supabase_storage_admin SET search_path =
-- "storage"`; under a default `"$user", public` it lands in **`public`** --
-- the one schema PostgREST exposes (`workers.py`'s `db-schemas = "public"`).
-- Granting `CREATE ON SCHEMA public` would have made the migration "pass"
-- while dropping a platform function into the customer's Data API namespace.
-- That is Phase 00 finding 4 exactly -- GoTrue's `schema_migrations` landing in
-- `public` -- and bootstrap 007 answered it the same way this file does:
--
--     ALTER ROLE <storage role> IN DATABASE <db> SET search_path = storage
--
-- With the search_path pinned, **all 63 migrations pass and `public` is
-- untouched**: zero relations and zero functions added to it. Verified by
-- `tests/test_object_storage.py`.
--
-- ## The privilege the storage role does *not* get
--
-- Nothing is granted to it on `public` -- not `CREATE`, and no privilege on any
-- table. Measured: all 63 migrations complete without one, so bootstrap 007's
-- `GRANT USAGE ON SCHEMA public` for the auth role has no counterpart here. If
-- slice 3 finds a runtime dependency on `public`, that is a new bootstrap file
-- and a recorded reason, not an edit here.
--
-- **This is not a claim that `storage-api` cannot read the tenant's tables.**
-- The role can `SET ROLE` into `anon`, `authenticated` and `service_role` --
-- it must, or no query it makes is governed by RLS -- and bootstrap 004 grants
-- those three `ALL ON ALL TABLES IN SCHEMA public`. So the reach of this
-- connection through a role switch is exactly the reach of PostgREST's
-- authenticator, and is bootstrap 004's posture rather than anything this file
-- decides. What the absence of a direct grant buys is narrower and still worth
-- having: the role's *own* privilege set names nothing outside `storage`, so a
-- query that has not switched role touches nothing outside `storage` either.
-- `USAGE` on `public` is not revoked and could not usefully be: PostgreSQL
-- grants it to `PUBLIC` by default, which is why `has_schema_privilege` answers
-- true for every role in the database.
--
-- ## Ownership, and why RLS is not forced
--
-- The schema and everything upstream creates in it are owned by
-- `mldb_<ref>_storage`, on bootstrap 007's precedent: the service that
-- migrates a schema owns it. That role is a platform-internal service
-- credential in the same class as `mldb_<ref>_auth` (`docs/AUTH.md`); it is
-- never issued to a customer.
--
-- RLS is on for every table and **not forced**, so the owner bypasses it. That
-- is deliberate rather than inherited, and `specs/storage-server-model.md`
-- asked for the decision explicitly. `storage-api` switches role per request --
-- `set_config('role', <role from the JWT>, true)` in
-- `dist/internal/database/postgres/scope.js` -- so a customer-scoped query runs
-- as `anon`, `authenticated` or `service_role` and RLS is evaluated. Measured
-- on a migrated tenant holding one object and no policies:
--
--     owner        -> 1 row   (bypasses RLS; forced = false)
--     authenticated-> 0 rows  (RLS denies; no policy grants it)
--     service_role -> 1 row   (BYPASSRLS, by design, as on Supabase)
--
-- Forcing RLS would deny the owner too, and with no policies that would deny
-- `storage-api`'s own bookkeeping -- migrations, multipart reaping, deletion.
-- The service would not work. So the owner bypass stays, and the control that
-- matters is that the owning role is not customer-reachable.
--
-- `service_role` bypassing storage policies is ADR-041's finding in a new
-- place, and it is upstream's behaviour rather than a MaluDB choice: a role
-- named in a request selects a credential, never a permission boundary. It is
-- the customer's own project either way, reached with the customer's own
-- service key.

DO $$
DECLARE
    storage_role text;
BEGIN
    -- Derived, not passed in, exactly as bootstrap 007, 008 and 010 derive
    -- theirs: bootstrap files are static SQL applied to every tenant, and
    -- TenantNames builds the database as mldb_<ref> and this role as
    -- <database>_storage from a project_ref validated against a strict
    -- alphabet before either name is constructed.
    storage_role := current_database() || '_storage';

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = storage_role) THEN
        -- Loud rather than skipped, on bootstrap 007's reasoning: a tenant
        -- without its storage role is misprovisioned, and continuing would
        -- leave the schema owned by the platform superuser and upstream's
        -- migrations unable to create a single table in it -- diagnosed much
        -- later, as a confusing migration failure inside a container.
        RAISE EXCEPTION 'no storage role % for this tenant; cannot hand over the storage schema',
            storage_role;
    END IF;

    -- IF NOT EXISTS plus an explicit ALTER, rather than a bare CREATE: a tenant
    -- migrated from Supabase (Phase 08) can arrive with `storage` already
    -- present, and the ownership is the part that must be true either way.
    IF to_regnamespace('storage') IS NULL THEN
        EXECUTE format('CREATE SCHEMA storage AUTHORIZATION %I', storage_role);
    ELSE
        EXECUTE format('ALTER SCHEMA storage OWNER TO %I', storage_role);
    END IF;

    -- The finding above. Scoped IN DATABASE because a bare ALTER ROLE ... SET
    -- is cluster-wide and this role's search_path is a property of its work in
    -- this tenant only -- the same scoping bootstrap 007 uses for the auth
    -- role and ADR-017 requires for resource settings.
    EXECUTE format(
        'ALTER ROLE %I IN DATABASE %I SET search_path = storage',
        storage_role, current_database()
    );
END $$;


-- The hardening, callable on its own so slice 3's registration path and any
-- future repair can invoke it directly.
--
-- It exists as a function rather than as statements in this file because the
-- objects it hardens **do not exist yet when this file runs**. Bootstrap
-- happens at provisioning; upstream's migrations run later, the first time the
-- storage worker serves this tenant, and again on every `storage-api` upgrade.
-- A one-shot revoke here would harden an empty schema and then be recorded as
-- applied forever -- which is bootstrap 003's mistake, and the reason 005
-- exists.
--
-- SECURITY DEFINER for bootstrap 005's reason, in a new place: the role
-- performing the DDL is the storage role, which owns the tables but is not
-- entitled to `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` on anything it does
-- not own and should not be trusted to be the one enforcing this anyway.
-- search_path pinned to pg_catalog so nothing in the body resolves through a
-- schema the tenant controls.
CREATE OR REPLACE FUNCTION maludb_platform.harden_storage_schema()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    -- Surface `storage-api` creates that Phase 10 does not use and does not
    -- expose. Upstream grants `SELECT` on the vector tables to all three shared
    -- roles unconditionally; `storage.migrations` is owner-only already and is
    -- listed so that stays an assertion rather than an accident.
    --
    -- `iceberg_namespaces` and `iceberg_tables` are named although a
    -- multi-tenant instance does not create them: migration 0038 returns early
    -- when `storage.multitenant` is true, which is ADR-058's topology. They are
    -- listed because a tenant migrated in dedicated mode, or a future upstream
    -- that stops short-circuiting, must not quietly acquire two granted tables.
    unexposed CONSTANT text[] := ARRAY[
        'migrations',
        'buckets_vectors', 'vector_indexes',
        'iceberg_namespaces', 'iceberg_tables'
    ];
    shared    CONSTANT text[] := ARRAY['anon', 'authenticated', 'service_role'];
    target  record;
    grantee text;
    changed integer := 0;
BEGIN
    IF to_regnamespace('storage') IS NULL THEN
        RETURN 0;
    END IF;

    -- Nothing in `storage` is reachable without a named grant. PostgreSQL
    -- grants no schema privileges to PUBLIC by default, so this is an
    -- assertion; upstream's migration 0002 would have granted broadly had
    -- DB_INSTALL_ROLES been left at its default.
    EXECUTE 'REVOKE ALL ON SCHEMA storage FROM PUBLIC';

    FOREACH grantee IN ARRAY shared
    LOOP
        -- The grant upstream does **not** make when DB_INSTALL_ROLES is false,
        -- and the entire remedy slice 0 measured. Without it every request
        -- answers `403 AccessDenied` with `permission denied for schema
        -- storage`: upstream's migrations grant table privileges to these three
        -- names but leave the schema itself owner-only.
        --
        -- A missing shared role is tolerated rather than raised, on bootstrap
        -- 005's reasoning: raising from the event trigger would abort the DDL
        -- that fired it. The names are cluster-scoped and should always exist.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee) THEN
            EXECUTE format('GRANT USAGE ON SCHEMA storage TO %I', grantee);
        END IF;
    END LOOP;

    FOR target IN
        SELECT c.oid::regclass AS rel, c.relname, c.relrowsecurity
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'storage'
           AND c.relkind IN ('r', 'p')
    LOOP
        -- Row-level security is the authorization mechanism here rather than an
        -- obstacle to it: storage policies *are* RLS policies, which is why
        -- Phase 08 found Supabase enables RLS on these tables by design
        -- (`services/migrate/source.py:248`). Upstream enables it on every
        -- table it creates; this makes that a property of the tenant rather
        -- than a property of the version of `storage-api` that migrated it.
        --
        -- The consequence if it were ever missed is specific: bootstrap 004's
        -- grant posture (ADR-018) means denial should look like an empty set
        -- rather than an error, so upstream grants broadly to `anon` and
        -- `authenticated` and lets RLS decide. A table in `storage` carrying
        -- those grants with RLS **off** is world-readable to anyone holding the
        -- project's publishable key.
        IF NOT target.relrowsecurity THEN
            EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target.rel);
            changed := changed + 1;
        END IF;

        IF target.relname = ANY(unexposed) THEN
            FOREACH grantee IN ARRAY shared
            LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee) THEN
                    EXECUTE format('REVOKE ALL ON TABLE %s FROM %I', target.rel, grantee);
                END IF;
            END LOOP;
            EXECUTE format('REVOKE ALL ON TABLE %s FROM PUBLIC', target.rel);
        END IF;
    END LOOP;

    RETURN changed;
END $$;

-- Only removes privileges and enables RLS, so reaching it would not be an
-- escalation -- but a SECURITY DEFINER function owned by a superuser inside a
-- customer's database should not be customer-reachable. Bootstrap 005 and 010
-- say the same about theirs.
REVOKE ALL ON FUNCTION maludb_platform.harden_storage_schema() FROM PUBLIC;


CREATE OR REPLACE FUNCTION maludb_platform.on_storage_ddl()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Filtered rather than unconditional: this trigger's tag list covers the
    -- ordinary DDL a customer runs on their own tables, and re-hardening
    -- `storage` on every `CREATE TABLE` in `public` would put a scan of the
    -- schema on a path that has nothing to do with it.
    IF EXISTS (
        SELECT 1 FROM pg_event_trigger_ddl_commands()
         WHERE schema_name = 'storage'
            OR object_identity = 'storage'
            OR object_identity LIKE 'storage.%'
    ) THEN
        PERFORM maludb_platform.harden_storage_schema();
    END IF;
END $$;

-- Deliberately not exception-handled, on bootstrap 005's reasoning: if the
-- hardening cannot be applied, the DDL that would have outrun it must not
-- commit. An aborted `storage-api` migration is recoverable and loud; a tenant
-- serving an ungoverned table in `storage` is neither.
--
-- `GRANT` and `REVOKE` are **not** in the tag list, and their absence is the
-- reason slice 3 must call `harden_storage_schema()` explicitly after running
-- upstream's migrations rather than relying on this: upstream's grant-only
-- migrations (0046, 0049) issue no DDL this fires on, and a hardening function
-- that issues GRANT and REVOKE should not be reachable from a trigger on GRANT
-- and REVOKE.
DROP EVENT TRIGGER IF EXISTS maludb_harden_storage;
CREATE EVENT TRIGGER maludb_harden_storage
    ON ddl_command_end
    WHEN TAG IN (
        'CREATE SCHEMA', 'ALTER SCHEMA',
        'CREATE TABLE', 'CREATE TABLE AS', 'ALTER TABLE',
        'CREATE VIEW', 'CREATE MATERIALIZED VIEW',
        'CREATE FUNCTION', 'CREATE TYPE', 'CREATE INDEX'
    )
    EXECUTE FUNCTION maludb_platform.on_storage_ddl();

-- Repair pass, for a tenant that arrives here with a `storage` schema already
-- populated -- a Supabase migration (Phase 08), or a re-bootstrap of a tenant
-- the storage worker has already served.
SELECT maludb_platform.harden_storage_schema();
