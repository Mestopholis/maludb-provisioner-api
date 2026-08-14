-- Starter logical schema only.
-- Types, indexes, constraints, encryption strategy, and DB engine are to be refined
-- once the control-plane technology choice is approved.

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
    account_id      UUID NOT NULL,
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

CREATE TABLE audit_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      UUID REFERENCES projects(id),
    actor_type      VARCHAR(30) NOT NULL,
    actor_id        VARCHAR(200),
    event_type      VARCHAR(100) NOT NULL,
    detail_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
