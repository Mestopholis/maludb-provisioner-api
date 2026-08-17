-- The port a node's PostgreSQL listens on, so a tenant connection can be built
-- without the node's admin DSN.
--
-- Every tenant connection the control plane has opened so far came from
-- `nodes.admin_dsn()`, which carries host, port and superuser credentials
-- together. ADR-038 keeps that out of the internet-facing application, and
-- ADR-039's SQL surface is the first route-driven tenant connection -- so the
-- public application needs somewhere to learn host and port that is not the
-- one place the superuser password also lives.
--
-- `internal_host` already exists and is the address platform components use.
-- The port did not, because the only consumer so far was a PostgREST worker
-- running *on* the node, which reaches 127.0.0.1:5432 and never needed to be
-- told. The control plane is not on the node.
--
-- 5432 as the default rather than NULL: every node registered before this
-- migration is a default-port cluster, and a nullable port would push the
-- question into every caller as a None to handle.
ALTER TABLE nodes ADD COLUMN db_port INTEGER NOT NULL DEFAULT 5432;

-- EXECUTOR_CREATING joins the provisioning state machine. Both CHECK
-- constraints have to learn it, because a step name is written to
-- `projects.status` while it runs and to `provisioning_jobs.state` when the
-- attempt is recorded -- and a constraint that rejected it would fail
-- provisioning at the step rather than at review.
ALTER TABLE projects DROP CONSTRAINT projects_status_check;
ALTER TABLE projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN (
        'REQUESTED', 'PLACEMENT_RESERVED', 'ROLES_CREATING', 'DATABASE_CREATING',
        'EXECUTOR_CREATING',
        'BOOTSTRAPPING', 'KEYS_CONFIGURING', 'VALIDATING', 'PROVISIONED',
        'API_CONFIGURING', 'ROUTING_CONFIGURING', 'ACTIVE',
        'PAUSING', 'PAUSED', 'RESUMING', 'SUSPENDING', 'SUSPENDED',
        'UPGRADING', 'DELETING', 'DELETED', 'RETRY_WAIT', 'FAILED'
    ));

ALTER TABLE provisioning_jobs DROP CONSTRAINT provisioning_jobs_state_check;
ALTER TABLE provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_state_check
    CHECK (state IN (
        'ROLES_CREATING', 'DATABASE_CREATING', 'EXECUTOR_CREATING',
        'BOOTSTRAPPING', 'VALIDATING',
        'PROVISIONED', 'FAILED'
    ));
