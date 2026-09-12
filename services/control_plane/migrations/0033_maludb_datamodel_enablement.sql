-- Phase 12 slice 2 (ADR-074 decision 2): a project turns the data-model graph on.
--
-- ADR-074 amends ADR-015, which ruled out a "MaluDB-enabled" project flag: the
-- extension stays in every tenant database unconditionally, and this flag is
-- about the customer-facing surface built on top of it. Realtime's
-- `realtime_enabled` (migration 0012) is the precedent for its shape.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS maludb_datamodel_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- When it was first turned on. Kept across re-runs, which are idempotent and
    -- must not make an old enablement look new.
    ADD COLUMN IF NOT EXISTS maludb_datamodel_enabled_at TIMESTAMPTZ,
    -- The version `enable_memory_schema` returned. Phase 12 slice 0 found nothing
    -- else reports a schema's facade version: a stale schema looks current until
    -- something missing is called. Enablement writes it; an extension upgrade that
    -- re-enables the schema writes it again.
    ADD COLUMN IF NOT EXISTS maludb_memory_schema_version TEXT;

-- An enabled flag with no recorded version is an enablement that did not finish
-- recording itself. Refused here rather than discovered later.
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_maludb_datamodel_recorded;
ALTER TABLE projects
    ADD CONSTRAINT projects_maludb_datamodel_recorded
    CHECK (NOT maludb_datamodel_enabled
           OR (maludb_datamodel_enabled_at IS NOT NULL AND maludb_memory_schema_version IS NOT NULL));
