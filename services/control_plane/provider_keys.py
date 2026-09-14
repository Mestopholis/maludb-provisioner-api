"""A project's own model provider API keys (ADR-079 decisions 4 and 5, memory slice 4).

The memory worker (slice 5) extracts and embeds with the customer's own keys, so
the model bill is the customer's. This module is the whole of how those keys are
kept, and it holds them to the standard the platform holds its own secrets to:

- **Sealed under the KEK** (ADR-023), with AAD binding each ciphertext to its
  project and provider: a row moved to another project, or relabelled as another
  provider, fails to open rather than decrypting as someone else's key.
- **Write-only.** Nothing here returns a key except `load_key`, which only the
  worker calls. The API answers with the provider, a four-character hint and
  timestamps.
- **Never logged, never audited in full.** Audit events name the provider and
  the hint.
- **Fixed providers** -- OpenAI, Anthropic, Voyage (decision 5). No endpoint is
  stored because none is accepted: a customer-supplied URL would let a key-holder
  aim the platform's worker at internal addresses.
- **One live key per provider.** Setting a key revokes the previous row; revoked
  rows are kept, as `project_credentials` keeps them.

Validation is shape only -- length and characters. Whether a key works is the
provider's answer, and asking it would be an outbound call from the public
application, which ADR-079 decision 6 reserves for the worker.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from services.control_plane import crypto, db

PROVIDERS = ("openai", "anthropic", "voyage")

AUDIT_SET = "maludb.memory.provider_key_set"
AUDIT_REMOVED = "maludb.memory.provider_key_removed"

# Real keys are 40 to a few hundred characters of URL-safe text; this refuses what
# is plainly not one -- pasted whitespace, a quoted string, a JSON blob -- without
# guessing any provider's current format.
_KEY_RE = re.compile(r"\A[A-Za-z0-9_\-.:]{20,512}\Z")


class ProviderKeyError(ValueError):
    """A key or provider the platform will not store. `status` is the HTTP answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class KeyInfo:
    provider: str
    hint: str
    created_at: datetime


def _aad(project_id: uuid.UUID, provider: str) -> bytes:
    return crypto.aad_for("project_provider_keys", "ciphertext", f"{project_id}:{provider}")


def checked_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ProviderKeyError(404, f"unknown provider; one of {', '.join(PROVIDERS)}")
    return provider


def set_key(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    provider: str,
    api_key: str,
    key_ring: crypto.KeyRing,
    actor_user_id: uuid.UUID | None,
) -> KeyInfo:
    """Seal and store a key, revoking the one it replaces. The caller commits."""
    checked_provider(provider)
    if not isinstance(api_key, str) or not _KEY_RE.match(api_key):
        # The key is never echoed, even partly, in a refusal.
        raise ProviderKeyError(422, "that does not look like an API key: 20 to 512 characters, no spaces or quotes")
    sealed = key_ring.seal(api_key.encode(), aad=_aad(project_id, provider))
    hint = api_key[-4:]
    db.execute(
        conn,
        "UPDATE project_provider_keys SET revoked_at = now() "
        " WHERE project_id = %s AND provider = %s AND revoked_at IS NULL",
        (project_id, provider),
    )
    row = db.one(
        conn,
        "INSERT INTO project_provider_keys (id, project_id, provider, ciphertext, nonce, key_version, "
        "                                   key_hint, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING provider, key_hint, created_at",
        (uuid.uuid4(), project_id, provider, sealed.ciphertext, sealed.nonce, sealed.key_version, hint,
         actor_user_id),
    )
    _audit(conn, project_id, actor_user_id, AUDIT_SET, provider, hint)
    return KeyInfo(provider=row["provider"], hint=row["key_hint"], created_at=row["created_at"])


def remove_key(conn: psycopg.Connection, *, project_id: uuid.UUID, provider: str,
               actor_user_id: uuid.UUID | None) -> bool:
    """Revoke a project's live key for `provider`. False when there was none. The caller commits."""
    checked_provider(provider)
    row = db.one(
        conn,
        "UPDATE project_provider_keys SET revoked_at = now() "
        " WHERE project_id = %s AND provider = %s AND revoked_at IS NULL RETURNING key_hint",
        (project_id, provider),
    )
    if row is None:
        return False
    _audit(conn, project_id, actor_user_id, AUDIT_REMOVED, provider, row["key_hint"])
    return True


def list_keys(conn: psycopg.Connection, *, project_id: uuid.UUID) -> list[KeyInfo]:
    """Which providers have a live key, and a hint of each. Never the key."""
    return [
        KeyInfo(provider=r["provider"], hint=r["key_hint"], created_at=r["created_at"])
        for r in db.query(
            conn,
            "SELECT provider, key_hint, created_at FROM project_provider_keys "
            " WHERE project_id = %s AND revoked_at IS NULL ORDER BY provider",
            (project_id,),
        )
    ]


def load_key(conn: psycopg.Connection, *, project_id: uuid.UUID, provider: str,
             key_ring: crypto.KeyRing) -> str | None:
    """The live key, for the memory worker only. A live secret: never log or return it."""
    checked_provider(provider)
    row = db.one(
        conn,
        "SELECT ciphertext, nonce, key_version FROM project_provider_keys "
        " WHERE project_id = %s AND provider = %s AND revoked_at IS NULL",
        (project_id, provider),
    )
    if row is None:
        return None
    sealed = crypto.SealedValue(ciphertext=bytes(row["ciphertext"]), nonce=bytes(row["nonce"]),
                                key_version=row["key_version"])
    return key_ring.open(sealed, aad=_aad(project_id, provider)).decode()


def _audit(conn, project_id: uuid.UUID, actor_user_id, event_type: str, provider: str, hint: str) -> None:
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, %s, %s, %s, %s)",
        (project_id, "user" if actor_user_id else "system", actor_user_id, event_type,
         Jsonb({"provider": provider, "hint": hint})),
    )


__all__ = [
    "AUDIT_REMOVED",
    "AUDIT_SET",
    "PROVIDERS",
    "KeyInfo",
    "ProviderKeyError",
    "checked_provider",
    "list_keys",
    "load_key",
    "remove_key",
    "set_key",
]
