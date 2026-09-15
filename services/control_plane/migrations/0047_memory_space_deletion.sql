-- ADR-079 memory slice 2c: deleting a memory space.
--
-- `deleting` is set in the same transaction that queues the provisioner's job, and
-- it is what stops the space being written to before the node work starts: the
-- gateway admits ingests only into an `active` space, the memory worker claims
-- only those, and the request that set it fails the space's pending ingests.
-- The row is removed when the tenant's transaction has committed, and with it
-- every ingest (ON DELETE CASCADE) -- which is what releases the plan's slot.
ALTER TABLE memory_spaces DROP CONSTRAINT IF EXISTS memory_spaces_state_check;
ALTER TABLE memory_spaces ADD CONSTRAINT memory_spaces_state_check
    CHECK (state IN ('pending', 'active', 'failed', 'deleting'));
