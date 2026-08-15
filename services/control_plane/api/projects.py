"""Project read endpoints.

Phase 01 is read-only by design: creating a project means provisioning a
tenant database, which is Phase 02 and explicitly a non-goal here
(tasks/PHASE-01-FOUNDATION.md).

Authentication is not wired up yet -- sessions and personal access tokens land
in slice 2. Until then these endpoints must not be exposed outside a
development environment. The scheme itself is already specified in
specs/control-plane-api.yaml and docs/ACCOUNTS.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from services.control_plane import db, models

router = APIRouter(prefix="/v1", tags=["projects"])


class ProjectOut(BaseModel):
    project_ref: str
    display_name: str
    status: str
    created_at: datetime
    # Deliberately omits node_id and database_name. docs/API-GATEWAY.md
    # forbids exposing internal node and database names to clients.


def _to_out(project: models.Project) -> ProjectOut:
    return ProjectOut(
        project_ref=project.project_ref,
        display_name=project.display_name,
        status=project.status,
        created_at=project.created_at,
    )


@router.get(
    "/organizations/{org_id}/projects",
    response_model=list[ProjectOut],
    summary="List an organization's projects",
)
def list_projects(org_id: uuid.UUID) -> list[ProjectOut]:
    with db.connection() as conn:
        return [_to_out(p) for p in models.list_projects_for_org(conn, org_id)]


@router.get("/projects/{project_ref}", response_model=ProjectOut, summary="Get a project by reference")
def get_project(project_ref: str) -> ProjectOut:
    # Treat the ref as untrusted input; reject malformed values before lookup.
    if not models.is_valid_project_ref(project_ref):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, project_ref)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return _to_out(project)
