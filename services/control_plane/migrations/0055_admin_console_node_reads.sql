-- ADR-082 slice 3c: the operator console reads node capacity and provisioning state.
--
-- `nodes`, `node_extension_pins` and `provisioning_jobs` have row security; their policies
-- admit a gateway's own node, or the memory worker. SELECT-only policies for members of
-- `cp_admin_console`, as 0053 and 0054 added. Columns are narrowed in `admin_grants`:
-- never a node's admin or storage credential, its hostnames, or a job's free-text error
-- detail, which can quote a node's own error messages.
CREATE POLICY admin_console_read ON nodes
    FOR SELECT USING ((SELECT public.is_admin_console()));

CREATE POLICY admin_console_read ON node_extension_pins
    FOR SELECT USING ((SELECT public.is_admin_console()));

CREATE POLICY admin_console_read ON provisioning_jobs
    FOR SELECT USING ((SELECT public.is_admin_console()));
