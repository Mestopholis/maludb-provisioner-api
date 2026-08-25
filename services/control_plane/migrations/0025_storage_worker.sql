-- Phase 10 slice 3 (ADR-058): the shared storage worker, and where its state
-- lives.
--
-- **Per node, not per project**, which is the whole shape of this slice.
-- Realtime's worker state hangs off `projects` because ADR-034 forced one
-- instance per project; `storage-api` has no cluster-unique resource, so one
-- instance serves every tenant on a node and its secrets belong to the node.
-- Migration 0005 already put node-scoped credentials on `nodes` for exactly
-- this reason, and these follow it.

-- One root secret per node, envelope-encrypted like the admin DSN beside it
-- (ADR-023, Class B). Three values are derived from it -- the admin API key,
-- upstream's `AUTH_ENCRYPTION_KEY`, and the metadata role's password -- rather
-- than stored separately, on `realtime_workers.derived_secrets`' reasoning: one
-- row carries an instance, HKDF info strings keep a leak of one from handing
-- over the others, and a rebuilt instance derives the same values without a
-- second round trip.
--
-- Reused rather than regenerated, and that is load-bearing here in a way it is
-- not for a per-project instance: `AUTH_ENCRYPTION_KEY` decrypts every
-- registered tenant's connection settings in the multitenant database. A fresh
-- root would leave **every tenant on the node** unreadable at once, not one.
ALTER TABLE nodes
    ADD COLUMN storage_secret_ciphertext  BYTEA,
    ADD COLUMN storage_secret_nonce       BYTEA,
    ADD COLUMN storage_secret_key_version INTEGER REFERENCES encryption_keys(key_version);

-- All three or none, matching `nodes_admin_credential_complete`: a half-written
-- credential is a decryption failure waiting to happen at the worst moment.
ALTER TABLE nodes
    ADD CONSTRAINT nodes_storage_secret_complete
    CHECK (num_nonnulls(storage_secret_ciphertext, storage_secret_nonce, storage_secret_key_version) IN (0, 3));


-- Whether this project has been registered with its node's storage worker.
--
-- Registration is the per-project half of a shared instance: the worker learns
-- the tenant's database URL and JWT secret through an admin API, and until it
-- has, that tenant's Storage requests answer `400 TenantNotFound`. Recorded on
-- the project because that is the grain the answer differs at, and because a
-- shared worker restarting must be able to re-register everything it serves
-- without asking the container what it already knows.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS storage_registered_at TIMESTAMPTZ;

-- Finding what a restarted worker owes registration to, and what a node sweep
-- should re-check.
CREATE INDEX IF NOT EXISTS projects_storage_registered_idx
    ON projects(node_id) WHERE storage_registered_at IS NOT NULL AND deleted_at IS NULL;


-- STORAGE_ROLE_CREATING already exists (migration 0023). No new provisioning
-- state: registering a tenant with a shared worker is not a stage a project can
-- be stuck half-way through the way creating a role is -- it either answered or
-- it did not, and a project that is not registered is simply one whose next
-- Storage request registers it.
--
-- That is a deliberate difference from Realtime, whose enablement has its own
-- states because it creates a replication slot, a role and a container. This
-- creates a row in somebody else's database.
