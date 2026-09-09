-- Phase 11 slice 7 (ADR-066): tenant movement is an operator-initiated
-- operation, not an automatic repair.
--
-- A move is not a plan change and not a restore activation. It copies one
-- tenant database to another node, verifies that the target has the same
-- tenant-owned schema posture, then changes the control-plane placement row.
-- The record exists because an operator needs to know which node used to hold
-- the database, whether the source was cleaned, and what failed if the move
-- stopped halfway through.

ALTER TABLE projects DROP CONSTRAINT projects_status_check;
ALTER TABLE projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN (
        'REQUESTED', 'PLACEMENT_RESERVED', 'ROLES_CREATING', 'DATABASE_CREATING',
        'EXECUTOR_CREATING', 'CLIENT_CREATING', 'STORAGE_ROLE_CREATING',
        'BOOTSTRAPPING', 'KEYS_CONFIGURING', 'VALIDATING', 'PROVISIONED',
        'API_CONFIGURING', 'ROUTING_CONFIGURING', 'ACTIVE',
        'PAUSING', 'PAUSED', 'RESUMING', 'SUSPENDING', 'SUSPENDED',
        'UPGRADING', 'MOVING', 'DELETING', 'DELETED', 'RETRY_WAIT', 'FAILED'
    ));

CREATE TABLE IF NOT EXISTS tenant_moves (
    id                  BIGSERIAL PRIMARY KEY,
    project_id          UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_node_id      INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_node_id      INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    source_database     TEXT NOT NULL,
    target_database     TEXT NOT NULL,
    original_status     TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'complete', 'failed')),
    ownership_verified  BOOLEAN,
    ownership_detail    TEXT,
    elapsed_seconds     NUMERIC(10, 2),
    dump_bytes          BIGINT,
    source_cleaned      BOOLEAN NOT NULL DEFAULT FALSE,
    error               TEXT,

    CONSTRAINT tenant_moves_different_nodes
        CHECK (source_node_id <> target_node_id),
    CONSTRAINT tenant_moves_finished_with_status
        CHECK ((status = 'running') = (finished_at IS NULL)),
    CONSTRAINT tenant_moves_complete_verified
        CHECK (status <> 'complete' OR ownership_verified IS TRUE)
);

CREATE INDEX IF NOT EXISTS tenant_moves_project_idx
    ON tenant_moves(project_id, started_at DESC);

CREATE INDEX IF NOT EXISTS tenant_moves_running_project_idx
    ON tenant_moves(project_id)
    WHERE status = 'running';
