"""Auth email: GoTrue's Send Email Hook to the MaluMail REST API (ADR-029).

GoTrue does not render or send anything when the hook is enabled. It posts an
`email_action_type` and a token and expects the receiver to do the rest, which
means composition, suppression, quota and delivery all live here.

Three properties this module exists to hold, each of which fails quietly if got
wrong:

- **The signature is the authentication.** Nothing else identifies the caller.
  The scheme is Standard Webhooks and was verified against a real GoTrue call
  rather than read from a specification -- see `verify_signature`.
- **A 200 from MaluMail is final.** `/v1/send` has no idempotency key and GoTrue
  retries a failing hook, so treating a successful send as retryable double-sends
  to a real person.
- **Recipient addresses are never stored.** Migration 0003 settled that: the
  control plane keeps a peppered hash, so suppression and quota work without the
  platform holding a list of every end user of every tenant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from services.control_plane import crypto, db, entitlements, hashing

log = logging.getLogger(__name__)

MALUMAIL_BASE_URL = "https://api.malumail.com"

# Standard Webhooks tolerance. Without it a captured call can be replayed
# forever: the signature stays valid because the body never changes.
TIMESTAMP_TOLERANCE_SECONDS = 300

SEND_TIMEOUT_SECONDS = 30.0

# Two things this module accepts rather than engineers around, recorded so the
# next reader knows they were considered:
#
# - A captured hook call can be replayed inside the timestamp window, costing
#   one duplicate email. Standard Webhooks suggests also tracking seen
#   `webhook-id`s; that needs storage and eviction, and the attacker already
#   needs a position between GoTrue and the control plane, which is loopback or
#   an internal network.
# - The quota check is not atomic with the send, so two concurrent hooks can
#   exceed an entitlement by one message. Serialising them would put a lock on
#   the signup path to save a single email.

# Retryable per MaluMail's documented status table. 200 is deliberately absent:
# see the module docstring.
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class MailError(RuntimeError):
    """Auth email could not be sent. Never carries the recipient or a token."""


class SuppressedRecipient(MailError):
    """The address is suppressed. Terminal -- never retry."""


class QuotaExceeded(MailError):
    """The project's email entitlement is spent."""


class HookNotAuthenticated(MailError):
    """The call did not carry a valid signature for this project."""


class SendingDisabled(MailError):
    """The project may not send: suspended, deleted, or email not configured."""


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------


def verify_signature(
    *, secret: str, webhook_id: str, timestamp: str, body: bytes, signature_header: str,
    now: float | None = None,
) -> None:
    """Verify a Standard Webhooks signature, or raise.

    Verified empirically against GoTrue 2.195.0 rather than inferred: signed
    content is `{id}.{timestamp}.{body}`, HMAC-SHA256 keyed by the base64-decoded
    portion after `whsec_`, and the header carries a space-separated list of
    `v<version>,<base64>` so a secret can be rotated without downtime.
    """
    if "whsec_" not in secret:
        raise HookNotAuthenticated("hook secret is not in the expected v1,whsec_ form")
    try:
        key = base64.b64decode(secret.split("whsec_", 1)[1])
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same fault
        raise HookNotAuthenticated("hook secret is not valid base64") from exc

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise HookNotAuthenticated("webhook timestamp is not an integer") from exc

    current = time.time() if now is None else now
    if abs(current - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
        raise HookNotAuthenticated("webhook timestamp is outside the accepted window")

    signed = f"{webhook_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for part in (signature_header or "").split():
        _, _, candidate = part.partition(",")
        # compare_digest, not ==: a timing-variable comparison on a MAC is the
        # textbook way to make a forgery feasible.
        if candidate and hmac.compare_digest(candidate, expected):
            return
    raise HookNotAuthenticated("webhook signature does not verify")


def generate_hook_secret() -> str:
    """A fresh per-project hook secret in the form GoTrue expects."""
    import secrets

    return "v1,whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

# What each GoTrue action means to a recipient. Subjects are deliberately plain:
# a confirmation message that reads as marketing is a confirmation message that
# gets filtered.
_ACTIONS = {
    "signup": ("Confirm your email address", "confirm your email address"),
    "recovery": ("Reset your password", "reset your password"),
    "invite": ("You have been invited", "accept your invitation"),
    "magiclink": ("Your sign-in link", "sign in"),
    "email_change": ("Confirm your new email address", "confirm your new email address"),
    "email_change_current": ("Confirm your email change", "confirm your email change"),
    "reauthentication": ("Confirm it is you", "confirm it is you"),
}


@dataclass(frozen=True)
class Message:
    subject: str
    text: str
    html: str


def verification_url(email_data: dict[str, Any]) -> str:
    """The link that completes the action.

    Built against GoTrue's own `/verify` endpoint using `token_hash`, never the
    bare `token`: the hash is what the endpoint expects in a link, and the token
    is the value a person would type. `site_url` here is GoTrue's external URL,
    which for a project is its gateway hostname -- so the link a user clicks
    comes back through the gateway rather than pointing at a loopback worker.
    """
    site = (email_data.get("site_url") or "").rstrip("/")
    if not site:
        raise MailError("hook payload carried no site_url; cannot build a link")
    query = {
        "token": email_data.get("token_hash") or "",
        "type": email_data.get("email_action_type") or "",
    }
    redirect = email_data.get("redirect_to")
    if redirect:
        query["redirect_to"] = redirect
    return f"{site}/verify?{urllib.parse.urlencode(query)}"


def compose(email_data: dict[str, Any], *, project_name: str) -> Message:
    action = email_data.get("email_action_type") or ""
    subject, purpose = _ACTIONS.get(action, ("Confirm your request", "continue"))
    link = verification_url(email_data)

    text = (
        f"{subject}\n\n"
        f"Follow this link to {purpose} for {project_name}:\n\n"
        f"{link}\n\n"
        "If you did not request this, you can ignore this message.\n"
    )
    # Minimal HTML on purpose. MaluMail supports no templates, no inline images
    # and no attachments, and an elaborate message is more to get wrong than to
    # gain -- the whole content is one sentence and one link.
    safe_link = link.replace("&", "&amp;")
    html = (
        f"<p>Follow this link to {purpose} for "
        f"{project_name.replace('&', '&amp;').replace('<', '&lt;')}:</p>"
        f'<p><a href="{safe_link}">{subject}</a></p>'
        "<p>If you did not request this, you can ignore this message.</p>"
    )
    return Message(subject=subject, text=text, html=html)


# --------------------------------------------------------------------------
# MaluMail
# --------------------------------------------------------------------------


class MaluMail:
    """A thin client over the four endpoints MaluMail actually exposes."""

    def __init__(self, api_key: str, *, base_url: str = MALUMAIL_BASE_URL,
                 client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=SEND_TIMEOUT_SECONDS)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def send(self, *, sender: str, sender_name: str | None, to: str, message: Message) -> dict:
        payload: dict[str, Any] = {
            "from": sender,
            "to": to,
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
        }
        if sender_name:
            payload["from_name"] = sender_name

        response = self._client.post(f"{self._base}/v1/send", json=payload, headers=self._headers())

        if response.status_code == 200:
            body = response.json()
            # Partial success is still 200. A suppressed or invalid recipient
            # appears in `rejected`, and treating the 200 as delivery would
            # report a send that never happened.
            rejected = body.get("rejected") or []
            if rejected and not body.get("accepted"):
                reason = str(rejected[0].get("reason", "rejected"))
                if reason.startswith("suppressed"):
                    raise SuppressedRecipient(f"recipient rejected: {reason}")
                raise MailError(f"recipient rejected: {reason}")
            return body

        if response.status_code == 400:
            # Documented as "all recipients undeliverable" when it carries
            # `rejected`, which is terminal rather than a malformed request.
            try:
                body = response.json()
            except ValueError:
                body = {}
            rejected = body.get("rejected") or []
            if rejected:
                reason = str(rejected[0].get("reason", "undeliverable"))
                if reason.startswith("suppressed"):
                    raise SuppressedRecipient(f"recipient rejected: {reason}")
            raise MailError(f"MaluMail rejected the request: {_error_of(body)}")

        if response.status_code == 429:
            raise QuotaExceeded("MaluMail rate limit exceeded")

        # The response body can echo the request, which contains the recipient
        # and the verification link. Only the status and MaluMail's own short
        # error string are surfaced.
        raise MailError(
            f"MaluMail returned {response.status_code}: {_error_of(_json_or_empty(response))}"
        )

    def suppressions(self) -> list[dict]:
        response = self._client.get(f"{self._base}/v1/suppressions", headers=self._headers())
        if response.status_code != 200:
            raise MailError(f"could not read suppressions: {response.status_code}")
        return response.json().get("suppressions") or []


def _json_or_empty(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


def _error_of(body: dict) -> str:
    return str(body.get("error", "no error message"))


# --------------------------------------------------------------------------
# Suppression and quota, held locally
# --------------------------------------------------------------------------


def recipient_hash(address: str, *, pepper: bytes) -> bytes:
    """A stable, peppered hash of an address.

    Migration 0003 requires the control plane not to store end-user addresses.
    Peppered rather than plain so the table is not a rainbow-table exercise
    against every address that ever signed up to any tenant.

    Normalised before hashing, because the same person typing `Ada@Example.com`
    must land on the same row as `ada@example.com` -- otherwise a suppression is
    trivially defeated by capitalisation.
    """
    digest = hashing.peppered(address.strip().lower(), pepper)
    # peppered() returns hex; the column is BYTEA.
    return bytes.fromhex(digest)


def is_suppressed(conn: psycopg.Connection, address: str, *, pepper: bytes) -> bool:
    row = db.one(
        conn,
        "SELECT 1 AS found FROM email_suppressions WHERE recipient_hash = %s",
        (recipient_hash(address, pepper=pepper),),
    )
    return row is not None


def sent_today(conn: psycopg.Connection, project_id: uuid.UUID) -> int:
    row = db.one(
        conn,
        "SELECT count(*) AS n FROM email_events "
        " WHERE project_id = %s AND event_type = 'sent' AND occurred_at > now() - interval '1 day'",
        (project_id,),
    )
    return row["n"]


def record_event(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    event_type: str,
    address: str,
    pepper: bytes,
    detail: dict | None = None,
) -> None:
    db.execute(
        conn,
        "INSERT INTO email_events (project_id, event_type, recipient_hash, detail_json, occurred_at) "
        "VALUES (%s, %s, %s, %s, now())",
        (
            project_id,
            event_type,
            recipient_hash(address, pepper=pepper),
            psycopg.types.json.Jsonb(detail or {}),
        ),
    )
    conn.commit()


def reconcile_suppressions(
    conn: psycopg.Connection, client: MaluMail, *, pepper: bytes
) -> int:
    """Pull MaluMail's suppression list into `email_suppressions`.

    MaluMail has no webhooks, so bounces and complaints arrive only as entries
    on a list to be read. Run on a schedule: a suppressed address that the
    platform has not heard about still fails at send time, but it fails after
    spending an API call and a quota unit rather than before.
    """
    added = 0
    for entry in client.suppressions():
        address = entry.get("email")
        if not address:
            continue
        reason = {"bounce": "hard_bounce", "complaint": "complaint"}.get(
            entry.get("reason", ""), "manual"
        )
        added += db.execute(
            conn,
            "INSERT INTO email_suppressions (recipient_hash, reason) VALUES (%s, %s) "
            "ON CONFLICT (recipient_hash) DO NOTHING",
            (recipient_hash(address, pepper=pepper), reason),
        )
    conn.commit()
    return added


# --------------------------------------------------------------------------
# The hook
# --------------------------------------------------------------------------

# Projects that may send. A suspended project must not, which is how ADR-029
# delivers "suspending a project immediately stops it sending" when the MaluMail
# key belongs to the customer and cannot be revoked by us.
SENDING_STATUSES = ("PROVISIONED", "ACTIVE")

@dataclass(frozen=True)
class EmailConfig:
    """A project's resolved sending configuration."""

    may_send: bool
    project_id: uuid.UUID
    display_name: str
    sender_address: str
    sender_name: str | None
    api_key: str
    hook_secret: str
    daily_limit: int | None
    custom_domain: bool


def load_config(conn: psycopg.Connection, project_ref: str, *, key_ring, settings) -> EmailConfig:
    """Resolve which account sends for this project, and with what limits.

    `platform_default` uses the platform's own MaluMail key from configuration;
    `custom_domain` uses the customer's, decrypted from their row. The two modes
    differ in who enforces quota, which is why the limit is only carried for the
    first (ADR-029).
    """
    row = db.one(
        conn,
        """
        SELECT p.id, p.display_name, p.status, p.plan_id,
               e.sender_mode, e.sender_address, e.sender_name, e.sending_suspended_at,
               e.hook_ciphertext, e.hook_nonce, e.hook_key_version,
               e.malumail_ciphertext, e.malumail_nonce, e.malumail_key_version,
               pl.code AS plan_code, pl.config_json
          FROM projects p
          JOIN project_email_settings e ON e.project_id = p.id
          LEFT JOIN plans pl ON pl.id = p.plan_id
         WHERE p.project_ref = %s AND p.deleted_at IS NULL
        """,
        (project_ref,),
    )
    # Unknown project, and configured-but-no-secret, are reported as an
    # authentication failure rather than as a distinct condition. A caller that
    # cannot sign must not be able to tell a real project from an invented one:
    # the gateway makes every refusal identical for the same reason, and a
    # security review of this slice found the endpoint had drifted from that.
    #
    # Whether the project *may* send -- suspended, not provisioned -- is
    # deliberately not decided here. It is checked after the signature verifies,
    # so suspension is never disclosed to an unauthenticated caller.
    if row is None or row["hook_ciphertext"] is None:
        raise HookNotAuthenticated("no hook secret for this project")

    hook_secret = key_ring.open(
        crypto.SealedValue(bytes(row["hook_ciphertext"]), bytes(row["hook_nonce"]),
                           row["hook_key_version"]),
        aad=crypto.aad_for("project_email_settings", "hook", str(row["id"])),
    ).decode()

    custom = row["sender_mode"] == "custom_domain"
    if custom:
        api_key = key_ring.open(
            crypto.SealedValue(bytes(row["malumail_ciphertext"]), bytes(row["malumail_nonce"]),
                               row["malumail_key_version"]),
            aad=crypto.aad_for("project_email_settings", "malumail", str(row["id"])),
        ).decode()
        limit = None          # the customer's own MaluMail plan governs
    else:
        api_key = settings.malumail_api_key or ""
        if not api_key:
            raise SendingDisabled("no platform MaluMail key configured")
        limit = entitlements.resolve(row["plan_code"], row["config_json"]).emails_per_day

    return EmailConfig(
        may_send=(row["status"] in SENDING_STATUSES and row["sending_suspended_at"] is None),
        project_id=row["id"],
        display_name=row["display_name"],
        sender_address=row["sender_address"],
        sender_name=row["sender_name"],
        api_key=api_key,
        hook_secret=hook_secret,
        daily_limit=limit,
        custom_domain=custom,
    )


def handle_hook(
    *,
    project_ref: str,
    body: bytes,
    webhook_id: str,
    timestamp: str,
    signature_header: str,
    client_factory=None,
    key_ring=None,
    settings=None,
) -> dict:
    """Verify, check, compose, send, record. In that order, deliberately.

    Nothing is read out of the payload before the signature is checked, and the
    suppression and quota checks happen before the API call rather than after --
    on `platform_default` the allowance is shared between every project using
    it, so discovering the limit from a 429 would mean one project could spend
    another's.
    """
    import json

    from services.control_plane import config as cp_config

    settings = settings or cp_config.load()
    if key_ring is None:
        key_ring = crypto.KeyRing(settings.kek)
        with db.connection() as conn:
            key_ring.load(conn)

    with db.connection() as conn:
        cfg = load_config(conn, project_ref, key_ring=key_ring, settings=settings)

        verify_signature(
            secret=cfg.hook_secret,
            webhook_id=webhook_id,
            timestamp=timestamp,
            body=body,
            signature_header=signature_header,
        )

        # Only now, with the caller proven to hold this project's secret, is it
        # safe to say why a send is refused.
        if not cfg.may_send:
            raise SendingDisabled("project may not send")

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise MailError("hook payload is not JSON") from exc

        address = ((payload.get("user") or {}).get("email") or "").strip()
        if not address:
            raise MailError("hook payload carried no recipient")

        if is_suppressed(conn, address, pepper=settings.token_pepper):
            record_event(conn, project_id=cfg.project_id, event_type="suppressed",
                         address=address, pepper=settings.token_pepper)
            raise SuppressedRecipient("recipient is suppressed")

        if cfg.daily_limit is not None and sent_today(conn, cfg.project_id) >= cfg.daily_limit:
            record_event(conn, project_id=cfg.project_id, event_type="quota_rejected",
                         address=address, pepper=settings.token_pepper)
            raise QuotaExceeded("daily email entitlement is spent")

        message = compose(payload.get("email_data") or {}, project_name=cfg.display_name)
        client = (client_factory or MaluMail)(cfg.api_key)

        result = client.send(
            sender=cfg.sender_address,
            sender_name=cfg.sender_name,
            to=address,
            message=message,
        )

        # Recorded only after MaluMail accepted it. Counting an attempt as sent
        # would let a run of failures exhaust a project's allowance without a
        # single message being delivered.
        record_event(
            conn,
            project_id=cfg.project_id,
            event_type="sent",
            address=address,
            pepper=settings.token_pepper,
            detail={"action": (payload.get("email_data") or {}).get("email_action_type"),
                    "custom_domain": cfg.custom_domain},
        )

    return {"accepted": len(result.get("accepted") or [])}
