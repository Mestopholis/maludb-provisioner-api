-- Phase 00 finding 3: PostgREST caches the schema, and a table created after
-- the worker started is invisible until it is told to look again.
--
-- Measured during the spike: a newly created table returned
--   PGRST205: Could not find the table 'public.notes' in the schema cache
-- until `NOTIFY pgrst, 'reload schema'` was issued.
--
-- Any customer schema change reaches this database without the control plane
-- involved -- the dashboard SQL editor, a migration tool, an ORM push, or a
-- paid direct-SQL session. So the reload cannot be something the control plane
-- remembers to send after operations it knows about; it has to fire on the DDL
-- itself. The Phase 00 findings say this explicitly: "it cannot be left to
-- worker restarts".
--
-- NOTIFY is transactional. The notification is delivered at commit, so a rolled
-- back migration never announces a schema that does not exist, and a
-- multi-statement migration reloads once at the end rather than per statement.

CREATE OR REPLACE FUNCTION maludb_platform.notify_schema_reload()
RETURNS event_trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    NOTIFY pgrst, 'reload schema';
END $$;

-- Two triggers, because one event does not cover both directions. ddl_command_end
-- sees CREATE and ALTER; a DROP is reported by sql_drop instead, and a dropped
-- table left in the cache is worse than a missing one -- PostgREST would keep
-- advertising an endpoint that now errors.
DROP EVENT TRIGGER IF EXISTS maludb_pgrst_reload_ddl;
CREATE EVENT TRIGGER maludb_pgrst_reload_ddl
    ON ddl_command_end
    EXECUTE FUNCTION maludb_platform.notify_schema_reload();

DROP EVENT TRIGGER IF EXISTS maludb_pgrst_reload_drop;
CREATE EVENT TRIGGER maludb_pgrst_reload_drop
    ON sql_drop
    EXECUTE FUNCTION maludb_platform.notify_schema_reload();
