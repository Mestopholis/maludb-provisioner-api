-- Free slice 14: what the platform has told an operator about, and when.
--
-- The gap this closes: nothing alerted anybody. A failing maintenance pass wrote
-- `maintenance_runs.failed` and its reasons to journald, a node that stopped
-- reporting left `nodes.last_health_at` behind, and both waited for a human to
-- run `deploy preflight` or open the console. On a deployment with real signups
-- that means a customer discovers the outage first.
--
-- One row per *condition*, not per notification: a fingerprint names the thing
-- that is wrong ("maintenance-stalled", "node-health:node-01"), and the row
-- carries when it was first seen, when it was last sent and how many times. That
-- is what makes a five-minute timer send one message rather than 288 a day --
-- an alert nobody can silence is an alert everybody filters.
--
-- Resolved rather than deleted: "this cleared at 04:12 after eleven hours" is
-- the sentence an operator wants the next morning, and a deleted row cannot say
-- it. `sends` outliving the condition is also the only record of how noisy a
-- condition was.
CREATE TABLE IF NOT EXISTS operator_alerts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fingerprint     VARCHAR(200) NOT NULL,
    kind            VARCHAR(60) NOT NULL,
    subject         TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sent_at    TIMESTAMPTZ,
    sends           INTEGER NOT NULL DEFAULT 0,
    resolved_at     TIMESTAMPTZ,
    resolved_sent   BOOLEAN NOT NULL DEFAULT FALSE
);

-- One open row per condition. A resolved one is history and may repeat, which is
-- why the uniqueness is partial rather than on the fingerprint alone.
CREATE UNIQUE INDEX IF NOT EXISTS operator_alerts_open_idx
    ON operator_alerts (fingerprint) WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS operator_alerts_seen_idx ON operator_alerts (last_seen_at DESC);

-- ADR-072: the gateway has no business reading what the platform tells its
-- operator. `gateway_grants` refuses the table outright as well; this is the
-- policy every project-keyed table carries, and this table is keyed to none.
ALTER TABLE operator_alerts ENABLE ROW LEVEL SECURITY;
