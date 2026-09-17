-- ADR-086, free slice 7a: a node records its own backups, as a role that can do nothing else.
--
-- Phase 11 recorded a backup from the process that ran pgBackRest, through the control
-- plane's own role. pgBackRest must run on the node, as the cluster's owner, and on a
-- deployment where the control plane is another host nothing on the node may write the
-- control-plane database. This is ADR-080's answer applied again: a login role mapped to
-- one node, holding EXECUTE on three functions and no table privilege, with which node
-- decided inside the database from `session_user`.
--
-- **What a stolen recorder password can do**, stated because it is the review question:
-- record backups and repository checks for its own node, which could make a node that is
-- not backed up look as though it is. It cannot read a row, touch another node, rewrite a
-- finished backup, choose the stanza a row claims, or change what the control plane's own
-- `node backup-check` recorded about the cluster's settings. A recorder is only ever as
-- trustworthy as the node it runs on; the restore drill (slice 7e) is what proves a backup.

-- Identity, as `health_reporter_role` (0050). Written only by `cp-manage node
-- backup-recorder grant`, as the control plane's own role.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_recorder_role TEXT UNIQUE;

COMMENT ON COLUMN nodes.backup_recorder_role IS
    'The login role whose backup records this node accepts (ADR-086). Written only by '
    'cp-manage node backup-recorder grant.';


-- A backup is beginning. Written before pgBackRest runs, for 0026's reason: a backup that
-- never returns must leave a `running` row the maintenance pass can age out.
--
-- The stanza is the node's, never the caller's: a row claims the repository the control
-- plane prepared, so a recorder cannot file backups of some other stanza against its node.
-- One start a minute, so a looping or hostile runner cannot bury the one row the pass reads.
CREATE OR REPLACE FUNCTION public.start_node_backup(p_backup_type TEXT)
    RETURNS BIGINT
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    node RECORD;
    started BIGINT;
BEGIN
    SELECT id, name, backup_stanza INTO node
      FROM public.nodes WHERE backup_recorder_role = session_user::text;
    IF node.id IS NULL THEN
        RAISE EXCEPTION 'role % records backups for no node; run cp-manage node backup-recorder grant', session_user
            USING ERRCODE = '42501';
    END IF;
    IF node.backup_stanza IS NULL THEN
        RAISE EXCEPTION 'node % has no stanza; run cp-manage node backup-check first', node.name
            USING ERRCODE = '55000';
    END IF;
    IF p_backup_type IS NULL OR p_backup_type NOT IN ('full', 'diff', 'incr') THEN
        RAISE EXCEPTION 'backup type must be full, diff or incr' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (SELECT 1 FROM public.node_backups
                WHERE node_id = node.id AND started_at > now() - interval '1 minute') THEN
        RAISE EXCEPTION 'node % started a backup less than a minute ago', node.name
            USING ERRCODE = '55000';
    END IF;
    INSERT INTO public.node_backups (node_id, stanza, backup_type)
         VALUES (node.id, node.backup_stanza, p_backup_type)
      RETURNING id INTO started;
    RETURN started;
END
$$;


-- A backup this recorder started has ended. Only a `running` row of its own node, so
-- history cannot be rewritten: a failure stays a failure and a completed backup keeps the
-- label it completed with. A completed row needs a label shaped like pgBackRest's, because
-- the label is what `pgbackrest restore --set` is given and 0026 refuses a complete row
-- without one.
CREATE OR REPLACE FUNCTION public.finish_node_backup(
    p_backup_id BIGINT,
    p_status TEXT,
    p_label TEXT,
    p_database_bytes BIGINT,
    p_repository_bytes BIGINT,
    p_wal_start TEXT,
    p_wal_stop TEXT,
    p_error TEXT
)
    RETURNS VOID
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    node_id_ INTEGER;
BEGIN
    SELECT id INTO node_id_ FROM public.nodes WHERE backup_recorder_role = session_user::text;
    IF node_id_ IS NULL THEN
        RAISE EXCEPTION 'role % records backups for no node; run cp-manage node backup-recorder grant', session_user
            USING ERRCODE = '42501';
    END IF;
    IF p_status IS NULL OR p_status NOT IN ('complete', 'failed') THEN
        RAISE EXCEPTION 'a finished backup is complete or failed' USING ERRCODE = '22023';
    END IF;
    IF p_status = 'complete'
       AND (p_label IS NULL OR p_label !~ '^[0-9]{8}-[0-9]{6}F(_[0-9]{8}-[0-9]{6}[DI])?$') THEN
        RAISE EXCEPTION 'a complete backup needs a pgBackRest label' USING ERRCODE = '22023';
    END IF;
    IF coalesce(p_database_bytes, 0) < 0 OR coalesce(p_repository_bytes, 0) < 0 THEN
        RAISE EXCEPTION 'sizes must be non-negative' USING ERRCODE = '22023';
    END IF;
    IF (p_wal_start IS NOT NULL AND p_wal_start !~ '^[0-9A-F]{24}$')
       OR (p_wal_stop IS NOT NULL AND p_wal_stop !~ '^[0-9A-F]{24}$') THEN
        RAISE EXCEPTION 'WAL segment names are 24 hexadecimal characters' USING ERRCODE = '22023';
    END IF;
    UPDATE public.node_backups
       SET status = p_status, finished_at = now(), label = p_label,
           database_bytes = p_database_bytes, repository_bytes = p_repository_bytes,
           wal_start = p_wal_start, wal_stop = p_wal_stop,
           error = left(p_error, 2000)
     WHERE id = p_backup_id AND node_id = node_id_ AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no running backup % on this node', p_backup_id USING ERRCODE = '55000';
    END IF;
END
$$;


-- The repository half of readiness, which only the node can see: `pgbackrest check`,
-- `info`, and the repository options, per repository (ADR-086 decision 3). Merged into
-- `metrics_json` under keys of its own, so it cannot overwrite what the control plane's
-- `node backup-check` recorded about archive_mode and wal_level -- readiness joins the two.
-- The time is the database's, for 0050's reason.
CREATE OR REPLACE FUNCTION public.record_node_backup_check(p_report JSONB)
    RETURNS TEXT
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    recorded TEXT;
BEGIN
    IF p_report IS NULL OR jsonb_typeof(p_report) <> 'object' THEN
        RAISE EXCEPTION 'a repository report is a JSON object' USING ERRCODE = '22023';
    END IF;
    IF octet_length(p_report::text) > 65536 THEN
        RAISE EXCEPTION 'a repository report is at most 64 KiB' USING ERRCODE = '22023';
    END IF;
    UPDATE public.nodes
       SET metrics_json = coalesce(metrics_json, '{}'::jsonb)
                          || jsonb_build_object('backup_repository', p_report,
                                                'backup_repository_checked_at', now(),
                                                'backup_repository_reported_by', session_user::text)
     WHERE backup_recorder_role = session_user::text
    RETURNING name INTO recorded;
    IF recorded IS NULL THEN
        RAISE EXCEPTION 'role % records backups for no node; run cp-manage node backup-recorder grant', session_user
            USING ERRCODE = '42501';
    END IF;
    RETURN recorded;
END
$$;

REVOKE ALL ON FUNCTION public.start_node_backup(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.finish_node_backup(BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.record_node_backup_check(JSONB) FROM PUBLIC;

COMMENT ON FUNCTION public.start_node_backup(TEXT) IS
    'Open a running node_backups row for the node whose backup_recorder_role is session_user (ADR-086).';
COMMENT ON FUNCTION public.finish_node_backup(BIGINT, TEXT, TEXT, BIGINT, BIGINT, TEXT, TEXT, TEXT) IS
    'Close a running node_backups row of the recorder''s own node (ADR-086).';
COMMENT ON FUNCTION public.record_node_backup_check(JSONB) IS
    'Merge the node-side repository check into metrics_json for the recorder''s own node (ADR-086).';
