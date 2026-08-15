"""Project-scoped API keys (ADR-008, ADR-023, ADR-028).

Two key types, stored differently because ADR-023 classifies by recoverability
rather than by sensitivity:

- **secret** — server-side only, never read back. Class A: an HMAC verifier and
  nothing else. A database leak yields no working key.
- **publishable** — public by design, embedded in the customer's client bundle,
  and a dashboard must be able to display it again indefinitely. That makes it
  recoverable by requirement, so Class B: envelope encrypted as well as
  verifiable. Encrypting a value that is already public looks redundant; the
  point is that the platform must be able to hand it back, and ADR-023 says a
  value with that requirement is never stored hashed-only.

The one property this module exists to guarantee is ADR-008's: a key is only
ever accepted for the project it belongs to. `authenticate` therefore takes the
expected project as a required argument and compares internally. It deliberately
does not offer a "look up this key and tell me its project" call for a caller to
compare afterwards -- that shape is what makes a forgotten comparison possible,
and a forgotten comparison here is a cross-tenant read available to anyone on
the internet holding any valid key.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

import psycopg

from services.control_plane import crypto, db, hashing

log = logging.getLogger(__name__)

PUBLISHABLE = "publishable"
SECRET = "secret"  # noqa: S105 - a key-type name, not a credential
KEY_TYPES = (PUBLISHABLE, SECRET)

# How stale last_used_at may be. At gateway volume, writing it on every request
# turns every read into a write on a hot row; the value only informs "is this
# key still in use", which does not need minute accuracy.
LAST_USED_RESOLUTION = timedelta(minutes=5)

# Gateways listen here so a revocation takes effect promptly rather than after
# a cache expiry. docs/API-GATEWAY.md requires revocation to invalidate cached
# material quickly; a TTL alone turns "revoke this key" into "revoke this key,
# eventually", and the gap is the window in which a leaked key still works.
REVOCATION_CHANNEL = "maludb_key_revoked"


class ApiKeyError(RuntimeError):
    """An API key could not be created or resolved."""


@dataclass(frozen=True)
class IssuedKey:
    """A freshly minted key. `plaintext` for a secret key is shown once."""

    id: uuid.UUID
    key_type: str
    key_identifier: str
    plaintext: str


@dataclass(frozen=True)
class KeyIdentity:
    """The result of a successful authentication. Carries no secret."""

    key_id: uuid.UUID
    project_id: uuid.UUID
    key_type: str

    @property
    def is_secret(self) -> bool:
        return self.key_type == SECRET


def _aad(project_id: uuid.UUID, key_id: uuid.UUID) -> bytes:
    # Binds the ciphertext to this row. A publishable key copied into another
    # project's row fails to decrypt rather than silently authorising there.
    return crypto.aad_for("api_keys", "ciphertext", f"{project_id}:{key_id}")


def create(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_type: str,
    pepper: bytes,
    key_ring: crypto.KeyRing | None = None,
    name: str | None = None,
) -> IssuedKey:
    """Mint a key for a project.

    A publishable key needs the key ring, because it is stored recoverably. A
    secret key must not be given one -- there is nothing to encrypt, and the
    schema refuses a secret row carrying ciphertext.
    """
    if key_type not in KEY_TYPES:
        raise ApiKeyError(f"unknown key type {key_type!r}")
    if key_type == PUBLISHABLE and key_ring is None:
        raise ApiKeyError("a publishable key is stored recoverably and needs the key ring")

    key_id = uuid.uuid4()
    token = hashing.generate_token(key_type, pepper)

    ciphertext = nonce = key_version = None
    if key_type == PUBLISHABLE:
        sealed = key_ring.seal(token.plaintext.encode(), aad=_aad(project_id, key_id))
        ciphertext, nonce, key_version = sealed.ciphertext, sealed.nonce, sealed.key_version

    db.execute(
        conn,
        """
        INSERT INTO api_keys
            (id, project_id, key_type, key_identifier, verification_data, name,
             ciphertext, nonce, key_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (key_id, project_id, key_type, token.prefix, token.verifier, name,
         ciphertext, nonce, key_version),
    )
    return IssuedKey(id=key_id, key_type=key_type, key_identifier=token.prefix,
                     plaintext=token.plaintext)


def reveal_publishable(
    conn: psycopg.Connection,
    *,
    key_id: uuid.UUID,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
) -> str:
    """Recover a publishable key for display.

    Scoped by project as well as id: the AAD would reject a mismatch anyway, but
    failing the lookup is a clearer answer than a decryption error, and it keeps
    the project scoping true of every read in this module rather than true by
    accident of how the ciphertext was bound.

    There is no equivalent for secret keys. That is the point of the class.
    """
    row = db.one(
        conn,
        """
        SELECT ciphertext, nonce, key_version FROM api_keys
         WHERE id = %s AND project_id = %s AND key_type = %s AND revoked_at IS NULL
        """,
        (key_id, project_id, PUBLISHABLE),
    )
    if row is None:
        raise ApiKeyError("no live publishable key with that id for this project")
    return key_ring.open(
        crypto.SealedValue(bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_version"]),
        aad=_aad(project_id, key_id),
    ).decode()


def list_for_project(conn: psycopg.Connection, *, project_id: uuid.UUID) -> list[dict]:
    """Key metadata. Never plaintext, never the verifier."""
    return db.query(
        conn,
        """
        SELECT id, key_type, key_identifier, name, created_at, last_used_at, revoked_at
          FROM api_keys WHERE project_id = %s ORDER BY created_at
        """,
        (project_id,),
    )


def revoke(conn: psycopg.Connection, *, key_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    """Revoke a key. Scoped by project so one project cannot revoke another's.

    Announces the revocation on REVOCATION_CHANNEL in the same transaction, so
    a rolled back revoke never tells a gateway to forget a key that is still
    live, and a committed one always does.
    """
    row = db.one(
        conn,
        "UPDATE api_keys SET revoked_at = now() "
        " WHERE id = %s AND project_id = %s AND revoked_at IS NULL "
        "RETURNING key_identifier",
        (key_id, project_id),
    )
    if row is None:
        return False
    db.execute(
        conn,
        "SELECT pg_notify(%s, %s)",
        (REVOCATION_CHANNEL, f"{project_id}:{row['key_identifier']}"),
    )
    return True


def authenticate(
    conn: psycopg.Connection,
    *,
    presented: str,
    project_id: uuid.UUID,
    pepper: bytes,
) -> KeyIdentity | None:
    """Resolve a presented key, but only for `project_id` (ADR-008).

    `project_id` is required rather than returned. The gateway learns the
    project from the hostname; if this function returned the key's own project
    and left the caller to compare, the comparison could be omitted and the
    omission would look like working code. A mismatch is indistinguishable from
    an unknown key from the outside, so it leaks nothing about which refs or
    keys exist.
    """
    split = hashing.split_token(presented)
    if split is None:
        return None
    kind, identifier = split
    if kind not in KEY_TYPES:
        return None

    row = db.one(
        conn,
        """
        SELECT k.id, k.project_id, k.key_type, k.verification_data, k.revoked_at,
               k.last_used_at, p.status AS project_status
          FROM api_keys k JOIN projects p ON p.id = k.project_id
         WHERE k.key_identifier = %s
        """,
        (identifier,),
    )

    # Verify before any branch on the row's contents, and on a dummy verifier
    # when the identifier is unknown, so a caller cannot distinguish "no such
    # key" from "wrong secret" by how long the answer took.
    stored = row["verification_data"] if row is not None else ""
    matches = hashing.verify_token(stored, presented, pepper)

    if row is None or not matches:
        return None
    if row["revoked_at"] is not None:
        return None
    if row["project_id"] != project_id:
        # The ADR-008 control. Logged because a key being presented against the
        # wrong project is either a misconfigured client or someone probing.
        log.warning(
            "api key for project %s presented against project %s", row["project_id"], project_id
        )
        return None
    if row["project_status"] not in ("PROVISIONED", "ACTIVE"):
        return None

    _touch(conn, key_id=row["id"])
    return KeyIdentity(key_id=row["id"], project_id=row["project_id"], key_type=row["key_type"])


def _touch(conn: psycopg.Connection, *, key_id: uuid.UUID) -> None:
    """Record use, at most once per LAST_USED_RESOLUTION.

    Rate-limited deliberately: an unconditional UPDATE makes every read request
    a write to the same row, which serialises a project's concurrent requests
    behind one row lock and produces WAL proportional to traffic. The value only
    answers "is this key still in use", which does not need minute accuracy.

    The staleness test is in the statement rather than in Python so it uses the
    database clock and costs no extra round trip, and so two concurrent requests
    cannot both decide they are the one that should write.
    """
    db.execute(
        conn,
        """
        UPDATE api_keys SET last_used_at = now()
         WHERE id = %s AND (last_used_at IS NULL OR last_used_at < now() - %s)
        """,
        (key_id, LAST_USED_RESOLUTION),
    )
