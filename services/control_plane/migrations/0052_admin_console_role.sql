-- ADR-082 slice 2: the operator console's own control-plane role.
--
-- **Identity is membership of `cp_admin_console`**, a NOLOGIN group an operator
-- creates, with the console's LOGIN role a member -- for the reason 0046 gives for the
-- memory worker: `pg_roles` is the one place the gateway, whose grants are a denylist,
-- cannot write itself into.
--
-- Column grants are in `services/control_plane/admin_grants.py`. This migration adds
-- only what a grant cannot say: which `audit_events` rows the console may write.
CREATE OR REPLACE FUNCTION public.is_admin_console() RETURNS BOOLEAN
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                    WHERE r.rolname = 'cp_admin_console'
                      AND pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
$$;

COMMENT ON FUNCTION public.is_admin_console() IS
    'Whether current_user is a member of cp_admin_console (ADR-082 slice 2). False for the '
    'gateway, the memory worker and the owner, which is exempt from policies anyway.';

-- `audit_events` has row security (0031): its only policy admits a gateway's own node,
-- so without this the console could not record a staff sign-in at all. The console
-- writes **staff** events and nothing else, attributed to no project -- it can say
-- that staff did something, never that a customer or the system did. It reads no
-- audit rows through this policy (FOR INSERT only).
CREATE POLICY admin_console_staff_events ON audit_events
    FOR INSERT
    WITH CHECK ((SELECT public.is_admin_console()) AND actor_type = 'staff' AND project_id IS NULL
                AND actor_user_id IS NULL);
