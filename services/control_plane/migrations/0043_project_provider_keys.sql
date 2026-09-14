-- ADR-079 decisions 4 and 5, memory slice 4: a customer's own model provider API
-- keys, which the memory worker (slice 5) calls OpenAI, Anthropic or Voyage with.
--
-- A customer's secret, so it is stored the way the platform stores its own:
-- sealed under the KEK (ADR-023), bound by AAD to its project and provider so a
-- ciphertext moved to another row does not open, write-only through the API, and
-- never logged. `key_hint` is the last four characters, enough for a person to
-- tell two keys apart and not enough to use one.
--
-- One live key per provider per project; replacing one revokes the old row
-- rather than overwriting it, as `project_credentials` does. Rows go with the
-- project (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS project_provider_keys (
    id           UUID PRIMARY KEY,
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider     VARCHAR(20) NOT NULL,
    ciphertext   BYTEA NOT NULL,
    nonce        BYTEA NOT NULL,
    key_version  INTEGER NOT NULL REFERENCES encryption_keys(key_version),
    key_hint     VARCHAR(4) NOT NULL,
    created_by   UUID REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ,
    CONSTRAINT project_provider_keys_provider_check CHECK (provider IN ('openai', 'anthropic', 'voyage'))
);

CREATE UNIQUE INDEX IF NOT EXISTS project_provider_keys_live
    ON project_provider_keys (project_id, provider) WHERE revoked_at IS NULL;

-- ADR-072's own-node policy, as on every project-keyed table -- and the gateway
-- role is additionally refused the table outright (`gateway_grants`): nothing on
-- the request path needs a customer's provider key.
ALTER TABLE project_provider_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS gateway_own_node ON project_provider_keys;
CREATE POLICY gateway_own_node ON project_provider_keys
    FOR ALL USING (project_id IN (SELECT p.id FROM public.projects p
                                   WHERE p.node_id = public.gateway_node_id()))
    WITH CHECK (project_id IN (SELECT p.id FROM public.projects p
                                WHERE p.node_id = public.gateway_node_id()));
