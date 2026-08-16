"""Rate limits for the control plane's own routes (Phase 07 slice 0).

Not the same thing as `services/gateway/limits.py`, and the difference is the
threat rather than the mechanism. The gateway bounds how much work a *project*
may ask of a node, keyed by project and driven by that project's plan. This
bounds how often an *anonymous caller* may try to create an account or guess a
password, keyed by where the call came from and driven by platform
configuration. A customer's plan has nothing to say about how many times
somebody may attempt to sign in as them.

It exists because signup is public at launch and the control plane has never
been throttled at all: nothing stood between the internet and `/v1/auth/signin`
except the cost of a bcrypt verify.

**Two keys, and they count different things.** Signin is limited per source
address *and* per account. Per-source alone does not stop a slow distributed
attempt against one account, which is what credential stuffing is; per-account
alone lets one host spray a thousand different accounts at one attempt each and
never trip a limit.

The source bucket counts **attempts** and is released when one succeeds. The
account bucket counts **failures** only, checked before the password is verified
and charged after it turns out to be wrong. Charging the account bucket per
attempt instead locks out the very person it protects: someone signing in from
several devices, or often enough for a short session lifetime, would exhaust
their own allowance by using the platform correctly.

**State is per process**, exactly as ADR-030 records for the gateway: with more
than one public application process the effective limit is the configured one
times the number of processes. That is a property of this implementation rather
than an oversight, and it is why the limits below are set where a single process
is still a meaningful obstacle.

A limiter on the signin path is also a denial-of-service surface pointed at your
own customers: too tight and an attacker locks a real user out by failing their
password on purpose. The account bucket is therefore generous enough that a
person mistyping a password several times is unaffected, and the refusal never
says whether the account exists.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# How long an idle bucket is kept before it is swept, and how often sweeping
# runs. A sweep walks every key seen, so it does not run per request.
IDLE_EVICTION_SECONDS = 3600.0
SWEEP_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True)
class Decision:
    """Whether a call may proceed, and how long until it may retry."""

    allowed: bool
    retry_after_seconds: int = 0


ALLOWED = Decision(allowed=True)


@dataclass(frozen=True)
class Limit:
    """A number of attempts and the window they are counted over.

    Configuration rather than logic (`AGENTS.md`): every one of these is
    overridable per deployment, and the defaults below are starting values for a
    launch rather than approved numbers.
    """

    attempts: int
    window_seconds: int

    @property
    def per_second(self) -> float:
        return self.attempts / self.window_seconds


@dataclass
class _Bucket:
    tokens: float
    updated: float
    last_seen: float = field(default_factory=time.monotonic)


class LocalLimiter:
    """Token buckets, counted in this process.

    A token bucket rather than a fixed window, for the reason the gateway's
    limiter gives: a fixed window lets a caller spend a whole allowance in the
    last second of one window and again in the first second of the next, which
    is twice the configured rate at the worst possible moment.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def check(self, key: str, limit: Limit) -> Decision:
        """Take one token for `key`, or refuse with how long to wait.

        A limit of zero attempts refuses everything, which is a usable way to
        close a route in an incident. A negative window is a configuration error
        and fails open rather than locking every caller out of signin over a
        typo -- the layers behind this one (password hashing, confirmation,
        entitlement ceilings) all still apply.
        """
        if limit.attempts <= 0:
            return Decision(allowed=False, retry_after_seconds=limit.window_seconds)
        if limit.window_seconds <= 0:
            return ALLOWED

        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit.attempts), updated=now, last_seen=now)
                self._buckets[key] = bucket

            elapsed = max(0.0, now - bucket.updated)
            # Capped at the bucket size, so an address that has been quiet for a
            # week cannot arrive with a week's worth of attempts to spend at
            # once -- which is precisely the shape of an account-farming run.
            bucket.tokens = min(float(limit.attempts), bucket.tokens + elapsed * limit.per_second)
            bucket.updated = now
            bucket.last_seen = now

            if bucket.tokens < 1.0:
                missing = 1.0 - bucket.tokens
                retry_after = max(1, int(missing / limit.per_second) + 1)
                return Decision(allowed=False, retry_after_seconds=retry_after)

            bucket.tokens -= 1.0
            return ALLOWED

    def peek(self, key: str, limit: Limit) -> Decision:
        """Is there an attempt left for `key`, without spending one?

        The account half of the signin limit counts *failures*, not attempts, so
        the check and the spend happen at different moments: refuse before the
        password is verified, charge only if it was wrong. Spending on every
        attempt instead would ration a legitimate user who signs in often --
        several devices, a short session lifetime -- which is a lockout the
        platform inflicted on the person it was protecting.
        """
        if limit.attempts <= 0:
            return Decision(allowed=False, retry_after_seconds=limit.window_seconds)
        if limit.window_seconds <= 0:
            return ALLOWED

        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return ALLOWED
            elapsed = max(0.0, now - bucket.updated)
            tokens = min(float(limit.attempts), bucket.tokens + elapsed * limit.per_second)
            if tokens >= 1.0:
                return ALLOWED
            retry_after = max(1, int((1.0 - tokens) / limit.per_second) + 1)
            return Decision(allowed=False, retry_after_seconds=retry_after)

    def forget(self, key: str) -> None:
        """Drop a key's bucket.

        Used when an attempt succeeds and the count should not follow the caller
        around -- a person who signs in correctly on the fourth try has not
        earned a reduced allowance for the next hour.
        """
        with self._lock:
            self._buckets.pop(key, None)

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - IDLE_EVICTION_SECONDS
        for key in [k for k, b in self._buckets.items() if b.last_seen < cutoff]:
            del self._buckets[key]
