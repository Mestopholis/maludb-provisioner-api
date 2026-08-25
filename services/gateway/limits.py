"""Per-project rate and concurrency limits (ADR-009's first layer, ADR-030).

Two different controls, protecting two different things:

- **Rate** bounds how much work a project can ask for over time. A token bucket
  rather than a fixed window, because a fixed window lets a project spend its
  whole allowance in the last second of one window and again in the first second
  of the next -- twice the configured rate, at the worst possible moment.
- **Concurrency** bounds how much it can hold *at once*, and is the one that
  actually protects the database. PostgREST's pool is 3 connections on the free
  tier; a handful of slow queries pins all of them, and every later request for
  that project queues behind them. Rate limiting alone does not prevent that,
  because ten simultaneous slow queries is a low request rate.

State is per gateway process and deliberately so -- see ADR-030. **With more
than one gateway the effective limit is the configured one times the number of
gateways.** That is a real property of this implementation, not a rounding
error, and it is why `Limiter` is a protocol: swapping in a shared counter is a
class, not a rewrite.

A limiter is a denial-of-service surface pointed at your own customers. Two
consequences run through this module: defaults are generous rather than clever,
and every rejection says which limit it hit, because the failure mode of a wrong
limit is silence from an application that looks healthy.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

# How long an idle project's state is kept before it is swept. Long enough that
# a normally-active project never loses its bucket, short enough that a scan of
# every project ref that ever appeared does not accumulate forever.
IDLE_EVICTION_SECONDS = 900.0

# Sweeping walks every tracked project, so it is not done per request.
SWEEP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class Decision:
    """Whether a request may proceed, and if not, why and for how long."""

    allowed: bool
    limit: str = ""
    retry_after_seconds: int = 0

    @property
    def message(self) -> str:
        """What the client is told. Names the limit, deliberately.

        A client that cannot tell a rate limit from a concurrency limit cannot
        act on either: one is fixed by slowing down, the other by not holding
        so many requests open at once.
        """
        if self.limit == "rate":
            return "project request rate limit exceeded"
        if self.limit == "concurrency":
            return "too many concurrent requests for this project"
        if self.limit == "realtime_connections":
            return "too many open Realtime connections for this project"
        return "request refused"


ALLOWED = Decision(allowed=True)


class Limiter(Protocol):
    """Narrow on purpose, so a shared-state implementation can replace it."""

    def acquire(self, project_id: uuid.UUID, *, rate: int, window_seconds: int,
                concurrency: int) -> Decision: ...

    def release(self, project_id: uuid.UUID) -> None: ...


@dataclass
class _State:
    tokens: float
    updated: float
    in_flight: int = 0
    last_seen: float = field(default_factory=time.monotonic)


class LocalLimiter:
    """Rate and concurrency, counted in this process.

    Honest about its scope: it enforces a per-gateway limit. With one gateway
    that is the platform limit; with several it is not, and ADR-030 records that
    rather than leaving it to be discovered.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._state: dict[uuid.UUID, _State] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def acquire(
        self, project_id: uuid.UUID, *, rate: int, window_seconds: int, concurrency: int
    ) -> Decision:
        """Take one token and one concurrency slot, or refuse.

        Both are checked before either is taken. Taking a token and then failing
        the concurrency check would spend a project's rate allowance on requests
        that never ran.
        """
        if rate <= 0 or window_seconds <= 0:
            # A plan with no rate configured is not a plan with no limit -- but
            # the entitlement resolver already guarantees a positive default, so
            # reaching here means something is badly wrong. Fail open rather
            # than lock a project out on a configuration error: the layers below
            # (pool size, statement timeout, node capacity) still apply.
            return ALLOWED

        now = self._clock()
        per_second = rate / window_seconds

        with self._lock:
            self._maybe_sweep(now)
            state = self._state.get(project_id)
            if state is None:
                state = _State(tokens=float(rate), updated=now)
                self._state[project_id] = state

            # Refill for elapsed time, capped at the bucket size. The cap is
            # what stops an idle project accumulating a burst it can spend all
            # at once weeks later.
            elapsed = max(0.0, now - state.updated)
            state.tokens = min(float(rate), state.tokens + elapsed * per_second)
            state.updated = now
            state.last_seen = now

            if concurrency > 0 and state.in_flight >= concurrency:
                # No Retry-After: how long depends on when an in-flight request
                # finishes, which is not knowable here. A number would be a
                # guess presented as fact.
                return Decision(allowed=False, limit="concurrency")

            if state.tokens < 1.0:
                deficit = 1.0 - state.tokens
                return Decision(
                    allowed=False,
                    limit="rate",
                    # Rounded up, and never zero: a Retry-After of 0 invites an
                    # immediate retry that is certain to fail again.
                    retry_after_seconds=max(1, int(deficit / per_second) + 1),
                )

            state.tokens -= 1.0
            state.in_flight += 1
            return ALLOWED

    def release(self, project_id: uuid.UUID) -> None:
        """Give back a concurrency slot. Must run even when the request failed.

        A leaked slot is permanent: it never expires, so a project that leaked
        `concurrency` of them can never serve another request. That is why the
        caller releases in a `finally` rather than after a successful proxy.
        """
        with self._lock:
            state = self._state.get(project_id)
            if state is not None and state.in_flight > 0:
                state.in_flight -= 1

    def _maybe_sweep(self, now: float) -> None:
        """Drop state for projects nothing has asked about in a while.

        Called with the lock held. Without this, one entry accumulates per
        project ref ever seen -- which an attacker enumerating hostnames could
        drive, though they would have to authenticate first.
        """
        if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        stale = [
            key for key, state in self._state.items()
            if state.in_flight == 0 and now - state.last_seen > IDLE_EVICTION_SECONDS
        ]
        for key in stale:
            del self._state[key]

    # -- introspection, for tests and operators ----------------------------

    def in_flight(self, project_id: uuid.UUID) -> int:
        with self._lock:
            state = self._state.get(project_id)
            return state.in_flight if state else 0

    def tracked_projects(self) -> int:
        with self._lock:
            return len(self._state)


class NoLimiter:
    """Enforces nothing. For tests that are about something else."""

    def acquire(self, project_id: uuid.UUID, **kwargs) -> Decision:
        return ALLOWED

    def release(self, project_id: uuid.UUID) -> None:
        return None


class SocketLimiter:
    """How many Realtime sockets a project may hold open at once.

    Separate from `LocalLimiter` rather than another dimension of it, because a
    socket is not a request and the token bucket would be wrong for it in both
    directions. A connection held open for an hour spends one token and then
    costs nothing, which under-counts what it holds; a client that reconnects on
    every network blip spends tokens at a rate that has nothing to do with load.
    What matters for a socket is the count, and only the count.

    The limit is `realtime_connections` from the plan -- the same number that
    decides whether a project may have Realtime at all, since it is `0` on free.

    Shares ADR-030's honesty: this is a per-gateway count. With N gateways a
    project can hold N times its limit, and the fix is the same shared store the
    request limiter is waiting for.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._open: dict[uuid.UUID, int] = {}
        self._lock = threading.Lock()

    def acquire(self, project_id: uuid.UUID, *, limit: int) -> Decision:
        """Take a connection slot, or refuse.

        A limit of zero refuses, which is the opposite of how the request
        limiter treats a missing rate -- and deliberately. Zero here is not a
        misconfiguration to fail open on: it is the free tier, and it is the
        number that says this project does not have Realtime.
        """
        if limit <= 0:
            return Decision(allowed=False, limit="realtime_connections")
        with self._lock:
            held = self._open.get(project_id, 0)
            if held >= limit:
                return Decision(allowed=False, limit="realtime_connections")
            self._open[project_id] = held + 1
        return ALLOWED

    def release(self, project_id: uuid.UUID) -> None:
        """Give a slot back. Must run on every close, including a failed proxy.

        A leaked socket slot is worse than a leaked request slot: requests end on
        their own, so a leak there is eventually visible as a project that
        stalls. A socket slot leaked while the socket is gone is invisible until
        the project cannot open another one.
        """
        with self._lock:
            held = self._open.get(project_id, 0)
            if held <= 1:
                self._open.pop(project_id, None)
            else:
                self._open[project_id] = held - 1

    def open_sockets(self, project_id: uuid.UUID) -> int:
        with self._lock:
            return self._open.get(project_id, 0)


# --------------------------------------------------------------------------
# Egress (ADR-056), which is counted rather than rated
# --------------------------------------------------------------------------
#
# The third control in this module and the only one that writes anything down.
# A rate limit forgets: the bucket refills and nothing outside the process ever
# knew. A monthly egress ceiling is a running total that has to survive a
# gateway restart, so it lives in `project_egress` and this class is the part
# that keeps the database off the hot path.
#
# Two costs are avoided, and they are different costs. **Reading** the total per
# request would be a round trip to answer a question whose answer changes
# slowly, so it is cached per project with a short TTL. **Writing** per response
# would be an INSERT ... ON CONFLICT on the path ADR-026 published a +6.3 ms
# figure for, so bytes accumulate in memory and are flushed in batches --
# which is precisely why `object_storage.record_egress` takes a total rather
# than one response.
#
# What that buys is bounded and worth stating plainly: a project can serve up to
# one flush interval's worth of bytes past its ceiling, and a gateway killed
# between flushes loses at most that. Both are bounded by the interval, both are
# in the customer's favour, and neither is true of the alternative anyone
# reaches for first -- a write per response, which is correct and too slow.

# How long bytes may sit in memory before they reach the database.
EGRESS_FLUSH_SECONDS = 5.0

# How long a project's recorded total may be reused before it is re-read.
# Longer than the flush interval on purpose: this process's own writes are
# already reflected locally, so a re-read exists to notice *another* gateway's
# writes and a new month, neither of which is urgent.
EGRESS_REFRESH_SECONDS = 30.0


@dataclass
class _EgressState:
    """One project's counter: what the database holds, and what has not reached it."""

    recorded: int
    period: object
    read_at: float
    pending: int = 0

    @property
    def total(self) -> int:
        return self.recorded + self.pending


class EgressMeter:
    """Counts bytes served per project against a monthly ceiling.

    Per gateway process, with ADR-030's caveat and one addition. The rate
    limiters multiply with the number of gateways; this one does not, because
    the total is in the database and every gateway adds to the same row. What
    does multiply is the overshoot: each gateway may be up to one flush interval
    ahead of what it has written down.
    """

    def __init__(
        self,
        *,
        flush_seconds: float = EGRESS_FLUSH_SECONDS,
        refresh_seconds: float = EGRESS_REFRESH_SECONDS,
    ) -> None:
        self._flush_seconds = flush_seconds
        self._refresh_seconds = refresh_seconds
        self._state: dict[uuid.UUID, _EgressState] = {}
        self._flush_due = time.monotonic() + flush_seconds
        self._lock = threading.Lock()

    def used(self, conn, *, project_id: uuid.UUID) -> int:
        """This project's bytes for the current month, including unflushed ones.

        Reads through to the database when the cached figure is stale or the
        month has turned. The period is compared rather than assumed: a gateway
        that has been up since last month must not judge this month's request
        against last month's total.
        """
        from services.control_plane import object_storage

        period = object_storage.period_start()
        now = time.monotonic()
        with self._lock:
            state = self._state.get(project_id)
            if (
                state is not None
                and state.period == period
                and state.read_at + self._refresh_seconds > now
            ):
                return state.total

        recorded = object_storage.egress_used(conn, project_id=project_id)
        with self._lock:
            state = self._state.get(project_id)
            if state is None or state.period != period:
                # A new month starts from what the database says and drops any
                # pending bytes belonging to the old one -- they were flushed
                # into their own period row, and carrying them forward would
                # charge them twice.
                state = _EgressState(recorded=recorded, period=period, read_at=now)
            else:
                state.recorded = recorded
                state.read_at = now
            self._state[project_id] = state
            return state.total

    def add(self, project_id: uuid.UUID, bytes_served: int) -> None:
        """Count bytes that have been served. Never negative, never blocking."""
        if bytes_served <= 0:
            return
        from services.control_plane import object_storage

        period = object_storage.period_start()
        with self._lock:
            state = self._state.get(project_id)
            if state is None or state.period != period:
                # Unknown, or a month that turned between the check and the
                # response. `recorded` is left at zero and read_at in the past,
                # so the next `used` reads through rather than trusting this.
                state = _EgressState(recorded=0, period=period, read_at=0.0)
                self._state[project_id] = state
            state.pending += int(bytes_served)

    def flush_due(self) -> bool:
        return time.monotonic() >= self._flush_due

    def flush(self, conn) -> int:
        """Write accumulated bytes to the database. Returns how many were written.

        Pending counts are taken out of the map *before* the write, so a
        concurrent `add` accumulates into the next batch rather than being lost
        to this one. If the write fails the bytes are put back, because the
        alternative is a project that served them and was never charged.
        """
        from services.control_plane import object_storage

        with self._lock:
            self._flush_due = time.monotonic() + self._flush_seconds
            batch = {
                project_id: state.pending
                for project_id, state in self._state.items()
                if state.pending > 0
            }
            for project_id in batch:
                self._state[project_id].pending = 0
        if not batch:
            return 0

        written = 0
        for project_id, pending in batch.items():
            try:
                recorded = object_storage.record_egress(
                    conn, project_id=project_id, bytes_served=pending
                )
            except Exception:  # noqa: BLE001 - a failed flush must not lose bytes
                with self._lock:
                    state = self._state.get(project_id)
                    if state is not None:
                        state.pending += pending
                raise
            written += pending
            with self._lock:
                state = self._state.get(project_id)
                if state is not None:
                    state.recorded = recorded
                    state.read_at = time.monotonic()
        return written

    def forget(self, project_id: uuid.UUID) -> None:
        """Drop a project's cached counter. Its pending bytes are not dropped."""
        with self._lock:
            state = self._state.get(project_id)
            if state is not None and state.pending == 0:
                self._state.pop(project_id, None)
            elif state is not None:
                state.read_at = 0.0
