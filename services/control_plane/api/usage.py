"""What a project has used, and asking for a bigger plan.

Phase 07 slice 3. Two routes with one rule between them: **this reports, it does
not grant.** Entitlements come from the plan (`entitlements.resolve`), and the
only thing an upgrade request changes is a row in a queue an operator works.
Phase 09 owns payment; a route here that moved a project onto a paid plan would
be granting paid capacity to a project nobody had billed.

**Consumption is reported where the platform records it, and named as absent
where it does not.** Storage is measured by the Phase 05 maintenance pass and
carries the time it was measured. Email sent today is counted from
`email_events`. Requests and connections are *not* metered: the gateway's
limiters are in-process by ADR-030 and nothing persists a counter, and reading
live connections would mean querying the node, which ADR-038 keeps out of this
process. So those report their limit with `metered: false` rather than a zero,
because a zero is a claim about usage and this platform does not have one to
make.

The distinction runs through the responses: `null` means *unknown*, not *none*.
A project measured five minutes ago and a project never measured at all are
different situations, and a dashboard that showed both as "0 bytes used" would
be telling a customer something nobody knows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from services.control_plane import db, entitlements, mail, models
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager

router = APIRouter(prefix="/v1", tags=["usage"])


class StorageUsage(BaseModel):
    used_bytes: int | None
    limit_bytes: int
    # When the number above was true. Null with a null `used_bytes` means the
    # project has never been measured -- a new project, or a maintenance pass
    # that is not running, and the second is worth being able to see.
    measured_at: datetime | None
    # ok | warning | restricted, from Phase 05's accounting. A customer whose
    # writes are being refused should be able to learn that here rather than
    # from their application's error log.
    state: str


class CountedUsage(BaseModel):
    """A quota the platform actually counts."""

    used: int
    limit: int


class UnmeteredLimit(BaseModel):
    """A limit that is enforced but whose consumption is not recorded.

    `metered` is false and `used` is null, deliberately and visibly. The limit
    is real -- the gateway enforces it per process -- but nothing accumulates a
    figure this API could report, and inventing a zero would be worse than
    saying so.
    """

    limit: int
    window_seconds: int | None = None
    used: None = None
    metered: bool = False


class RealtimeUsage(BaseModel):
    enabled: bool
    connection_limit: int
    # Two per enabled project (ADR-034), and zero otherwise. Slots are a node
    # resource a customer cannot see, so what is reported is what their own
    # project holds.
    replication_slots_held: int


class UsageOut(BaseModel):
    project_ref: str
    plan_code: str
    storage: StorageUsage
    email: CountedUsage
    realtime: RealtimeUsage
    api_requests: UnmeteredLimit
    database_connections: UnmeteredLimit


class UpgradeRequestIn(BaseModel):
    requested_plan_code: str = Field(min_length=1, max_length=50)


class UpgradeRequestOut(BaseModel):
    id: uuid.UUID
    requested_plan_code: str
    requested_at: datetime
    state: str


def _project_for(conn, project_ref: str, principal, *, manage: bool = False) -> models.Project:
    project = models.get_project_by_ref(conn, project_ref)
    # Does-not-exist and not-a-member are the *same* answer, body included.
    # Splitting them is an oracle: a project ref is the customer's API subdomain
    # (ADR-008), so confirming one is real confirms a target. The first version
    # of this helper raised "project not found" here and let `require_member`
    # raise "organization not found" below, which is a uniform status code with
    # a distinguishable body -- and the test asserting 404 passed either way.
    if project is None or not principal.is_member_of(project.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if manage:
        # Only reached by someone who has already proved they belong here, so
        # 403 discloses nothing they did not already know.
        require_manager(principal, project.org_id)
    return project


@router.get(
    "/projects/{project_ref}/usage",
    response_model=UsageOut,
    summary="What a project has used against what its plan allows",
)
def get_usage(project_ref: str, principal: CurrentPrincipal) -> UsageOut:
    with db.connection() as conn:
        project = _project_for(conn, project_ref, principal)
        row = db.one(
            conn,
            """
            SELECT pr.database_bytes, pr.database_measured_at, pr.storage_state,
                   pr.realtime_enabled, pl.code AS plan_code, pl.config_json
              FROM projects pr LEFT JOIN plans pl ON pl.id = pr.plan_id
             WHERE pr.id = %s
            """,
            (project.id,),
        )
        allowed = entitlements.resolve(row["plan_code"], row["config_json"])
        emails_today = mail.sent_today(conn, project.id)

    return UsageOut(
        project_ref=project.project_ref,
        plan_code=allowed.plan_code,
        storage=StorageUsage(
            used_bytes=row["database_bytes"],
            limit_bytes=allowed.database_storage_bytes,
            measured_at=row["database_measured_at"],
            state=row["storage_state"],
        ),
        email=CountedUsage(used=emails_today, limit=allowed.emails_per_day),
        realtime=RealtimeUsage(
            enabled=bool(row["realtime_enabled"]),
            connection_limit=allowed.realtime_connections,
            # ADR-034: one slot on wal2json for Postgres Changes and one on
            # pgoutput for broadcast, created by the server rather than by us.
            replication_slots_held=2 if row["realtime_enabled"] else 0,
        ),
        api_requests=UnmeteredLimit(
            limit=allowed.api_requests_per_window, window_seconds=allowed.api_window_seconds
        ),
        database_connections=UnmeteredLimit(limit=allowed.database_connections),
    )


@router.post(
    "/projects/{project_ref}/upgrade-request",
    response_model=UpgradeRequestOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask to move this project to another plan",
)
def request_upgrade(
    project_ref: str,
    body: UpgradeRequestIn,
    request: Request,  # noqa: ARG001 - kept for symmetry with the other routes
    principal: CurrentPrincipal,
) -> UpgradeRequestOut:
    """202, and the project's entitlements are exactly what they were.

    Asking is a manager's action: it is the beginning of committing the
    organization to money, even though nothing here takes any. Pressing the
    button twice returns the existing open request rather than queueing a second
    one -- the customer is asking the same question, and two rows would have an
    operator answer it twice.
    """
    with db.connection() as conn:
        project = _project_for(conn, project_ref, principal, manage=True)

        plan = models.plan_by_code(conn, body.requested_plan_code)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown plan")

        existing = db.one(
            conn,
            "SELECT id, requested_plan_code, requested_at, state FROM upgrade_requests "
            " WHERE project_id = %s AND state <> 'CLOSED'",
            (project.id,),
        )
        if existing is not None:
            return UpgradeRequestOut(**existing)

        request_id = uuid.uuid4()
        db.execute(
            conn,
            "INSERT INTO upgrade_requests "
            "  (id, project_id, requested_plan_code, requested_by) "
            "VALUES (%s, %s, %s, %s)",
            (request_id, project.id, plan.code, principal.user.id),
        )
        created = db.one(
            conn,
            "SELECT id, requested_plan_code, requested_at, state FROM upgrade_requests "
            " WHERE id = %s",
            (request_id,),
        )
        conn.commit()

    return UpgradeRequestOut(**created)


@router.get(
    "/projects/{project_ref}/upgrade-request",
    response_model=UpgradeRequestOut | None,
    summary="The project's open upgrade request, if it has one",
)
def get_upgrade_request(project_ref: str, principal: CurrentPrincipal) -> UpgradeRequestOut | None:
    """So a dashboard can say "we have your request" rather than offering the
    button again to someone who already pressed it."""
    with db.connection() as conn:
        project = _project_for(conn, project_ref, principal)
        row = db.one(
            conn,
            "SELECT id, requested_plan_code, requested_at, state FROM upgrade_requests "
            " WHERE project_id = %s AND state <> 'CLOSED'",
            (project.id,),
        )
    # The operator's note is deliberately not selected: it is written for
    # whoever works the queue, and a note written in the belief that it is
    # private should not become a support reply.
    return UpgradeRequestOut(**row) if row else None
