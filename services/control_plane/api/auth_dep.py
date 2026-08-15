"""Authentication and authorization dependencies.

Deny by default. Every route that touches data depends on `CurrentPrincipal`,
so forgetting to authenticate is a missing parameter rather than a silent
security hole.

ADR-021: these are platform-user credentials. They are unrelated to project API
keys and to end-user tokens issued by a tenant's Auth service.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from services.control_plane import db, identity

# auto_error=False so a missing header produces our own 401 rather than a 403.
_bearer = HTTPBearer(auto_error=False, scheme_name="platformCredential")

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="authentication required",
    headers={"WWW-Authenticate": "Bearer"},
)


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> identity.Principal:
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED

    pepper = request.app.state.config.token_pepper
    with db.connection() as conn:
        principal = identity.resolve_principal(conn, presented=credentials.credentials, pepper=pepper)

    # resolve_principal returns None for unknown, malformed, revoked and expired
    # alike, so the response cannot be used to distinguish them.
    if principal is None:
        raise _UNAUTHENTICATED
    return principal


CurrentPrincipal = Annotated[identity.Principal, Depends(current_principal)]


def require_member(principal: identity.Principal, org_id: uuid.UUID) -> None:
    """404 rather than 403 for non-members.

    Answering 403 would confirm the organization exists to someone with no
    right to know it does.
    """
    if not principal.is_member_of(org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="organization not found")


def require_manager(principal: identity.Principal, org_id: uuid.UUID) -> None:
    require_member(principal, org_id)
    if not principal.can_manage(org_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requires the owner or admin role",
        )
