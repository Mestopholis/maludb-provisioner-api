"""Rate-limit dependencies for the routes an anonymous caller can reach.

Deliberately shaped like `auth_dep`: a dependency rather than middleware, so a
route that should be limited says so in its own signature and a reviewer can see
which routes are protected by reading them rather than by reading a table of
path prefixes somewhere else.

The limiter itself is `services.control_plane.ratelimit`, one instance per
application, held on `app.state` so a test can drive it with its own clock.
"""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request, status

from services.control_plane import ratelimit


def client_key(request: Request) -> str:
    """Who this call is counted against: the client's address, as far as this deployment can know it.

    The peer address by default. `X-Forwarded-For` is read **only** when the deployment names the
    proxies in front of it (`trusted_proxies`) or trusts its immediate peer (`trust_forwarded_for`),
    because a forwarded header that nothing strips is attacker-controlled: a caller that can set it
    picks its own bucket, and every limit built on it counts one attempt each for a million invented
    clients.

    **The chain is walked from the right**, skipping trusted proxies, and the first address that is not
    one is the client. Each proxy appends the address it observed, so everything to the right of the
    client was written by infrastructure and everything to the left by whoever connected -- which is
    why a forged entry can never be chosen: it is always to the left of a real one. Rehearsal finding
    19: behind TLS on one host and Apache on this one, "the last hop" was the TLS proxy for every
    visitor, so the whole internet shared one bucket.

    With every entry trusted (a request that started inside the proxies), the leftmost is used.
    """
    config = request.app.state.config
    client = request.client
    # No peer address at all (an ASGI transport that does not report one) counts as a single shared
    # bucket rather than as unlimited. Sharing one bucket is a bad experience for those callers;
    # having none is no limit at all.
    peer = client.host if client and client.host else "unknown"
    trusted = tuple(getattr(config, "trusted_proxies", ()) or ())
    if not trusted and not config.trust_forwarded_for:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    chain = [hop.strip() for hop in forwarded.split(",") if hop.strip()] + [peer]

    def is_trusted(address: str, *, is_peer: bool) -> bool:
        if is_peer and config.trust_forwarded_for and not trusted:
            return True  # the older, one-proxy setting: the immediate peer is the proxy
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(parsed in network for network in trusted)

    for index in range(len(chain) - 1, -1, -1):
        if not is_trusted(chain[index], is_peer=index == len(chain) - 1):
            return chain[index][:64]
    return chain[0][:64]


client_address = client_key


def enforce(request: Request, *, bucket: str, limit: ratelimit.Limit, subject: str = "") -> None:
    """Spend one attempt, or raise 429.

    `subject` scopes the bucket to something other than the caller -- an email
    address, for the account half of the signin limit. It is never returned in
    the response, and the refusal is identical whether or not the account
    exists: a 429 that only appeared for real accounts would be an oracle for
    which addresses are registered, which is exactly what the uniform 401 on the
    route below exists to avoid.
    """
    limiter: ratelimit.LocalLimiter = request.app.state.limiter
    key = f"{bucket}:{subject or client_key(request)}"
    decision = limiter.check(key, limit)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="too many attempts; try again later",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def spend(request: Request, *, bucket: str, limit: ratelimit.Limit, subject: str = "") -> None:
    """Charge one attempt without refusing on exhaustion.

    The refusal already happened in `guard`; this records that the attempt was a
    failure. Separating the two is what lets one bucket count failures while
    another counts attempts.
    """
    limiter: ratelimit.LocalLimiter = request.app.state.limiter
    limiter.check(f"{bucket}:{subject or client_key(request)}", limit)


def guard(request: Request, *, bucket: str, limit: ratelimit.Limit, subject: str = "") -> None:
    """Refuse if `subject` has no attempts left, without spending one.

    Paired with `enforce` after the fact: the account half of the signin limit
    counts failures, so it is checked here and charged only once the password
    turns out to be wrong.
    """
    limiter: ratelimit.LocalLimiter = request.app.state.limiter
    decision = limiter.peek(f"{bucket}:{subject or client_key(request)}", limit)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="too many attempts; try again later",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def forget(request: Request, *, bucket: str, subject: str = "") -> None:
    """Drop a bucket after an attempt that turned out to be legitimate."""
    limiter: ratelimit.LocalLimiter = request.app.state.limiter
    limiter.forget(f"{bucket}:{subject or client_key(request)}")


def signup_limit(request: Request) -> ratelimit.Limit:
    config = request.app.state.config
    return ratelimit.Limit(config.signup_attempts, config.signup_window_seconds)


def signin_limit(request: Request) -> ratelimit.Limit:
    config = request.app.state.config
    return ratelimit.Limit(config.signin_attempts, config.signin_window_seconds)


def signin_account_limit(request: Request) -> ratelimit.Limit:
    config = request.app.state.config
    return ratelimit.Limit(config.signin_account_attempts, config.signin_account_window_seconds)
