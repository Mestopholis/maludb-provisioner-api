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

import hmac
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from services.control_plane import api_keys, hashing

log = logging.getLogger(__name__)

# Named by the control plane, which is the producer. Imported rather than
# redeclared so the two cannot drift into listening on different channels --
# a failure that would look exactly like the cache working.
REVOCATION_CHANNEL = api_keys.REVOCATION_CHANNEL

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_NEGATIVE_TTL_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0

# Ceilings on what one gateway process will hold. A node's live keys number in
# the thousands at most; wrong keys are unlimited and free to make, so they get
# a tighter cap of their own. At roughly 400 bytes an entry this is a few
# megabytes, against a gateway that has to share a node with its tenants.
MAX_ENTRIES = 20_000
MAX_NEGATIVE_ENTRIES = 5_000
# How long an invalidation marker outlives the entry it removed. Only lookups
# still in flight can be affected, and the TTL bounds the rest.
MARKER_GRACE_SECONDS = 60.0
# How long a listener session must last to count as healthy rather than as one
# more flap, for the purpose of resetting the reconnect delay.
STABLE_SESSION_SECONDS = 30.0


@dataclass(frozen=True)
class CacheEntry:
    identity: api_keys.KeyIdentity | None
    expires_at: float
    # The peppered HMAC of the key this answer was computed for -- the same
    # value ADR-023 stores as the verifier, so holding it here holds nothing a
    # database read would not. `repr=False` because an entry reaching a log
    # line through an exception or a debug print would otherwise carry it, and
    # no redaction pattern matches a bare hex string.
    digest: str = field(repr=False)


class KeyCache:
    """Thread-safe, bounded-staleness cache of key authentications.

    Entries are keyed by (project_id, key identifier) rather than by the key
    itself. The plaintext is a live credential and there is no reason to hold
    it as a dictionary key for the lifetime of the process; the identifier is
    the public prefix and is enough to invalidate on.

    **The identifier locates an entry; it never authenticates one.** It is the
    first eight characters of the key, returned by the key listing and shown in
    the dashboard. Until 2026-09-15 a hit returned the stored identity without
    looking at the rest of what was presented, so once a project's secret key
    had been used, its prefix followed by anything was `service_role` for the
    next thirty seconds -- and a project in use keeps the entry warm. Every hit
    now compares the presented key's digest with the entry's.

    Successes and failures are held separately and each is capped, because they
    are abused differently. A success is worth keeping and there are only as
    many as the node has live keys. A failure is cheap to manufacture: every
    distinct wrong key is now its own miss (it must be, or the check above is
    not a check), so without a cap of its own a stream of junk would fill this
    process's memory and evict every real key on the way.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        negative_ttl_seconds: float = DEFAULT_NEGATIVE_TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
        max_negative_entries: int = MAX_NEGATIVE_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._max = max_entries
        self._max_negative = max_negative_entries
        # Insertion-ordered, and re-inserted on use, so discarding the oldest
        # item discards the least recently used one.
        self._entries: OrderedDict[tuple[uuid.UUID, str], CacheEntry] = OrderedDict()
        self._negative: OrderedDict[tuple[uuid.UUID, str], CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        # When each key was last invalidated, and when each project was last
        # cleared. A lookup that started before an invalidation must not cache
        # what it read: the database answered before the revocation committed,
        # and the announcement was applied to an entry that did not exist yet.
        #
        # Per key rather than one counter for the process: a single counter
        # made every revocation anywhere on the node suppress caching for every
        # lookup in flight, which a customer could drive by revoking in a loop.
        self._invalidated_at: dict[tuple[uuid.UUID, str], float] = {}
        self._project_cleared_at: dict[uuid.UUID, float] = {}
        self._cleared_at = float("-inf")

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
        digest = hashing.peppered(presented, pepper)

        started = time.monotonic()
        with self._lock:
            for store in (self._entries, self._negative):
                entry = store.get(cache_key)
                if (
                    entry is not None
                    and entry.expires_at > started
                    and hmac.compare_digest(entry.digest, digest)
                ):
                    store.move_to_end(cache_key)
                    return entry.identity

        identity = api_keys.authenticate(
            conn, presented=presented, project_id=project_id, pepper=pepper
        )

        # A cached success is only safe because the entry is keyed by the
        # identifier *and* the project, and because authenticate() already
        # refused a mismatch. Caching by identifier alone would let a hit for
        # project A answer a request for project B.
        now = time.monotonic()
        with self._lock:
            if self._invalidated(cache_key, project_id, since=started):
                return identity
            if identity is None:
                live = self._entries.get(cache_key)
                # A wrong key sharing a real key's prefix must not displace the
                # real key's success: that would turn a known prefix into a way
                # to push the project's traffic onto the database, and before
                # digests were compared it locked the real key out outright.
                if live is not None and live.expires_at > now:
                    return identity
                self._store(
                    self._negative,
                    cache_key,
                    CacheEntry(identity=None, expires_at=now + self._negative_ttl, digest=digest),
                    self._max_negative,
                )
            else:
                self._negative.pop(cache_key, None)
                self._store(
                    self._entries,
                    cache_key,
                    CacheEntry(identity=identity, expires_at=now + self._ttl, digest=digest),
                    self._max,
                )
        return identity

    @staticmethod
    def _store(store, cache_key, entry: CacheEntry, maximum: int) -> None:
        """Insert, discarding the least recently used entry at the cap."""
        store[cache_key] = entry
        store.move_to_end(cache_key)
        while len(store) > maximum:
            store.popitem(last=False)

    def _invalidated(self, cache_key, project_id: uuid.UUID, *, since: float) -> bool:
        """Whether anything invalidated this key while the lookup was running."""
        return (
            self._invalidated_at.get(cache_key, float("-inf")) >= since
            or self._project_cleared_at.get(project_id, float("-inf")) >= since
            or self._cleared_at >= since
        )

    # -- invalidation ------------------------------------------------------

    def invalidate(self, *, project_id: uuid.UUID, identifier: str) -> None:
        cache_key = (project_id, identifier)
        with self._lock:
            now = time.monotonic()
            self._invalidated_at[cache_key] = now
            self._entries.pop(cache_key, None)
            self._negative.pop(cache_key, None)
            # These markers only matter for as long as a lookup can still be in
            # flight; anything older is bounded by the TTL anyway.
            if len(self._invalidated_at) > self._max:
                cutoff = now - (self._ttl + MARKER_GRACE_SECONDS)
                self._invalidated_at = {
                    key: at for key, at in self._invalidated_at.items() if at > cutoff
                }

    def invalidate_project(self, project_id: uuid.UUID) -> None:
        """Used when a project stops serving, not only when one key dies."""
        with self._lock:
            self._project_cleared_at[project_id] = time.monotonic()
            for store in (self._entries, self._negative):
                for key in [k for k in store if k[0] == project_id]:
                    del store[key]

    def clear(self) -> None:
        with self._lock:
            self._cleared_at = time.monotonic()
            self._entries.clear()
            self._negative.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries) + len(self._negative)

    @property
    def ttl(self) -> float:
        """How long a success can outlive a revocation nobody told us about."""
        return self._ttl


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


class RevocationListener:
    """Applies the control plane's revocation announcements to a cache.

    This is the consumer the module docstring describes, and until 2026-09-15
    it did not exist: the control plane announced every revocation, the suite
    applied them by calling `apply_revocation` itself, and a deployed gateway
    heard nothing, so a revoked key worked until its entry expired.

    One thread and one autocommit connection, separate from the pool, because a
    `LISTEN` belongs to a session and a pooled connection is handed to whoever
    asks next. A lost connection is retried for as long as the process runs.
    **The cache is cleared every time the listen starts** -- including the
    first -- because an announcement made while nothing was listening is not
    queued for anyone, and the entries it would have removed are still there.
    `LISTEN` is issued before the clear, so a revocation committed between the
    two is either already reflected in what was cleared or delivered afterwards.
    """

    def __init__(
        self,
        cache: KeyCache,
        dsn: str,
        *,
        poll_seconds: float = 1.0,
        retry_seconds: float = 5.0,
        connect_timeout: int = 5,
    ) -> None:
        self.cache = cache
        self._dsn = dsn
        self._poll = poll_seconds
        self._retry = retry_seconds
        # Without it a black-holed control-plane address parks this thread in
        # connect() for the kernel's timeout, which `stop()` then cannot join.
        self._connect_timeout = connect_timeout
        self._stop = threading.Event()
        self._listening = threading.Event()
        self._thread: threading.Thread | None = None
        self.backend_pid: int | None = None
        # How many times this listener has established a session. Tests wait on
        # it; a reconnect is not observable through `backend_pid`, which
        # PostgreSQL may reuse.
        self.sessions = 0

    @property
    def listening(self) -> bool:
        return self._listening.is_set()

    def wait_listening(self, timeout: float) -> bool:
        return self._listening.wait(timeout)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="key-revocation-listener", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Blocking; call it off the event loop.

        The thread handle is kept if the join times out, so `start()` refuses
        rather than running a second listener against the same cache -- two of
        those clear it on every reconnect between them.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("the key revocation listener did not stop within %ss", timeout)
            else:
                self._thread = None

    def _run(self) -> None:
        backoff = self._retry
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                with psycopg.connect(
                    self._dsn, autocommit=True, connect_timeout=self._connect_timeout
                ) as conn:
                    conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(REVOCATION_CHANNEL)))
                    # Always, even after a short outage. An entry cached a
                    # second before the connection dropped still has most of
                    # its TTL to run, and a revocation during the gap was
                    # announced to nobody, so expiry does not cover it.
                    self.cache.clear()
                    self.backend_pid = conn.info.backend_pid
                    self.sessions += 1
                    self._listening.set()
                    log.info("listening for key revocations")
                    while not self._stop.is_set():
                        for notify in conn.notifies(timeout=self._poll):
                            if not apply_revocation(self.cache, notify.payload):
                                log.warning("ignored a malformed key revocation announcement")
            except Exception:  # noqa: BLE001 - this thread must outlive any failure
                if self._stop.is_set():
                    break
                lasted = time.monotonic() - began
                # ERROR once the gap exceeds the TTL, because past that point
                # revocation is no longer merely slower than it should be: keys
                # this gateway was told to forget are being accepted.
                log.log(
                    logging.ERROR if lasted + backoff > self.cache.ttl else logging.WARNING,
                    "key revocation listener lost its connection after %.1fs; revoked keys stay "
                    "usable for up to the cache TTL until it reconnects",
                    lasted,
                    exc_info=True,
                )
                # A session that survived a while starts again from the short
                # delay; one that dropped immediately does not, or a database
                # that accepts a connection and kills it keeps this thread
                # clearing the whole cache every few seconds -- sending every
                # request back to a database that is already unwell.
                backoff = self._retry if lasted > STABLE_SESSION_SECONDS else backoff
            finally:
                self._listening.clear()
                self.backend_pid = None
            # Backed off, because a listener that cannot connect at all -- the
            # role or the channel misconfigured rather than a dropped session --
            # would otherwise write this line every few seconds for the life of
            # the process, and bury the one that says what broke.
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_RETRY_SECONDS)
