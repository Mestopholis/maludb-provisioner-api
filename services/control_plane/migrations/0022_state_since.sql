-- Phase 09 slice 5: when a state *began*, as distinct from when it was last true.
--
-- ADR-051 gives a failed payment fourteen days of unchanged service. That
-- period has to be measured from something, and the obvious candidate --
-- `state_as_of` -- is wrong in a way that would have been invisible until it
-- mattered.
--
-- `state_as_of` is *when this fact was true*, and it moves on every delivery.
-- Stripe re-sends `customer.subscription.updated` with `status=past_due` on
-- every dunning retry, each carrying a newer `created`. A grace period measured
-- from it restarts on every retry, so it never expires: the customer keeps a
-- paid plan indefinitely, and the failure looks exactly like the system working.
--
-- `state_since` is *when this state began*: written when the state changes, left
-- alone when the same state is re-asserted. The two are equal until a state is
-- confirmed twice, which is why the difference is easy to miss.
-- `DEFAULT now()` rather than a bare NOT NULL, and it is semantics rather than
-- convenience: a row being written is entering its state now. It also means any
-- inserter that does not know about this column -- a backfill, a psql session,
-- a future code path -- gets a correct value instead of an error, which is the
-- opposite of how a nullable column would fail.
ALTER TABLE subscriptions ADD COLUMN state_since TIMESTAMPTZ NOT NULL DEFAULT now();

-- Existing rows have been in their current state since whenever they last
-- changed, and the closest honest answer available is `state_as_of` -- which the
-- default above would otherwise replace with "now", restarting every grace
-- period in flight. It can only be too late, never too early, so a project
-- mid-grace at deploy time gets a little more grace rather than less, which is
-- the direction to be wrong in.
UPDATE subscriptions SET state_since = state_as_of;

-- The grace pass reads exactly this.
CREATE INDEX IF NOT EXISTS subscriptions_past_due_idx
    ON subscriptions(state_since) WHERE state = 'past_due';
