-- ADR-082 slice 3a: the operator console reads sales and customer records.
--
-- `projects`, `subscriptions` and `billing_events` have row security (0031), and their
-- only policy admits a gateway's own node -- so a column grant alone would show the
-- console no rows. These add a SELECT-only policy for members of `cp_admin_console`.
-- Which *columns* is `services/control_plane/admin_grants.py`: never a credential, and
-- these tables hold none.
--
-- `(SELECT public.is_admin_console())` is a scalar subquery, so it is evaluated once per
-- query rather than per row -- these policies are ORed into the gateway's queries on
-- `projects` too (0046's note).
CREATE POLICY admin_console_read ON projects
    FOR SELECT USING ((SELECT public.is_admin_console()));

CREATE POLICY admin_console_read ON subscriptions
    FOR SELECT USING ((SELECT public.is_admin_console()));

CREATE POLICY admin_console_read ON billing_events
    FOR SELECT USING ((SELECT public.is_admin_console()));
