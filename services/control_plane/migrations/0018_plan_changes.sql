-- Phase 09 slice 1: a plan change as an operation with a record, rather than
-- an UPDATE nobody can audit or resume.
--
-- **Deliberately not the `UPGRADING` project status**, which migration 0017
-- reserves and nothing sets. Measured before choosing: three separate gates
-- read `("PROVISIONED", "ACTIVE")` and refuse anything else -- the gateway's
-- `SERVING_STATUSES`, `api/tenant_access.py` for the SQL and schema routes, and
-- `workers.py` for starting a worker. Parking a project in `UPGRADING` would
-- take its data API, its console and its workers down for the duration of a
-- purchase, and leave them down if the change failed partway. An upgrade is the
-- moment a customer least wants an outage, and ADR-006 makes keeping the
-- database in place the whole point.
--
-- So the in-flight marker lives here instead and the project's status is not
-- touched at all. `UPGRADING` stays reserved and unused on purpose; anyone
-- reaching for it should read this comment first.

CREATE TABLE IF NOT EXISTS plan_changes (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- Codes rather than foreign keys, for `upgrade_requests`' reason: a plan
    -- may be retired from the catalogue while a change naming it is still the
    -- most recent record of what happened, and losing the history to a
    -- catalogue edit would lose the answer to "what were they on in March".
    from_plan_code  VARCHAR(50) NOT NULL,
    to_plan_code    VARCHAR(50) NOT NULL,
    -- RUNNING -> APPLIED | FAILED. A RUNNING row that outlives its process is
    -- the resumable case: the node work is idempotent, so re-running finishes
    -- it, and until then the row is what says somebody should.
    state           VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    -- Who asked. ON DELETE SET NULL so removing a user does not delete the
    -- record of a change their organization was charged for. NULL is an
    -- operator acting through cp-manage without a user to attribute it to.
    requested_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    -- What went wrong, for an operator. Never shown to a customer: it can name
    -- a node, a role, or a PostgreSQL error about platform internals.
    error           TEXT,

    CONSTRAINT plan_changes_state_check
        CHECK (state IN ('RUNNING', 'APPLIED', 'FAILED'))
);

-- The mutual exclusion, and the reason this is an index rather than a lock: a
-- lock is held by a process, and the thing being excluded is a second process
-- starting while the first is partway through writing to a node. Two concurrent
-- changes could otherwise interleave `ALTER ROLE`s and leave the tenant on
-- neither plan.
CREATE UNIQUE INDEX IF NOT EXISTS plan_changes_one_running_per_project
    ON plan_changes(project_id) WHERE state = 'RUNNING';

-- The history query: what has this project been on, most recent first.
CREATE INDEX IF NOT EXISTS plan_changes_project_idx
    ON plan_changes(project_id, started_at DESC);
