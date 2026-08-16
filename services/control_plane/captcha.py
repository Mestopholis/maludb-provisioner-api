"""Challenge verification for the routes an anonymous caller can reach.

Phase 07 slice 5. Signup is public at launch (repository owner, 2026-08-16) and
a challenge is required from day one rather than added once abuse appears --
because by the time abuse appears the accounts already exist, and cleaning up a
farm is work nobody has budgeted.

**The failure mode is the decision worth arguing about, not the provider.** When
the challenge service cannot be reached, a verifier either fails closed (nobody
signs up until it returns) or fails open (everybody signs up, unverified, for as
long as the outage lasts). Failing open turns a third party's bad afternoon into
an unbounded window with no control at all, and the window is exactly when
somebody watching for it would farm accounts. Failing closed costs real signups
during an outage the platform did not cause.

This defaults to **fail closed**, and the default is a decision rather than an
accident: signup is the one route where the cost of being wrong is permanent
(accounts, projects, databases on shared nodes) and the cost of being
unavailable is a customer trying again later. `MALUDB_CAPTCHA_FAIL_OPEN=1`
inverts it for a deployment that would rather take the accounts, and the
override exists so that choice is made in configuration by somebody who means
it rather than by editing this file during an incident.

The provider is behind a protocol because it is a deployment choice and every
one of them speaks the same shape: post a token and a secret, get a verdict.
Cloudflare Turnstile is the implementation here; hCaptcha and reCAPTCHA differ
only in the URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

# Short. A person is waiting on a signup form, and a challenge service that is
# slow is a challenge service that is failing.
VERIFY_TIMEOUT_SECONDS = 5.0

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@dataclass(frozen=True)
class Verdict:
    """Whether the challenge was solved, and why not if it was not.

    `reason` is for the platform's logs, never for the caller: telling a client
    that its token was already spent, or minted for a different site key, is
    telling whoever is automating the form exactly what to fix.
    """

    passed: bool
    reason: str = ""


PASSED = Verdict(passed=True)


class Verifier(Protocol):
    def verify(self, token: str, *, remote_ip: str | None = None) -> Verdict: ...


class NullVerifier:
    """Accepts everything. For development and tests only.

    Selected when no provider is configured, which is why the *route* decides
    whether a challenge is required rather than trusting the verifier to refuse:
    a deployment that forgot to configure one must not silently accept every
    signup because the object it got back says yes to everything.
    """

    def verify(self, token: str, *, remote_ip: str | None = None) -> Verdict:  # noqa: ARG002
        return PASSED


class TurnstileVerifier:
    """Cloudflare Turnstile. The token comes from the browser widget."""

    def __init__(
        self,
        secret: str,
        *,
        fail_open: bool = False,
        url: str = TURNSTILE_VERIFY_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self._secret = secret
        self._fail_open = fail_open
        self._url = url
        self._client = client or httpx.Client(timeout=VERIFY_TIMEOUT_SECONDS)

    def verify(self, token: str, *, remote_ip: str | None = None) -> Verdict:
        if not token:
            # Not an outage: a caller that sent no token did not attempt the
            # challenge, and `fail_open` is about the service being unreachable
            # rather than about the challenge being skipped.
            return Verdict(passed=False, reason="no challenge token presented")

        payload = {"secret": self._secret, "response": token}
        if remote_ip:
            payload["remoteip"] = remote_ip

        try:
            response = self._client.post(self._url, data=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The outage case, and the only place `fail_open` applies.
            log.warning("challenge verification is unavailable: %s", type(exc).__name__)
            if self._fail_open:
                return Verdict(passed=True, reason="verification unavailable; failing open")
            return Verdict(passed=False, reason="verification unavailable")

        if body.get("success") is True:
            return PASSED
        # Upstream's own codes, logged and never returned: they name precisely
        # what an automated client would need to correct.
        codes = ",".join(str(c) for c in (body.get("error-codes") or [])) or "rejected"
        return Verdict(passed=False, reason=codes)


def build(config, *, client: httpx.Client | None = None) -> Verifier:
    """The verifier this deployment's configuration asks for."""
    if not config.captcha_secret:
        return NullVerifier()
    return TurnstileVerifier(
        config.captcha_secret, fail_open=config.captcha_fail_open, client=client
    )
