"""Organization membership and invitations."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from services.control_plane import db, identity
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager, require_member

router = APIRouter(prefix="/v1/organizations", tags=["organizations"])


class OrgOut(BaseModel):
    org_id: uuid.UUID
    slug: str
    name: str
    role: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str


class InviteIn(BaseModel):
    email: EmailStr
    role: str = Field(pattern="^(owner|admin|developer|billing|viewer)$")


class InviteOut(BaseModel):
    token: str
    email: str
    role: str


class RoleIn(BaseModel):
    role: str = Field(pattern="^(owner|admin|developer|billing|viewer)$")


@router.get("", response_model=list[OrgOut], summary="List organizations the caller belongs to")
def list_organizations(principal: CurrentPrincipal) -> list[OrgOut]:
    return [
        OrgOut(org_id=m.org_id, slug=m.org_slug, name=m.org_name, role=m.role) for m in principal.memberships
    ]


@router.get("/{org_id}/members", response_model=list[MemberOut], summary="List members")
def list_members(org_id: uuid.UUID, principal: CurrentPrincipal) -> list[MemberOut]:
    require_member(principal, org_id)
    with db.connection() as conn:
        rows = db.query(
            conn,
            """
            SELECT m.user_id, m.role, u.email
              FROM org_members m
              JOIN users u ON u.id = m.user_id
             WHERE m.org_id = %s
             ORDER BY u.email
            """,
            (org_id,),
        )
    return [MemberOut(user_id=r["user_id"], email=r["email"], role=r["role"]) for r in rows]


@router.post(
    "/{org_id}/invitations",
    response_model=InviteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a user by email",
)
def invite(org_id: uuid.UUID, body: InviteIn, request: Request, principal: CurrentPrincipal) -> InviteOut:
    require_manager(principal, org_id)
    with db.connection() as conn:
        try:
            token = identity.create_invitation(
                conn,
                org_id=org_id,
                email=str(body.email),
                role=body.role,
                invited_by=principal.user.id,
                pepper=request.app.state.config.token_pepper,
            )
        except identity.IdentityError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    # The token is returned for now. Once ADR-019 delivery is wired up it is
    # emailed to the invitee instead of handed to the inviter.
    return InviteOut(token=token, email=str(body.email), role=body.role)


@router.post("/invitations/accept", response_model=OrgOut, summary="Accept an invitation")
def accept(token: str, request: Request, principal: CurrentPrincipal) -> OrgOut:
    with db.connection() as conn:
        try:
            org_id = identity.accept_invitation(
                conn,
                token=token,
                user=principal.user,
                pepper=request.app.state.config.token_pepper,
            )
        except identity.IdentityError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        memberships = identity.memberships_for(conn, principal.user.id)

    for membership in memberships:
        if membership.org_id == org_id:
            return OrgOut(
                org_id=membership.org_id,
                slug=membership.org_slug,
                name=membership.org_name,
                role=membership.role,
            )
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="membership not found after accept")


@router.put("/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Change a member's role")
def set_role(org_id: uuid.UUID, user_id: uuid.UUID, body: RoleIn, principal: CurrentPrincipal) -> Response:
    require_manager(principal, org_id)
    with db.connection() as conn:
        try:
            identity.change_role(conn, org_id=org_id, user_id=user_id, role=body.role)
        except identity.IdentityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a member")
def remove_member(org_id: uuid.UUID, user_id: uuid.UUID, principal: CurrentPrincipal) -> Response:
    require_manager(principal, org_id)
    with db.connection() as conn:
        try:
            identity.remove_member(conn, org_id=org_id, user_id=user_id)
        except identity.IdentityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
