"""The operator console's sales and customer reports (ADR-082 slice 3a).

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
