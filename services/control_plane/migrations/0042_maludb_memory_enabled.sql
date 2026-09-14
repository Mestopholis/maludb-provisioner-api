-- ADR-079 memory slice 3: whether a project has a built memory space, and so a
-- search wrapper published in its `maludb` schema.
--
-- A flag on `projects` beside `maludb_datamodel_enabled` and
-- `maludb_vectors_enabled` rather than a query against `memory_spaces`, because
-- the gateway already reads those two to answer a request for the `maludb` schema
-- (ADR-074, ADR-077 decision 6), and a third column is what its narrowed role can
-- already read (ADR-072) -- no new grant on a new table for it.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS maludb_memory_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS maludb_memory_enabled_at TIMESTAMPTZ;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_maludb_memory_recorded;
ALTER TABLE projects
    ADD CONSTRAINT projects_maludb_memory_recorded
    CHECK (NOT maludb_memory_enabled OR maludb_memory_enabled_at IS NOT NULL);
