-- ADR-079 memory slice 6a: the query embedder's own control-plane role.
--
-- `memory_embedder` is an internal HTTP service on the control-plane host. The
-- gateway sends it a customer's secret key and a text query; it verifies the key
-- against the named project and embeds the query with the space's model and the
-- project's provider key. So it reads API key hashes and provider keys, and nothing
-- the memory worker holds besides: no memory writer credential, no queued ingest.
--
-- Identity is membership of `cp_memory_embedder`, for the reason migration 0046 gives
-- for `cp_memory_worker`: `pg_roles` is the one place a compromised gateway cannot
-- write itself into. Each policy evaluates the check once per query (InitPlan).
CREATE OR REPLACE FUNCTION public.is_memory_embedder() RETURNS BOOLEAN
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                    WHERE r.rolname = 'cp_memory_embedder'
                      AND pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
$$;

DROP POLICY IF EXISTS memory_embedder_reach ON projects;
CREATE POLICY memory_embedder_reach ON projects
    FOR SELECT USING ((SELECT public.is_memory_embedder()));

DROP POLICY IF EXISTS memory_embedder_reach ON nodes;
CREATE POLICY memory_embedder_reach ON nodes
    FOR SELECT USING ((SELECT public.is_memory_embedder()));

-- Authenticating a key reads its hash and records its use; the column grants allow
-- nothing else of the row, and never the recoverable ciphertext of a publishable key.
DROP POLICY IF EXISTS memory_embedder_reach ON api_keys;
CREATE POLICY memory_embedder_reach ON api_keys
    FOR ALL USING ((SELECT public.is_memory_embedder())) WITH CHECK ((SELECT public.is_memory_embedder()));

DROP POLICY IF EXISTS memory_embedder_reach ON project_provider_keys;
CREATE POLICY memory_embedder_reach ON project_provider_keys
    FOR SELECT USING ((SELECT public.is_memory_embedder()) AND revoked_at IS NULL);

DROP POLICY IF EXISTS memory_embedder_reach ON memory_spaces;
CREATE POLICY memory_embedder_reach ON memory_spaces
    FOR SELECT USING ((SELECT public.is_memory_embedder()) AND state = 'active');
