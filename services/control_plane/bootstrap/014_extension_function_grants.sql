-- ADR-076 decisions 1 and 4: customer roles execute extension functions.
--
-- ADR-018 revoked EXECUTE on every extension function from PUBLIC so that anon
-- could not call /rpc/gen_salt. Every other role held EXECUTE only through
-- PUBLIC, and nothing granted it back, so no customer role could use them at
-- all: a pgvector distance query, a `uuid_generate_v4()` column default and
-- `crypt()` in a trigger were all `permission denied` -- for `service_role` and
-- the tenant's own admin as much as for anon (pinning slice 0, finding 7).
--
-- The RPC exposure is now closed by bootstrap 013's pre-request check, so this
-- file is only safe once that check is live in the tenant's PostgREST.
-- `tenant_bootstrap.apply` holds it back unless the caller says so: a new tenant
-- has no worker yet, and an existing one waits for the fleet run that reloads
-- its worker and confirms the refusal first (grants slice 2).
--
-- What changes, in `harden_extension_functions`, which the existing event
-- trigger calls on every CREATE and ALTER EXTENSION:
--
-- * PUBLIC stays revoked. The grant is explicit, to the six customer roles --
--   anon, authenticated, service_role and the tenant's admin, client and
--   executor -- so it is something the platform states and `verify` asserts,
--   and roles outside the tenant, MaluDB's own among them, gain nothing.
-- * **maludb_core's functions are excluded** (ADR-076, as amended during grants
--   slice 1). Its 544 include 94 SECURITY DEFINER functions owned by the node
--   superuser and twelve in `mc2db`, a schema PUBLIC can already reach, some of
--   which write MaluDB's MCP registry (`register_tool`, `create_server`). They
--   keep today's posture: reachable through MaluDB's own roles and ADR-074's
--   platform copy, never by a customer role.
--
-- The function keeps its name so the trigger in 005 needs no change, and so
-- the tenants recorded as having 005 and 011 pick this up by CREATE OR REPLACE.

CREATE OR REPLACE FUNCTION maludb_platform.harden_extension_functions()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    target    record;
    grantee   text;
    grantees  text;
    base      text := current_database();
    touched   integer := 0;
BEGIN
    -- Derived like bootstraps 007, 008 and 012: TenantNames builds the database
    -- as mldb_<ref> and each role as <database>_<suffix>, from a ref validated
    -- against a strict alphabet. A project provisioned before the client or
    -- executor role existed is granted what it has; `verify` names the rest.
    SELECT string_agg(quote_ident(r.rolname), ', ' ORDER BY r.rolname)
      INTO grantees
      FROM pg_roles r
     WHERE r.rolname IN ('anon', 'authenticated', 'service_role',
                         base || '_admin', base || '_client', base || '_executor');

    FOR target IN
        SELECT p.oid::regprocedure AS signature, e.extname
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          -- classid, which 011 did not constrain: an objid is only unique
          -- within its catalogue, so without it a dependency recorded for some
          -- other kind of object could name a function by coincidence.
          JOIN pg_depend d ON d.classid = 'pg_proc'::regclass
                          AND d.objid = p.oid
                          AND d.deptype = 'e'
          JOIN pg_extension e ON e.oid = d.refobjid
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    LOOP
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM PUBLIC', target.signature);

        IF target.extname = 'maludb_core' THEN
            FOREACH grantee IN ARRAY ARRAY['anon', 'authenticated']
            LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee) THEN
                    EXECUTE format('REVOKE ALL ON ROUTINE %s FROM %I', target.signature, grantee);
                END IF;
            END LOOP;
        ELSIF grantees IS NOT NULL THEN
            EXECUTE format('GRANT EXECUTE ON ROUTINE %s TO %s', target.signature, grantees);
        END IF;
        touched := touched + 1;
    END LOOP;

    RETURN touched;
END $$;

REVOKE ALL ON FUNCTION maludb_platform.harden_extension_functions() FROM PUBLIC;

-- Repair pass: everything already installed gets the new posture now, not at
-- the next extension change.
SELECT maludb_platform.harden_extension_functions();
