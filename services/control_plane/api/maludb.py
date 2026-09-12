"""A customer turns on, refreshes and checks the MaluDB data-model graph (ADR-074).

Public under ADR-037. Phase 12 slice 4.

**These routes queue work; they never do it.** Enabling and refreshing run as the
node superuser, and ADR-038 keeps that credential out of this application --
enforced by `tests/test_control_plane_surfaces.py`, which walks what these
routes can import. So a request writes a row in `maludb_jobs` and answers `202`,
and the provisioner does the work. The status route is how a client learns it
finished.

What bounds them, in the order they are checked:

- **Membership.** A non-member gets `404` before anything reveals whether the
  project exists, the rule `database.py` states for why: a project ref is the
  customer's API hostname, so confirming one confirms a target.
- **Role.** Enabling needs a manager: it turns a feature on and publishes a copy
  of every table's structure to `service_role`. Refreshing needs only
  membership: it is bounded by the plan's limit, and is far less than the SQL
  console every member already has.
- **The plan.** The entitlement, and for refresh, `datamodel_refreshes_per_hour`
  -- refused at the request, as a `429` naming the limit, with `Retry-After`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from services.control_plane import db, maludb_jobs, models
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager

router = APIRouter(prefix="/v1", tags=["maludb"])


class JobOut(BaseModel):
    id: int
    kind: str
    state: str
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # A sentence meant for the customer when the platform refused, or a generic
    # one when something unexpected failed. Never a node error's own text.
    detail: str | None = None
    result: dict = {}


class QueuedOut(BaseModel):
    job: JobOut | None
    # True when this request joined a pending one rather than queueing its own.
    coalesced: bool = False
    message: str


class DatamodelStatusOut(BaseModel):
    entitled: bool
    enabled: bool
    enabled_at: datetime | None
    memory_schema_version: str | None
    refreshes_per_hour: int
    refreshes_in_last_hour: int
    latest_enable: JobOut | None
    latest_refresh: JobOut | None


def _job(row: dict | None) -> JobOut | None:
    if row is None:
        return None
    return JobOut(
        id=row["id"], kind=row["kind"], state=row["state"], requested_at=row["requested_at"],
        started_at=row.get("started_at"), completed_at=row.get("completed_at"),
        detail=row.get("detail"), result=row.get("result_json") or {},
    )


def _member_project(conn, project_ref: str, principal):
    project = models.get_project_by_ref(conn, project_ref)
    if project is None or not principal.is_member_of(project.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


def _refused(exc: maludb_jobs.JobRefused) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(status_code=exc.status, detail=str(exc), headers=headers)


def _queued_out(queued: maludb_jobs.Queued, what: str) -> QueuedOut:
    return QueuedOut(
        job=JobOut(id=queued.job_id, kind=queued.kind, state=queued.state,
                   requested_at=queued.requested_at),
        coalesced=queued.coalesced,
        message=(f"joined the {what} already waiting" if queued.coalesced
                 else f"{what} queued"),
    )


@router.post(
    "/projects/{project_ref}/maludb/datamodel/enable",
    response_model=QueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Turn on the MaluDB data-model graph for a project",
    responses={200: {"model": QueuedOut, "description": "Already enabled; nothing queued"}},
)
def enable_datamodel(
    project_ref: str, response: Response, principal: CurrentPrincipal
) -> QueuedOut:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            queued = maludb_jobs.request_enable(
                conn, project_id=project.id, requested_by=principal.user.id
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    if queued is None:
        response.status_code = status.HTTP_200_OK
        return QueuedOut(job=None, message="the data-model graph is already enabled")
    return _queued_out(queued, "enablement")


@router.post(
    "/projects/{project_ref}/maludb/datamodel/refresh",
    response_model=QueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Refresh a project's MaluDB data-model graph",
)
def refresh_datamodel(project_ref: str, principal: CurrentPrincipal) -> QueuedOut:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        try:
            queued = maludb_jobs.request_refresh(
                conn, project_id=project.id, requested_by=principal.user.id
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    return _queued_out(queued, "refresh")


@router.get(
    "/projects/{project_ref}/maludb/datamodel",
    response_model=DatamodelStatusOut,
    summary="Whether the MaluDB data-model graph is on, and what was last asked of it",
)
def datamodel_status(project_ref: str, principal: CurrentPrincipal) -> DatamodelStatusOut:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        state = maludb_jobs.status(conn, project_id=project.id)
    return DatamodelStatusOut(
        **{k: v for k, v in state.items() if k not in ("latest_enable", "latest_refresh")},
        latest_enable=_job(state["latest_enable"]),
        latest_refresh=_job(state["latest_refresh"]),
    )
