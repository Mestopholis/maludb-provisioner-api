-- ADR-018: keep extension functions off the public Data API.
--
-- CREATE EXTENSION maludb_core CASCADE installs 373 functions into `public`.
-- PostgREST exposes the callable subset as RPC, and during the Phase 00 spike
-- `anon` successfully invoked /rpc/gen_salt among others.
--
-- Supabase's mitigation -- installing extensions into their own schema -- is
-- unavailable here: maludb_core hard-codes public.gen_random_bytes, so
-- relocating its dependencies fails the install outright. Revoking EXECUTE is
-- the verified alternative.
--
-- Scoped to extension-owned functions specifically (pg_depend deptype 'e'), so
-- a tenant's own functions in public keep working and stay callable as RPC.

DO $$
DECLARE
    target record;
    revoked integer := 0;
BEGIN
    FOR target IN
        SELECT p.oid::regprocedure AS signature
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
         WHERE n.nspname = 'public'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', target.signature);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', target.signature);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', target.signature);
        revoked := revoked + 1;
    END LOOP;
    RAISE NOTICE 'revoked EXECUTE on % extension functions in public', revoked;
END $$;
