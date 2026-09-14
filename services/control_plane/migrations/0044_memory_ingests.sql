-- ADR-079 decision 3, memory slice 5a: the queue between a customer's ingest
-- request and the memory worker.
--
-- Written by the gateway, which validated the project's secret key (the user's
-- choice for slice 5: one credential for an agent's whole memory surface), and
-- claimed by the memory worker, which writes as the project's memory writer.
--
-- **`items_json` is customer content in the control plane**, which is why it is
-- held only as long as the work needs it: the worker clears it when the request
-- completes and keeps only `results_json` -- per item, what was written or why it
-- was not. The gateway role reads and writes rows for its own node's projects
-- only (ADR-072's policy below).
CREATE TABLE IF NOT EXISTS memory_ingests (
    id             UUID PRIMARY KEY,
    project_id     UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    space_id       BIGINT NOT NULL REFERENCES memory_spaces(id) ON DELETE CASCADE,
    state          VARCHAR(20) NOT NULL DEFAULT 'pending',
    item_count     INTEGER NOT NULL,
    items_json     JSONB,
    results_json   JSONB,
    written        INTEGER,
    failed         INTEGER,
    detail         TEXT,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    CONSTRAINT memory_ingests_state_check CHECK (state IN ('pending', 'running', 'succeeded', 'partial', 'failed')),
    CONSTRAINT memory_ingests_items_check CHECK (item_count BETWEEN 1 AND 100),
    -- Pending and running rows need their items; finished ones must not keep them.
    CONSTRAINT memory_ingests_payload_held_only_while_needed
        CHECK ((state IN ('pending', 'running')) = (items_json IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS memory_ingests_claim ON memory_ingests (requested_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS memory_ingests_project_recent ON memory_ingests (project_id, requested_at DESC);

-- What a space holds, counted by the worker as it writes, so the gateway can hold
-- `memory_max_items` without reaching a tenant database.
ALTER TABLE memory_spaces ADD COLUMN IF NOT EXISTS item_count BIGINT NOT NULL DEFAULT 0;

ALTER TABLE memory_ingests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS gateway_own_node ON memory_ingests;
CREATE POLICY gateway_own_node ON memory_ingests
    FOR ALL USING (project_id IN (SELECT p.id FROM public.projects p
                                   WHERE p.node_id = public.gateway_node_id()))
    WITH CHECK (project_id IN (SELECT p.id FROM public.projects p
                                WHERE p.node_id = public.gateway_node_id()));
