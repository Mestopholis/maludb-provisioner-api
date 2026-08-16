-- Make `mldb_<ref>_admin` the role specs/tenant-role-model.md already claims it
-- is: the one a paid customer uses for direct SQL.
--
-- It was created with no privilege on `public` at all -- not CREATE, not even
-- USAGE. On PostgreSQL 15+ the schema no longer grants CREATE to PUBLIC, so a
-- customer connecting as this role got `permission denied for schema public`
-- and could not create a table. Nothing had noticed because every table in a
-- tenant is created by the platform superuser, including in the tests.
--
-- Found 2026-08-16 while writing the storage-restriction tests.
--
-- Two separate problems, and fixing only the first would have produced a worse
-- bug than the one it replaced:
--
-- 1. The role could not create anything.
-- 2. `ALTER DEFAULT PRIVILEGES` only affects objects created by the role it
--    names, and bootstrap 004 named only the bootstrapping role. So a table the
--    admin created would carry **no grant for anon or authenticated** -- and
--    Phase 00 finding 7 established that no grant surfaces to an application as
--    `42501 permission denied`, not as an empty result. A customer creating
--    their first table through direct SQL would find it invisible to their own
--    Data API, with an error that says nothing about grants.

DO $$
DECLARE
    admin_role text;
BEGIN
    -- Derived like bootstrap 007's: TenantNames builds the database as
    -- mldb_<ref> and the admin role as <database>_admin, from a ref validated
    -- against a strict alphabet.
    admin_role := current_database() || '_admin';

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = admin_role) THEN
        RAISE EXCEPTION 'no admin role % for this tenant', admin_role;
    END IF;

    -- CREATE, so the role can make tables. Not ownership of the schema: the
    -- platform owns the database and the schema (ADR-004), and a customer that
    -- owned `public` could drop it along with the auth helpers and the
    -- platform's bookkeeping.
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA public TO %I', admin_role);

    -- So a table the customer creates is reachable by their own Data API.
    -- Matching bootstrap 004's grants exactly: the alternative is a table that
    -- exists, that RLS would happily filter, and that answers 42501 instead.
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT ALL ON TABLES TO anon, authenticated, service_role', admin_role
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT ALL ON SEQUENCES TO anon, authenticated, service_role', admin_role
    );

    -- The auth helpers, so a policy on a customer's own table can call them.
    EXECUTE format('GRANT USAGE ON SCHEMA auth TO %I', admin_role);
END $$;
