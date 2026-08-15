-- Phase 04 slice 4: auth email via GoTrue's Send Email Hook (ADR-029).
--
-- The SMTP columns modelled a transport that does not exist. MaluMail offers no
-- SMTP submission, and GoTrue 2.195.0 turns out to have an HTTP send hook --
-- both verified, see ADR-029. Nothing has ever written this table, so the
-- columns are dropped rather than left to confuse the next reader.

ALTER TABLE project_email_settings DROP COLUMN IF EXISTS smtp_username;
ALTER TABLE project_email_settings DROP COLUMN IF EXISTS smtp_ciphertext;
ALTER TABLE project_email_settings DROP COLUMN IF EXISTS smtp_nonce;
ALTER TABLE project_email_settings DROP COLUMN IF EXISTS smtp_key_version;

-- The per-project secret GoTrue signs its hook calls with. Class B under
-- ADR-023: the hook must reproduce it to verify a signature, so it is encrypted
-- rather than hashed. Stored as the full `v1,whsec_<base64>` form GoTrue expects
-- in its configuration.
ALTER TABLE project_email_settings ADD COLUMN IF NOT EXISTS hook_ciphertext  BYTEA;
ALTER TABLE project_email_settings ADD COLUMN IF NOT EXISTS hook_nonce       BYTEA;
ALTER TABLE project_email_settings ADD COLUMN IF NOT EXISTS hook_key_version INTEGER REFERENCES encryption_keys(key_version);

-- The customer's own MaluMail API key, for sender_mode = 'custom_domain'.
-- NULL on platform_default, where the platform's own key is used and lives in
-- configuration rather than in a tenant row.
--
-- Also Class B, and the most sensitive credential the platform holds on a
-- customer's behalf: it sends mail that passes SPF and DKIM as their domain.
ALTER TABLE project_email_settings ADD COLUMN IF NOT EXISTS malumail_ciphertext  BYTEA;
ALTER TABLE project_email_settings ADD COLUMN IF NOT EXISTS malumail_nonce       BYTEA;
ALTER TABLE project_email_settings ADD COLUMN IF NOT EXISTS malumail_key_version INTEGER REFERENCES encryption_keys(key_version);

ALTER TABLE project_email_settings
    ADD CONSTRAINT project_email_settings_sender_mode_check
    CHECK (sender_mode IN ('platform_default', 'custom_domain'));

-- A custom domain without a key cannot send, and would fail at send time with a
-- confusing error. Refuse the row instead.
ALTER TABLE project_email_settings
    ADD CONSTRAINT project_email_settings_custom_domain_needs_key
    CHECK (sender_mode <> 'custom_domain' OR malumail_ciphertext IS NOT NULL);

-- Quota is counted from email_events, which already exists. This index is what
-- makes the count cheap enough to run before every send: on platform_default
-- the allowance is shared, so the check has to happen on the hot path rather
-- than in a nightly reconciliation.
CREATE INDEX IF NOT EXISTS email_events_project_window_idx
    ON email_events(project_id, occurred_at DESC) WHERE event_type = 'sent';
