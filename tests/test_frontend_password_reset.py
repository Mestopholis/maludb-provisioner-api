"""The forgot-password flow, and the one step a customer cannot work around.

Found on 2026-09-29, while creating an account for the owner after closing theirs: the control plane
has had `POST /v1/auth/password-reset` and `/password-reset/complete` since Phase 07, and **nothing
in the site called either**. There was no "Forgot your password?" link and no page for the emailed
link to land on, so a customer who lost their password could not get back in and support had no
command for it either. The gap survived a go-live gap survey because both halves existed -- just not
joined up.

What is held here:

- **the emailed link names a file this document root serves.** `password_reset.reset_link` used to
  build `/reset-password`, and the console is hash-routed with no rewrite (DEPLOYMENT section 3), so
  every customer clicking it would have got a 404 at the step with no alternative;
- **the request form says the same thing whichever it was.** The endpoint answers 202 for an
  unregistered address on purpose, and a page that distinguished them would put the membership
  oracle back;
- **the token is never written into the page**, only posted back;
- **a dead link says so and offers a new one**, rather than looking like a broken site.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
RESET = (FRONTEND / "reset-password.html").read_text()
INDEX = (FRONTEND / "index.html").read_text()
API_JS = (FRONTEND / "api.js").read_text()
APP_JS = (FRONTEND / "app.js").read_text()
LINK_PY = (ROOT / "services" / "control_plane" / "password_reset.py").read_text()


def test_the_emailed_link_lands_on_a_page_that_exists():
    """The single most breakable joint in this flow, and it broke silently: a link to a path the
    document root has no rewrite for is a 404 the customer cannot route around."""
    match = re.search(r'return f"\{dashboard_url\.rstrip\(\'/\'\)\}/([A-Za-z0-9._-]+)\?token=', LINK_PY)
    assert match, "password_reset.reset_link no longer builds a link this test can read"
    target = match.group(1)
    assert target == "reset-password.html"
    assert (FRONTEND / target).is_file(), f"the reset email points at {target}, which is not a file here"


def test_the_sign_in_form_offers_the_reset_and_the_request_form_gives_nothing_away():
    assert 'id="forgot-link"' in INDEX and 'id="forgot-form"' in INDEX
    assert 'id="signin-form"' in INDEX

    handler = APP_JS[APP_JS.index('submit($("#forgot-form")'):]
    handler = handler[:handler.index('submit($("#create-project-form")')]
    assert "requestPasswordReset(" in handler
    assert "If that address has an account" in handler, (
        "the endpoint answers 202 either way; the page must not distinguish them"
    )
    # Comments stripped first: the one above says the words the page must not, which is the point
    # of it and would otherwise fail this check.
    code = "\n".join(line.split("//")[0] for line in handler.splitlines())
    for leak in ("no such account", "not registered", "unknown address"):
        assert leak not in code.lower()


def test_both_calls_are_unauthenticated_and_written_out_in_full():
    """A person resetting a password has no session, so `auth: false` is not an optimisation."""
    for name, path in (("requestPasswordReset", "/v1/auth/password-reset"),
                       ("completePasswordReset", "/v1/auth/password-reset/complete")):
        call = re.search(rf"export const {name} = .*?;\n", API_JS, re.S)
        assert call, f"api.js has no {name}"
        assert f'api("{path}"' in call.group(0), f"{name} must name {path} in full"
        assert "auth: false" in call.group(0), f"{name} is called without a session"


def test_the_reset_page_posts_the_token_without_showing_it():
    assert 'URLSearchParams(window.location.search).get("token")' in RESET
    assert "completePasswordReset({ token, password })" in RESET
    # The token reaches the request and nothing else: no interpolation of it into the document.
    for written in ("innerHTML", "document.write", "${token}"):
        assert written not in RESET, f"the reset page must not put the token in the page ({written})"
    assert 'name="robots" content="noindex"' in RESET, "the address carries a token; keep it out of search"


def test_a_dead_link_is_a_page_rather_than_a_broken_form():
    assert 'id="reset-dead"' in RESET and "expired or was already used" in RESET
    assert "error.status === 400" in RESET, "the API answers every bad token 400; that is the dead link"
    assert 'if (!token)' in RESET, "an address with no token at all is the same dead end"
    assert "Every session and personal access token on the account is revoked" in RESET, (
        "a customer has to know what a reset does to whatever is already signed in"
    )
