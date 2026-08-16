-- Phase 07 slice 3: a customer can ask to upgrade, and nothing changes yet.
--
-- Phase 09 owns payment and entitlements. What this table records is *intent*:
-- a customer pressed the button, and somebody has to do something about it.
-- Recording it as a row rather than as an audit event is the difference between
-- a queue an operator works and a line in a log nobody lists.
--
-- Deliberately **not** a change to `projects.plan_id`. An upgrade that took
-- effect here would grant paid entitlements to a project nobody has billed,
-- and the platform would find out at renewal rather than at purchase.

CREATE TABLE IF NOT EXISTS upgrade_requests (
    id                  UUID PRIMARY KEY,
    project_id          UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- By code, matching what the customer sees, and stored as text rather than
    -- as a foreign key to `plans`: a plan may be retired from the catalogue
    -- while a request naming it is still open, and losing the request to a
    -- catalogue edit would lose the customer.
    requested_plan_code VARCHAR(50) NOT NULL,
    -- Who asked. ON DELETE SET NULL so removing a user does not delete the
    -- request their organization is waiting on.
    requested_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- REQUESTED -> CONTACTED -> CLOSED. Three states because two would not
    -- distinguish "nobody has looked at this" from "somebody is dealing with
    -- it", and that distinction is the whole value of a queue.
    state               VARCHAR(20) NOT NULL DEFAULT 'REQUESTED',
    -- Free text for whoever works it. Never shown to the customer: it is an
    -- operator's note, and a note written in the belief that it is private
    -- should not become a support reply.
    operator_note       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT upgrade_requests_state_check
        CHECK (state IN ('REQUESTED', 'CONTACTED', 'CLOSED'))
);

-- One open request per project. A customer pressing the button twice is asking
-- the same question, and two rows would have an operator answer it twice --
-- while a customer whose request was closed may legitimately ask again.
CREATE UNIQUE INDEX IF NOT EXISTS upgrade_requests_one_open_per_project
    ON upgrade_requests(project_id) WHERE state <> 'CLOSED';

-- The operator's query: what is waiting, oldest first.
CREATE INDEX IF NOT EXISTS upgrade_requests_open_idx
    ON upgrade_requests(requested_at) WHERE state = 'REQUESTED';
