"""Gateway-side API key resolution and caching.

`docs/API-GATEWAY.md` forbids querying the control-plane database on every API
request, and requires that "revocation/update paths must invalidate cached
material quickly". Those pull in opposite directions, and the resolution here
is deliberate:

- a **short TTL** bounds how stale any entry can be even if everything else
  fails, and
- a **LISTEN/NOTIFY channel** on the control-plane database makes the usual
  case prompt rather than merely bounded.

The TTL is the backstop, not the mechanism. A cache whose only invalidation is
expiry turns "revoke this key" into "revoke this key, eventually", and the
gap is exactly the window in which a leaked key keeps working.

Negative results are cached too, and this is load-bearing rather than an
optimisation: without it, an unknown key costs a database round trip, so
anyone can turn a stream of junk keys into control-plane load. They are cached
for a shorter time than successes, because a key that becomes valid should
start working promptly while a key that stays invalid should stay cheap.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass

import psycopg

from services.control_plane import api_keys, hashing

log = logging.getLogger(__name__)

# Named by the control plane, which is the producer. Imported rather than
# redeclared so the two cannot drift into listening on different channels --
# a failure that would look exactly like the cache working.
REVOCATION_CHANNEL = api_keys.REVOCATION_CHANNEL

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_NEGATIVE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class CacheEntry:
    identity: api_keys.KeyIdentity | None
    expires_at: float


class KeyCache:
    """Thread-safe, bounded-staleness cache of key authentications.

    Entries are keyed by (project_id, key identifier) rather than by the key
    itself. The plaintext is a live credential and there is no reason to hold
    it as a dictionary key for the lifetime of the process; the identifier is
    the public prefix and is enough to invalidate on.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        negative_ttl_seconds: float = DEFAULT_NEGATIVE_TTL_SECONDS,
    ) -> None:
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._entries: dict[tuple[uuid.UUID, str], CacheEntry] = {}
        self._lock = threading.Lock()

    # -- lookup ------------------------------------------------------------

    def resolve(
        self,
        conn: psycopg.Connection,
        *,
        presented: str,
        project_id: uuid.UUID,
        pepper: bytes,
    ) -> api_keys.KeyIdentity | None:
        """Authenticate a key for a project, using the cache when it can.

        The project is passed through to `api_keys.authenticate` rather than
        compared here. Re-implementing the ADR-008 check in the gateway would
        give the platform two copies of its most important comparison, and the
        one that drifts is the one nobody is testing.
        """
        split = hashing.split_token(presented)
        if split is None:
            return None
        identifier = split[1]
        cache_key = (project_id, identifier)

        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is not None and entry.expires_at > now:
                return entry.identity

        identity = api_keys.authenticate(
            conn, presented=presented, project_id=project_id, pepper=pepper
        )

        # A cached success is only safe because the entry is keyed by the
        # identifier *and* the project, and because authenticate() already
        # refused a mismatch. Caching by identifier alone would let a hit for
        # project A answer a request for project B.
        ttl = self._ttl if identity is not None else self._negative_ttl
        with self._lock:
            self._entries[cache_key] = CacheEntry(identity=identity, expires_at=now + ttl)
        return identity

    # -- invalidation ------------------------------------------------------

    def invalidate(self, *, project_id: uuid.UUID, identifier: str) -> None:
        with self._lock:
            self._entries.pop((project_id, identifier), None)

    def invalidate_project(self, project_id: uuid.UUID) -> None:
        """Used when a project stops serving, not only when one key dies."""
        with self._lock:
            for key in [k for k in self._entries if k[0] == project_id]:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)


def apply_revocation(cache: KeyCache, payload: str) -> bool:
    """Apply one announcement. Returns whether it was understood.

    Deliberately tolerant of a payload it cannot parse: a malformed
    announcement must not take the gateway down, and the TTL still bounds the
    staleness it would have prevented.
    """
    project, _, identifier = payload.partition(":")
    if not identifier:
        return False
    try:
        project_id = uuid.UUID(project)
    except ValueError:
        return False
    cache.invalidate(project_id=project_id, identifier=identifier)
    return True
