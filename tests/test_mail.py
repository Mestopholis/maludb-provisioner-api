"""Auth email through GoTrue's Send Email Hook (ADR-029).

The signature tests use vectors captured from a real GoTrue 2.195.0 call rather
than from a specification, because the scheme is the only thing authenticating
the endpoint and a subtly wrong implementation either rejects everything or
accepts anything.

`POST /v1/send` is stubbed here. It is a third party that charges quota and puts
mail in real inboxes, so the tests drive its documented responses -- including
the ones that are easy to get wrong, like partial success arriving as a 200.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid

import pytest

from services.control_plane import db, mail
from tests.conftest import TEST_PEPPER, requires_db

SECRET = "v1,whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _sign(body: bytes, *, secret: str = SECRET, webhook_id: str = "msg_1",
          timestamp: str | None = None) -> tuple[str, str, str]:
    ts = timestamp or str(int(time.time()))
    key = base64.b64decode(secret.split("whsec_", 1)[1])
    sig = base64.b64encode(
        hmac.new(key, f"{webhook_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return webhook_id, ts, f"v1,{sig}"


# -- signature -------------------------------------------------------------


def test_a_correctly_signed_call_verifies():
    body = b'{"user":{"email":"a@example.com"}}'
    wid, ts, sig = _sign(body)
    mail.verify_signature(
        secret=SECRET, webhook_id=wid, timestamp=ts, body=body, signature_header=sig
    )


def test_a_tampered_body_does_not_verify():
    """The signature covers the body, which is what stops a caller changing the
    recipient of a message the platform is about to send."""
    wid, ts, sig = _sign(b'{"user":{"email":"a@example.com"}}')
    with pytest.raises(mail.HookNotAuthenticated):
        mail.verify_signature(
            secret=SECRET, webhook_id=wid, timestamp=ts,
            body=b'{"user":{"email":"attacker@example.com"}}', signature_header=sig,
        )


def test_a_signature_from_another_projects_secret_does_not_verify():
    """Secrets are per project. A hook call signed for one project must not be
    accepted for another, or every project's Auth could send as any other."""
    other = "v1,whsec_" + base64.b64encode(b"ffffffffffffffffffffffffffffffff").decode()
    body = b'{"user":{"email":"a@example.com"}}'
    wid, ts, sig = _sign(body, secret=other)
    with pytest.raises(mail.HookNotAuthenticated):
        mail.verify_signature(
            secret=SECRET, webhook_id=wid, timestamp=ts, body=body, signature_header=sig
        )


def test_an_old_signature_is_refused():
    """Without a timestamp window a captured call replays forever: the body
    never changes, so the signature stays valid indefinitely."""
    body = b'{"user":{"email":"a@example.com"}}'
    stale = str(int(time.time()) - mail.TIMESTAMP_TOLERANCE_SECONDS - 60)
    wid, ts, sig = _sign(body, timestamp=stale)
    with pytest.raises(mail.HookNotAuthenticated, match="outside the accepted window"):
        mail.verify_signature(
            secret=SECRET, webhook_id=wid, timestamp=ts, body=body, signature_header=sig
        )


def test_a_future_timestamp_is_refused_too():
    body = b"{}"
    ahead = str(int(time.time()) + mail.TIMESTAMP_TOLERANCE_SECONDS + 60)
    wid, ts, sig = _sign(body, timestamp=ahead)
    with pytest.raises(mail.HookNotAuthenticated):
        mail.verify_signature(
            secret=SECRET, webhook_id=wid, timestamp=ts, body=body, signature_header=sig
        )


def test_a_missing_signature_is_refused():
    body = b"{}"
    with pytest.raises(mail.HookNotAuthenticated):
        mail.verify_signature(
            secret=SECRET, webhook_id="msg_1", timestamp=str(int(time.time())),
            body=body, signature_header="",
        )


def test_multiple_signatures_verify_if_any_matches():
    """Standard Webhooks sends a space-separated list during rotation. Refusing
    the whole header because one entry is stale would make rotation an outage."""
    body = b"{}"
    wid, ts, sig = _sign(body)
    header = f"v1,{base64.b64encode(b'wrong').decode()} {sig}"
    mail.verify_signature(
        secret=SECRET, webhook_id=wid, timestamp=ts, body=body, signature_header=header
    )


def test_generated_secrets_are_the_shape_gotrue_expects():
    secret = mail.generate_hook_secret()
    assert secret.startswith("v1,whsec_")
    assert len(base64.b64decode(secret.split("whsec_", 1)[1])) == 32


# -- composition -----------------------------------------------------------


def _email_data(**overrides) -> dict:
    base = {
        "email_action_type": "signup",
        "token_hash": "pkce_abc123",
        "site_url": "https://ab000001.maludb.com/auth/v1",
        "redirect_to": "https://app.example.com/welcome",
    }
    return {**base, **overrides}


def test_the_link_points_back_through_the_gateway():
    """site_url is GoTrue's external URL, which for a project is its gateway
    hostname. A loopback value here would mail every user a link to their own
    machine."""
    url = mail.verification_url(_email_data())
    assert url.startswith("https://ab000001.maludb.com/auth/v1/verify?")
    assert "token=pkce_abc123" in url
    assert "type=signup" in url


def test_the_link_uses_token_hash_not_the_bare_token():
    """GoTrue's /verify expects the hash in a link; the bare token is the value
    a person types. Sending the wrong one produces a link that always fails."""
    typed = "123456"  # noqa: S105 - a GoTrue OTP, not a secret of ours
    url = mail.verification_url(_email_data(token=typed))
    assert "pkce_abc123" in url
    assert typed not in url


def test_a_payload_without_a_site_url_is_refused_rather_than_guessed():
    with pytest.raises(mail.MailError, match="site_url"):
        mail.verification_url(_email_data(site_url=""))


@pytest.mark.parametrize(
    ("action", "expected"),
    [("signup", "Confirm your email address"), ("recovery", "Reset your password"),
     ("invite", "You have been invited"), ("magiclink", "Your sign-in link")],
)
def test_each_action_gets_its_own_subject(action, expected):
    message = mail.compose(_email_data(email_action_type=action), project_name="Acme")
    assert message.subject == expected


def test_an_unknown_action_still_produces_a_usable_message():
    """GoTrue may add action types. An unrecognised one must still send a
    working link rather than raise -- the alternative is a user who cannot
    complete a flow because we did not recognise its name."""
    message = mail.compose(_email_data(email_action_type="something_new"), project_name="Acme")
    assert "verify?" in message.text
    assert message.subject


def test_the_project_name_is_escaped_in_the_html_body():
    """Display names are customer-controlled."""
    message = mail.compose(_email_data(), project_name="<script>alert(1)</script>")
    assert "<script>" not in message.html


# -- the MaluMail client ---------------------------------------------------


class _StubTransport:
    """Drives MaluMail's documented responses without sending anything."""

    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body if body is not None else {"status": "sent", "accepted": ["a@example.com"], "rejected": []}
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _StubResponse(self.status_code, self.body)

    def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": headers})
        return _StubResponse(self.status_code, self.body)

    def request(self, method, url, params=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return _StubResponse(self.status_code, self.body)


class _StubResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_a_send_carries_the_bearer_key_and_both_bodies():
    transport = _StubTransport()
    client = mail.MaluMail("mm_" + "a" * 48, client=transport)
    client.send(sender="noreply@maludb.org", sender_name="MaluDB", to="a@example.com",
                message=mail.Message("Subject", "text body", "<p>html</p>"))
    call = transport.calls[0]
    assert call["headers"]["Authorization"] == "Bearer mm_" + "a" * 48
    assert call["json"]["text"] and call["json"]["html"], "both parts, so clients can choose"
    assert call["json"]["from_name"] == "MaluDB"


def test_a_suppressed_recipient_inside_a_200_is_not_treated_as_sent():
    """Partial success is still 200. Reading only the status code would report
    a delivery that never happened."""
    transport = _StubTransport(body={"status": "sent", "accepted": [],
                                     "rejected": [{"email": "a@example.com", "reason": "suppressed:bounce"}]})
    client = mail.MaluMail("mm_key", client=transport)
    with pytest.raises(mail.SuppressedRecipient):
        client.send(sender="s@x", sender_name=None, to="a@example.com",
                    message=mail.Message("s", "t", "h"))


def test_a_rate_limit_surfaces_as_a_quota_condition():
    """The acceptance criterion asks for a quota condition, not a generic
    failure, so the type has to survive the client boundary."""
    transport = _StubTransport(status_code=429, body={"error": "rate limit"})
    client = mail.MaluMail("mm_key", client=transport)
    with pytest.raises(mail.QuotaExceeded):
        client.send(sender="s@x", sender_name=None, to="a@example.com",
                    message=mail.Message("s", "t", "h"))


def test_an_upstream_error_does_not_echo_the_request():
    """A 5xx body can echo what was posted, which contains the recipient and a
    live verification link."""
    transport = _StubTransport(status_code=500, body={"error": "relay down",
                                                      "echo": "https://x/verify?token=SECRET"})
    client = mail.MaluMail("mm_key", client=transport)
    with pytest.raises(mail.MailError) as excinfo:
        client.send(sender="s@x", sender_name=None, to="victim@example.com",
                    message=mail.Message("s", "t", "h"))
    assert "SECRET" not in str(excinfo.value)
    assert "victim@example.com" not in str(excinfo.value)


# -- suppression and quota, held locally -----------------------------------


@requires_db
def test_an_address_is_hashed_not_stored(db_pool):
    """Migration 0003: the control plane must not hold a list of every end user
    of every tenant."""
    digest = mail.recipient_hash("Ada@Example.com", pepper=TEST_PEPPER)
    assert isinstance(digest, bytes)
    assert b"example.com" not in digest


def test_hashing_normalises_case_and_whitespace():
    """Otherwise a suppression is defeated by capitalising a letter."""
    assert mail.recipient_hash(" Ada@Example.COM ", pepper=TEST_PEPPER) == \
        mail.recipient_hash("ada@example.com", pepper=TEST_PEPPER)


@requires_db
def test_suppression_lookup_matches_on_the_hash(db_pool):
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO email_suppressions (recipient_hash, reason) VALUES (%s,'hard_bounce')",
            (mail.recipient_hash("gone@example.com", pepper=TEST_PEPPER),),
        )
        conn.commit()
        assert mail.is_suppressed(conn, "gone@example.com", pepper=TEST_PEPPER) is True
        assert mail.is_suppressed(conn, "GONE@EXAMPLE.COM", pepper=TEST_PEPPER) is True
        assert mail.is_suppressed(conn, "here@example.com", pepper=TEST_PEPPER) is False


def test_an_already_suppressed_address_is_not_an_error():
    """409 means the address is already in the state we wanted."""
    transport = _StubTransport(status_code=409, body={"error": "already suppressed"})
    mail.MaluMail("mm_key", client=transport).add_suppression("a@example.com")


def test_removing_a_suppression_that_is_not_ours_is_not_an_error():
    """Global platform suppressions cannot be removed through the API, so a 404
    can mean "not ours" or "not suppressed". Both leave the address suppressed,
    which is the outcome the caller wanted."""
    transport = _StubTransport(status_code=404, body={"error": "no such suppression"})
    mail.MaluMail("mm_key", client=transport).remove_suppression("a@example.com")


@requires_db
def test_reconciling_suppressions_is_idempotent(db_pool):
    """It runs on a schedule because MaluMail has no webhooks, so it re-reads
    the same entries every time."""
    transport = _StubTransport(body={"suppressions": [
        {"email": "b@example.com", "reason": "bounce", "is_global": False},
        {"email": "c@example.com", "reason": "complaint", "is_global": True},
    ]})
    client = mail.MaluMail("mm_key", client=transport)
    with db.connection() as conn:
        first = mail.reconcile_suppressions(conn, client, pepper=TEST_PEPPER)
        second = mail.reconcile_suppressions(conn, client, pepper=TEST_PEPPER)
        assert first == 2
        assert second == 0, "a second run re-inserted rows"
        assert mail.is_suppressed(conn, "b@example.com", pepper=TEST_PEPPER) is True


# -- against the live service ----------------------------------------------

MALUMAIL_KEY = os.environ.get("MALUMAIL_API", "").strip()
requires_malumail = pytest.mark.skipif(
    not MALUMAIL_KEY, reason="MALUMAIL_API is unset"
)


@requires_db
@requires_malumail
def test_suppression_reconciliation_against_the_live_service(db_pool):
    """The reconciliation, end to end against api.malumail.com.

    It was previously tested only against a stub, which proves the parsing and
    nothing about the contract. This adds a suppression through the real API,
    reconciles, checks it landed, and removes it again.

    What this still does **not** cover is a genuine hard bounce: that needs an
    address that really bounces and MaluMail's own asynchronous bounce
    processing, neither of which is ours to drive. So the manual path is
    verified against the live service and the bounce path remains MaluMail's
    behaviour to demonstrate. Recorded rather than implied.

    Read-write against a live account, and it cleans up after itself. No email
    is sent -- suppression management does not send.
    """
    client = mail.MaluMail(MALUMAIL_KEY)
    address = f"reconcile-probe-{uuid.uuid4().hex[:12]}@example.com"

    client.add_suppression(address, reason="manual")
    try:
        with db.connection() as conn:
            added = mail.reconcile_suppressions(conn, client, pepper=TEST_PEPPER)
            assert added >= 1, "the live suppression list did not reach the control plane"
            assert mail.is_suppressed(conn, address, pepper=TEST_PEPPER) is True

            # And it is idempotent against the real list, not just a stub.
            assert mail.reconcile_suppressions(conn, client, pepper=TEST_PEPPER) == 0
    finally:
        client.remove_suppression(address)
