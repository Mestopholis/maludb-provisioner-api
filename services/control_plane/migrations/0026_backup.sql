-- Phase 11 slice 1 (ADR-067, ADR-064): where a node's backups are recorded,
-- and what the platform believes about them.
--
-- `docs/ARCHITECTURE.md` has reserved "backup metadata later" since Phase 01.
-- This is later. The shape follows from two slice-0 findings rather than from
-- what a backup table usually looks like, and both are recorded here because
-- the columns are otherwise hard to justify:
--
--  1. **An untuned pgBackRest backup of an idle cluster waits forever.** Its
--     default begins after the next *regular* checkpoint and PostgreSQL skips
--     timed checkpoints when no WAL has been written -- measured at 15+ minutes
--     at 0% CPU with `num_timed = 0` after forty minutes of uptime. That is the
--     free tier's exact shape (ADR-022), so the nightly backup of a node full
--     of sleeping projects hangs rather than fails: no error, no exit code, and
--     nothing in the repository the next morning. A row is therefore written
--     when a backup *starts*, not when it finishes, and `running` is a state
--     the verification pass can age out. A table that only recorded successes
--     could not tell "hung since Tuesday" from "never ran".
--
--  2. **The repository is the durable record; this table is not.** These rows
--     say what the platform did and what it saw. They are not evidence that a
--     restorable backup exists -- only the repository is that, and only a
--     restore proves it (slice 2). Nothing here should ever be read as a
--     durability guarantee, which is why there is no `verified` flag: this
--     slice has no honest way to set one.

-- Which pgBackRest stanza covers this node's cluster. NULL means the node has
-- not been prepared for backup, which is a reportable state rather than an
-- error -- see `backup.BackupReadiness`.
--
-- One stanza per node, because a stanza owns a whole cluster and ADR-002 puts
-- every tenant of a node in one cluster. Per-tenant granularity is a restore
-- concern (slice 2), not a repository one.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_stanza TEXT;

-- How old the most recent completed backup may be before the maintenance pass
-- calls it a failure. NULL falls back to `backup.DEFAULT_MAX_AGE_HOURS`.
--
-- Configuration rather than a constant in application logic, per AGENTS.md: a
-- node backed up hourly and a node backed up weekly are both legitimate, and
-- the deployment knows which it is.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS backup_max_age_hours INTEGER;

ALTER TABLE nodes
    ADD CONSTRAINT nodes_backup_max_age_positive
    CHECK (backup_max_age_hours IS NULL OR backup_max_age_hours > 0);


CREATE TABLE IF NOT EXISTS node_backups (
    id                BIGSERIAL PRIMARY KEY,
    node_id           INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,

    -- Denormalised from `nodes.backup_stanza` on purpose. A stanza can be
    -- renamed or a node re-prepared against a different repository, and a
    -- history that silently re-labels its own past is worse than no history:
    -- this column says which repository the row was actually written against.
    stanza            TEXT NOT NULL,

    -- pgBackRest's three. `full` is self-contained; `diff` and `incr` are not,
    -- and a restore needs the chain -- which is why age is measured against a
    -- *full* backup separately from against any backup at all.
    backup_type       TEXT NOT NULL CHECK (backup_type IN ('full', 'diff', 'incr')),

    -- pgBackRest's own label, e.g. `20260826-143012F`. NULL while running,
    -- because it is not assigned until the backup completes. It is the join
    -- key back to the repository, and the only identifier that means anything
    -- to `pgbackrest restore --set=`.
    label             TEXT,

    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,

    -- `running` is a first-class outcome here, not a transient. See note 1
    -- above: the failure mode this whole slice is defending against presents as
    -- a row that stays `running`.
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'complete', 'failed')),

    -- What the cluster weighed and what the repository grew by. Both from
    -- pgBackRest rather than from `du`, so the numbers mean the same thing on
    -- every node. Slice 0 measured 9.4:1 compression -- structural, because
    -- ADR-015 puts the same ~15 MB of `maludb_core` in every tenant database --
    -- so these two columns differ by an order of magnitude and neither one
    -- alone answers a capacity question.
    database_bytes    BIGINT,
    repository_bytes  BIGINT,

    -- The WAL range the backup needs replayed to be consistent. Kept because a
    -- backup whose WAL has been expired out from under it is not restorable,
    -- and `repo1-retention-archive` unset is precisely how that happens
    -- (ADR-067).
    wal_start         TEXT,
    wal_stop          TEXT,

    -- Why it failed, for the pass that reports it. Never the command line:
    -- pgBackRest is invoked with a stanza and options, none of which are
    -- secret, but the rule that operator output does not accumulate credential
    -- material is worth keeping even where this particular command has none.
    error             TEXT,

    CONSTRAINT node_backups_finished_with_status
        CHECK ((status = 'running') = (finished_at IS NULL)),

    -- A completed backup that named no label cannot be restored from, so it is
    -- not a completed backup.
    CONSTRAINT node_backups_complete_has_label
        CHECK (status <> 'complete' OR label IS NOT NULL)
);

-- The verification pass asks exactly one question per node -- "what is the most
-- recent one, and what became of it" -- and asks it on every run.
CREATE INDEX IF NOT EXISTS node_backups_node_started_idx
    ON node_backups(node_id, started_at DESC);

-- And the same question restricted to full backups, because a `diff` chain
-- rooted on an expired full is a chain to nowhere.
CREATE INDEX IF NOT EXISTS node_backups_node_full_idx
    ON node_backups(node_id, started_at DESC)
    WHERE backup_type = 'full' AND status = 'complete';
