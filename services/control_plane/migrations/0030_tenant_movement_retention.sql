-- Phase 11 slice 7, follow-up to 0029 (ADR-066, ADR-071).
--
-- A completed move retires the source by renaming it aside rather than dropping
-- it. 0029 recorded only `source_cleaned`, which could say that the source was
-- dealt with but not what it is now called -- and the name is the whole of the
-- rollback path. Without it, undoing a move means guessing a timestamp.
--
-- `still_frozen` records the one state an operator has to act on by hand: a
-- move that failed *and* could not give the source its CONNECT grants back, so
-- the tenant is offline until someone runs `cp-manage node release-freeze`.
-- It is false for every ordinary failure, which unfreezes itself.

ALTER TABLE tenant_moves
    ADD COLUMN IF NOT EXISTS retained_database TEXT,
    ADD COLUMN IF NOT EXISTS still_frozen BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS frozen_roles TEXT[],
    ADD COLUMN IF NOT EXISTS frozen_public BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN tenant_moves.source_cleaned IS
    'The source was retired: renamed to retained_database. Nothing is dropped.';
COMMENT ON COLUMN tenant_moves.retained_database IS
    'What the source database was renamed to, and the name to rename back to '
    'undo this move. NULL if the move did not reach retirement.';
COMMENT ON COLUMN tenant_moves.still_frozen IS
    'A failed move whose source could not be unfrozen. Needs an operator.';
COMMENT ON COLUMN tenant_moves.frozen_roles IS
    'Exactly the roles CONNECT was revoked from, recorded before it was taken. '
    'This is what `cp-manage node release-freeze` gives back, and the reason it '
    'is recorded rather than inferred: after a freeze every tenant role lacks '
    'CONNECT, so reading the catalogue cannot tell a role the freeze took it '
    'from apart from one that never had it -- a lingering replicator on a '
    'project whose Realtime was turned off, say -- and the release would grant '
    'it to both.';
COMMENT ON COLUMN tenant_moves.frozen_public IS
    'Whether PUBLIC held CONNECT when the freeze was taken. Normally false: '
    'lock_down_database revokes it at provisioning.';

-- A retained name is recorded only by a move that got far enough to retire the
-- source, and that only happens on a complete one.
ALTER TABLE tenant_moves
    ADD CONSTRAINT tenant_moves_retained_only_when_complete
    CHECK (retained_database IS NULL OR status = 'complete');

-- The inverse: a stranded freeze is only reachable from a failure.
ALTER TABLE tenant_moves
    ADD CONSTRAINT tenant_moves_frozen_only_when_failed
    CHECK (still_frozen IS FALSE OR status = 'failed');

CREATE INDEX IF NOT EXISTS tenant_moves_still_frozen_idx
    ON tenant_moves(project_id)
    WHERE still_frozen;
