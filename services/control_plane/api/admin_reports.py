"""The operator console's reports (ADR-082 slice 3: sales and customers, usage and abuse, nodes).

Every route needs a staff session and only reads. Response models are explicit, so a
column added to a query cannot reach a browser without someone adding it here too.

Viewing one organization writes `staff.view` to the audit trail (decision 6). Lists do
not: a list is the platform's view of itself, and one row per page load would bury the
event that matters -- a staff member opening a particular customer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.control_plane import admin_reports, db
from services.control_plane.api.admin_session import CurrentStaff

router = APIRouter(prefix="/admin/v1", tags=["admin"])

SUBSCRIPTION_STATES = ("incomplete", "trialing", "active", "past_due", "canceled", "unpaid", "paused")
EVENT_OUTCOMES = ("received", "applied", "ignored", "refused", "failed")


class PlanCount(BaseModel):
    plan_code: str
    projects: int


class StateCount(BaseModel):
    state: str
    subscriptions: int


class Overview(BaseModel):
    organizations: int
    users: int
    users_last_7_days: int
    projects: int
    projects_serving: int
    projects_failed: int
    projects_by_plan: list[PlanCount]
    subscriptions_by_state: list[StateCount]
    in_grace: int
    pending_reconciliation: int
    events_7d: int
    events_7d_unhandled: int


class Subscription(BaseModel):
    plan_code: str
    state: str
    state_since: datetime | None
    period_start: datetime | None
    period_end: datetime | None
    created_at: datetime
    provider_subscription_id: str | None
    provider_customer_id: str | None
    project_ref: str
    project_name: str
    org_id: uuid.UUID
    org_name: str


class GraceRow(BaseModel):
    plan_code: str
    state_since: datetime | None
    expires_at: datetime | None
    project_ref: str
    org_id: uuid.UUID
    org_name: str


class PendingRow(BaseModel):
    plan_code: str
    state: str
    state_as_of: datetime | None
    project_ref: str


class Sales(BaseModel):
    grace_days: int
    subscriptions: list[Subscription]
    in_grace: list[GraceRow]
    pending_reconciliation: list[PendingRow]


class BillingEvent(BaseModel):
    event_id: str
    event_type: str
    livemode: bool
    event_at: datetime | None
    received_at: datetime
    outcome: str
    note: str | None
    project_ref: str | None


class CustomerRow(BaseModel):
    id: uuid.UUID
    display_name: str
    slug: str
    is_personal: bool
    created_at: datetime
    owners: list[str]
    members: int
    projects: int
    paying_subscriptions: int


class Member(BaseModel):
    email: str
    display_name: str | None
    status: str
    role: str
    created_at: datetime
    joined_at: datetime
    last_login_at: datetime | None
    email_verified_at: datetime | None


class CustomerProject(BaseModel):
    project_ref: str
    display_name: str
    status: str
    plan_code: str
    created_at: datetime
    database_bytes: int | None
    object_bytes: int | None
    storage_state: str | None
    object_storage_state: str | None


class CustomerSubscription(BaseModel):
    plan_code: str
    state: str
    state_since: datetime | None
    period_end: datetime | None
    provider_subscription_id: str | None
    provider_customer_id: str | None
    project_ref: str


class CustomerEvent(BaseModel):
    event_type: str
    livemode: bool
    received_at: datetime
    outcome: str
    note: str | None
    project_ref: str


class Customer(BaseModel):
    id: uuid.UUID
    display_name: str
    slug: str
    is_personal: bool
    created_at: datetime
    members: list[Member]
    projects: list[CustomerProject]
    subscriptions: list[CustomerSubscription]
    billing_events: list[CustomerEvent]


@router.get("/overview", response_model=Overview, summary="Platform totals")
def overview(principal: CurrentStaff, request: Request) -> Overview:  # noqa: ARG001 - authentication
    with db.connection() as conn:
        return Overview(**admin_reports.overview(conn, grace_days=request.app.state.config.billing_grace_days))


@router.get("/sales", response_model=Sales, summary="Subscriptions, failed payments and pending changes")
def sales(
    principal: CurrentStaff,  # noqa: ARG001 - authentication
    request: Request,
    state: Annotated[str | None, Query(pattern="^(" + "|".join(SUBSCRIPTION_STATES) + ")$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Sales:
    grace_days = request.app.state.config.billing_grace_days
    with db.connection() as conn:
        return Sales(
            grace_days=grace_days,
            subscriptions=admin_reports.subscriptions(conn, state=state, limit=limit),
            in_grace=admin_reports.in_grace(conn, grace_days=grace_days),
            pending_reconciliation=admin_reports.pending_reconciliation(conn),
        )


@router.get("/billing-events", response_model=list[BillingEvent], summary="What the payment provider delivered")
def billing_events(
    principal: CurrentStaff,  # noqa: ARG001 - authentication
    outcome: Annotated[str | None, Query(pattern="^(" + "|".join(EVENT_OUTCOMES) + ")$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[BillingEvent]:
    with db.connection() as conn:
        return admin_reports.billing_events(conn, outcome=outcome, limit=limit)


@router.get("/customers", response_model=list[CustomerRow], summary="Organizations, newest first")
def customers(
    principal: CurrentStaff,  # noqa: ARG001 - authentication
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[CustomerRow]:
    with db.connection() as conn:
        rows = admin_reports.customers(conn, search=q, limit=limit)
    return [CustomerRow(**{**row, "owners": row["owners"] or []}) for row in rows]


@router.get("/customers/{org_id}", response_model=Customer, summary="One organization's platform records")
def customer(org_id: uuid.UUID, principal: CurrentStaff) -> Customer:
    with db.connection() as conn:
        found = admin_reports.customer(conn, org_id)
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="organization not found")
        admin_reports.record_view(conn, staff_id=principal.staff.id, org_id=org_id, page="customer")
        conn.commit()
    return Customer(**found)


# -- usage and abuse (slice 3b) -------------------------------------------------------

PLAN_CODE = "^[a-z0-9_-]{1,64}$"


class Meter(BaseModel):
    used: int | None
    limit: int
    # Percent of the ceiling; None when never measured. A ceiling of zero that is in use is
    # reported as `over_zero_ceiling` rather than as an infinite percentage JSON cannot carry.
    percent: float | None
    over_zero_ceiling: bool
    state: str | None
    measured_at: datetime | None


class UsageRow(BaseModel):
    project_ref: str
    display_name: str
    status: str
    plan_code: str
    org_id: uuid.UUID
    org_name: str
    account_age_days: int
    database: Meter
    objects: Meter
    egress: Meter
    email_day: Meter
    peak_percent: float | None
    peak_meter: str | None


def _meter(raw: dict) -> Meter:
    ratio = raw["ratio"]
    infinite = ratio == float("inf")
    return Meter(used=raw["used"], limit=raw["limit"], percent=None if ratio is None or infinite else
                 round(ratio * 100, 1), over_zero_ceiling=infinite, state=raw["state"],
                 measured_at=raw["measured_at"])


def _usage_row(project: admin_reports.ProjectUsage) -> UsageRow:
    peak = project.peak
    return UsageRow(
        project_ref=project.project_ref, display_name=project.display_name, status=project.status,
        plan_code=project.plan_code, org_id=project.org_id, org_name=project.org_name,
        account_age_days=project.account_age_days, database=_meter(project.database),
        objects=_meter(project.objects), egress=_meter(project.egress), email_day=_meter(project.email_day),
        peak_percent=None if peak == float("inf") else round(peak * 100, 1), peak_meter=project.peak_meter,
    )


@router.get("/usage", response_model=list[UsageRow], summary="Every project against its plan's ceilings")
def usage(
    principal: CurrentStaff,  # noqa: ARG001 - authentication
    plan: Annotated[str | None, Query(pattern=PLAN_CODE)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[UsageRow]:
    with db.connection() as conn:
        projects = admin_reports.project_usage(conn, plan_code=plan)
    return [_usage_row(p) for p in projects[:limit]]


@router.get("/abuse", response_model=list[UsageRow],
            summary="Projects on one plan pressing on their ceilings, youngest accounts first among equals")
def abuse(
    principal: CurrentStaff,  # noqa: ARG001 - authentication
    plan: Annotated[str, Query(pattern=PLAN_CODE)] = "free",
    min_percent: Annotated[float, Query(ge=0, le=1000)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[UsageRow]:
    """`cp-manage abuse report` as a page. Reports, never acts: suspending a project has a name on it."""
    with db.connection() as conn:
        projects = admin_reports.project_usage(conn, plan_code=plan)
    pressed = [p for p in projects if p.peak * 100 >= min_percent]
    return [_usage_row(p) for p in pressed[:limit]]


# -- nodes and provisioning (slice 3c) ------------------------------------------------


class NodeRow(BaseModel):
    name: str
    node_pool: str
    status: str
    created_at: datetime
    last_health_at: datetime | None
    health_stale: bool
    projects: int
    max_projects: int
    warm_projects: int
    max_warm_projects: int
    projected_connections: int
    usable_connections: int
    committed_slots: int
    usable_replication_slots: int
    free_disk_bytes: int | None
    min_free_disk_bytes: int
    realtime_ready: bool
    backup_ready: bool
    extension_refusal: str | None
    accepting: bool
    refusal: str | None


class ProvisioningRow(BaseModel):
    project_ref: str
    display_name: str
    status: str
    plan_code: str
    org_id: uuid.UUID
    org_name: str
    node_name: str | None
    requested_at: datetime | None
    created_at: datetime
    failed_at: datetime | None
    retry_after: datetime | None
    attempt: int | None
    error_code: str | None
    job_state: str | None
    job_updated_at: datetime | None


class Provisioning(BaseModel):
    failed: list[ProvisioningRow]
    stuck: list[ProvisioningRow]
    stuck_after_minutes: int


@router.get("/nodes", response_model=list[NodeRow], summary="Node health and capacity, as placement sees it")
def nodes(principal: CurrentStaff) -> list[NodeRow]:  # noqa: ARG001 - authentication
    with db.connection() as conn:
        return admin_reports.node_report(conn)


@router.get("/provisioning", response_model=Provisioning,
            summary="Projects that failed, are waiting to retry, or are stuck in setup")
def provisioning(principal: CurrentStaff) -> Provisioning:  # noqa: ARG001 - authentication
    """Reports, never retries: `cp-manage project retry` is the action, with a name on it."""
    with db.connection() as conn:
        return Provisioning(**admin_reports.provisioning_problems(conn))
