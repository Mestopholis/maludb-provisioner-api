-- Phase 07 slice 4: a platform user can reset their own password.
--
-- Nothing could before this. `identity` authenticates, creates sessions and
-- personal access tokens, and manages invitations and roles -- and a customer
-- who forgot their password had no way back to their own organization's
-- projects except asking an operator to do something no route supports.

CREATE TABLE IF NOT EXISTS password_resets (
    id                UUID PRIMARY KEY,
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Stored the way every other credential in this schema is: an indexed
    -- prefix and a peppered HMAC of the whole token (ADR-023 Class A). The
    -- token itself exists only in the email that carried it, so a database
    -- leak yields nothing that can reset an account.
    token_prefix      VARCHAR(32) NOT NULL,
    verification_data TEXT NOT NULL,
    -- Short, because a reset link is a bearer credential for an account. Long
    -- enough that a person who reads mail on a phone an hour later is not
    -- locked out of the process they just started.
    expires_at        TIMESTAMPTZ NOT NULL,
    -- Set the moment it is spent. A reset link that works twice is a link that
    -- still works after the attacker who intercepted it has been locked out by
    -- the legitimate reset.
    used_at           TIMESTAMPTZ,
    requested_ip      INET,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS password_resets_prefix_idx
    ON password_resets(token_prefix);

-- The two queries this table has: spend one by prefix, and invalidate every
-- outstanding one for a user when a reset succeeds or the password changes.
CREATE INDEX IF NOT EXISTS password_resets_open_for_user_idx
    ON password_resets(user_id) WHERE used_at IS NULL;
