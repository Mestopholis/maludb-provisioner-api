"""Platform identity: users, organizations, membership, sessions, tokens.

ADR-020 (projects are owned by organizations from row one) and ADR-021
(control-plane identity is separate from tenant Auth). These are MaluDB's own
users; a customer application's end users live in each tenant database's auth
schema and never appear here.

Invariants enforced in this module, each with a blocking test in
docs/TESTING.md:

- signup creates a personal organization with the user as owner;
- an organization always retains at least one owner;
- a user cannot read or act on an organization they do not belong to;
- revoking a session or token takes effect on the next request;
- an invitation is single-use, expiring, and acceptable only by its address.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from services.control_plane import db, hashing

SESSION_LIFETIME = timedelta(hours=12)
INVITATION_LIFETIME = timedelta(days=7)

ROLES = ("owner", "admin", "developer", "billing", "viewer")
# Roles permitted to manage members, invitations and project lifecycle.
MANAGING_ROLES = ("owner", "admin")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class IdentityError(RuntimeError):
    """A rule of the identity model was violated."""


@dataclass(frozen=True)
class User:
    id: uuid.UUID
    email: str
    display_name: str | None
    status: str


@dataclass(frozen=True)
class Membership:
    org_id: uuid.UUID
    org_slug: str
    org_name: str
    role: str


@dataclass(frozen=True)
class Principal:
    """An authenticated caller and the organizations it can act in."""

    user: User
    memberships: tuple[Membership, ...]
    via: str  # "session" or "token"

    def role_in(self, org_id: uuid.UUID) -> str | None:
        for membership in self.memberships:
            if membership.org_id == org_id:
                return membership.role
        return None

    def is_member_of(self, org_id: uuid.UUID) -> bool:
        return self.role_in(org_id) is not None

    def can_manage(self, org_id: uuid.UUID) -> bool:
        return self.role_in(org_id) in MANAGING_ROLES


def _now() -> datetime:
    return datetime.now(UTC)


def slugify(value: str, *, suffix: str) -> str:
    base = _SLUG_RE.sub("-", value.strip().lower()).strip("-") or "org"
    return f"{base[:40]}-{suffix}"


# -- users and organizations ----------------------------------------------


def create_user_with_personal_org(
    conn: psycopg.Connection,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
) -> tuple[User, uuid.UUID]:
    """Register a user and give them a personal organization as owner.

    ADR-020: ownership is an organization from the first row written, so a solo
    developer never encounters organizational concepts while the ownership edge
    still exists.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise IdentityError("a valid email address is required")

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()

    with conn.transaction():
        existing = db.one(conn, "SELECT id FROM users WHERE email = %s", (email,))
        if existing is not None:
            raise IdentityError("email already registered")

        db.execute(
            conn,
            """
            INSERT INTO users (id, email, password_hash, display_name, status)
            VALUES (%s, %s, %s, %s, 'active')
            """,
            (user_id, email, hashing.hash_password(password), display_name),
        )
        db.execute(
            conn,
            """
            INSERT INTO organizations (id, slug, display_name, is_personal)
            VALUES (%s, %s, %s, TRUE)
            """,
            (org_id, slugify(display_name or email.split("@")[0], suffix=str(org_id)[:8]), display_name or email),
        )
        db.execute(
            conn,
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, user_id),
        )

    return User(id=user_id, email=email, display_name=display_name, status="active"), org_id


def authenticate(conn: psycopg.Connection, *, email: str, password: str) -> User | None:
    row = db.one(
        conn,
        "SELECT id, email, password_hash, display_name, status FROM users WHERE email = %s AND deleted_at IS NULL",
        (email.strip().lower(),),
    )
    if row is None:
        # Hash anyway so a missing account and a wrong password cost the same.
        hashing.verify_password(
            "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21l$0000000000000000000000000000000000000000000",
            password,
        )
        return None
    if row["status"] != "active" or not row["password_hash"]:
        return None
    if not hashing.verify_password(row["password_hash"], password):
        return None
    db.execute(conn, "UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))
    return User(id=row["id"], email=row["email"], display_name=row["display_name"], status=row["status"])


def memberships_for(conn: psycopg.Connection, user_id: uuid.UUID) -> tuple[Membership, ...]:
    rows = db.query(
        conn,
        """
        SELECT m.org_id, m.role, o.slug, o.display_name
          FROM org_members m
          JOIN organizations o ON o.id = m.org_id
         WHERE m.user_id = %s AND o.deleted_at IS NULL
         ORDER BY o.display_name
        """,
        (user_id,),
    )
    return tuple(
        Membership(org_id=r["org_id"], org_slug=r["slug"], org_name=r["display_name"], role=r["role"]) for r in rows
    )


def count_owners(conn: psycopg.Connection, org_id: uuid.UUID) -> int:
    row = db.one(conn, "SELECT count(*) AS n FROM org_members WHERE org_id = %s AND role = 'owner'", (org_id,))
    return int(row["n"]) if row else 0


def guard_owner_tier(*, actor_role: str, target_role: str | None, granted_role: str | None) -> None:
    """Only an owner may grant the owner role, or act on an existing owner.

    Enforced here rather than in the route layer so a future endpoint cannot
    reach the owner tier by forgetting a check.

    Without this an `admin` -- documented in docs/ACCOUNTS.md as explicitly
    lacking billing and organization deletion -- could assign itself `owner`
    and then evict the real owner, taking over the organization and every
    project and subscription it holds. Confirmed exploitable before this guard
    existed.
    """
    if actor_role == "owner":
        return
    if granted_role == "owner":
        raise IdentityError("only an owner may grant the owner role")
    if target_role == "owner":
        raise IdentityError("only an owner may modify or remove another owner")


def change_role(
    conn: psycopg.Connection,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    actor: Principal,
) -> None:
    if role not in ROLES:
        raise IdentityError(f"unknown role {role!r}")

    # No self-elevation, and no accidental self-demotion either. Changing your
    # own standing goes through explicit ownership transfer, never in place.
    if actor.user.id == user_id:
        raise IdentityError("cannot change your own role")

    actor_role = actor.role_in(org_id)
    if actor_role is None:
        raise IdentityError("not a member of that organization")

    with conn.transaction():
        current = db.one(
            conn,
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        if current is None:
            raise IdentityError("not a member of that organization")

        guard_owner_tier(actor_role=actor_role, target_role=current["role"], granted_role=role)

        # An organization must always retain at least one owner.
        if current["role"] == "owner" and role != "owner" and count_owners(conn, org_id) == 1:
            raise IdentityError("cannot demote the last owner; transfer ownership first")
        db.execute(
            conn,
            "UPDATE org_members SET role = %s WHERE org_id = %s AND user_id = %s",
            (role, org_id, user_id),
        )


def remove_member(conn: psycopg.Connection, *, org_id: uuid.UUID, user_id: uuid.UUID, actor: Principal) -> None:
    actor_role = actor.role_in(org_id)
    if actor_role is None:
        raise IdentityError("not a member of that organization")

    with conn.transaction():
        current = db.one(
            conn,
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        )
        if current is None:
            raise IdentityError("not a member of that organization")

        # Leaving voluntarily is allowed; evicting an owner is not, unless you
        # are one yourself.
        if actor.user.id != user_id:
            guard_owner_tier(actor_role=actor_role, target_role=current["role"], granted_role=None)

        if current["role"] == "owner" and count_owners(conn, org_id) == 1:
            raise IdentityError("cannot remove the last owner; transfer ownership first")
        db.execute(conn, "DELETE FROM org_members WHERE org_id = %s AND user_id = %s", (org_id, user_id))


# -- sessions --------------------------------------------------------------


def normalise_ip(value: str | None) -> str | None:
    """Return value only if it parses as an IP address, else None.

    user_sessions.ip_address is INET, so an unparseable client host would abort
    the insert and make sign-in fail. A client address we cannot parse is worth
    dropping from the audit trail, never worth refusing a login over.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def create_session(
    conn: psycopg.Connection,
    *,
    user_id: uuid.UUID,
    pepper: bytes,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Return the session token. Stored hashed; returned exactly once."""
    token = hashing.generate_token("sess", pepper)
    ip_address = normalise_ip(ip_address)
    db.execute(
        conn,
        """
        INSERT INTO user_sessions (id, user_id, token_hash, ip_address, user_agent, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (uuid.uuid4(), user_id, token.verifier, ip_address, user_agent, _now() + SESSION_LIFETIME),
    )
    return token.plaintext


def revoke_session(conn: psycopg.Connection, *, token: str, pepper: bytes) -> bool:
    verifier = hashing.peppered(token, pepper)
    return db.execute(
        conn,
        "UPDATE user_sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
        (verifier,),
    ) > 0


def revoke_all_sessions(conn: psycopg.Connection, user_id: uuid.UUID) -> int:
    return db.execute(
        conn,
        "UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )


# -- personal access tokens ------------------------------------------------


def create_pat(
    conn: psycopg.Connection,
    *,
    user_id: uuid.UUID,
    name: str,
    pepper: bytes,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> str:
    token = hashing.generate_token("pat", pepper)
    db.execute(
        conn,
        """
        INSERT INTO personal_access_tokens
            (id, user_id, name, token_prefix, verification_data, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (uuid.uuid4(), user_id, name, token.prefix, token.verifier, scopes or [], expires_at),
    )
    return token.plaintext


def revoke_pat(conn: psycopg.Connection, *, token_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return db.execute(
        conn,
        "UPDATE personal_access_tokens SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
        (token_id, user_id),
    ) > 0


# -- resolving a presented credential -------------------------------------


def resolve_principal(conn: psycopg.Connection, *, presented: str, pepper: bytes) -> Principal | None:
    """Resolve a bearer credential to a principal, or None.

    Returns None for every failure mode -- unknown, malformed, revoked, expired
    -- so the caller cannot distinguish them and neither can an attacker.
    """
    parts = hashing.split_token(presented)
    if parts is None:
        return None
    kind, prefix = parts

    if kind == "sess":
        row = db.one(
            conn,
            """
            SELECT s.user_id, s.expires_at, s.revoked_at,
                   u.email, u.display_name, u.status
              FROM user_sessions s
              JOIN users u ON u.id = s.user_id
             WHERE s.token_hash = %s
            """,
            (hashing.peppered(presented, pepper),),
        )
        via = "session"
    elif kind == "pat":
        row = db.one(
            conn,
            """
            SELECT p.user_id, p.expires_at, p.revoked_at, p.verification_data,
                   u.email, u.display_name, u.status
              FROM personal_access_tokens p
              JOIN users u ON u.id = p.user_id
             WHERE p.token_prefix = %s
            """,
            (prefix,),
        )
        if row is not None and not hashing.verify_token(row["verification_data"], presented, pepper):
            return None
        via = "token"
    else:
        return None

    if row is None or row["revoked_at"] is not None or row["status"] != "active":
        return None
    if row["expires_at"] is not None and row["expires_at"] <= _now():
        return None

    if via == "token":
        db.execute(
            conn,
            "UPDATE personal_access_tokens SET last_used_at = now() WHERE token_prefix = %s",
            (prefix,),
        )
    else:
        db.execute(
            conn,
            "UPDATE user_sessions SET last_seen_at = now() WHERE token_hash = %s",
            (hashing.peppered(presented, pepper),),
        )

    user = User(
        id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        status=row["status"],
    )
    return Principal(user=user, memberships=memberships_for(conn, user.id), via=via)


# -- invitations -----------------------------------------------------------


def create_invitation(
    conn: psycopg.Connection,
    *,
    org_id: uuid.UUID,
    email: str,
    role: str,
    actor: Principal,
    pepper: bytes,
) -> str:
    if role not in ROLES:
        raise IdentityError(f"unknown role {role!r}")

    actor_role = actor.role_in(org_id)
    if actor_role is None:
        raise IdentityError("not a member of that organization")
    # Inviting at owner level is the same grant as promoting to owner, and must
    # be gated identically or it becomes the escalation path instead.
    guard_owner_tier(actor_role=actor_role, target_role=None, granted_role=role)
    invited_by = actor.user.id
    token = hashing.generate_token("inv", pepper)
    db.execute(
        conn,
        """
        INSERT INTO org_invitations (id, org_id, email, role, invited_by, token_hash, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid.uuid4(),
            org_id,
            email.strip().lower(),
            role,
            invited_by,
            token.verifier,
            _now() + INVITATION_LIFETIME,
        ),
    )
    return token.plaintext


def accept_invitation(conn: psycopg.Connection, *, token: str, user: User, pepper: bytes) -> uuid.UUID:
    """Single-use, expiring, and acceptable only by the invited address."""
    verifier = hashing.peppered(token, pepper)
    with conn.transaction():
        row = db.one(
            conn,
            """
            SELECT id, org_id, email, role, expires_at, accepted_at, revoked_at
              FROM org_invitations
             WHERE token_hash = %s
             FOR UPDATE
            """,
            (verifier,),
        )
        if row is None or row["accepted_at"] is not None or row["revoked_at"] is not None:
            raise IdentityError("invitation is not valid")
        if row["expires_at"] <= _now():
            raise IdentityError("invitation has expired")
        if row["email"] != user.email:
            raise IdentityError("invitation was issued to a different address")

        db.execute(
            conn,
            """
            INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s)
            ON CONFLICT (org_id, user_id) DO NOTHING
            """,
            (row["org_id"], user.id, row["role"]),
        )
        db.execute(conn, "UPDATE org_invitations SET accepted_at = now() WHERE id = %s", (row["id"],))
    return row["org_id"]
