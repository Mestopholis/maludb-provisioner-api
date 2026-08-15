-- Generated from specs/control-plane-schema.sql.
-- ADR-024: migrations are authoritative; the spec is the human-readable
-- reference. Change migrations first, then reconcile the spec.

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
