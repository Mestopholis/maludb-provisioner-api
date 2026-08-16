"""Project API keys, and the URL they are used against.

Phase 07 slice 2. The model underneath already had the right shape, and the
shape is the acceptance criterion: ADR-023 classifies key material by whether
the platform is *required* to hand it back, not by how sensitive it feels.

- A **secret** key is stored as a verifier and nothing else. It is returned once,
  at creation, and there is no route here that can produce it again -- not
  because a route was left out, but because the value does not exist anywhere
  to return. Losing it means creating another and revoking the old one.
- A **publishable** key ships in a customer's client bundle and a dashboard must
  display it indefinitely, so it is stored envelope-encrypted and listing
  returns it. Encrypting a value that is public looks redundant until you ask
  what happens the second time someone opens the page.

Which is why listing returns the publishable value inline rather than behind a
separate reveal call: a dashboard needs it on every page load, and a "reveal"
endpoint for a value that is public by design would be ceremony that suggests
the value is a secret.

**Nothing here returns anything that could reach PostgreSQL directly.** A key
authenticates to the gateway, which holds the tenant's real credentials; the
project's database name, its node and its roles appear in no response. That is
the phase's first acceptance criterion and this is the router where breaking it
would be easiest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from services.control_plane import api_keys, db, models
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager, require_member

router = APIRouter(prefix="/v1", tags=["api-keys"])


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    key_type: str
    name: str | None
    # The prefix, which is what a customer sees in a log line and what makes
    # "which key is this?" answerable without holding the key.
    key_identifier: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    # Populated for publishable keys only. Null for a secret key is not a
    # permission problem to be worked around: there is nothing stored to fill
    # it with.
    key: str | None = None


class IssuedKeyOut(BaseModel):
    """A newly created key, including the one and only sight of a secret."""

    id: uuid.UUID
    key_type: str
    key_identifier: str
    key: str
    # Said in the response rather than only in the documentation, because the
    # client that stores this is the one that needs to know.
    shown_once: bool


class ApiKeyCreateIn(BaseModel):
    key_type: str = Field(pattern="^(publishable|secret)$")
    name: str | None = Field(default=None, max_length=100)


def _project_for(conn, project_ref: str, principal, *, manage: bool = False) -> models.Project:
    """Resolve a project the caller may act on, or 404.

    404 rather than 403 for a project in another organization, so the route
    cannot be used to discover which refs exist -- the same rule the rest of the
    API follows. Requiring management is separate and *does* answer 403, because
    by then the caller has already proved they belong here.
    """
    project = models.get_project_by_ref(conn, project_ref)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if manage:
        require_manager(principal, project.org_id)
    else:
        require_member(principal, project.org_id)
    return project


@router.get(
    "/projects/{project_ref}/api-keys",
    response_model=list[ApiKeyOut],
    summary="List a project's API keys",
)
def list_keys(project_ref: str, request: Request, principal: CurrentPrincipal) -> list[ApiKeyOut]:
    key_ring = request.app.state.key_ring
    with db.connection() as conn:
        project = _project_for(conn, project_ref, principal)
        rows = api_keys.list_for_project(conn, project_id=project.id)

        out: list[ApiKeyOut] = []
        for row in rows:
            value = None
            if row["key_type"] == api_keys.PUBLISHABLE and row["revoked_at"] is None:
                # A revoked publishable key is deliberately not returned: it is
                # of no use to a client and showing it invites pasting a dead
                # key into a bundle.
                value = api_keys.reveal_publishable(
                    conn, key_id=row["id"], project_id=project.id, key_ring=key_ring
                )
            out.append(ApiKeyOut(key=value, **row))
    return out


@router.post(
    "/projects/{project_ref}/api-keys",
    response_model=IssuedKeyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key; a secret key is shown once",
)
def create_key(
    project_ref: str, body: ApiKeyCreateIn, request: Request, principal: CurrentPrincipal
) -> IssuedKeyOut:
    """201 here, unlike creating a project: this key exists the moment it answers.

    Creating keys is a manager's privilege. A secret key is the project's data
    API without row-level security in front of it, so issuing one is closer to
    adding an owner than to reading a setting.
    """
    key_ring = request.app.state.key_ring
    with db.connection() as conn:
        project = _project_for(conn, project_ref, principal, manage=True)
        issued = api_keys.create(
            conn,
            project_id=project.id,
            key_type=body.key_type,
            pepper=request.app.state.config.token_pepper,
            key_ring=key_ring if body.key_type == api_keys.PUBLISHABLE else None,
            name=body.name,
        )
        conn.commit()

    return IssuedKeyOut(
        id=issued.id,
        key_type=issued.key_type,
        key_identifier=issued.key_identifier,
        key=issued.plaintext,
        # True for a secret key and false for a publishable one, which is a
        # statement about where the value lives rather than about how careful
        # the client should be.
        shown_once=issued.key_type == api_keys.SECRET,
    )


@router.delete(
    "/projects/{project_ref}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
def revoke_key(
    project_ref: str, key_id: uuid.UUID, principal: CurrentPrincipal
) -> None:
    """Revocation is how a key is reset: create the replacement, then revoke this.

    Deliberately two calls rather than a rotate endpoint. A rotation that
    revoked the old key at the moment it minted the new one would break every
    running client between the two deployments, and the customer who wanted
    that could always do it in this order anyway.
    """
    with db.connection() as conn:
        project = _project_for(conn, project_ref, principal, manage=True)
        revoked = api_keys.revoke(conn, key_id=key_id, project_id=project.id)
        conn.commit()
    if not revoked:
        # Already revoked, or belongs to another project. Both answer 404: a
        # distinguishable response would tell a caller which key ids exist
        # elsewhere on the platform.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key not found")
