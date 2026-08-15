-- Phase 02 slice 4: provisioning as a resumable sequence of steps.
--
-- One provisioning_jobs row per attempt rather than one per project. A tenant
-- that failed twice and succeeded on the third try is a thing an operator needs
-- to be able to see, and overwriting the row in place destroys exactly that.

ALTER TABLE provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_attempt_unique UNIQUE (project_id, attempt);

ALTER TABLE provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_attempt_positive CHECK (attempt >= 1);

-- The step the attempt was executing. Mirrors projects.status for operational
-- states, plus the two terminal outcomes of an attempt.
ALTER TABLE provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_state_check
    CHECK (state IN (
        'ROLES_CREATING', 'DATABASE_CREATING', 'BOOTSTRAPPING', 'VALIDATING',
        'PROVISIONED', 'FAILED'
    ));

-- Finding one open attempt for a project is the common query, and two
-- concurrent runs for the same project is the thing worth making impossible.
CREATE UNIQUE INDEX IF NOT EXISTS provisioning_jobs_one_open_per_project
    ON provisioning_jobs(project_id) WHERE completed_at IS NULL;

-- When a retry may next be attempted. NULL means "now": RETRY_WAIT without a
-- time is a project that would be retried immediately and fail immediately.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ;

-- Set when provisioning last failed, so an operator can age out stuck projects
-- without inferring it from the job history.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;

-- Consecutive failures, which is what a retry cap should actually measure.
-- Counting rows in provisioning_jobs instead would make cleanup a trap: it
-- reclaims everything and returns the project to REQUESTED, but the history it
-- leaves behind still counts against the cap, so the project could never be
-- provisioned again. Reset on success and on cleanup; provisioning_jobs.attempt
-- stays monotonic because it is the audit trail.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS provisioning_failures INTEGER NOT NULL DEFAULT 0;
