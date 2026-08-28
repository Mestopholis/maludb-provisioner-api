-- Phase 11 slice 4: when each project's two data sets were last compared.
--
-- A project is a tenant database and a set of bytes in the shared platform
-- bucket (ADR-057), and nothing until now compared them. `object_measured_at`
-- next door records when the project's *size* was last taken; this records when
-- its metadata was last checked against the store, which is a different
-- question with a different answer.
--
-- Three columns rather than one, and the reason is that the absence of a
-- finding has to be distinguishable from the absence of a check:
--
--  * `objects_reconciled_at` -- when the comparison last ran to completion. A
--    project that has never been reconciled reads NULL and sorts first, which
--    is what paces the maintenance pass: ordering by project id would let a
--    bounded pass re-check the same head of the list forever while the tail
--    went years without being looked at.
--  * `objects_dangling` -- rows naming bytes the store does not have. The
--    project lists files that cannot be downloaded.
--  * `objects_orphaned` -- keys with no row, older than the pass's age
--    threshold. Bytes nobody can reach and nobody is billed for.
--
-- Counts rather than the keys themselves. A project can hold millions of
-- objects, the keys are customer-authored text, and this table is read by
-- operators and printed to terminals -- `cp-manage storage reconcile` is where
-- the detail lives, against the store, at the moment somebody is looking.
--
-- Both counts are nullable and stay NULL until a comparison completes. Zero
-- means "checked, and they agree"; NULL means "not checked" -- and a pass that
-- wrote 0 on a store it could not read would be recording a clean bill of
-- health it never established, which is the failure mode this whole phase
-- keeps finding.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS objects_reconciled_at timestamptz,
    ADD COLUMN IF NOT EXISTS objects_dangling      integer,
    ADD COLUMN IF NOT EXISTS objects_orphaned      integer;

-- The pass reads least-recently-reconciled first, and NULLS FIRST is the half
-- that matters: a project nobody has ever compared is the most important row in
-- the report.
CREATE INDEX IF NOT EXISTS projects_objects_reconciled_at_idx
    ON projects (objects_reconciled_at NULLS FIRST)
 WHERE database_name IS NOT NULL AND deleted_at IS NULL;
