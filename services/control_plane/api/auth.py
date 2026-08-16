"""Sign-up, sign-in, sign-out, and credential management."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from services.control_plane import db, identity, password_reset
from services.control_plane.api import limit_dep
from services.control_plane.api.auth_dep import CurrentPrincipal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    # Length is the only rule enforced here. Composition rules push users
    # toward predictable patterns; length plus Argon2id is the better trade.
    password: str = Field(min_length=12, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)


class SigninIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=1024)


class SessionOut(BaseModel):
    token: str
    expires_in_seconds: int


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    slug: str
    name: str
    role: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    authenticated_via: str
    organizations: list[MembershipOut]


class PatIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetCompleteIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)


class SessionSummaryOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None


class PatSummaryOut(BaseModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class PatOut(BaseModel):
    token: str
    name: str


@router.post("/signup", response_model=MeOut, status_code=status.HTTP_201_CREATED, summary="Register a platform user")
def signup(body: SignupIn, request: Request) -> MeOut:
    # Signup is public at launch, so this is the one route on the platform that
    # an anonymous caller can use to create durable state. Limited per source
    # before any work is done -- a limit applied after the password hash would
    # still let a flood spend the CPU it was meant to protect.
    limit_dep.enforce(request, bucket="signup", limit=limit_dep.signup_limit(request))
    with db.connection() as conn:
        try:
            user, _ = identity.create_user_with_personal_org(
                conn,
                email=str(body.email),
                password=body.password,
                display_name=body.display_name,
            )
        except identity.IdentityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        memberships = identity.memberships_for(conn, user.id)

    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        authenticated_via="none",
        organizations=[
            MembershipOut(org_id=m.org_id, slug=m.org_slug, name=m.org_name, role=m.role) for m in memberships
        ],
    )


@router.post("/signin", response_model=SessionOut, summary="Exchange credentials for a session")
def signin(body: SigninIn, request: Request) -> SessionOut:
    # Two limits, counting two different things. Per source bounds attempts and
    # is released on success; per account bounds *failures*, which is what a
    # distributed credential-stuffing run produces and what a legitimate user
    # does not.
    email = str(body.email).strip().lower()
    account_limit = limit_dep.signin_account_limit(request)
    limit_dep.enforce(request, bucket="signin", limit=limit_dep.signin_limit(request))
    limit_dep.guard(request, bucket="signin-account", limit=account_limit, subject=email)
    with db.connection() as conn:
        user = identity.authenticate(conn, email=str(body.email), password=body.password)
        if user is None:
            # Charged on failure only. An account bucket spent per *attempt*
            # would ration the person it protects -- several devices, or a
            # session lifetime short enough to sign in daily, and they exhaust
            # their own allowance by using the platform correctly.
            limit_dep.spend(request, bucket="signin-account", limit=account_limit, subject=email)
            # One message for both unknown account and wrong password.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid email or password",
            )
        # The source bucket is released on success: someone who mistypes a
        # password three times and then gets it right has not earned a reduced
        # allowance for the next five minutes. The *account* bucket deliberately
        # is not, because releasing it would let an attacker who guesses
        # correctly reset the ceiling for the next account.
        limit_dep.forget(request, bucket="signin")
        token = identity.create_session(
            conn,
            user_id=user.id,
            pepper=request.app.state.config.token_pepper,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    return SessionOut(token=token, expires_in_seconds=int(identity.SESSION_LIFETIME.total_seconds()))


@router.get("/me", response_model=MeOut, summary="Describe the authenticated principal")
def me(principal: CurrentPrincipal) -> MeOut:
    return MeOut(
        id=principal.user.id,
        email=principal.user.email,
        display_name=principal.user.display_name,
        authenticated_via=principal.via,
        organizations=[
            MembershipOut(org_id=m.org_id, slug=m.org_slug, name=m.org_name, role=m.role)
            for m in principal.memberships
        ],
    )


@router.post("/signout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke the presented session")
def signout(request: Request, principal: CurrentPrincipal) -> Response:
    header = request.headers.get("authorization", "")
    presented = header.split(" ", 1)[1] if " " in header else ""
    with db.connection() as conn:
        identity.revoke_session(conn, token=presented, pepper=request.app.state.config.token_pepper)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tokens",
    response_model=PatOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a personal access token; shown once",
)
def create_token(body: PatIn, request: Request, principal: CurrentPrincipal) -> PatOut:
    # A personal access token must not be able to mint further credentials,
    # or a leaked token becomes self-renewing.
    if principal.via != "session":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="personal access tokens may only be created from an interactive session",
        )
    with db.connection() as conn:
        token = identity.create_pat(
            conn,
            user_id=principal.user.id,
            name=body.name,
            pepper=request.app.state.config.token_pepper,
            expires_at=body.expires_at,
        )
    return PatOut(token=token, name=body.name)


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a personal access token",
)
def revoke_token(token_id: uuid.UUID, principal: CurrentPrincipal) -> Response:
    with db.connection() as conn:
        # Scoped to the caller, so one user cannot revoke another's token.
        if not identity.revoke_pat(conn, token_id=token_id, user_id=principal.user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- password reset --------------------------------------------------------
#
# The one flow here an anonymous caller drives end to end, which is why both
# halves answer uniformly. See `services/control_plane/password_reset.py` for
# what each refusal deliberately does not say.


@router.post(
    "/password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a password reset link",
)
def request_password_reset(body: PasswordResetRequestIn, request: Request) -> dict:
    """202 whether or not the address belongs to anybody.

    A different answer for a registered address would be a membership oracle for
    any address someone cared to try -- and the addresses worth checking are the
    ones worth attacking. The rate limit is per source *and* per address for the
    same reason it is on signin: one host working through a list and a
    distributed attempt against one person are different attacks.
    """
    email = str(body.email).strip().lower()
    limit_dep.enforce(request, bucket="reset", limit=limit_dep.signin_limit(request))
    limit_dep.enforce(
        request, bucket="reset-account", limit=limit_dep.signin_account_limit(request),
        subject=email,
    )

    config = request.app.state.config
    # Decided *before* the user table is touched, so "we cannot send mail" is a
    # property of the deployment rather than of the address. Checking it after
    # the lookup -- as the first version did -- answered 503 for a registered
    # address and 202 for an unknown one on any control plane without a sender
    # configured, which is precisely the oracle this endpoint exists to avoid.
    if not config.platform_email_from or not config.malumail_api_key:
        log.error("a password reset could not be sent: no platform sender configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="password reset is temporarily unavailable",
        )

    with db.connection() as conn:
        issued = password_reset.request(
            conn,
            email=email,
            pepper=config.token_pepper,
            ip_address=request.client.host if request.client else None,
        )
        conn.commit()

    if issued is not None:
        try:
            password_reset.send(issued, config=config)
        except Exception:  # noqa: BLE001 - a send failure must not disclose the account
            # Every send failure is swallowed, including SendingUnavailable if
            # configuration changed under us between the check above and here.
            # Which addresses fail to deliver is exactly the signal this
            # endpoint refuses to give, and a delivery failure is not something
            # the caller can act on anyway.
            log.exception("a password reset email could not be delivered")

    return {"status": "accepted"}


@router.post(
    "/password-reset/complete",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password using a reset token",
)
def complete_password_reset(body: PasswordResetCompleteIn, request: Request) -> Response:
    """Every failure is the same failure.

    Expired, already spent, forged, or belonging to a deleted user: the
    differences are useful only to somebody who did not receive the mail.
    """
    with db.connection() as conn:
        try:
            password_reset.complete(
                conn,
                token=body.token,
                new_password=body.password,
                pepper=request.app.state.config.token_pepper,
            )
        except password_reset.ResetError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid or expired reset token"
            ) from exc
        conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- what a signed-in user can see and revoke ------------------------------


@router.get(
    "/sessions",
    response_model=list[SessionSummaryOut],
    summary="List this user's live sessions",
)
def list_sessions(principal: CurrentPrincipal) -> list[SessionSummaryOut]:
    """Revocation without a list is a control nobody can exercise.

    "Sign out everywhere" only reassures if you can see what everywhere is. The
    session token never appears here: what identifies a session to its owner is
    where and when it was used.
    """
    with db.connection() as conn:
        rows = identity.list_sessions(conn, principal.user.id)
    return [
        # inet comes back as an ipaddress object; the response is a string.
        SessionSummaryOut(
            **{**row, "ip_address": str(row["ip_address"]) if row["ip_address"] else None}
        )
        for row in rows
    ]


@router.post(
    "/sessions/revoke-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of every session, including this one",
)
def revoke_all_sessions(principal: CurrentPrincipal) -> Response:
    """Including the caller's own, deliberately.

    Somebody pressing this believes their account is compromised. Keeping the
    current session alive because it is convenient would leave one door open at
    the moment the whole point is that every door closes.

    **Sessions only.** Personal access tokens survive this, and that is worth
    knowing rather than assuming: a PAT authenticates wherever a session does.
    Revoking them is `DELETE /v1/auth/tokens/{id}`, and a password reset revokes
    all of them at once -- which is the route to take if the account itself is
    believed compromised rather than merely signed in somewhere unwanted.
    """
    with db.connection() as conn:
        identity.revoke_all_sessions(conn, principal.user.id)
        conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tokens",
    response_model=list[PatSummaryOut],
    summary="List this user's personal access tokens",
)
def list_tokens(principal: CurrentPrincipal) -> list[PatSummaryOut]:
    with db.connection() as conn:
        return [PatSummaryOut(**row) for row in identity.list_pats(conn, principal.user.id)]
