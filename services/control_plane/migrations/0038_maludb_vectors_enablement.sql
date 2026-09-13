-- ADR-077 decision 6: vector compartments are their own opt-in, beside the
-- data-model graph's flag rather than folded into it, so enabling one MaluDB
-- surface never turns another on.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS maludb_vectors_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- When it was first turned on. Kept across re-runs and a disable, like the
    -- data-model graph's.
    ADD COLUMN IF NOT EXISTS maludb_vectors_enabled_at TIMESTAMPTZ;

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_maludb_vectors_recorded;
ALTER TABLE projects
    ADD CONSTRAINT projects_maludb_vectors_recorded
    CHECK (NOT maludb_vectors_enabled OR maludb_vectors_enabled_at IS NOT NULL);
