-- Free slice 10b: a project can be deleted, deliberately and on the record.
--
-- Found by the launch walkthrough: nothing could delete a live project. `jobs.cleanup` reclaims a
-- *failed* one and refuses a database that was ever handed over (that refusal is right: it exists so
-- a reconciliation pass can never destroy customer data to restore desired state). What was missing
-- is the other path -- the one a customer asks for -- and without it the platform could not honour a
-- deletion request it makes in its own terms.
--
-- `DELETING` and `DELETED` are already in `projects_status_check` (0023), reserved for exactly this.
-- What this adds is the *request*: who asked, and when. Deletion is then work a worker does, and the
-- row survives it -- `deleted_at` set, the ref kept -- because a project's identity has to stay
-- unreusable and the audit trail has to outlive the data.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS delete_requested_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS delete_requested_by UUID REFERENCES users(id);

COMMENT ON COLUMN projects.delete_requested_at IS
    'When a customer or an operator asked for this project to be deleted (free slice 10b). The '
    'worker refuses to destroy anything for a project that carries no request.';

-- The worker claims by status; this keeps that claim cheap next to the provisioning one.
CREATE INDEX IF NOT EXISTS projects_deleting_idx ON projects (delete_requested_at)
    WHERE status = 'DELETING';
