-- Phase 03 slice 1: project API keys (ADR-008, ADR-023, ADR-028).
--
-- The api_keys table was created in 0002 with the right shape for hashed
-- storage. What it lacked is the distinction ADR-023 actually turns on:
-- whether a secret has to be *recoverable*.
--
-- A secret key is server-side only and never needs to be read back, so it is
-- Class A -- an HMAC verifier and nothing else. A publishable key is different.
-- It is public by design, embedded in the customer's client bundle, and a
-- dashboard has to be able to show it again next month. That makes it Class B:
-- envelope encrypted, recoverable, never hashed-only.
--
-- Deciding this now rather than when the dashboard lands is deliberate.
-- Migrations are immutable once applied; discovering later that the publishable
-- key can never be displayed again would mean forcing every project to rotate.

ALTER TABLE api_keys
    ADD CONSTRAINT api_keys_type_check CHECK (key_type IN ('publishable', 'secret'));

-- Envelope columns, mirroring project_credentials (ADR-023). Nullable because
-- only the recoverable class uses them.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS ciphertext  BYTEA;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS nonce       BYTEA;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_version INTEGER REFERENCES encryption_keys(key_version);

-- The classification is an invariant, not a convention. A secret key that
-- carried recoverable ciphertext would be a Class A secret stored Class B --
-- exactly the confusion ADR-023 exists to prevent -- and it would not be
-- visible in review of the row that created it.
ALTER TABLE api_keys
    ADD CONSTRAINT api_keys_recoverability_check CHECK (
        (key_type = 'secret'      AND ciphertext IS NULL AND nonce IS NULL AND key_version IS NULL)
     OR (key_type = 'publishable' AND ciphertext IS NOT NULL AND nonce IS NOT NULL AND key_version IS NOT NULL)
    );

-- key_identifier is the lookup handle presented on every API request, so
-- validation must be one indexed read rather than a scan over every key on the
-- platform. Unique because two keys sharing one identifier would make the
-- lookup ambiguous, and resolving that ambiguity by verifying against each
-- candidate is how a timing signal gets introduced.
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_identifier_idx ON api_keys(key_identifier);

-- Deliberately NOT unique per (project, type): rotation without downtime needs
-- the new key live before the old one is revoked. A one-live-key-per-type
-- constraint would force every rotation to be an outage.
CREATE INDEX IF NOT EXISTS api_keys_project_live_idx
    ON api_keys(project_id, key_type) WHERE revoked_at IS NULL;

-- A human label, so an operator revoking one of four live keys can tell which
-- deployment it belongs to.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS name TEXT;
