-- Generated from specs/control-plane-schema.sql.
-- ADR-024: migrations are authoritative; the spec is the human-readable
-- reference. Change migrations first, then reconcile the spec.

-- Email (ADR-019). Sender identity and relay credentials are per project;
-- the relay is the authoritative enforcement point for quota and revocation.
CREATE TABLE project_email_settings (
    project_id          UUID PRIMARY KEY REFERENCES projects(id),
    sender_mode         VARCHAR(20) NOT NULL,   -- 'platform_default' | 'custom_domain'
    sender_domain       VARCHAR(255),           -- NULL when platform_default
    sender_address      VARCHAR(320) NOT NULL,
    sender_name         VARCHAR(100),
    dkim_selector       VARCHAR(64),
    domain_verified_at  TIMESTAMPTZ,
    smtp_username       VARCHAR(100) NOT NULL UNIQUE,
    -- Class B: encrypted, not hashed -- the control plane must recover this to
    -- configure the project's Auth worker. Distinct from
    -- api_keys.verification_data, which is a Class A hash. See docs/SECRETS.md.
    smtp_ciphertext     BYTEA NOT NULL,
    smtp_nonce          BYTEA NOT NULL,
    smtp_key_version    INTEGER NOT NULL REFERENCES encryption_keys(key_version),
    confirmations_required BOOLEAN NOT NULL DEFAULT TRUE,
    sending_suspended_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Asynchronous outcomes reported back by the relay. Relay acceptance is not
-- delivery, so bounces and complaints only arrive here.
CREATE TABLE email_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    event_type      VARCHAR(30) NOT NULL,   -- sent|delivered|hard_bounce|soft_bounce|complaint|quota_rejected
    recipient_hash  BYTEA NOT NULL,         -- hashed: do not store end-user addresses in the control plane
    message_id      VARCHAR(200),
    detail_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX email_events_project_time_idx ON email_events(project_id, occurred_at DESC);

-- Global suppression, consulted before every send by any project.
CREATE TABLE email_suppressions (
    recipient_hash  BYTEA PRIMARY KEY,
    reason          VARCHAR(30) NOT NULL,   -- hard_bounce | complaint | manual
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      UUID REFERENCES projects(id),
    org_id          UUID REFERENCES organizations(id),
    -- actor_type distinguishes customer action from staff support access, which
    -- must always be attributable and visible to the customer (docs/ACCOUNTS.md)
    actor_type      VARCHAR(30) NOT NULL,   -- user|staff|system|service
    actor_user_id   UUID REFERENCES users(id),
    actor_id        VARCHAR(200),
    event_type      VARCHAR(100) NOT NULL,
    detail_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
