-- ADR-079 memory slice 2c, batched deletion: a MaluDB job that shows it is alive.
--
-- A job running longer than `maludb_jobs.ABANDONED_AFTER` was taken for a dead
-- worker's and failed. Deleting a large memory space now legitimately runs longer
-- than that, in bounded batches, so the job records a heartbeat after each batch and
-- "abandoned" is measured from the last one rather than from when it started.
ALTER TABLE maludb_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
