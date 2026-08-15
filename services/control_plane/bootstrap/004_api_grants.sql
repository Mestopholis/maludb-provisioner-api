-- The `anon` grant posture, chosen deliberately (ADR-018).
--
-- Phase 00 finding: with no grant, a public read returns `42501 permission
-- denied for table` rather than an empty result set. Supabase convention is to
-- grant and let RLS return empty, and migrated applications depend on that
-- difference in their error handling -- so compatibility decides this, not
-- preference.
--
-- The consequence is load-bearing and must be understood: a table created
-- WITHOUT row-level security enabled is readable by anyone holding the
-- publishable key. That is Supabase's model too. RLS is the control; these
-- grants only decide whether a denial looks like an error or an empty set.
-- maludb_platform.tables_without_rls exists so the dashboard can surface it.

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA auth   TO anon, authenticated, service_role;

GRANT EXECUTE ON FUNCTION auth.jwt(), auth.uid(), auth.role(), auth.email()
    TO anon, authenticated, service_role;

-- Existing and future objects created by the database owner.
GRANT ALL ON ALL TABLES    IN SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;

-- Diagnostic, not enforcement. Enabling RLS automatically would change the
-- behaviour of a migrated application, which ADR-001 forbids.
CREATE OR REPLACE VIEW maludb_platform.tables_without_rls AS
    SELECT n.nspname AS schema_name, c.relname AS table_name
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity;
