-- Phase 11 slice 2: what was restored, from when, to where, and what it cost.
--
-- A sibling of `node_backups` (migration 0026) and deliberately a separate
-- table rather than a status column on it. A backup is a property of a *node*;
-- a restore is an operation on a *tenant*, it can be run many times against one
-- backup, and it produces a database that did not exist before. Folding the two
-- together would have made "the last thing that happened to this backup"
-- ambiguous the first time a tenant was restored twice.
--
-- Recorded for three reasons, in descending order of how much they matter:
--
--  1. **A restored database is indistinguishable from a live one by
--     inspection.** It carries the same schemas, the same 164 RLS policies and
--     the same extensions. The only durable record that `mldb_x_restore_...`
--     holds data from a point in the past -- rather than data somebody is
--     using -- is this row. Without it, a later operator finds two tenant-shaped
--     databases and no way to tell which is which.
--  2. **Activation has to know what it is activating.** Swapping a restored
--     database in for the live one is guarded on a completed, verified restore
--     of the *same* project, and that guard reads this table.
--  3. Audit. Restoring a tenant is reading a customer's data at a point they
--     did not choose, by an operator. That should leave a trace.

CREATE TABLE IF NOT EXISTS tenant_restores (
    id                  BIGSERIAL PRIMARY KEY,

    -- The project as the control plane knows it. ON DELETE CASCADE rather than
    -- RESTRICT: a deleted project's restore history is not a reason to keep the
    -- project row, and the database this points at is named in `restored_database`
    -- either way.
    project_id          UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id             INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,

    -- Denormalised from `nodes.backup_stanza` for migration 0026's reason: this
    -- says which repository the restore actually read, not which one the node
    -- points at now.
    stanza              TEXT NOT NULL,

    -- The point in time asked for. NULL means "the end of the backup set" --
    -- pgBackRest's default, which is the latest consistent state rather than a
    -- chosen moment. The distinction is worth a NULL rather than a sentinel
    -- timestamp: "as recent as possible" and "23:59 last Tuesday" are different
    -- requests and a restore should not have to guess which one it served.
    target_time         TIMESTAMPTZ,

    -- Where the recovered data landed. A *new* database beside the live one --
    -- never over it. Nothing in this slice writes to the database a project is
    -- currently serving from; activation renames, and renaming keeps both.
    restored_database   TEXT,

    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,

    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'complete', 'failed', 'activated')),

    -- Whether the per-schema owners on the restored copy match the per-tenant
    -- roles the source had. **The finding this column exists for is that they
    -- silently do not**: a restore into a cluster that has never seen the tenant
    -- reassigns `auth` and `storage` from their per-tenant service roles to
    -- whoever ran it -- the platform superuser -- while every row and all 164
    -- policies arrive intact and `pg_restore`'s exit code says only "1".
    --
    -- ADR-059 puts the `storage` schema under a per-tenant role precisely so it
    -- is not owned by something with superuser reach, and leaves its RLS
    -- unforced on that basis. A restored tenant whose schemas are owned by
    -- `postgres` has a different security posture from the one that was backed
    -- up, and nothing in the database says so. So it is checked, and recorded,
    -- and activation refuses without it.
    ownership_verified  BOOLEAN,
    ownership_detail    TEXT,

    -- What it cost, so an RTO figure comes from records rather than from
    -- somebody's memory of a good run. Slice 8 needs these.
    elapsed_seconds     NUMERIC(10, 2),
    dump_bytes          BIGINT,

    -- How many other tenant databases on the node answered while this ran.
    -- **This is the acceptance criterion, stored.** "Restore one tenant without
    -- taking the other tenants offline" is not a property of the design, it is
    -- a property of each run, and a run that could not confirm it should not be
    -- remembered as one that did.
    neighbours_available INTEGER,

    error               TEXT,

    CONSTRAINT tenant_restores_finished_with_status
        CHECK ((status = 'running') = (finished_at IS NULL)),

    -- A restore that succeeded produced a database. One that did not, did not.
    CONSTRAINT tenant_restores_complete_has_database
        CHECK (status NOT IN ('complete', 'activated') OR restored_database IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS tenant_restores_project_idx
    ON tenant_restores(project_id, started_at DESC);

-- What activation looks for: the most recent completed, ownership-verified
-- restore for a project. Partial, because that is the only row it ever wants.
CREATE INDEX IF NOT EXISTS tenant_restores_activatable_idx
    ON tenant_restores(project_id, started_at DESC)
    WHERE status = 'complete' AND ownership_verified;
