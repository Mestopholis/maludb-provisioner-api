-- ADR-083: the maintenance pass is split, and each half records its runs where only it can write.
--
-- The control plane's passes keep recording in `maintenance_runs`; `gateway_grants` now takes
-- that table out of the gateway's reach, because preflight reads it to decide whether the
-- control-plane pass is running and a gateway could otherwise forge a fresh run.
--
-- The node half -- sleeping idle workers, run on each node as its gateway -- records here. The
-- gateway's own-node row policy (0031's shape) means a gateway can record runs for its node and
-- read none of any other's.
CREATE TABLE node_maintenance_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id       BIGINT NOT NULL REFERENCES nodes(id),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    slept         INTEGER,
    failed        INTEGER,
    CONSTRAINT node_maintenance_runs_counts_check CHECK (slept IS NULL OR (slept >= 0 AND failed >= 0))
);

CREATE INDEX node_maintenance_runs_node_started_idx ON node_maintenance_runs (node_id, started_at DESC);

ALTER TABLE node_maintenance_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY gateway_own_node ON node_maintenance_runs
    FOR ALL USING (node_id = public.gateway_node_id())
    WITH CHECK (node_id = public.gateway_node_id());
