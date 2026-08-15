-- Phase 04 slice 1: per-project GoTrue workers (ADR-007, ADR-022, ADR-027).
--
-- Tracked separately from the PostgREST worker rather than folded into
-- worker_state. ADR-022 measured Auth as the single largest per-project
-- allocation -- 17.6 MB PSS of the 31.8 MB a warm project costs -- and
-- requires it not be started for projects that do not use Auth. Collapsing the
-- two into one "warm" flag would make that saving impossible to express and
-- impossible to account for.

-- Its own port on the node, on loopback like the API worker.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS auth_port INTEGER;

-- Same constraint as api_port: two projects on one node cannot share a port.
-- Deliberately a separate index rather than a shared pool, because a port is
-- either an API port or an Auth port and a collision between the two would be
-- a routing bug rather than an allocation one.
CREATE UNIQUE INDEX IF NOT EXISTS projects_node_auth_port_idx
    ON projects(node_id, auth_port) WHERE auth_port IS NOT NULL;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS auth_worker_state VARCHAR(20) NOT NULL DEFAULT 'STOPPED';

ALTER TABLE projects
    ADD CONSTRAINT projects_auth_worker_state_check
    CHECK (auth_worker_state IN ('STOPPED', 'STARTING', 'RUNNING', 'FAILED'));

ALTER TABLE projects ADD COLUMN IF NOT EXISTS auth_worker_last_active_at TIMESTAMPTZ;

-- Whether this project uses Auth at all. Default false is the ADR-022
-- requirement made structural: a project gets an Auth worker because something
-- asked for one, not because it exists.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS auth_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- When GoTrue's migrations last ran against this tenant, and at what version of
-- our own bootstrap contract. Recorded in the control plane for the same reason
-- bootstrap_version is (ADR-015 and tenant_bootstrap): answering "which tenants
-- are behind" should not require connecting to every database.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS auth_migrated_at TIMESTAMPTZ;
