-- The `supabase_realtime` publication, created empty for every tenant.
--
-- Upstream Supabase ships this publication and its dashboard adds tables to it;
-- a client subscribing to Postgres Changes for a table that is not in it simply
-- receives nothing. AGENTS.md's compatibility rule prefers upstream behaviour,
-- so the name and the semantics are theirs, not ours.
--
-- Created for **every** tenant rather than on enablement, which is the opposite
-- of how the replication slot is handled, and deliberately:
--
--   * An empty publication costs nothing. It is a catalogue row, it reserves no
--     WAL, and it is invisible until a slot decodes through it. The slot is the
--     scarce, dangerous resource (ADR-032); the publication is not.
--   * Enabling Realtime for a project should not need a schema change inside
--     the tenant database. A tenant provisioned today and upgraded in a year
--     must not depend on a bootstrap file having been applied in between.
--
-- Not `FOR ALL TABLES`: that form requires superuser to create *and* to own, so
-- a customer could never manage it, and it would put every table a tenant ever
-- creates into the WAL stream whether or not anybody subscribes to it.

DO $$
DECLARE
    admin_role text;
BEGIN
    -- Derived like bootstrap 007 and 008: TenantNames builds the database as
    -- mldb_<ref> and the admin role as <database>_admin, from a ref validated
    -- against a strict alphabet.
    admin_role := current_database() || '_admin';

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = admin_role) THEN
        RAISE EXCEPTION 'no admin role % for this tenant', admin_role;
    END IF;

    -- CREATE PUBLICATION has no IF NOT EXISTS, and bootstrap files must be
    -- re-runnable against a tenant that already has one.
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        CREATE PUBLICATION supabase_realtime;
    END IF;

    -- Owned by the tenant admin, which is what lets a paid customer run
    -- `ALTER PUBLICATION supabase_realtime ADD TABLE x` exactly as they would
    -- against Supabase. Publications are database-scoped, so this confers
    -- nothing outside this tenant, and it is not database ownership: ADR-004
    -- still holds and the platform still owns the database and the schema.
    --
    -- Note what this does *not* decide. Adding a table to the publication
    -- controls what the Realtime server is told about; it is not the
    -- authorisation boundary. A replication consumer reads every table in the
    -- database past grants and row-level security anyway (ADR-031), so RLS for
    -- Postgres Changes is enforced in the Realtime server. A customer removing
    -- a table from the publication is choosing not to broadcast it, not
    -- securing it.
    EXECUTE format('ALTER PUBLICATION supabase_realtime OWNER TO %I', admin_role);
END $$;
