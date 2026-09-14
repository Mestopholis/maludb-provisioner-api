-- ADR-079, memory slice 2a: a project's named memory spaces.
--
-- A space is a MaluDB memory schema (`enable_memory_schema`) the platform builds
-- in the tenant database and reaches only through its own code (decision 1).
-- The row is written when a customer asks -- `pending`, which reserves the name
-- and counts against the plan's `memory_max_spaces` under the project's row lock
-- -- and the provisioner builds every pending space for the project in one job
-- (ADR-038: node work runs as the node superuser, away from the public app).
--
-- `schema_name` is derived from `name` by the platform (`mem_<name>`), never
-- supplied: the customer's text becomes an SQL identifier only through a fixed
-- pattern that cannot name a platform or extension schema.
CREATE TABLE IF NOT EXISTS memory_spaces (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id             UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                   VARCHAR(40) NOT NULL,
    schema_name            VARCHAR(63) NOT NULL,
    state                  VARCHAR(20) NOT NULL DEFAULT 'pending',
    requested_by           UUID REFERENCES users(id),
    requested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    active_at              TIMESTAMPTZ,
    memory_schema_version  TEXT,
    detail                 TEXT,
    CONSTRAINT memory_spaces_name_check CHECK (name ~ '^[a-z][a-z0-9_]{0,39}$'),
    CONSTRAINT memory_spaces_schema_check CHECK (schema_name = 'mem_' || name),
    CONSTRAINT memory_spaces_state_check CHECK (state IN ('pending', 'active', 'failed')),
    CONSTRAINT memory_spaces_active_recorded
        CHECK (state <> 'active' OR (active_at IS NOT NULL AND memory_schema_version IS NOT NULL)),
    CONSTRAINT memory_spaces_unique_name UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS memory_spaces_pending ON memory_spaces (project_id) WHERE state = 'pending';

-- ADR-072: every project-keyed table carries the gateway's own-node policy.
ALTER TABLE memory_spaces ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS gateway_own_node ON memory_spaces;
CREATE POLICY gateway_own_node ON memory_spaces
    FOR ALL USING (project_id IN (SELECT p.id FROM public.projects p
                                   WHERE p.node_id = public.gateway_node_id()))
    WITH CHECK (project_id IN (SELECT p.id FROM public.projects p
                                WHERE p.node_id = public.gateway_node_id()));

ALTER TABLE maludb_jobs DROP CONSTRAINT IF EXISTS maludb_jobs_kind_check;
ALTER TABLE maludb_jobs
    ADD CONSTRAINT maludb_jobs_kind_check
    CHECK (kind IN ('enable', 'refresh', 'disable', 'vectors_enable', 'vectors_disable', 'memory_spaces'));
