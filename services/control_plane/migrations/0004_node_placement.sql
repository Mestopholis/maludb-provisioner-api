-- Phase 02 slice 1: constraints and indexes supporting node placement.
--
-- No placement-reservation table. A reservation is precisely "this project is
-- assigned to this node", which projects.node_id already expresses; reserving
-- inside a transaction holding SELECT ... FOR UPDATE on the node row makes the
-- capacity check and the assignment atomic. See
-- plans/active/phase-02-tenant-provisioning.md.

-- Capacity is counted by scanning projects on a node, on every placement.
CREATE INDEX IF NOT EXISTS projects_node_idx ON projects(node_id) WHERE deleted_at IS NULL;

-- Node lifecycle states. 'draining' and 'maintenance' both stop new placement
-- but keep existing tenants served; 'unhealthy' is set by health reporting.
ALTER TABLE nodes
    ADD CONSTRAINT nodes_status_check
    CHECK (status IN ('active', 'draining', 'maintenance', 'unhealthy'));

-- Project lifecycle states, matching specs/provisioning-state-machine.md.
-- PROVISIONED is Phase 02's terminal state; Phase 03 carries a project to
-- ACTIVE once it has API workers and a route.
ALTER TABLE projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN (
        'REQUESTED', 'PLACEMENT_RESERVED', 'ROLES_CREATING', 'DATABASE_CREATING',
        'BOOTSTRAPPING', 'KEYS_CONFIGURING', 'VALIDATING', 'PROVISIONED',
        'API_CONFIGURING', 'ROUTING_CONFIGURING', 'ACTIVE',
        'PAUSING', 'PAUSED', 'RESUMING', 'SUSPENDING', 'SUSPENDED',
        'UPGRADING', 'DELETING', 'DELETED', 'RETRY_WAIT', 'FAILED'
    ));
