-- Launch slice 4: when the maintenance pass last ran, and how it went.
--
-- The pass is what applies a purchase (ADR-053: the webhook records, the pass
-- applies), measures storage, and ends failed-payment grace (ADR-051). It is a
-- command a timer runs, deliberately not a daemon -- which means nothing noticed
-- when it was never scheduled: customers paid and nothing changed, and every
-- other route still answered. `cp-manage deploy preflight` reads this table and
-- refuses a deployment whose pass has not run recently.
--
-- One row per run, written when it starts and completed when it ends, so a run
-- that died mid-way is a row with no `finished_at` rather than no row at all.
CREATE TABLE IF NOT EXISTS maintenance_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    passes        INTEGER,
    failed        INTEGER,
    CONSTRAINT maintenance_runs_counts_check CHECK (passes IS NULL OR (passes >= 0 AND failed >= 0))
);

CREATE INDEX IF NOT EXISTS maintenance_runs_started_idx ON maintenance_runs (started_at DESC);
