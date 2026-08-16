-- Phase 06 slice 1: replication-slot accounting and invalidation state.
--
-- Nothing here enables Realtime for anybody -- that is slice 2. What it adds is
-- the thing slice 1 must be able to count and the thing it must be able to
-- report, because ADR-031 and ADR-032 both turn on facts the control plane does
-- not currently record: how many logical replication slots a node has committed,
-- and whether any of them has been invalidated.
--
-- The slot ceiling is a third resource alongside projects and connections, and
-- specs/realtime-replication-model.md measured it as the tightest of the three:
-- max_replication_slots defaults to 10 against ADR-022's warm ceiling of ~24
-- projects. Capacity cannot enforce a ceiling it cannot count against.

-- Whether this project holds a logical replication slot on its node. Distinct
-- from "the plan entitles it to Realtime": entitlement says what a project may
-- have, this says what it is actually consuming from a cluster-wide pool of ten.
-- Placement must count the second, not the first.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- The slot's name on the node, recorded rather than derived, so a maintenance
-- pass can match what PostgreSQL reports against what the platform believes it
-- created -- including the case where the two disagree.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_slot_name VARCHAR(63);

-- none    -- Realtime is not enabled for this project
-- pending -- enablement recorded, the slot has not been observed yet
-- active  -- the node reports the slot present and reserving WAL
-- lost    -- ADR-032: invalidated, wal_status='lost'. The project has silently
--            stopped receiving changes, and re-creating the slot resumes from
--            the present without replaying the gap.
-- missing -- the platform believes in a slot the node does not have. Different
--            from 'lost' and worth distinguishing: 'lost' is PostgreSQL doing
--            what ADR-032 asked it to, 'missing' is drift nobody asked for.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_slot_state VARCHAR(20) NOT NULL DEFAULT 'none';

ALTER TABLE projects
    ADD CONSTRAINT projects_realtime_slot_state_check
    CHECK (realtime_slot_state IN ('none', 'pending', 'active', 'lost', 'missing'));

-- When the node was last asked. A stale check is not evidence of health, and an
-- operator reading a slot report needs to know which of the two they are seeing.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_slot_checked_at TIMESTAMPTZ;

-- When the slot was first observed invalidated, so the size of the gap a
-- customer's application missed is answerable without reading the audit log.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS realtime_slot_lost_at TIMESTAMPTZ;

-- Counting a node's committed slots is on the placement path, which holds a row
-- lock while it runs.
CREATE INDEX IF NOT EXISTS projects_realtime_node_idx
    ON projects(node_id) WHERE realtime_enabled AND deleted_at IS NULL;
