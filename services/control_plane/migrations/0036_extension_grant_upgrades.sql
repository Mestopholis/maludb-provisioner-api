-- ADR-076 grants slice 2: bringing existing tenants to the new extension-function
-- posture, a canary and then batches per node.
--
-- One row per tenant per attempt, in the class of `extension_upgrades`. What an
-- operator needs from it is the same: which tenant stopped a run, why, and that
-- nothing after it was touched -- plus, for this procedure, what the run saw of
-- the tenant's PostgREST before it granted anything, because that evidence is the
-- whole reason the grants were safe to apply.

CREATE TABLE IF NOT EXISTS extension_grant_upgrades (
    id           BIGSERIAL PRIMARY KEY,
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id      BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    -- upgraded: check live, bootstrap 014 applied and verified, committed.
    -- current:  already had 014; nothing changed.
    -- failed:   014 rolled back, or never attempted because the check could not
    --           be shown live. The run stopped here.
    -- skipped:  not attempted, because the project was mid-operation.
    status       TEXT NOT NULL CHECK (status IN ('upgraded', 'current', 'failed', 'skipped')),
    canary       BOOLEAN NOT NULL DEFAULT FALSE,
    -- What pg_stat_activity showed of the tenant's PostgREST when the check was
    -- confirmed: 'not running', or how many connections and whether a listener.
    worker       TEXT,
    detail       TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS extension_grant_upgrades_node
    ON extension_grant_upgrades (node_id, status);
CREATE INDEX IF NOT EXISTS extension_grant_upgrades_project
    ON extension_grant_upgrades (project_id, started_at DESC);

-- ADR-072 point 2: every project-keyed table carries the gateway's row policy,
-- and tests/test_gateway_grants.py fails on one that does not.
ALTER TABLE extension_grant_upgrades ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON extension_grant_upgrades
    FOR ALL USING (project_id IN (SELECT p.id FROM public.projects p
                                   WHERE p.node_id = public.gateway_node_id()))
    WITH CHECK (project_id IN (SELECT p.id FROM public.projects p
                                WHERE p.node_id = public.gateway_node_id()));
