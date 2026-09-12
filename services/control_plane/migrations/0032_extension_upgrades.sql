-- Phase 12 slice 1 (ADR-074 decision 5): the fleet extension upgrade procedure.
--
-- `projects.extension_versions` (migration 0005) records what a tenant has now.
-- This records how it got there, and -- the reason it exists -- what happened
-- when it did not: an upgrade that stops at a failing tenant leaves that tenant
-- on its previous version, and an operator needs to know which one, why, and
-- that nothing after it on the node was touched.
--
-- One row per tenant per attempt. `current` rows are written too, so a run's
-- report can say "already at 0.104.0" rather than leaving an operator to wonder
-- whether a tenant was skipped or forgotten.

CREATE TABLE IF NOT EXISTS extension_upgrades (
    id                     BIGSERIAL PRIMARY KEY,
    project_id             UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id                BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    extension              TEXT NOT NULL DEFAULT 'maludb_core',
    from_version           TEXT,
    to_version             TEXT NOT NULL,
    -- upgraded: updated and verified, committed.
    -- current:  already at the target; nothing changed.
    -- failed:   rolled back; the tenant is still on from_version.
    -- skipped:  not attempted, because the project was mid-operation.
    status                 TEXT NOT NULL
                           CHECK (status IN ('upgraded', 'current', 'failed', 'skipped')),
    canary                 BOOLEAN NOT NULL DEFAULT FALSE,
    detail                 TEXT,
    -- The version `enable_memory_schema` returned when it was re-run, if the
    -- project has a platform-owned memory schema. Nothing else reports a
    -- schema's facade version: a stale schema looks current until something
    -- missing is called (Phase 12 slice 0).
    memory_schema_version  TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS extension_upgrades_node_target
    ON extension_upgrades (node_id, to_version, status);
CREATE INDEX IF NOT EXISTS extension_upgrades_project
    ON extension_upgrades (project_id, started_at DESC);

-- ADR-072 point 2, for the same reason migration 0031 gives it to every other
-- table keyed to a project: the gateway's permission model is a denylist, so a
-- project-keyed table without this policy is readable across nodes by any
-- gateway. tests/test_gateway_grants.py now fails on the next table that
-- forgets.
ALTER TABLE extension_upgrades ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON extension_upgrades
    FOR ALL USING (project_id IN (SELECT p.id FROM public.projects p
                                   WHERE p.node_id = public.gateway_node_id()))
    WITH CHECK (project_id IN (SELECT p.id FROM public.projects p
                                WHERE p.node_id = public.gateway_node_id()));
