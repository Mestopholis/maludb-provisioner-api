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
