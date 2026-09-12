-- Phase 12 slice 6: a project can turn the data-model graph off.
--
-- Withdrawing, not purging. Disabling takes `maludb` off the project's Data API
-- and stops refreshes; the memory schema and the copy stay in the tenant
-- database, so nothing is lost and enabling again rebuilds on what is there.
-- Dropping them is a separate, destructive decision this does not make: a later
-- MaluDB surface such as the memory pipeline would keep real data in that
-- schema, and "turn it off" must never be the thing that destroys it.

ALTER TABLE maludb_jobs DROP CONSTRAINT IF EXISTS maludb_jobs_kind_check;
ALTER TABLE maludb_jobs
    ADD CONSTRAINT maludb_jobs_kind_check CHECK (kind IN ('enable', 'refresh', 'disable'));
