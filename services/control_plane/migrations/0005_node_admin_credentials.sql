-- Phase 02 slice 2: privileged connection details for each node.
--
-- Provisioning must CREATE ROLE, CREATE DATABASE and CREATE EXTENSION on the
-- node. maludb_core is not a trusted extension, so installing it requires
-- superuser -- an unavoidable consequence of ADR-015 requiring it in every
-- tenant database.
--
-- docs/CONTROL-PLANE.md permits the control plane to hold these, subject to:
-- never sent to tenant clients, never logged, scoped to the operations needed,
-- and held in a proper secret mechanism. They are therefore Class B secrets
-- under ADR-023 -- envelope encrypted, never hashed, since the control plane
-- must reproduce the DSN to connect.

ALTER TABLE nodes
    ADD COLUMN admin_ciphertext  BYTEA,
    ADD COLUMN admin_nonce       BYTEA,
    ADD COLUMN admin_key_version INTEGER REFERENCES encryption_keys(key_version);

-- All three present or all three absent; a half-written credential is a
-- decryption failure waiting to happen during a provisioning run.
ALTER TABLE nodes
    ADD CONSTRAINT nodes_admin_credential_complete
    CHECK (num_nonnulls(admin_ciphertext, admin_nonce, admin_key_version) IN (0, 3));

-- Provisioning records what it actually installed, because dependency versions
-- drift with the node's OS packages (docs/MALUDB.md).
ALTER TABLE projects
    ADD COLUMN extension_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN bootstrap_version  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN provisioned_at     TIMESTAMPTZ;
