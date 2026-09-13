-- ADR-077, compartments slice 2b: a customer asks for vector compartments to be
-- turned on or off through the same queue the data-model graph uses, and the
-- provisioner does the node work (ADR-038). Two kinds of their own rather than a
-- feature column, so the one-pending-per-kind index keeps coalescing each
-- feature's requests separately.
ALTER TABLE maludb_jobs DROP CONSTRAINT IF EXISTS maludb_jobs_kind_check;
ALTER TABLE maludb_jobs
    ADD CONSTRAINT maludb_jobs_kind_check
    CHECK (kind IN ('enable', 'refresh', 'disable', 'vectors_enable', 'vectors_disable'));
