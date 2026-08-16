-- Phase 06 slice 5: per-project Realtime workers (ADR-027, ADR-033, ADR-034).
--
-- ADR-034 returned Realtime to ADR-007's per-project worker model, so it gets
-- the same columns the API and Auth workers have. Slice 3 argued the opposite
-- and was right at the time: under ADR-031's shared server there was no port to
-- look up and nothing to sleep. There is now one of each.

-- The loopback port the node's Realtime instance for this project publishes.
-- **One port, not two.** Slice 4 measured two because it ran the container on
-- the host's network; with a network namespace per instance, gen_rpc's port is
-- identical in every instance and never leaves the namespace.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_port INTEGER;

-- Same shape as the API and Auth ports: two projects on one node cannot share
-- one, and a collision would be a routing bug rather than an allocation one.
CREATE UNIQUE INDEX IF NOT EXISTS projects_node_realtime_port_idx
    ON projects(node_id, realtime_port) WHERE realtime_port IS NOT NULL;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_worker_state VARCHAR(20) NOT NULL DEFAULT 'STOPPED';

ALTER TABLE projects
    ADD CONSTRAINT projects_realtime_worker_state_check
    CHECK (realtime_worker_state IN ('STOPPED', 'STARTING', 'RUNNING', 'FAILED'));

-- Feeds the sleep policy, and it matters more here than anywhere else: ADR-034
-- measured a Realtime instance at ~146 MB against 31.8 MB for an entire warm
-- project, so an idle one is the largest single thing a node can reclaim.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_worker_last_active_at TIMESTAMPTZ;

-- When this project was last registered with its own Realtime server over the
-- server's admin API. Recorded because registration is what makes the tenant
-- resolvable at all: an instance that is running with no tenant registered
-- answers a client's handshake and then refuses every channel, which is
-- indistinguishable from a configuration problem unless the platform knows
-- which of the two it did.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_registered_at TIMESTAMPTZ;
