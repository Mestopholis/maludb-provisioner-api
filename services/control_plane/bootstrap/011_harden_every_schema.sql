-- ADR-018, third time, and the first time it covers the whole database.
--
-- Bootstrap 005's revoke loop filters `WHERE n.nspname = 'public'`. That was
-- sufficient while both halves of its premise held: every extension in a tenant
-- was installed by the platform into `public`, and no tenant role could
-- `CREATE EXTENSION` at all.
--
-- Bootstrap 010 ends both. The admin role now holds `CREATE ON DATABASE`, which
-- is `CREATE SCHEMA` as well as trusted-extension installs, and
-- `CREATE EXTENSION ... SCHEMA <mine>` puts the functions somewhere 005 never
-- looked. Measured during the slice 6a security review, as the tenant admin:
--
--     CREATE SCHEMA zz_s;
--     CREATE EXTENSION pgcrypto SCHEMA zz_s;   -- allowlisted, so permitted
--     GRANT USAGE ON SCHEMA zz_s TO anon;      -- the customer owns zz_s
--     -- => anon holds EXECUTE on digest(), hmac(), ...
--
-- `specs/tenant-role-model.md` lists "grant extension functions to anon, which
-- would undo ADR-018 for the project's own public API" among the things the
-- admin role must never be able to do, and `tests/test_direct_sql.py` asserts
-- it -- by trying the *direct* grant on a function in `public`, which is still
-- refused. The invariant had become true by accident rather than by
-- construction.
--
-- So the filter goes. The revoke now covers every routine owned by an
-- extension, whatever schema it landed in. `search_path` is still pinned to
-- `pg_catalog`, which is what makes `regprocedure` render schema-qualified, so
-- `format('REVOKE ALL ON ROUTINE %s ...')` works unchanged for a function
-- outside `public`.
--
-- A new file rather than an edit: 005 is immutable once applied, and tenants
-- provisioned before today have it recorded. `CREATE OR REPLACE` here is what
-- reaches them, and the trailing repair call re-applies the wider revoke to
-- anything already installed.

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
         -- No schema filter. A customer may now create schemas and install
         -- allowlisted extensions into them (bootstrap 010), and an extension
         -- function is exactly as reachable from `anon` in `their_schema` as in
         -- `public` once they grant USAGE on it -- which they may, because they
         -- own the schema.
         WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    LOOP
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM PUBLIC', target.signature);
        FOREACH grantee IN ARRAY ARRAY['anon', 'authenticated']
        LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee) THEN
                EXECUTE format('REVOKE ALL ON ROUTINE %s FROM %I', target.signature, grantee);
            END IF;
        END LOOP;
        revoked := revoked + 1;
    END LOOP;

    RETURN revoked;
END $$;

REVOKE ALL ON FUNCTION maludb_platform.harden_extension_functions() FROM PUBLIC;

-- Repair pass for anything installed outside `public` before this file existed.
SELECT maludb_platform.harden_extension_functions();
