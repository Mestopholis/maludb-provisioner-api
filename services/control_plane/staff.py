"""Platform staff: accounts, a mandatory second factor, and sessions (ADR-082 slice 0).

Staff are not customers. Nothing here reads `users`, `organizations` or `org_members`,
and the session token has its own kind (`mldb_staff_...`), so:

- `identity.resolve_principal` refuses a staff token -- it accepts only `sess` and `pat`;
- `resolve` here refuses a customer token -- it accepts only `staff`.

Both directions are tested, because either one failing turns a customer credential into
operator access or the reverse.

**Every sign-in needs the password and a code together.** There is no half-signed-in
state to attack: no token exists until both have been checked, and failures from either
count toward one lockout. A code is accepted only for a later 30-second step than the
last one used, so a code read over someone's shoulder cannot be replayed.

**The second factor's seed is sealed under the staff key, not the platform KEK.** The
admin process must open seeds to verify codes; with the KEK it could open every node
and project secret as well. `StaffKey` refuses KEK material outright.

Accounts are created, enrolled, re-passworded and revoked only by `cp-manage staff`
(ADR-082: one staff role, so no staff member can safely manage the others from a
browser). Each of those writes `audit_events` with `actor_type = 'staff'` and the OS
account that ran the command.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from urllib.parse import quote

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.types.json import Jsonb

from services.control_plane import crypto, db, hashing, identity

SESSION_LIFETIME = timedelta(hours=8)
IDLE_TIMEOUT = timedelta(minutes=30)
MAX_FAILED_SIGNINS = 5
LOCKOUT = timedelta(minutes=15)
PASSWORD_MIN = 16

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
# One step either side: a phone clock a little off, or a code typed as it rolled over.
TOTP_WINDOW = 1
SEED_BYTES = 20  # RFC 4226 recommends 160 bits for HMAC-SHA-1
ISSUER = "MaluDB staff"

_SEED_AAD_TABLE = "staff_mfa_factors"
_SEED_AAD_COLUMN = "seed_ciphertext"

# A valid Argon2id hash of nothing anyone knows, verified when the address matches no
# account so an unknown address and a wrong password cost the same.
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21l$0000000000000000000000000000000000000000000"


class StaffError(RuntimeError):
    """A rule of the staff model was violated."""


@dataclass(frozen=True)
class Staff:
    id: uuid.UUID
    email: str
    display_name: str | None
    status: str


@dataclass(frozen=True)
class StaffPrincipal:
    """An authenticated staff member. Deliberately shares nothing with `identity.Principal`."""

    staff: Staff
    session_id: uuid.UUID


@dataclass(frozen=True)
class Enrolment:
    """What an authenticator app needs, shown once when a factor is created."""

    secret: str  # base32, for typing in by hand
    uri: str  # otpauth://, for a QR code


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(conn: psycopg.Connection, *, actor: str, event: str, detail: dict) -> None:
    db.execute(
        conn,
        "INSERT INTO audit_events (actor_type, actor_id, event_type, detail_json) VALUES ('staff', %s, %s, %s)",
        (actor[:200], event, Jsonb(detail)),
    )


# -- the staff key -----------------------------------------------------------


class StaffKey:
    """Seals staff TOTP seeds, and nothing else.

    AES-256-GCM with the seed bound to its staff account, as `crypto` binds every
    ciphertext to its row. No data-encryption-key table: the only things sealed are a
    handful of seeds, and losing this key is recovered by re-enrolling, not by rotation.
    """

    def __init__(self, material: bytes, *, kek: bytes | None = None) -> None:
        if len(material) < 32:
            raise StaffError("the staff key needs at least 32 bytes of material")
        if kek is not None and hmac.compare_digest(material, kek):
            # ADR-082 decision 4. The same material would let whatever holds the staff key
            # derive the KEK's keys too, which is the reach the separate key exists to deny.
            raise StaffError("the staff key must not be the KEK; generate separate material")
        self._key = crypto.derive_key(material, info=b"maludb-staff-mfa-seed-v1")
        # ADR-082 decision 4 says the admin process holds one secret. Staff session tokens
        # are verified with a pepper, and the platform pepper also verifies every customer
        # session, access token and API key -- so staff sessions take theirs from this key.
        self.session_pepper = crypto.derive_key(material, info=b"maludb-staff-session-pepper-v1")

    @staticmethod
    def _aad(staff_id: uuid.UUID) -> bytes:
        return crypto.aad_for(_SEED_AAD_TABLE, _SEED_AAD_COLUMN, str(staff_id))

    def seal(self, seed: bytes, *, staff_id: uuid.UUID) -> tuple[bytes, bytes]:
        nonce = secrets.token_bytes(crypto.NONCE_BYTES)
        return AESGCM(self._key).encrypt(nonce, seed, self._aad(staff_id)), nonce

    def open(self, ciphertext: bytes, nonce: bytes, *, staff_id: uuid.UUID) -> bytes:
        try:
            return AESGCM(self._key).decrypt(nonce, ciphertext, self._aad(staff_id))
        except InvalidTag as exc:
            raise StaffError(
                "a staff second factor could not be opened: the staff key is wrong, or the seed "
                "was moved from another account. Re-enrol with `cp-manage staff enrol`."
            ) from exc


# -- TOTP (RFC 6238 over RFC 4226) -------------------------------------------


def totp(seed: bytes, step: int) -> str:
    """The code for one 30-second step. HMAC-SHA-1, six digits: what every authenticator app does."""
    digest = hmac.new(seed, struct.pack(">Q", step), sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def step_at(moment: datetime) -> int:
    return int(moment.timestamp()) // TOTP_STEP_SECONDS


def accepted_step(seed: bytes, code: str, *, now: datetime, after_step: int) -> int | None:
    """The step `code` belongs to, if it is within the window and later than `after_step`.

    Every candidate is compared, so how long this takes does not say which one matched.
    """
    code = (code or "").strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = step_at(now)
    matched = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if hmac.compare_digest(totp(seed, step), code) and step > after_step:
            matched = step
    return matched


# -- accounts ----------------------------------------------------------------


def _row_to_staff(row: dict) -> Staff:
    return Staff(id=row["id"], email=row["email"], display_name=row["display_name"], status=row["status"])


def _check_password(password: str) -> None:
    if len(password or "") < PASSWORD_MIN:
        raise StaffError(f"a staff password needs at least {PASSWORD_MIN} characters")


def find_active(conn: psycopg.Connection, email: str) -> Staff | None:
    row = db.one(
        conn,
        "SELECT id, email, display_name, status FROM staff_users WHERE email = %s AND status = 'active'",
        (email.strip().lower(),),
    )
    return _row_to_staff(row) if row else None


def create(conn: psycopg.Connection, *, email: str, password: str, display_name: str | None, actor: str) -> Staff:
    """A staff account with no factor yet. It cannot sign in until `enrol` and `confirm_enrolment`."""
    email = email.strip().lower()
    if not email or "@" not in email:
        raise StaffError("a valid email address is required")
    _check_password(password)
    staff_id = uuid.uuid4()
    with conn.transaction():
        if find_active(conn, email) is not None:
            raise StaffError(f"{email} already has an active staff account")
        db.execute(
            conn,
            """
            INSERT INTO staff_users (id, email, display_name, password_hash, created_by)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (staff_id, email, display_name, hashing.hash_password(password), actor[:200]),
        )
        _audit(conn, actor=actor, event="staff.created", detail={"staff_id": str(staff_id), "email": email})
    return Staff(id=staff_id, email=email, display_name=display_name, status="active")


def enrol(conn: psycopg.Connection, *, staff: Staff, staff_key: StaffKey, actor: str) -> Enrolment:
    """Create a new second factor, replacing any old one, and end the account's sessions.

    The factor is unconfirmed until a code from it is entered (`confirm_enrolment`), so a
    seed printed to a terminal and never scanned does not become a way in.
    """
    seed = secrets.token_bytes(SEED_BYTES)
    ciphertext, nonce = staff_key.seal(seed, staff_id=staff.id)
    with conn.transaction():
        db.execute(
            conn,
            """
            INSERT INTO staff_mfa_factors (staff_id, seed_ciphertext, seed_nonce)
            VALUES (%s, %s, %s)
            ON CONFLICT (staff_id) DO UPDATE
               SET seed_ciphertext = EXCLUDED.seed_ciphertext, seed_nonce = EXCLUDED.seed_nonce,
                   confirmed_at = NULL, last_used_step = 0, created_at = now()
            """,
            (staff.id, ciphertext, nonce),
        )
        revoke_sessions(conn, staff.id)
        _audit(conn, actor=actor, event="staff.enrolled", detail={"staff_id": str(staff.id)})
    secret = base64.b32encode(seed).decode().rstrip("=")
    label = quote(f"{ISSUER}:{staff.email}")
    uri = (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )
    return Enrolment(secret=secret, uri=uri)


def confirm_enrolment(
    conn: psycopg.Connection, *, staff: Staff, code: str, staff_key: StaffKey, actor: str, now: datetime | None = None
) -> bool:
    """Confirm an unconfirmed factor with one code from it. The step is spent, as at sign-in."""
    now = now or _now()
    with conn.transaction():
        row = db.one(
            conn,
            "SELECT seed_ciphertext, seed_nonce, last_used_step, confirmed_at FROM staff_mfa_factors "
            "WHERE staff_id = %s FOR UPDATE",
            (staff.id,),
        )
        if row is None or row["confirmed_at"] is not None:
            return False
        seed = staff_key.open(bytes(row["seed_ciphertext"]), bytes(row["seed_nonce"]), staff_id=staff.id)
        step = accepted_step(seed, code, now=now, after_step=row["last_used_step"])
        if step is None:
            return False
        db.execute(
            conn,
            "UPDATE staff_mfa_factors SET confirmed_at = %s, last_used_step = %s WHERE staff_id = %s",
            (now, step, staff.id),
        )
        _audit(conn, actor=actor, event="staff.factor_confirmed", detail={"staff_id": str(staff.id)})
    return True


def set_password(conn: psycopg.Connection, *, staff: Staff, password: str, actor: str) -> None:
    _check_password(password)
    with conn.transaction():
        db.execute(
            conn,
            "UPDATE staff_users SET password_hash = %s, failed_signins = 0, locked_until = NULL "
            "WHERE id = %s AND status = 'active'",
            (hashing.hash_password(password), staff.id),
        )
        revoke_sessions(conn, staff.id)
        _audit(conn, actor=actor, event="staff.password_set", detail={"staff_id": str(staff.id)})


def revoke(conn: psycopg.Connection, *, staff: Staff, actor: str) -> None:
    """Permanent. The row stays for the audit trail; the address may be given a new account."""
    with conn.transaction():
        db.execute(
            conn,
            "UPDATE staff_users SET status = 'revoked', revoked_at = now() WHERE id = %s AND status = 'active'",
            (staff.id,),
        )
        revoke_sessions(conn, staff.id)
        _audit(conn, actor=actor, event="staff.revoked", detail={"staff_id": str(staff.id), "email": staff.email})


def list_staff(conn: psycopg.Connection) -> list[dict]:
    """Every account, by metadata alone: never a hash, a seed or a session token."""
    return db.query(
        conn,
        """
        SELECT s.id, s.email, s.display_name, s.status, s.last_signin_at, s.locked_until, s.created_at,
               s.created_by, f.confirmed_at AS factor_confirmed_at,
               (SELECT count(*) FROM staff_sessions x
                 WHERE x.staff_id = s.id AND x.revoked_at IS NULL AND x.expires_at > now()) AS live_sessions
          FROM staff_users s
          LEFT JOIN staff_mfa_factors f ON f.staff_id = s.id
         ORDER BY s.status, s.email
        """,
    )


# -- sessions ----------------------------------------------------------------


def revoke_sessions(conn: psycopg.Connection, staff_id: uuid.UUID) -> int:
    return db.execute(
        conn,
        "UPDATE staff_sessions SET revoked_at = now() WHERE staff_id = %s AND revoked_at IS NULL",
        (staff_id,),
    )


def sign_in(
    conn: psycopg.Connection,
    *,
    email: str,
    password: str,
    code: str,
    staff_key: StaffKey,
    pepper: bytes,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """A session token for a correct password *and* code, or None for any failure.

    Every failure looks the same to the caller. Each one against a real account counts
    toward a lockout, which is recorded even though nothing is raised: the transaction
    commits the failure count rather than rolling it back with the refusal.
    """
    now = now or _now()
    email = (email or "").strip().lower()
    ip_address = identity.normalise_ip(ip_address)
    with conn.transaction():
        row = db.one(
            conn,
            """
            SELECT s.id, s.email, s.display_name, s.status, s.password_hash, s.failed_signins, s.locked_until,
                   f.seed_ciphertext, f.seed_nonce, f.confirmed_at, f.last_used_step
              FROM staff_users s
              LEFT JOIN staff_mfa_factors f ON f.staff_id = s.id
             WHERE s.email = %s AND s.status = 'active'
             FOR UPDATE OF s
            """,
            (email,),
        )
        if row is None:
            hashing.verify_password(_DUMMY_HASH, password or "")
            _audit(conn, actor="staff:unknown", event="staff.signin_failed",
                   detail={"email": email[:320], "reason": "unknown", "ip": ip_address})
            return None

        actor = f"staff:{row['id']}"
        if row["locked_until"] is not None and row["locked_until"] > now:
            _audit(conn, actor=actor, event="staff.signin_failed", detail={"reason": "locked", "ip": ip_address})
            return None

        reason = None
        step = None
        if not hashing.verify_password(row["password_hash"], password or ""):
            reason = "credentials"
        elif row["confirmed_at"] is None:
            reason = "no_factor"
        else:
            seed = staff_key.open(bytes(row["seed_ciphertext"]), bytes(row["seed_nonce"]), staff_id=row["id"])
            step = accepted_step(seed, code, now=now, after_step=row["last_used_step"])
            if step is None:
                reason = "credentials"
            else:
                # Spend the step. Conditional, so two sign-ins racing with one code cannot both win.
                spent = db.execute(
                    conn,
                    "UPDATE staff_mfa_factors SET last_used_step = %s WHERE staff_id = %s AND last_used_step < %s",
                    (step, row["id"], step),
                )
                if spent != 1:
                    reason = "credentials"

        if reason is not None:
            failures = row["failed_signins"] + 1
            locked = failures >= MAX_FAILED_SIGNINS
            db.execute(
                conn,
                "UPDATE staff_users SET failed_signins = %s, locked_until = %s WHERE id = %s",
                (0 if locked else failures, now + LOCKOUT if locked else row["locked_until"], row["id"]),
            )
            _audit(conn, actor=actor, event="staff.signin_failed",
                   detail={"reason": reason, "ip": ip_address, "locked": locked})
            return None

        token = hashing.generate_token("staff", pepper)
        db.execute(
            conn,
            "UPDATE staff_users SET failed_signins = 0, locked_until = NULL, last_signin_at = %s WHERE id = %s",
            (now, row["id"]),
        )
        session_id = uuid.uuid4()
        db.execute(
            conn,
            """
            INSERT INTO staff_sessions (id, staff_id, token_hash, ip_address, user_agent, created_at,
                                        last_seen_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (session_id, row["id"], token.verifier, ip_address, (user_agent or "")[:500] or None, now, now,
             now + SESSION_LIFETIME),
        )
        _audit(conn, actor=actor, event="staff.signin", detail={"session_id": str(session_id), "ip": ip_address})
    return token.plaintext


def resolve(
    conn: psycopg.Connection, *, presented: str, pepper: bytes, now: datetime | None = None
) -> StaffPrincipal | None:
    """Resolve a staff session token, or None -- for a customer token, as for any other failure.

    Refused when the session is revoked, past its absolute lifetime or idle too long, the
    account is revoked, or its factor is no longer confirmed (a re-enrolment in progress).
    """
    now = now or _now()
    parts = hashing.split_token(presented or "")
    if parts is None or parts[0] != "staff":
        return None
    verifier = hashing.peppered(presented, pepper)
    row = db.one(
        conn,
        """
        SELECT x.id AS session_id, x.expires_at, x.revoked_at, x.last_seen_at,
               s.id, s.email, s.display_name, s.status, f.confirmed_at
          FROM staff_sessions x
          JOIN staff_users s ON s.id = x.staff_id
          LEFT JOIN staff_mfa_factors f ON f.staff_id = s.id
         WHERE x.token_hash = %s
        """,
        (verifier,),
    )
    if row is None or row["revoked_at"] is not None or row["status"] != "active" or row["confirmed_at"] is None:
        return None
    if row["expires_at"] <= now or row["last_seen_at"] + IDLE_TIMEOUT <= now:
        return None
    db.execute(conn, "UPDATE staff_sessions SET last_seen_at = %s WHERE id = %s", (now, row["session_id"]))
    return StaffPrincipal(staff=_row_to_staff(row), session_id=row["session_id"])


def sign_out(conn: psycopg.Connection, *, presented: str, pepper: bytes) -> bool:
    parts = hashing.split_token(presented or "")
    if parts is None or parts[0] != "staff":
        return False
    return db.execute(
        conn,
        "UPDATE staff_sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
        (hashing.peppered(presented, pepper),),
    ) > 0
