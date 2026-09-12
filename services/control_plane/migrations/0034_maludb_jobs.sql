-- Phase 12 slice 4 (ADR-074, decision 3 as amended): what a customer asks for,
-- and what the platform did about it.
--
-- Enabling and refreshing the data-model graph run as the node superuser, and
-- ADR-038 keeps that credential out of the internet-facing application. So a
-- customer's request writes a row here and the provisioner -- the process ADR-038
-- says may hold it -- claims the row and does the work. `provisioning_jobs` is
-- a per-attempt record of one state machine and is not reused for this.

CREATE TABLE IF NOT EXISTS maludb_jobs (
    id            BIGSERIAL PRIMARY KEY,
    project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('enable', 'refresh')),
    state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    requested_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    -- A customer-facing sentence when the platform refused, or a generic one when
    -- something unexpected failed. Never the text of a node error, which is the
    -- one thing here that could carry an internal detail to a customer.
    detail        TEXT,
    -- True when the job failed because the platform refused -- a squatted
    -- schema, an extension too old -- rather than because something broke.
    -- Refusals count against the plan's limit and breakage does not: a customer
    -- who can make the superuser work fail on purpose must not get to run it
    -- again for free, and a customer whose request the platform dropped must not
    -- pay for it.
    refused       BOOLEAN NOT NULL DEFAULT FALSE,
    result_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    CHECK ((state = 'pending') = (started_at IS NULL)),
    CHECK ((state IN ('succeeded', 'failed')) = (completed_at IS NOT NULL))
);

-- Coalescing, enforced where a race cannot get past it: at most one *pending*
-- job of a kind per project. Not one open job -- a refresh already running may
-- have read the catalogue before the customer's latest migration, so a request
-- that arrives while one runs gets a pending job of its own behind it.
CREATE UNIQUE INDEX IF NOT EXISTS maludb_jobs_one_pending
    ON maludb_jobs (project_id, kind) WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS maludb_jobs_claim
    ON maludb_jobs (requested_at) WHERE state = 'pending';

-- The per-plan refresh limit counts a project's recent jobs.
CREATE INDEX IF NOT EXISTS maludb_jobs_project_recent
    ON maludb_jobs (project_id, kind, requested_at DESC);

-- ADR-072 point 2: every project-keyed table carries the gateway's row policy,
-- and tests/test_gateway_grants.py fails on one that does not.
ALTER TABLE maludb_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON maludb_jobs
    FOR ALL USING (project_id IN (SELECT p.id FROM public.projects p
                                   WHERE p.node_id = public.gateway_node_id()))
    WITH CHECK (project_id IN (SELECT p.id FROM public.projects p
                                WHERE p.node_id = public.gateway_node_id()));
