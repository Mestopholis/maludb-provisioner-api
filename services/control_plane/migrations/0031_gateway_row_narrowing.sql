-- Deployment step 8 (ADR-072, point 2): a gateway sees only its own node's rows.
--
-- Point 1 gave the gateway its own role and took `nodes.admin_ciphertext` away
-- from it, so a compromised public listener can no longer recover any node's
-- superuser DSN. What it could still do is read every *project* row on the
-- platform -- and it holds the KEK, so that is every project's database
-- password and JWT signing key, on every node, from one machine.
--
-- The narrowing keys on `current_user` rather than on anything the process
-- says about itself. That is the whole design: the threat is a compromised
-- gateway, and a compromised gateway can set a session variable or an
-- environment value to whatever it likes. It cannot restate which role the
-- connection authenticated as.
--
-- The policies below name no role. A policy with no `TO` applies to every role
-- except the table's owner, which means the control plane -- the owner -- is
-- unaffected, and any future non-owner role is narrowed by default rather than
-- by somebody remembering. PostgreSQL exempts a table's owner from its own
-- policies unless FORCE ROW LEVEL SECURITY is set, so it is deliberately not
-- set here.

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS gateway_role TEXT UNIQUE;

COMMENT ON COLUMN nodes.gateway_role IS
    'The login role the gateway on this node connects as (ADR-072). Row policies '
    'resolve current_user through this column, so it is the node identity and not '
    'a label. Written by `cp-manage gateway grant --role <r> --node <n>`.';

-- Resolve the connected role to the node it serves, or NULL.
--
-- NULL is the important case: a role mapped to no node matches no row, because
-- `node_id = NULL` is never true. An unmapped gateway therefore sees nothing
-- rather than everything, which is the direction a mistake here has to fail in.
--
-- `public.nodes` is qualified and `search_path` is pinned with `pg_temp` named
-- **last**, both deliberately. An unqualified `nodes` would be resolved at
-- execution time against the caller's search_path, and PostgreSQL searches the
-- temporary schema before the listed ones unless pg_temp is named explicitly --
-- so a gateway that created a temp table called `nodes` with a `gateway_role`
-- column could make this function return any node id it wanted, and the
-- policies would admit that node's rows. This is the same class of mistake as
-- upstream storage migration 0011's unqualified function, which is why it is
-- written out rather than assumed.
--
-- Not SECURITY DEFINER: it reads nothing the caller cannot read for itself, and
-- a definer function here would add an escalation surface for no gain.
CREATE OR REPLACE FUNCTION public.gateway_node_id() RETURNS BIGINT
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT n.id FROM public.nodes n WHERE n.gateway_role = current_user
$$;

COMMENT ON FUNCTION public.gateway_node_id() IS
    'The node whose gateway connects as current_user, or NULL (ADR-072). Used by '
    'the row policies; returns NULL for the control plane itself, which does not '
    'matter because the owner is exempt from them.';

-- Projects are keyed on the node directly. `node_id IS NULL` is an unplaced
-- project, which is not any gateway''s business and is excluded by the same
-- comparison rather than by a second clause.
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON projects
    FOR ALL USING (node_id = public.gateway_node_id())
    WITH CHECK (node_id = public.gateway_node_id());

-- Everything keyed to a project follows the project's placement. Every such
-- table is covered, not only the ones the gateway's code reads today: the
-- permission model is a denylist (see services/control_plane/gateway_grants.py
-- for why), so the reachable set is "every table with a grant", and a narrowing
-- that covered less would be a comment rather than a control.
--
-- A NULL project_id -- a platform-level audit or billing event -- belongs to no
-- node and is excluded for the same reason an unplaced project is.
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'api_keys', 'audit_events', 'billing_events', 'checkout_sessions',
        'email_events', 'plan_changes', 'project_credentials', 'project_egress',
        'project_email_settings', 'provisioning_jobs', 'subscriptions',
        'tenant_moves', 'upgrade_requests'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY gateway_own_node ON public.%I FOR ALL '
            'USING (project_id IN (SELECT p.id FROM public.projects p '
            '                       WHERE p.node_id = public.gateway_node_id())) '
            'WITH CHECK (project_id IN (SELECT p.id FROM public.projects p '
            '                            WHERE p.node_id = public.gateway_node_id()))',
            t
        );
    END LOOP;
END
$$;

-- `nodes` itself: its own row and no other.
--
-- This is what makes the column model above able to *widen* safely. Slice 1
-- granted `id` alone, which left `storage_workers.ensure_node_secret` unable to
-- read `storage_secret_ciphertext` -- so a correctly narrowed gateway broke
-- Storage, and no test caught it because the suite runs the gateway as the
-- schema owner. A node's storage root is that node's own secret and the gateway
-- on it may legitimately hold it; what it may not hold is another node's. A row
-- policy says that, and a column grant cannot.
--
-- The predicate compares `gateway_role` directly rather than calling
-- `gateway_node_id()`, which reads this same table: a policy on `nodes` that
-- called it would recurse, and PostgreSQL would refuse the query outright.
ALTER TABLE nodes ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON nodes
    FOR ALL USING (gateway_role = current_user)
    WITH CHECK (gateway_role = current_user);

-- And the two keyed on a node rather than on a project.
ALTER TABLE node_backups ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON node_backups
    FOR ALL USING (node_id = public.gateway_node_id())
    WITH CHECK (node_id = public.gateway_node_id());

ALTER TABLE tenant_restores ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON tenant_restores
    FOR ALL USING (node_id = public.gateway_node_id())
    WITH CHECK (node_id = public.gateway_node_id());
