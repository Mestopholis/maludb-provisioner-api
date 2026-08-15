"""Class A (verifier) secret hashing.

ADR-023: the algorithm differs by **entropy, not importance**.

- User passwords are human-chosen and dictionary-attackable, so they use
  Argon2id, where memory-hard cost is the entire defence.
- Session tokens, personal access tokens and invitation tokens are 256-bit
  CSPRNG values. There is no feasible search space to slow down, and project
  API keys will be verified on every gateway request, where a memory-hard
  function would be a self-inflicted denial of service. Those use HMAC-SHA-256
  with a server-side pepper.

The pepper lives outside the database, so a database-only compromise yields no
offline-verifiable hashes.

Every token carries a non-secret indexed prefix. A hashed value cannot be
searched for, so the prefix is what turns verification into a single-row fetch
followed by a constant-time comparison.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from hashlib import sha256

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Argon2id defaults from argon2-cffi. Tuned for interactive login, which
# happens rarely, unlike token verification.
_password_hasher = PasswordHasher()

TOKEN_ENTROPY_BYTES = 32
PREFIX_BYTES = 8


@dataclass(frozen=True)
class GeneratedToken:
    """A freshly minted credential.

    `plaintext` is returned to the caller exactly once and never stored.
    `prefix` is stored in the clear and indexed. `verifier` is stored instead
    of the secret.
    """

    plaintext: str
    prefix: str
    verifier: str


# -- passwords -------------------------------------------------------------


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# -- high-entropy tokens ---------------------------------------------------


def peppered(token: str, pepper: bytes) -> str:
    """The stored verifier for a token. Public: lookups need it directly."""
    return hmac.new(pepper, token.encode(), sha256).hexdigest()


def generate_token(kind: str, pepper: bytes) -> GeneratedToken:
    """Mint a prefixed, high-entropy credential.

    Format: mldb_<kind>_<prefix><secret>. The prefix is stored and indexed; the
    whole string is presented on each request and verified against the stored
    HMAC.
    """
    prefix = secrets.token_hex(PREFIX_BYTES // 2)
    secret = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    plaintext = f"mldb_{kind}_{prefix}{secret}"
    return GeneratedToken(plaintext=plaintext, prefix=prefix, verifier=peppered(plaintext, pepper))


def split_token(plaintext: str) -> tuple[str, str] | None:
    """Extract (kind, prefix) from a presented token, or None if malformed."""
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != "mldb":
        return None
    kind, remainder = parts[1], parts[2]
    if len(remainder) <= PREFIX_BYTES:
        return None
    return kind, remainder[:PREFIX_BYTES]


def verify_token(stored_verifier: str, plaintext: str, pepper: bytes) -> bool:
    """Constant-time comparison, so verification leaks no timing signal."""
    return hmac.compare_digest(stored_verifier, peppered(plaintext, pepper))
