-- Phase 03 slice 2: per-project PostgREST workers (ADR-007, ADR-022, ADR-027).

-- Where this project's worker listens. Bound to loopback, never a public
-- interface -- docs/API-GATEWAY.md requires internal worker endpoints to be
-- unreachable from the internet, and a loopback bind makes that a property of
-- the socket rather than of a firewall rule somebody has to maintain.
--
-- A unix socket would remove this column entirely and be private by
-- construction, but it also forces the gateway onto the same host as every
-- worker. "Separate API worker hosts vs colocated on DB nodes" is still an open
-- question in docs/OPEN-QUESTIONS.md, so this takes the option that does not
-- quietly decide it.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS api_port INTEGER;

-- Unique per node, not globally: two projects on different nodes may share a
-- port number, and requiring otherwise would exhaust the range for no reason.
CREATE UNIQUE INDEX IF NOT EXISTS projects_node_api_port_idx
    ON projects(node_id, api_port) WHERE api_port IS NOT NULL;

-- Worker lifecycle, separate from project status. A free project can be ACTIVE
-- and asleep at the same time: ADR-022 measured that a slept project costs zero
-- RAM and zero connections, and free-tier economics rest entirely on that.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS worker_state VARCHAR(20) NOT NULL DEFAULT 'STOPPED';

ALTER TABLE projects
    ADD CONSTRAINT projects_worker_state_check
    CHECK (worker_state IN ('STOPPED', 'STARTING', 'RUNNING', 'FAILED'));

-- Last request served, so a sleep policy has something to age on.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS worker_last_active_at TIMESTAMPTZ;

-- The warm count is read on every placement decision.
CREATE INDEX IF NOT EXISTS projects_node_warm_idx
    ON projects(node_id) WHERE worker_state = 'RUNNING' AND deleted_at IS NULL;
