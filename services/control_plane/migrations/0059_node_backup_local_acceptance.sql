-- ADR-087, free slice 7c: a node may keep its backups on the node, by a recorded, time-boxed acceptance.
--
-- ADR-064 fails a repository in the data's failure domain in production. The owner decided the
-- free-tier beta keeps backups local for now, and a check that is red for ever is a check nobody
-- reads -- so the acceptance is recorded here, per node, with who, why and until when, and lapses by
-- itself. Only the co-location failure is affected; `backup.BackupReadiness` keeps every other one.
--
-- Columns on `nodes`, which the gateway reaches by column grant only (ADR-072) and the backup
-- recorder not at all (0058), so neither can accept on a node's behalf. Written only by
-- `cp-manage node backup-accept-local`, as the control plane's own role.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_local_accepted_until DATE;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_local_accepted_reason TEXT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_local_accepted_by TEXT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_local_accepted_at TIMESTAMPTZ;

-- All four or none: an acceptance with no reason or no author is not a decision anyone made.
-- At most 90 days from when it was recorded (ADR-087 decision 2); renewing is a new acceptance.
ALTER TABLE nodes ADD CONSTRAINT nodes_backup_local_acceptance_complete CHECK (
    (backup_local_accepted_until IS NULL) = (backup_local_accepted_reason IS NULL)
    AND (backup_local_accepted_until IS NULL) = (backup_local_accepted_by IS NULL)
    AND (backup_local_accepted_until IS NULL) = (backup_local_accepted_at IS NULL)
);
ALTER TABLE nodes ADD CONSTRAINT nodes_backup_local_acceptance_bounded CHECK (
    backup_local_accepted_until IS NULL
    OR backup_local_accepted_until <= (backup_local_accepted_at AT TIME ZONE 'UTC')::date + 90
);

COMMENT ON COLUMN nodes.backup_local_accepted_until IS
    'ADR-087: the last day a repository on this node is accepted in production. Lapses by itself.';
