"""Staff sign-in, sign-out and who-am-I for the operator console (ADR-082 slice 1).

Mounted only by `admin_main.create_admin_app`, never by the public or internal
application; `tests/test_admin_app.py` asserts both halves.

**The session is a cookie, not a bearer token in page storage.** HttpOnly, so no script
on the console can read it; SameSite=Strict, so no other site's page can make a
browser send it; Secure unless the deployment says its listener is plain HTTP inside
the operator network; and scoped to `/admin`. Every request that changes state also
needs the `X-MaluDB-Staff` header, which a cross-site form cannot set -- a second line
behind SameSite, not a replacement for it.

**Every refusal to sign in is the same 401**, whatever was wrong: the address, the
password, the code, a lock, or a factor never confirmed. `staff.sign_in` records which
one in the audit trail, where an operator can read it and an attacker cannot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from services.control_plane import db, ratelimit, staff
from services.control_plane.api import limit_dep

router = APIRouter(prefix="/admin/v1", tags=["admin"])

COOKIE = "maludb_staff"
COOKIE_PATH = "/admin"
STAFF_HEADER = "x-maludb-staff"

_UNAUTHENTICATED = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="staff sign-in required")
_REFUSED = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email, password or code")


class SignInIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=12)


class StaffOut(BaseModel):
    email: str
    display_name: str | None
    session_expires_at: datetime | None = None


def require_staff_header(x_maludb_staff: Annotated[str | None, Header()] = None) -> None:
    """A header a cross-site form or image cannot send. Required on every state change."""
    if x_maludb_staff != "1":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing X-MaluDB-Staff header")


def current_staff(request: Request) -> staff.StaffPrincipal:
    """The signed-in staff member, from the session cookie alone. A customer token is never looked at."""
    presented = request.cookies.get(COOKIE)
    if not presented:
        raise _UNAUTHENTICATED
    key: staff.StaffKey = request.app.state.staff_key
    with db.connection() as conn:
        principal = staff.resolve(conn, presented=presented, pepper=key.session_pepper)
    if principal is None:
        raise _UNAUTHENTICATED
    return principal


CurrentStaff = Annotated[staff.StaffPrincipal, Depends(current_staff)]


def _signin_limit(request: Request) -> ratelimit.Limit:
    cfg = request.app.state.config
    return ratelimit.Limit(cfg.signin_attempts, cfg.signin_window_seconds)


@router.post(
    "/session",
    response_model=StaffOut,
    summary="Sign in with email, password and authenticator code",
    dependencies=[Depends(require_staff_header)],
)
def sign_in(body: SignInIn, request: Request, response: Response) -> StaffOut:
    # Per client address, before any work: the account lockout in `staff.sign_in` bounds
    # guesses at one account, and this bounds how fast one address can spread across many.
    limit_dep.enforce(request, bucket="staff-signin", limit=_signin_limit(request))
    key: staff.StaffKey = request.app.state.staff_key
    with db.connection() as conn:
        token = staff.sign_in(
            conn,
            email=body.email,
            password=body.password,
            code=body.code,
            staff_key=key,
            pepper=key.session_pepper,
            ip_address=limit_dep.client_key(request),
            user_agent=request.headers.get("user-agent"),
        )
        if token is None:
            conn.commit()  # the failure count and the audit row are the point of a refusal
            raise _REFUSED
        principal = staff.resolve(conn, presented=token, pepper=key.session_pepper)
        expires = db.one(conn, "SELECT expires_at FROM staff_sessions WHERE id = %s", (principal.session_id,))
        conn.commit()
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(staff.SESSION_LIFETIME.total_seconds()),
        path=COOKIE_PATH,
        secure=request.app.state.config.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return StaffOut(email=principal.staff.email, display_name=principal.staff.display_name,
                    session_expires_at=expires["expires_at"])


@router.get("/session", response_model=StaffOut, summary="The signed-in staff member")
def who_am_i(principal: CurrentStaff) -> StaffOut:
    return StaffOut(email=principal.staff.email, display_name=principal.staff.display_name)


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    dependencies=[Depends(require_staff_header)],
)
def sign_out(request: Request) -> Response:
    presented = request.cookies.get(COOKIE)
    if presented:
        key: staff.StaffKey = request.app.state.staff_key
        with db.connection() as conn:
            staff.sign_out(conn, presented=presented, pepper=key.session_pepper)
            conn.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(COOKIE, path=COOKIE_PATH, secure=request.app.state.config.cookie_secure,
                           httponly=True, samesite="strict")
    return response
