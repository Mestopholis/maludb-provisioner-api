-- Phase 07 slice 1: a customer can ask for a project (ADR-037, ADR-038).
--
-- Until now nothing created a project row at all -- `cp-manage` had retry,
-- cleanup, email and storage, and rows existed only in tests. The dashboard's
-- most ordinary feature is therefore new code on the provisioning path,
-- reachable by anyone who can sign up, and `AGENTS.md` singles provisioning out
-- for idempotency.

-- The client's own key for a create request. A dashboard button that is clicked
-- twice, or a request retried after a timeout that actually succeeded, must not
-- produce two tenant databases: a project is a database, a set of roles and a
-- slot on a node, and the second one costs all of that with nobody using it.
--
-- Optional. A caller that supplies no key is asking for a new project each
-- time, which is a legitimate thing to want; the dashboard supplies one.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(200);

-- Scoped to the organization rather than global: two organizations choosing the
-- same key is a coincidence, not a conflict, and a global unique index would
-- let one customer's key collide with another's and refuse their creation.
--
-- Partial, so the column stays NULL for every project made without a key --
-- including every project that existed before this migration.
CREATE UNIQUE INDEX IF NOT EXISTS projects_org_idempotency_key_idx
    ON projects(org_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Who asked for it. Not the same as the organization: an organization has
-- several members who may create projects, and "which of them created this" is
-- the first question asked when one turns out to be unexpected -- and an
-- account-farming investigation is exactly a question about who created what.
--
-- ON DELETE SET NULL rather than CASCADE: removing a user must never delete
-- their organization's projects, which is the sort of thing a cascade does
-- quietly and once.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS requested_by UUID
    REFERENCES users(id) ON DELETE SET NULL;

-- When the request arrived, which is not created_at: a project whose
-- provisioning is queued behind a busy node was requested long before its rows
-- on the node existed, and the difference between the two is the number a
-- customer experiences as "how long did it take".
ALTER TABLE projects ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;

-- The provisioner claims work with SELECT ... FOR UPDATE SKIP LOCKED over this,
-- so it wants the due ones cheaply rather than a scan of every project the
-- platform has ever had.
CREATE INDEX IF NOT EXISTS projects_awaiting_provisioning_idx
    ON projects(retry_after NULLS FIRST)
    WHERE deleted_at IS NULL AND status IN ('REQUESTED', 'PLACEMENT_RESERVED', 'RETRY_WAIT');
