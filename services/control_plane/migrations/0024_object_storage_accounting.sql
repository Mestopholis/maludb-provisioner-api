-- Phase 10 slice 2 (ADR-056): the two object-storage entitlements, and where
-- their consumption is recorded.
--
-- Both are **hard ceilings under ADR-050**: refused at the point of use, never
-- converted into a charge, never reported to any payment provider. Nothing here
-- is a meter. That is worth stating in the schema rather than only in an ADR,
-- because a `bytes` column that accumulates monthly looks exactly like the
-- start of a metering pipeline and is not one -- no invoice reads it, and a
-- wrong value here refuses a download rather than billing for one.
--
-- Deliberately separate from migration 0011's `database_bytes` /
-- `storage_state` columns, which are **database** storage: `pg_database_size`
-- and the ADR-040 write restriction. Two resources, two quotas, two states, and
-- a project can be over one and under the other. Sharing a column would have
-- made "restricted" ambiguous at exactly the moment somebody needs to know
-- which limit they hit.

-- What the project's objects weigh, from the tenant's own `storage.objects`
-- metadata. Recorded rather than measured on demand, for migration 0011's
-- reason: answering "how big is this project" should not open a connection to
-- a node on a request path.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS object_bytes BIGINT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS object_measured_at TIMESTAMPTZ;

-- ok | warning | exceeded.
--
-- `exceeded`, not `restricted`, and the difference is real rather than
-- cosmetic. Database storage restriction *revokes* INSERT and UPDATE inside the
-- tenant, so the state names a change made to the database. Nothing is revoked
-- here: object bytes are written through the Storage API, so the ceiling is
-- enforced where the request arrives (slice 4) and the database is untouched.
-- A reader who saw `restricted` on both would reasonably expect a revoke behind
-- both.
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS object_storage_state VARCHAR(20) NOT NULL DEFAULT 'ok';

ALTER TABLE projects
    ADD CONSTRAINT projects_object_storage_state_check
    CHECK (object_storage_state IN ('ok', 'warning', 'exceeded'));

-- When uploads started being refused, so an operator can see how long without
-- reading the audit log. Mirrors `storage_restricted_at`.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS object_exceeded_at TIMESTAMPTZ;

-- Finding the projects an object-storage measurement pass should look at,
-- oldest first. Separate from `projects_storage_measured_idx` because the two
-- passes advance independently: a node that is reachable for
-- `pg_database_size` is reachable for this too, but a failure in one must not
-- park the other.
CREATE INDEX IF NOT EXISTS projects_object_measured_idx
    ON projects(object_measured_at NULLS FIRST) WHERE deleted_at IS NULL;


-- Egress, per project per month.
--
-- The platform's **first persisted usage counter**. Everything metered until
-- now was refused in-process and forgotten: ADR-030 keeps the gateway's rate
-- and concurrency limiters in memory, and `services/control_plane/api/usage.py`
-- reports `metered: false` for requests and connections rather than inventing a
-- number. This one has to persist, because a monthly ceiling cannot be held in
-- a process that restarts.
--
-- **A row per period, not a column that resets.** A counter reset in place has
-- no answer to "what did this project serve last month", and the reset itself
-- becomes a job that can fail to run -- leaving a project either permanently
-- over its ceiling or silently under it, depending on which way the bug went.
-- Rows expire by being old rather than by being cleared.
--
-- `period_start` is the first day of a **UTC calendar month**, and that is a
-- decision rather than a default. The alternative was the subscription's
-- billing period, which `api/usage.py` already reports -- rejected because a
-- free project has no subscription and therefore no period, and ADR-056 puts
-- this ceiling on every tier including free. Aligning a free project's ceiling
-- to a billing period would mean inventing one for it. The ceiling is also not
-- a charge (ADR-050), so there is nothing for it to line up *with*.
CREATE TABLE IF NOT EXISTS project_egress (
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- First day of the UTC month. A DATE rather than a TIMESTAMPTZ: the value
    -- is a period label, and storing an instant would invite a reader to
    -- compare it against `now()` in some other timezone.
    period_start DATE NOT NULL,
    -- Bytes served on this project's behalf in that month. Accumulated with
    -- `+= excluded.bytes` so a caller can flush a batch rather than write per
    -- request -- which is what ADR-026's published gateway throughput requires
    -- of anything on that path.
    bytes        BIGINT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, period_start)
);

-- Never negative. A subtraction bug that drove a counter below zero would hand
-- a project unlimited egress for the rest of the month, which is the failure
-- direction that costs money rather than the one that annoys a customer.
ALTER TABLE project_egress
    ADD CONSTRAINT project_egress_bytes_check CHECK (bytes >= 0);
