"""Per-project rate and concurrency limits.

A limiter is a denial-of-service surface pointed at your own customers, so these
lean towards the cases where it wrongly refuses rather than the ones where it
wrongly allows. The failure mode of a limiter that is too strict is silence from
an application that looks healthy, which is far harder to diagnose than an
error.

The clock is injected. Sleeping through a rate window would make the suite slow
and flaky at once, and neither would test anything the injected clock does not.
"""

from __future__ import annotations

import uuid

import pytest

from services.gateway import limits


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def limiter(clock) -> limits.LocalLimiter:
    return limits.LocalLimiter(clock=clock)


def _take(limiter, project, *, rate=10, window=60, concurrency=100):
    return limiter.acquire(project, rate=rate, window_seconds=window, concurrency=concurrency)


# -- rate ------------------------------------------------------------------


def test_requests_within_the_allowance_are_served(limiter):
    project = uuid.uuid4()
    for _ in range(10):
        decision = _take(limiter, project)
        assert decision.allowed
        limiter.release(project)


def test_the_allowance_is_enforced(limiter):
    project = uuid.uuid4()
    for _ in range(10):
        assert _take(limiter, project).allowed
        limiter.release(project)
    refused = _take(limiter, project)
    assert not refused.allowed
    assert refused.limit == "rate"


def test_a_refusal_says_which_limit_and_when_to_retry(limiter):
    """A client that cannot tell a rate limit from a concurrency limit cannot
    act on either: one is fixed by slowing down, the other by holding fewer
    requests open."""
    project = uuid.uuid4()
    for _ in range(10):
        _take(limiter, project)
        limiter.release(project)
    refused = _take(limiter, project)
    assert "rate limit" in refused.message
    assert refused.retry_after_seconds >= 1


def test_retry_after_is_never_zero(limiter):
    """Zero invites an immediate retry that is certain to fail again."""
    project = uuid.uuid4()
    for _ in range(1000):
        _take(limiter, project, rate=1000, window=1)
        limiter.release(project)
    refused = _take(limiter, project, rate=1000, window=1)
    assert not refused.allowed
    assert refused.retry_after_seconds >= 1


def test_the_allowance_refills_over_time(limiter, clock):
    project = uuid.uuid4()
    for _ in range(10):
        _take(limiter, project)
        limiter.release(project)
    assert not _take(limiter, project).allowed

    clock.advance(6.1)  # 10 per 60s = one token every 6 seconds
    assert _take(limiter, project).allowed


def test_an_idle_project_cannot_bank_an_unbounded_burst(limiter, clock):
    """A fixed window would let a project spend its whole allowance at the end
    of one window and again at the start of the next. A bucket capped at the
    allowance is what stops a week of idleness becoming a week's worth of
    requests in one second."""
    project = uuid.uuid4()
    clock.advance(7 * 24 * 3600)
    served = 0
    while _take(limiter, project).allowed:
        limiter.release(project)
        served += 1
        if served > 50:
            break
    assert served == 10, f"an idle project banked {served} requests against an allowance of 10"


def test_projects_do_not_share_an_allowance(limiter):
    """The whole point: one noisy project must not spend another's."""
    noisy, quiet = uuid.uuid4(), uuid.uuid4()
    for _ in range(10):
        _take(limiter, noisy)
        limiter.release(noisy)
    assert not _take(limiter, noisy).allowed
    assert _take(limiter, quiet).allowed


# -- concurrency -----------------------------------------------------------


def test_concurrent_requests_are_capped(limiter):
    """The control that actually protects the database: PostgREST's pool is 3
    on the free tier, and slow queries pin it regardless of request rate."""
    project = uuid.uuid4()
    for _ in range(3):
        assert _take(limiter, project, rate=1000, concurrency=3).allowed
    refused = _take(limiter, project, rate=1000, concurrency=3)
    assert not refused.allowed
    assert refused.limit == "concurrency"
    assert "concurrent" in refused.message


def test_a_released_slot_is_reusable(limiter):
    project = uuid.uuid4()
    for _ in range(3):
        _take(limiter, project, rate=1000, concurrency=3)
    assert not _take(limiter, project, rate=1000, concurrency=3).allowed
    limiter.release(project)
    assert _take(limiter, project, rate=1000, concurrency=3).allowed


def test_a_concurrency_refusal_carries_no_retry_after(limiter):
    """How long depends on when an in-flight request finishes, which is not
    knowable here. A number would be a guess presented as fact."""
    project = uuid.uuid4()
    _take(limiter, project, rate=1000, concurrency=1)
    refused = _take(limiter, project, rate=1000, concurrency=1)
    assert refused.retry_after_seconds == 0


def test_releasing_more_than_was_taken_does_not_create_capacity(limiter):
    """Otherwise a double release -- a retry path calling it twice, say -- would
    silently raise a project's concurrency ceiling."""
    project = uuid.uuid4()
    _take(limiter, project, rate=1000, concurrency=1)
    for _ in range(5):
        limiter.release(project)
    assert limiter.in_flight(project) == 0
    assert _take(limiter, project, rate=1000, concurrency=1).allowed
    assert not _take(limiter, project, rate=1000, concurrency=1).allowed


def test_a_refused_request_holds_no_slot(limiter):
    """A refusal that consumed a slot would ratchet a project down to zero: each
    rejection would make the next one likelier."""
    project = uuid.uuid4()
    _take(limiter, project, rate=1000, concurrency=1)
    for _ in range(10):
        _take(limiter, project, rate=1000, concurrency=1)
    assert limiter.in_flight(project) == 1


def test_a_rate_refusal_does_not_consume_a_concurrency_slot(limiter):
    """Both are checked before either is taken."""
    project = uuid.uuid4()
    for _ in range(10):
        _take(limiter, project, rate=10, concurrency=100)
        limiter.release(project)
    assert not _take(limiter, project, rate=10, concurrency=100).allowed
    assert limiter.in_flight(project) == 0


# -- failing in the right direction ----------------------------------------


def test_a_zero_limit_allows_rather_than_locks_out(limiter):
    """Reaching the limiter with no configured rate means something upstream is
    already wrong. Refusing would turn a configuration error into an outage,
    and ADR-009's other layers still apply."""
    assert _take(limiter, uuid.uuid4(), rate=0, window=60).allowed
    assert _take(limiter, uuid.uuid4(), rate=10, window=0).allowed


def test_state_for_idle_projects_is_swept(limiter, clock):
    """Without this, one entry accumulates per project ref ever seen."""
    for _ in range(5):
        project = uuid.uuid4()
        _take(limiter, project)
        limiter.release(project)
    assert limiter.tracked_projects() == 5

    clock.advance(limits.IDLE_EVICTION_SECONDS + limits.SWEEP_INTERVAL_SECONDS + 1)
    _take(limiter, uuid.uuid4())
    assert limiter.tracked_projects() == 1, "idle state was not swept"


def test_an_in_flight_project_is_never_swept(limiter, clock):
    """Sweeping a project mid-request would lose its slot count and let it
    exceed its concurrency ceiling."""
    busy = uuid.uuid4()
    _take(limiter, busy, concurrency=5)
    clock.advance(limits.IDLE_EVICTION_SECONDS + limits.SWEEP_INTERVAL_SECONDS + 1)
    _take(limiter, uuid.uuid4())
    assert limiter.in_flight(busy) == 1


def test_the_no_op_limiter_enforces_nothing():
    """Used by tests that are about something else, so it must not quietly
    become a limiter."""
    limiter = limits.NoLimiter()
    project = uuid.uuid4()
    for _ in range(1000):
        assert limiter.acquire(project, rate=1, window_seconds=60, concurrency=1).allowed
