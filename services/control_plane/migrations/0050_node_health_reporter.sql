-- ADR-080: a node reports its own health, as a role that can do nothing else.
--
-- Placement refuses a node without a health report newer than
-- `nodes.HEALTH_STALE_AFTER`, and until now the only writer was `cp-manage node
-- health`, which needs the control plane's own database role and the KEK -- so it
-- could not run on the node it describes, and the runbook named nothing to run it.
--
-- **Identity is a mapping column, as `nodes.gateway_role` is, and that is safe here
-- for the reason 0046 gives against it elsewhere.** A mapping is dangerous when a
-- narrowed role can write it. The gateway's grant revokes everything on `nodes` and
-- gives back UPDATE on `last_health_at` alone; the memory worker and embedder read
-- `nodes` and write nothing. Only the control plane's own role, through
-- `cp-manage node reporter grant`, writes this column.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS health_reporter_role TEXT UNIQUE;

COMMENT ON COLUMN nodes.health_reporter_role IS
    'The login role whose reports this node accepts (ADR-080). Written only by '
    'cp-manage node reporter grant.';

-- **No table privilege at all.** The reporter's role is granted EXECUTE on this and
-- USAGE on the schema, nothing else, so a stolen reporter password marks one node
-- fresh and reports its disk -- it reads no row, and writes no other column.
--
-- SECURITY DEFINER, so the body runs as the owner and needs no grant on `nodes`.
-- Which node is decided by `session_user`: inside a definer function
-- `current_user` is the owner, and `session_user` is the role that logged in.
-- Only a superuser can change it (SET SESSION AUTHORIZATION), and a superuser
-- already has the table.
--
-- The report *merges*: `metrics_json` also carries what `node realtime-check` and
-- `node backup-check` recorded, and a health report must not erase it. Only the
-- keys this function names are written, so a reporter cannot forge those.
--
-- The time is the database's (`now()`), not the node's: freshness is compared
-- against the control plane's clock, and a node with a wrong clock must not be
-- able to report itself fresh for ever.
CREATE OR REPLACE FUNCTION public.report_node_health(p_free_disk_bytes BIGINT)
    RETURNS TEXT
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    reported TEXT;
BEGIN
    IF p_free_disk_bytes IS NULL OR p_free_disk_bytes < 0 THEN
        RAISE EXCEPTION 'free disk must be a non-negative number of bytes'
            USING ERRCODE = '22023';
    END IF;
    UPDATE public.nodes
       SET metrics_json = coalesce(metrics_json, '{}'::jsonb)
                          || jsonb_build_object('free_disk_bytes', p_free_disk_bytes,
                                                'health_reported_by', session_user::text),
           last_health_at = now()
     WHERE health_reporter_role = session_user::text
    RETURNING name INTO reported;
    IF reported IS NULL THEN
        RAISE EXCEPTION 'role % reports health for no node; run cp-manage node reporter grant', session_user
            USING ERRCODE = '42501';
    END IF;
    RETURN reported;
END
$$;

REVOKE ALL ON FUNCTION public.report_node_health(BIGINT) FROM PUBLIC;

COMMENT ON FUNCTION public.report_node_health(BIGINT) IS
    'Record a health report for the node whose health_reporter_role is session_user '
    '(ADR-080). Merges free_disk_bytes into metrics_json; the time is the database''s.';
