-- Phase 10 slice 1: `mldb_<ref>_storage`, the role upstream `storage-api`
-- connects as and the owner of the tenant's `storage` schema.
--
-- Its own provisioning step, on the `EXECUTOR_CREATING` (migration 0017) and
-- `CLIENT_CREATING` (migration 0019) precedent, and for the same reason both
-- of those give: folding it into `ROLES_CREATING` would make every project
-- provisioned before this report as unfinished, and the repair for that
-- predicate resets the authenticator and auth passwords -- which stops every
-- PostgREST and GoTrue worker on the node. A new capability must not be able
-- to do that.
--
-- The step runs **before** `BOOTSTRAPPING`, not after: bootstrap 012 hands the
-- `storage` schema to this role and raises if it does not exist, exactly as
-- bootstrap 007 does for `mldb_<ref>_auth`.

ALTER TABLE projects DROP CONSTRAINT projects_status_check;
ALTER TABLE projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN (
        'REQUESTED', 'PLACEMENT_RESERVED', 'ROLES_CREATING', 'DATABASE_CREATING',
        'EXECUTOR_CREATING', 'CLIENT_CREATING', 'STORAGE_ROLE_CREATING',
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
        'STORAGE_ROLE_CREATING', 'BOOTSTRAPPING', 'VALIDATING',
        'PROVISIONED', 'FAILED'
    ));
