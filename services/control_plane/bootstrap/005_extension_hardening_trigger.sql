-- ADR-018, continued: keep the revoke true over time, not just at bootstrap.
--
-- 003 revokes EXECUTE on the extension functions that exist when it runs, and
-- is then recorded as applied and never runs again. Anything installed
-- afterwards lands in `public` with PostgreSQL's default EXECUTE grant to
-- PUBLIC and is immediately callable by anon and authenticated -- which is to
-- say, exposed as PostgREST RPC to anyone holding the publishable key.
--
-- That is not hypothetical. ADR-015 puts maludb_core in every tenant database
-- and makes upgrades a per-tenant fleet operation; the extension ships 146
-- update scripts. An `ALTER EXTENSION maludb_core UPDATE` that adds a function
-- would re-open the Phase 00 finding across the whole fleet at once, silently.
--
-- So the property is maintained by the database rather than by remembering to
-- re-run a script: an event trigger re-applies the revoke after any extension
-- is created or altered. Verified by installing uuid-ossp into a bootstrapped
-- tenant -- without this, ten functions became anon-callable and anon
-- successfully invoked uuid_generate_v4().

-- The revoke itself, callable on its own so the fleet-upgrade runbook and any
-- future repair path can invoke it directly rather than duplicating the logic.
--
-- SECURITY DEFINER because the role performing the DDL is usually not the one
-- that can revoke on the result: a tenant admin installing a trusted extension
-- ends up with functions owned by the bootstrap superuser, which the tenant
-- admin has no privilege to REVOKE on. search_path is pinned to pg_catalog so
-- nothing in the body resolves through a schema the tenant controls -- which
-- also makes regprocedure render schema-qualified, since `public` is then not
-- in the path.
CREATE OR REPLACE FUNCTION maludb_platform.harden_extension_functions()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    target  record;
    grantee text;
    revoked integer := 0;
BEGIN
    FOR target IN
        SELECT p.oid::regprocedure AS signature
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
         WHERE n.nspname = 'public'
    LOOP
        -- PUBLIC carries the default grant; anon and authenticated are revoked
        -- separately because an explicit grant to a role survives a revoke
        -- from PUBLIC. ROUTINE rather than FUNCTION so procedures and
        -- aggregates are covered too.
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM PUBLIC', target.signature);
        FOREACH grantee IN ARRAY ARRAY['anon', 'authenticated']
        LOOP
            -- A missing role would raise and, from the event trigger, abort
            -- the DDL that fired it. The shared roles are cluster-scoped and
            -- should always exist; tolerate their absence rather than making
            -- extension management depend on it.
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee) THEN
                EXECUTE format('REVOKE ALL ON ROUTINE %s FROM %I', target.signature, grantee);
            END IF;
        END LOOP;
        revoked := revoked + 1;
    END LOOP;

    RETURN revoked;
END $$;

-- Nothing else has a reason to call it. It only removes privileges, so reaching
-- it would not be an escalation, but a SECURITY DEFINER function owned by a
-- superuser inside a customer's database should not be customer-reachable.
REVOKE ALL ON FUNCTION maludb_platform.harden_extension_functions() FROM PUBLIC;

CREATE OR REPLACE FUNCTION maludb_platform.on_extension_change()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM maludb_platform.harden_extension_functions();
END $$;

-- Deliberately not exception-handled. If the revoke cannot be applied, the
-- extension must not be installed: an aborted CREATE EXTENSION is recoverable,
-- a tenant serving unrevoked functions on the public API is not.
DROP EVENT TRIGGER IF EXISTS maludb_harden_extensions;
CREATE EVENT TRIGGER maludb_harden_extensions
    ON ddl_command_end
    WHEN TAG IN ('CREATE EXTENSION', 'ALTER EXTENSION')
    EXECUTE FUNCTION maludb_platform.on_extension_change();

-- Repair pass, for a tenant bootstrapped before this file existed that has had
-- an extension installed in the meantime.
SELECT maludb_platform.harden_extension_functions();
