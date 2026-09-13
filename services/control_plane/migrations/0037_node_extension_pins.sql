-- ADR-075 decision 5, pinning slice 1: each node's pin for the extensions the
-- platform pins exactly.
--
-- A pin is an operator's decision, not a measurement, which is why it is a row
-- here rather than a key in `nodes.capacity_json` beside the Realtime and backup
-- checks: a re-run check rewrites `capacity_json`, and it must never be able to
-- overwrite what an operator decided. What the node was last measured to
-- provide does live there (`extension_check`), and placement compares the two.
--
-- The version must be one `specs/extension-versions.yaml` lists as tested. That
-- is enforced where the list is read -- `extension_pins.set_pin` -- rather than by
-- a constraint, because the list is a reviewed file and not a table.

CREATE TABLE IF NOT EXISTS node_extension_pins (
    node_id    BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    extension  TEXT NOT NULL CHECK (extension IN ('vector', 'maludb_core')),
    version    TEXT NOT NULL,
    set_by     TEXT NOT NULL,
    set_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, extension)
);

-- ADR-072 point 2: every node-keyed table carries the gateway's row policy, and
-- tests/test_gateway_grants.py fails on one that does not.
ALTER TABLE node_extension_pins ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON node_extension_pins
    FOR ALL USING (node_id = public.gateway_node_id())
    WITH CHECK (node_id = public.gateway_node_id());
