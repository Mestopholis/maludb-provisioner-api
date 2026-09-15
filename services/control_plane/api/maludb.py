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

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, SecretStr

from services.control_plane import db, maludb_jobs, models, provider_keys
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
    latest_disable: JobOut | None = None


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


@router.post(
    "/projects/{project_ref}/maludb/datamodel/disable",
    response_model=QueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Turn off the MaluDB data-model graph for a project",
    responses={200: {"model": QueuedOut, "description": "Already off; nothing queued"}},
)
def disable_datamodel(
    project_ref: str, response: Response, principal: CurrentPrincipal
) -> QueuedOut:
    """Withdraw the graph from the project's Data API. Nothing is dropped.

    Manager-only, like enabling, because it changes what the project publishes.
    Not gated on the plan: a project that has lost the entitlement must still be
    able to switch the feature off.
    """
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            queued = maludb_jobs.request_disable(
                conn, project_id=project.id, requested_by=principal.user.id
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    if queued is None:
        response.status_code = status.HTTP_200_OK
        return QueuedOut(job=None, message="the data-model graph is already off")
    return _queued_out(queued, "disablement")


class VectorsStatusOut(BaseModel):
    entitled: bool
    enabled: bool
    enabled_at: datetime | None
    # The plan's limits (ADR-077 decision 4). How many vectors are stored is the
    # project's own `maludb.vector_compartments()`, from its Data API.
    max_vectors: int
    max_dimensions: int
    max_compartments: int
    latest_enable: JobOut | None
    latest_disable: JobOut | None


@router.post(
    "/projects/{project_ref}/maludb/vectors/enable",
    response_model=QueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Turn on MaluDB vector compartments for a project",
    responses={200: {"model": QueuedOut, "description": "Already enabled; nothing queued"}},
)
def enable_vectors(project_ref: str, response: Response, principal: CurrentPrincipal) -> QueuedOut:
    """Queue ADR-077's opt-in. Manager-only: it publishes wrappers on the project's
    Data API. Draws on the plan's per-hour MaluDB request budget."""
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            queued = maludb_jobs.request_vectors_enable(
                conn, project_id=project.id, requested_by=principal.user.id
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    if queued is None:
        response.status_code = status.HTTP_200_OK
        return QueuedOut(job=None, message="vector compartments are already enabled")
    return _queued_out(queued, "enablement")


@router.post(
    "/projects/{project_ref}/maludb/vectors/disable",
    response_model=QueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Turn off MaluDB vector compartments for a project",
    responses={200: {"model": QueuedOut, "description": "Already off; nothing queued"}},
)
def disable_vectors(project_ref: str, response: Response, principal: CurrentPrincipal) -> QueuedOut:
    """Withdraw the wrappers from the Data API. Nothing is dropped, and no stored
    vector is lost. Not gated on the plan, for `disable_datamodel`'s reason."""
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            queued = maludb_jobs.request_vectors_disable(
                conn, project_id=project.id, requested_by=principal.user.id
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    if queued is None:
        response.status_code = status.HTTP_200_OK
        return QueuedOut(job=None, message="vector compartments are already off")
    return _queued_out(queued, "disablement")


@router.get(
    "/projects/{project_ref}/maludb/vectors",
    response_model=VectorsStatusOut,
    summary="Whether MaluDB vector compartments are on, the plan's limits, and what was last asked",
)
def vectors_status(project_ref: str, principal: CurrentPrincipal) -> VectorsStatusOut:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        state = maludb_jobs.vectors_status(conn, project_id=project.id)
    return VectorsStatusOut(
        **{k: v for k, v in state.items() if k not in ("latest_enable", "latest_disable")},
        latest_enable=_job(state["latest_enable"]),
        latest_disable=_job(state["latest_disable"]),
    )


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
        **{k: v for k, v in state.items()
           if k not in ("latest_enable", "latest_refresh", "latest_disable")},
        latest_enable=_job(state["latest_enable"]),
        latest_refresh=_job(state["latest_refresh"]),
        latest_disable=_job(state["latest_disable"]),
    )


# --------------------------------------------------------------------------
# Memory spaces (ADR-079, memory slice 2a)


class MemorySpaceIn(BaseModel):
    # Validated in `maludb_jobs.space_schema`, which answers 422 in words; a
    # pattern here would answer Pydantic's generic message first.
    name: str


class MemorySpaceOut(BaseModel):
    name: str
    state: str
    requested_at: datetime
    active_at: datetime | None = None
    memory_schema_version: str | None = None
    detail: str | None = None
    extraction_provider: str | None = None
    extraction_model: str | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    item_count: int = 0


class MemoryModelsIn(BaseModel):
    """Checked in `maludb_jobs.set_memory_models`, which answers 422 in words."""

    extraction_provider: str
    extraction_model: str | None = None
    embedding_provider: str
    embedding_model: str | None = None


class MemoryModelsOut(BaseModel):
    name: str
    extraction_provider: str
    extraction_model: str
    embedding_provider: str
    embedding_model: str


class MemorySpaceQueuedOut(BaseModel):
    space: MemorySpaceOut
    job: JobOut | None
    coalesced: bool = False
    message: str


class MemorySpacesOut(BaseModel):
    entitled: bool
    max_spaces: int
    max_items: int
    ingests_per_hour: int
    spaces: list[MemorySpaceOut]


def _space_out(row: dict) -> MemorySpaceOut:
    # The schema name is the platform's, not the customer's, and not returned.
    return MemorySpaceOut(**{k: row[k] for k in MemorySpaceOut.model_fields if k in row})


@router.post(
    "/projects/{project_ref}/maludb/memory/spaces",
    response_model=MemorySpaceQueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a named MaluDB memory space for a project",
    responses={200: {"model": MemorySpaceQueuedOut, "description": "The space already exists; nothing queued"}},
)
def create_memory_space(
    project_ref: str, body: MemorySpaceIn, response: Response, principal: CurrentPrincipal
) -> MemorySpaceQueuedOut:
    """Reserve the name and queue the build (ADR-079 decision 1). Manager-only: a
    space holds one of the plan's `memory_max_spaces` for as long as it exists."""
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            space, queued = maludb_jobs.request_memory_space(
                conn, project_id=project.id, name=body.name, requested_by=principal.user.id
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    if queued is None:
        response.status_code = status.HTTP_200_OK
        return MemorySpaceQueuedOut(space=_space_out(space), job=None, message="the memory space already exists")
    out = _queued_out(queued, "memory space build")
    return MemorySpaceQueuedOut(space=_space_out(space), job=out.job, coalesced=out.coalesced, message=out.message)


@router.get(
    "/projects/{project_ref}/maludb/memory/spaces",
    response_model=MemorySpacesOut,
    summary="A project's MaluDB memory spaces and the plan's memory limits",
)
def list_memory_spaces(project_ref: str, principal: CurrentPrincipal) -> MemorySpacesOut:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        state = maludb_jobs.memory_spaces(conn, project_id=project.id)
    return MemorySpacesOut(
        **{k: v for k, v in state.items() if k != "spaces"},
        spaces=[_space_out(row) for row in state["spaces"]],
    )


@router.put(
    "/projects/{project_ref}/maludb/memory/spaces/{name}/models",
    response_model=MemoryModelsOut,
    summary="Set the models a memory space extracts and embeds text with",
)
def set_memory_models(
    project_ref: str, name: str, body: MemoryModelsIn, principal: CurrentPrincipal
) -> MemoryModelsOut:
    """Extraction through `anthropic` or `openai`, embeddings through `openai` or
    `voyage`, each with an optional model name (a default otherwise), called with the
    project's own provider keys. There is no endpoint to set: the hosts are fixed.
    The embedding model cannot change once the space holds memories. Manager-only:
    the models spend the organization's money at the provider."""
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            row = maludb_jobs.set_memory_models(
                conn, project_id=project.id, name=name, extraction_provider=body.extraction_provider,
                extraction_model=body.extraction_model, embedding_provider=body.embedding_provider,
                embedding_model=body.embedding_model, actor_user_id=principal.user.id,
            )
        except maludb_jobs.JobRefused as exc:
            conn.rollback()
            raise _refused(exc) from None
        conn.commit()
    return MemoryModelsOut(**row)


# --------------------------------------------------------------------------
# Provider API keys (ADR-079 decisions 4 and 5, memory slice 4)


class ProviderKeyIn(BaseModel):
    # SecretStr so the value is masked in any repr, validation error or log line
    # FastAPI or Pydantic might produce.
    api_key: SecretStr


class ProviderKeyOut(BaseModel):
    provider: str
    hint: str
    created_at: datetime


class ProviderKeysOut(BaseModel):
    providers: list[str]
    keys: list[ProviderKeyOut]


@router.put(
    "/projects/{project_ref}/maludb/memory/provider-keys/{provider}",
    response_model=ProviderKeyOut,
    summary="Set this project's API key for a model provider (write-only)",
)
def set_provider_key(
    project_ref: str, provider: str, body: ProviderKeyIn, request: Request, principal: CurrentPrincipal
) -> ProviderKeyOut:
    """Seal and store the key; the previous one for this provider is revoked. The key
    is never returned by any route. Manager-only: the key spends the organization's
    money at the provider."""
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            info = provider_keys.set_key(
                conn, project_id=project.id, provider=provider, api_key=body.api_key.get_secret_value(),
                key_ring=request.app.state.key_ring, actor_user_id=principal.user.id,
            )
        except provider_keys.ProviderKeyError as exc:
            conn.rollback()
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        conn.commit()
    return ProviderKeyOut(provider=info.provider, hint=info.hint, created_at=info.created_at)


@router.get(
    "/projects/{project_ref}/maludb/memory/provider-keys",
    response_model=ProviderKeysOut,
    summary="Which model providers have a key set for this project (never the keys)",
)
def list_provider_keys(project_ref: str, principal: CurrentPrincipal) -> ProviderKeysOut:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        keys = provider_keys.list_keys(conn, project_id=project.id)
    return ProviderKeysOut(
        providers=list(provider_keys.PROVIDERS),
        keys=[ProviderKeyOut(provider=k.provider, hint=k.hint, created_at=k.created_at) for k in keys],
    )


@router.delete(
    "/projects/{project_ref}/maludb/memory/provider-keys/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove this project's API key for a model provider",
)
def remove_provider_key(project_ref: str, provider: str, principal: CurrentPrincipal) -> Response:
    with db.connection() as conn:
        project = _member_project(conn, project_ref, principal)
        require_manager(principal, project.org_id)
        try:
            removed = provider_keys.remove_key(conn, project_id=project.id, provider=provider,
                                               actor_user_id=principal.user.id)
        except provider_keys.ProviderKeyError as exc:
            conn.rollback()
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        conn.commit()
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no key is set for that provider")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
