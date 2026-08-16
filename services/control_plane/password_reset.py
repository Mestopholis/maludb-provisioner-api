"""Password reset for platform users (Phase 07 slice 4).

Nothing could reset a password before this. `identity` authenticates, creates
sessions and personal access tokens and manages invitations -- and a customer
who forgot their password had no route back to their own organization.

Three properties this module exists to hold, all of them about what an
*unauthenticated* caller can learn or do:

**Requesting a reset says nothing about who exists.** The response is identical
for a registered address and an invented one. An endpoint that answered
differently would be a free membership oracle for any address someone cared to
try, and the people worth checking are exactly the ones worth attacking.

**A token is a bearer credential for an account**, so it is stored the way every
other credential here is: an indexed prefix and a peppered HMAC (ADR-023 Class
A). The value exists in the email that carried it and nowhere else. It is short
lived, single use, and spending one invalidates every other outstanding reset
for that user -- otherwise a reset requested by an attacker stays live after the
owner has taken their account back.

**Completing a reset ends every session and every personal access token.** A
password change that left the attacker's credentials alive would give the owner
a false sense of having recovered: they changed the lock while the other party
was already inside. Tokens matter more than sessions here, not less -- a PAT
authenticates wherever a session does, can be minted with no expiry, and is what
an attacker with a moment's access leaves behind on purpose.

The mail goes from the platform's own account on its own domain, never from a
customer's sender. A MaluDB account reset has nothing to do with whichever
project the person happens to own, and sending it as them would be the platform
impersonating a customer to that customer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

import psycopg

from services.control_plane import db, hashing, identity, mail

log = logging.getLogger(__name__)

# Long enough for someone who reads mail on a phone an hour later, short enough
# that an intercepted link is not a standing key to the account.
RESET_LIFETIME = timedelta(hours=1)

TOKEN_KIND = "pwreset"  # noqa: S105 - a token kind, not a credential


class ResetError(RuntimeError):
    """A reset could not be completed. Never says why to the caller."""


class SendingUnavailable(RuntimeError):
    """The platform cannot send mail, which is the platform's problem."""


@dataclass(frozen=True)
class IssuedReset:
    token: str
    user_email: str


def request(
    conn: psycopg.Connection,
    *,
    email: str,
    pepper: bytes,
    ip_address: str | None = None,
) -> IssuedReset | None:
    """Mint a reset token for `email`, or None if nobody has that address.

    The None is for the *caller's* logic, not for the caller's response: every
    route above this answers the same way either way. Returning it rather than
    raising keeps the decision about disclosure where it belongs -- one place,
    in the route, rather than in an exception type somebody might report.
    """
    user = db.one(
        conn,
        "SELECT id, email FROM users WHERE lower(email) = lower(%s) AND deleted_at IS NULL",
        (email,),
    )
    if user is None:
        return None

    token = hashing.generate_token(TOKEN_KIND, pepper)
    db.execute(
        conn,
        """
        INSERT INTO password_resets
            (id, user_id, token_prefix, verification_data, expires_at, requested_ip)
        VALUES (%s, %s, %s, %s, now() + %s, %s)
        """,
        (
            uuid.uuid4(), user["id"], token.prefix, token.verifier, RESET_LIFETIME,
            # INET, so an unparseable client host would abort the insert and
            # fail the reset. `identity.normalise_ip` exists because sign-in hit
            # exactly this: an address we cannot parse is worth dropping from
            # the audit trail, never worth refusing the operation over.
            identity.normalise_ip(ip_address),
        ),
    )
    return IssuedReset(token=token.plaintext, user_email=user["email"])


def complete(
    conn: psycopg.Connection,
    *,
    token: str,
    new_password: str,
    pepper: bytes,
) -> uuid.UUID:
    """Spend a reset token and set the password. Returns the user it belonged to.

    Every failure raises the same `ResetError`. An expired token, a spent one, a
    forged one and one belonging to a deleted user are indistinguishable from
    outside, because the differences are only useful to somebody who did not
    receive the mail.
    """
    split = hashing.split_token(token)
    if split is None or split[0] != TOKEN_KIND:
        raise ResetError("invalid reset token")
    _, prefix = split

    row = db.one(
        conn,
        """
        SELECT r.id, r.user_id, r.verification_data, r.used_at,
               r.expires_at <= now() AS expired
          FROM password_resets r
          JOIN users u ON u.id = r.user_id AND u.deleted_at IS NULL
         WHERE r.token_prefix = %s
        """,
        (prefix,),
    )

    # Verified before any branch on the row, and against a dummy when the prefix
    # is unknown, so a forged token cannot be told from a real one by how long
    # the answer took.
    stored = row["verification_data"] if row is not None else ""
    matches = hashing.verify_token(stored, token, pepper)
    if row is None or not matches or row["used_at"] is not None or row["expired"]:
        raise ResetError("invalid reset token")

    identity.set_password(conn, user_id=row["user_id"], password=new_password)

    db.execute(conn, "UPDATE password_resets SET used_at = now() WHERE id = %s", (row["id"],))
    # Every other outstanding reset for this user dies with it. A reset an
    # attacker requested must not still work after the owner has recovered.
    db.execute(
        conn,
        "UPDATE password_resets SET used_at = now() "
        " WHERE user_id = %s AND used_at IS NULL",
        (row["user_id"],),
    )
    # And every session ends. Changing the lock while the other party is inside
    # is not recovery.
    ended = identity.revoke_all_sessions(conn, row["user_id"])
    # **And every personal access token.** A PAT is a full platform credential
    # -- `resolve_principal` accepts one wherever a session works, including
    # issuing a project's secret API key, which is its data API with row-level
    # security bypassed -- and it may be minted with no expiry at all. An
    # attacker holding a session for a moment can leave one behind, and the
    # owner who then resets their password is told every session has ended
    # while the token keeps working indefinitely. Recovery that leaves the
    # longest-lived credential alive is not recovery.
    #
    # The cost is real and accepted: a customer's CI token stops working when
    # they reset their password, and they must issue a new one. That is the
    # right way round. Silent survival is the option that cannot stand, because
    # the person it fails is the one who has just been compromised.
    tokens = db.execute(
        conn,
        "UPDATE personal_access_tokens SET revoked_at = now() "
        " WHERE user_id = %s AND revoked_at IS NULL",
        (row["user_id"],),
    )
    log.info(
        "password reset completed; %d session(s) and %d token(s) revoked",
        ended,
        tokens,
        extra={"extra_fields": {"user_id": str(row["user_id"])}},
    )
    return row["user_id"]


def reset_link(token: str, *, dashboard_url: str) -> str:
    """Where the customer is sent to type a new password.

    The dashboard's origin, not this API's: the link is for a person, and this
    API has no page. The token is percent-encoded rather than trusted to be
    URL-safe -- it is today, and a change to the token alphabet should not
    silently produce links that break.
    """
    return f"{dashboard_url.rstrip('/')}/reset-password?token={quote(token, safe='')}"


def compose(link: str) -> mail.Message:
    """The message itself. Plain about what to do and what to do if it was not you."""
    subject = "Reset your MaluDB password"
    text = (
        "Someone asked to reset the password for this MaluDB account.\n\n"
        f"{link}\n\n"
        "The link works once and expires in an hour.\n\n"
        "If this was not you, you can ignore this message -- your password has "
        "not changed, and nobody has been given access to your account."
    )
    html = (
        "<p>Someone asked to reset the password for this MaluDB account.</p>"
        f'<p><a href="{link}">Choose a new password</a></p>'
        "<p>The link works once and expires in an hour.</p>"
        "<p>If this was not you, you can ignore this message &mdash; your password "
        "has not changed, and nobody has been given access to your account.</p>"
    )
    return mail.Message(subject=subject, text=text, html=html)


def send(issued: IssuedReset, *, config, client: mail.MaluMail | None = None) -> None:
    """Send the reset mail from the platform's own account.

    Raises `SendingUnavailable` when the platform has no sender configured,
    which is an operator's problem and is reported as one -- a customer told
    "check your inbox" for mail that was never sent has no way to discover the
    difference.
    """
    if not config.platform_email_from or not config.malumail_api_key:
        raise SendingUnavailable("no platform sender is configured")

    message = compose(reset_link(issued.token, dashboard_url=config.dashboard_url))
    sender = client or mail.MaluMail(config.malumail_api_key)
    sender.send(
        sender=config.platform_email_from,
        sender_name=config.platform_email_from_name,
        to=issued.user_email,
        message=message,
    )
