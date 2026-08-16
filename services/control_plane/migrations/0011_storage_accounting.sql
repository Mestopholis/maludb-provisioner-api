-- Phase 05 slice 3: storage accounting and quota enforcement.
--
-- The size is recorded rather than measured on demand. `pg_database_size()`
-- needs a connection to the node, which the control plane should not be opening
-- on a request path just to answer "how big is this project".

-- Gross, as PostgreSQL reports it. The quota is compared against the figure net
-- of the maludb_core baseline (ADR-015), but the measurement stored is the raw
-- one: deriving the net figure from a stored gross is reversible, and storing
-- an adjusted number would leave nothing able to answer what the database
-- actually weighs.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS database_bytes BIGINT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS database_measured_at TIMESTAMPTZ;

-- ok | warning | restricted. Held on the project because the gateway and the
-- dashboard both need it without connecting to the tenant.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS storage_state VARCHAR(20) NOT NULL DEFAULT 'ok';

ALTER TABLE projects
    ADD CONSTRAINT projects_storage_state_check
    CHECK (storage_state IN ('ok', 'warning', 'restricted'));

-- When writes were revoked, so an operator can see how long a project has been
-- in that state without reading the audit log.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS storage_restricted_at TIMESTAMPTZ;

-- Finding the projects a measurement pass should look at, oldest first.
CREATE INDEX IF NOT EXISTS projects_storage_measured_idx
    ON projects(database_measured_at NULLS FIRST) WHERE deleted_at IS NULL;

-- What this tenant weighed with maludb_core installed and no customer data.
-- Recorded per project rather than assumed from a constant, for the reason
-- ADR-015 gives for recording the extension version per project: CREATE
-- EXTENSION installs whatever the node's packages currently provide, and drift
-- is already observable. A constant is wrong the day the extension changes, and
-- wrong in the direction that hides usage.
--
-- Measured 2026-08-16 on the development node at 23,639,731 bytes. The constant
-- in storage.py is only a fallback for projects provisioned before this column
-- existed, and is deliberately set below the measured figure -- under-
-- subtracting over-reports a customer's usage slightly, which is the safe
-- direction; over-subtracting hides it.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS storage_baseline_bytes BIGINT;
