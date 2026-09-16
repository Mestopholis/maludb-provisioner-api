-- ADR-082 slice 3b: the operator console reads usage.
--
-- Egress is counted per project per month in `project_egress` and email in
-- `email_events`; both have row security (0031) whose only policy admits a gateway's
-- own node. SELECT-only policies for members of `cp_admin_console`, as 0053 added for
-- sales. Columns are narrowed in `admin_grants`: never `email_events.recipient_hash`,
-- which is a customer's end user's address, hashed.
CREATE POLICY admin_console_read ON project_egress
    FOR SELECT USING ((SELECT public.is_admin_console()));

CREATE POLICY admin_console_read ON email_events
    FOR SELECT USING ((SELECT public.is_admin_console()));
