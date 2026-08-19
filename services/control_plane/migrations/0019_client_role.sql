-- Phase 09 slice 2 (ADR-047): the role a paid customer connects as.
--
-- Its own provisioning step, on the `EXECUTOR_CREATING` precedent from
-- migration 0017. Folding it into `ROLES_CREATING` would make every project
-- provisioned before this report as unfinished, and the repair for that
-- predicate resets the authenticator and auth passwords -- which stops every
-- PostgREST and GoTrue worker on the node. A new capability must not be able
-- to do that.

ALTER TABLE projects DROP CONSTRAINT projects_status_check;
ALTER TABLE projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN (
        'REQUESTED', 'PLACEMENT_RESERVED', 'ROLES_CREATING', 'DATABASE_CREATING',
        'EXECUTOR_CREATING', 'CLIENT_CREATING',
        'BOOTSTRAPPING', 'KEYS_CONFIGURING', 'VALIDATING', 'PROVISIONED',
        'API_CONFIGURING', 'ROUTING_CONFIGURING', 'ACTIVE',
        'PAUSING', 'PAUSED', 'RESUMING', 'SUSPENDING', 'SUSPENDED',
        -- Still reserved, still unset. Migration 0018 records why a plan change
        -- must not park a project here.
        'UPGRADING', 'DELETING', 'DELETED', 'RETRY_WAIT', 'FAILED'
    ));

ALTER TABLE provisioning_jobs DROP CONSTRAINT provisioning_jobs_state_check;
ALTER TABLE provisioning_jobs
    ADD CONSTRAINT provisioning_jobs_state_check
    CHECK (state IN (
        'ROLES_CREATING', 'DATABASE_CREATING', 'EXECUTOR_CREATING', 'CLIENT_CREATING',
        'BOOTSTRAPPING', 'VALIDATING',
        'PROVISIONED', 'FAILED'
    ));
