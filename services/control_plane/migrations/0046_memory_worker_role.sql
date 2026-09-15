-- ADR-079 decision 6, memory slice 5c: the memory worker's own control-plane role.
--
-- The worker holds the KEK, because it opens each project's sealed memory writer
-- password and the customer's provider keys. Connected as the control plane's own
-- role it could also open every node's superuser DSN and every tenant's database
-- password and JWT signing key -- decision 6 promises a compromised worker reaches
-- memory, not the fleet, and that promise was held only by the code it runs.
--
-- **Identity is membership of `cp_memory_worker`**, a NOLOGIN group role an
-- operator creates, and the login role the worker connects as is a member of it.
-- Deliberately not a mapping table, as `nodes.gateway_role` is: the gateway's
-- permission model is a denylist that grants INSERT and UPDATE on every table, so
-- a table naming the worker role would be a table a compromised gateway could add
-- itself to -- and every policy below would then admit it to every project's
-- writer credential and provider keys. `pg_roles` is the one place the gateway
-- cannot write. It is also world-readable, so these policies evaluate for the
-- gateway without a grant it might not have been given yet.
--
-- `pg_catalog` is named first and `pg_temp` last, for the reason
-- `gateway_node_id()` gives: a temp relation named `pg_roles` must not be able to
-- answer this question.
CREATE OR REPLACE FUNCTION public.is_memory_worker() RETURNS BOOLEAN
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                    WHERE r.rolname = 'cp_memory_worker'
                      AND pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
$$;

COMMENT ON FUNCTION public.is_memory_worker() IS
    'Whether current_user is a member of cp_memory_worker (ADR-079 memory slice 5c). '
    'Used by the memory worker row policies; false for the gateway and for the owner, '
    'which is exempt from policies anyway.';

-- The rows. Column grants (`services/control_plane/memory_worker_grants.py`) say
-- which columns; these say which rows, and they are the half that matters for
-- `project_credentials`: the worker needs `db_memwriter` and no other type, and a
-- column grant cannot say that.
--
-- Each is `(SELECT public.is_memory_worker())`, a scalar subquery, so the planner
-- evaluates it once per query as an InitPlan rather than once per row -- these
-- policies are ORed into every query the gateway makes on `projects` too.
--
-- Every other table with row security keeps only the gateway's own-node policy,
-- which admits nobody who is not a gateway -- so the worker sees none of their rows
-- even where a grant is added by mistake.
DROP POLICY IF EXISTS memory_worker_reach ON projects;
CREATE POLICY memory_worker_reach ON projects
    FOR SELECT USING ((SELECT public.is_memory_worker()));

DROP POLICY IF EXISTS memory_worker_reach ON nodes;
CREATE POLICY memory_worker_reach ON nodes
    FOR SELECT USING ((SELECT public.is_memory_worker()));

DROP POLICY IF EXISTS memory_worker_reach ON project_credentials;
CREATE POLICY memory_worker_reach ON project_credentials
    FOR SELECT USING ((SELECT public.is_memory_worker()) AND credential_type = 'db_memwriter');

DROP POLICY IF EXISTS memory_worker_reach ON project_provider_keys;
CREATE POLICY memory_worker_reach ON project_provider_keys
    FOR SELECT USING ((SELECT public.is_memory_worker()) AND revoked_at IS NULL);

DROP POLICY IF EXISTS memory_worker_reach ON memory_spaces;
CREATE POLICY memory_worker_reach ON memory_spaces
    FOR ALL USING ((SELECT public.is_memory_worker())) WITH CHECK ((SELECT public.is_memory_worker()));

DROP POLICY IF EXISTS memory_worker_reach ON memory_ingests;
CREATE POLICY memory_worker_reach ON memory_ingests
    FOR ALL USING ((SELECT public.is_memory_worker())) WITH CHECK ((SELECT public.is_memory_worker()));
