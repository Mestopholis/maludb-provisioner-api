-- ADR-082 slice 0: platform staff are a principal of their own.
--
-- **Not a flag on `users`.** A staff account shares no table, session or token
-- format with a customer account, so a phished customer password, a bug in
-- organization authorization, or a signup with the right address never becomes
-- operator access. Nothing here references `users`, `organizations` or
-- `org_members`, and nothing there references this.
--
-- One role for the first version (ADR-082, settled with acceptance), so there is
-- no role column: every active staff account sees every read-only report.

CREATE TABLE staff_users (
    id                  UUID PRIMARY KEY,
    email               VARCHAR(320) NOT NULL,
    display_name        VARCHAR(200),
    -- Argon2id, as for customer passwords (ADR-023: human-chosen, so memory-hard).
    password_hash       TEXT NOT NULL,
    -- active: may sign in once a factor is confirmed.
    -- revoked: permanent; sessions ended; the address may be given a new account.
    status              VARCHAR(20) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'revoked')),
    -- Consecutive failed sign-ins, and when the account may try again. A password
    -- and a six-digit code together still leave the code to guess, so failures
    -- lock the account for a while rather than trusting the network to be private.
    failed_signins      INTEGER NOT NULL DEFAULT 0,
    locked_until        TIMESTAMPTZ,
    last_signin_at      TIMESTAMPTZ,
    created_by          VARCHAR(200) NOT NULL,   -- the OS account that ran cp-manage
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at          TIMESTAMPTZ
);

-- One live account per address; a revoked account keeps its row for the audit trail.
CREATE UNIQUE INDEX staff_users_live_email_idx ON staff_users (email) WHERE status = 'active';

-- **The seed is sealed under the staff key, not the platform KEK** (ADR-082
-- decision 4). The admin process verifies codes and so must open seeds; giving it
-- the KEK would give it every node and project secret too. So there is no
-- key_version into `encryption_keys` here: losing the staff key means every staff
-- member re-enrols (`cp-manage staff enrol`), and nothing else is lost.
CREATE TABLE staff_mfa_factors (
    staff_id            UUID PRIMARY KEY REFERENCES staff_users(id),
    seed_ciphertext     BYTEA NOT NULL,
    seed_nonce          BYTEA NOT NULL,
    -- Null until a code from the authenticator app has been entered once. No
    -- session is issued against an unconfirmed factor.
    confirmed_at        TIMESTAMPTZ,
    -- The newest 30-second step a code was accepted for. A code is accepted only
    -- for a later step, so an observed code cannot be replayed within its window.
    last_used_step      BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Server-side, like `user_sessions` (ADR-021: revocation must take effect at once),
-- but a separate table with a separate token kind (`mldb_staff_...`), so neither
-- resolver can accept the other's credential.
CREATE TABLE staff_sessions (
    id                  UUID PRIMARY KEY,
    staff_id            UUID NOT NULL REFERENCES staff_users(id),
    -- HMAC-SHA-256 with the platform pepper (ADR-023 Class A); the token is returned once.
    token_hash          TEXT NOT NULL UNIQUE,
    ip_address          INET,
    user_agent          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ
);

CREATE INDEX staff_sessions_staff_idx ON staff_sessions (staff_id) WHERE revoked_at IS NULL;

COMMENT ON TABLE staff_users IS
    'Platform staff (ADR-082). Separate from users; created only by cp-manage staff create.';
COMMENT ON COLUMN staff_mfa_factors.seed_ciphertext IS
    'TOTP seed, AES-256-GCM under the staff key (MALUDB_STAFF_KEY_REF), never the KEK.';
