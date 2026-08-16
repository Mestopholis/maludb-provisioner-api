"""Sign-up, sign-in, sign-out, and credential management."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from services.control_plane import db, identity
from services.control_plane.api import limit_dep
from services.control_plane.api.auth_dep import CurrentPrincipal

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
