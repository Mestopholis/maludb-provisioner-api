-- Generated from specs/control-plane-schema.sql.
-- ADR-024: migrations are authoritative; the spec is the human-readable
-- reference. Change migrations first, then reconcile the spec.

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
