-- Starter logical schema only.
-- Types, indexes, constraints, encryption strategy, and DB engine are to be refined
-- once the control-plane technology choice is approved.

-- ---------------------------------------------------------------------------
-- Key hierarchy (ADR-023, docs/SECRETS.md). Declared first because recoverable
-- secrets across this schema reference it.
--
-- Data encryption keys are stored only in wrapped form. The key encryption key
-- (KEK) that unwraps them is never persisted here; a dump of this database must
-- be useless without it.
-- ---------------------------------------------------------------------------

CREATE TABLE encryption_keys (
    key_version     INTEGER PRIMARY KEY,
    wrapped_dek     BYTEA NOT NULL,
    algorithm       VARCHAR(40) NOT NULL,   -- e.g. aes-256-gcm, xchacha20-poly1305
    kek_identifier  VARCHAR(200) NOT NULL,  -- which KEK wrapped this; not the key itself
    state           VARCHAR(20) NOT NULL,   -- active|retiring|retired
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at      TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Platform identity (ADR-020, ADR-021). These are MaluDB's own users, not the
-- end users of a customer's application -- those live in each tenant database's
-- auth schema and never appear here. See docs/ACCOUNTS.md.
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id                  UUID PRIMARY KEY,
    email               VARCHAR(320) NOT NULL UNIQUE,
    email_verified_at   TIMESTAMPTZ,
    -- memory-hard hash; never a reversible representation
    password_hash       TEXT,
    display_name        VARCHAR(200),
    status              VARCHAR(30) NOT NULL DEFAULT 'active',  -- active|suspended|deleted
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

CREATE TABLE organizations (
    id                  UUID PRIMARY KEY,
    slug                VARCHAR(64) NOT NULL UNIQUE,
    display_name        VARCHAR(200) NOT NULL,
    -- personal orgs are auto-created at signup so a solo developer never sees
    -- organizational concepts, but ownership is an org from the first row
    is_personal         BOOLEAN NOT NULL DEFAULT FALSE,
    require_mfa         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

CREATE TABLE org_members (
    org_id              UUID NOT NULL REFERENCES organizations(id),
    user_id             UUID NOT NULL REFERENCES users(id),
    role                VARCHAR(20) NOT NULL,   -- owner|admin|developer|billing|viewer
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);

CREATE INDEX org_members_user_idx ON org_members(user_id);
-- an organization must always retain at least one owner; enforced in
-- application logic and asserted by test, not expressible as a table constraint

CREATE TABLE org_invitations (
    id                  UUID PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES organizations(id),
    email               VARCHAR(320) NOT NULL,
    role                VARCHAR(20) NOT NULL,
    invited_by          UUID NOT NULL REFERENCES users(id),
    token_hash          TEXT NOT NULL,          -- single use; never stored in clear
    expires_at          TIMESTAMPTZ NOT NULL,
    accepted_at         TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX org_invitations_pending_idx
    ON org_invitations(org_id, email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;

-- Server-side sessions, not stateless JWTs: an account that controls production
-- databases needs revocation to take effect immediately.
CREATE TABLE user_sessions (
    id                  UUID PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id),
    token_hash          TEXT NOT NULL UNIQUE,
    ip_address          INET,
    user_agent          TEXT,
    mfa_satisfied       BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at          TIMESTAMPTZ NOT NULL,
    last_seen_at        TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX user_sessions_user_idx ON user_sessions(user_id);

-- CLI/CI credentials. Same storage discipline as project api_keys: prefixed
-- identifier for lookup, non-reversible verification material.
CREATE TABLE personal_access_tokens (
    id                  UUID PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id),
    name                VARCHAR(200) NOT NULL,
    token_prefix        VARCHAR(32) NOT NULL UNIQUE,
    verification_data   TEXT NOT NULL,
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    expires_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX personal_access_tokens_user_idx ON personal_access_tokens(user_id);

CREATE TABLE user_mfa_factors (
    id                  UUID PRIMARY KEY,
    user_id             UUID NOT NULL REFERENCES users(id),
    factor_type         VARCHAR(20) NOT NULL,   -- totp
    -- Class B: TOTP verification recomputes codes from the seed, so the seed
    -- must be recoverable. See docs/SECRETS.md.
    ciphertext          BYTEA NOT NULL,
    nonce               BYTEA NOT NULL,
    key_version         INTEGER NOT NULL REFERENCES encryption_keys(key_version),
    verified_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX user_mfa_factors_user_idx ON user_mfa_factors(user_id);

CREATE TABLE plans (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code            VARCHAR(50) NOT NULL UNIQUE,
    name            VARCHAR(100) NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    config_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE nodes (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            VARCHAR(100) NOT NULL UNIQUE,
    hostname        VARCHAR(255) NOT NULL,
    internal_host   VARCHAR(255) NOT NULL,
    node_pool       VARCHAR(50) NOT NULL DEFAULT 'shared',
    status          VARCHAR(30) NOT NULL,
    maludb_version  VARCHAR(100),
    postgres_version VARCHAR(100),
    capacity_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_health_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id              UUID PRIMARY KEY,
    -- ADR-020: projects are owned by organizations, never directly by users.
    -- Was an unconstrained account_id; billing and team access attach here.
    org_id          UUID NOT NULL REFERENCES organizations(id),
    project_ref     VARCHAR(32) NOT NULL UNIQUE,
    display_name    VARCHAR(200) NOT NULL,
    plan_id         BIGINT NOT NULL REFERENCES plans(id),
    node_id         BIGINT REFERENCES nodes(id),
    database_name   VARCHAR(100) UNIQUE,
    status          VARCHAR(40) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    upgraded_at     TIMESTAMPTZ,
    suspended_at    TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    key_type        VARCHAR(30) NOT NULL,
    key_identifier  VARCHAR(100) NOT NULL,
    verification_data TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX api_keys_project_idx ON api_keys(project_id);

-- ---------------------------------------------------------------------------
-- Secret storage (ADR-023, docs/SECRETS.md).
--
-- Class A secrets (API keys, tokens, passwords) are hashed and live on their
-- own tables. Class B secrets below are RECOVERABLE: the platform must
-- reproduce the exact value to configure a worker, so they are envelope
-- encrypted, never hashed.
--
-- Every Class B value carries the triple (ciphertext, nonce, key_version).
-- AEAD associated data must bind each ciphertext to its table, column, and
-- owning identifier, so a ciphertext moved between rows fails to decrypt.
-- ---------------------------------------------------------------------------

-- Per-project recoverable credentials. Previously these had nowhere to live:
-- provisioning generates them and the control plane must supply them to the
-- PostgREST and Auth workers.
CREATE TABLE project_credentials (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    -- db_authenticator | db_auth | db_admin | jwt_signing | smtp
    credential_type VARCHAR(40) NOT NULL,
    role_name       VARCHAR(100),           -- for db_* types; NULL otherwise
    ciphertext      BYTEA NOT NULL,
    nonce           BYTEA NOT NULL,
    key_version     INTEGER NOT NULL REFERENCES encryption_keys(key_version),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);

-- one live credential of each type per project; rotation supersedes rather
-- than overwrites, so revoked rows are retained
CREATE UNIQUE INDEX project_credentials_live_idx
    ON project_credentials(project_id, credential_type)
    WHERE revoked_at IS NULL;

CREATE TABLE provisioning_jobs (
    id              UUID PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id),
    state           VARCHAR(50) NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    error_code      VARCHAR(100),
    error_detail    TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX provisioning_jobs_project_idx ON provisioning_jobs(project_id);

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
