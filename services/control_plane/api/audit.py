"""What happened to a project, as far as its customer is concerned.

Phase 07 slice 5. `audit_events` has existed since Phase 01 and nothing has ever
shown it to a customer. The events worth showing are the ones that explain
something the customer can otherwise only observe as breakage: storage
restricted so writes are refused, a replication slot invalidated so Postgres
Changes stopped arriving, Realtime enabled or disabled.

**An allowlist, not a filter.** Two things are chosen explicitly here: which
event types a customer may see, and which keys of each event's `detail_json`
they may see. Returning the row and redacting what looks sensitive is the wrong
way round -- `detail_json` is a free-form column written by several subsystems,
and the next one to write a node hostname or an internal error string into it
would publish it to customers without anybody deciding to. An event type nobody
has classified is invisible here, which is the failure direction that costs a
support question rather than a disclosure.

Actor identity is deliberately coarse. A customer sees that the *platform* acted
or that a *member of their organization* did, and which member, but never an
operator's identity or an internal actor id: who at MaluDB touched a project is
not a customer's business, and naming them invites the sort of pressure nobody
should be under.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from services.control_plane import db, models
from services.control_plane.api.auth_dep import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["audit"])

# Event type -> the detail keys a customer may see, and a plain description.
#
# Everything here is something the customer experiences and would otherwise have
# to guess at. Notably absent: anything about placement, nodes, capacity or
# provisioning internals, which explain the platform's own decisions rather than
# the customer's project.
VISIBLE_EVENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "storage.warning": (
        "The project is approaching its storage limit.",
        ("gross_bytes", "billable_bytes", "quota_bytes", "fraction"),
    ),
    "storage.restricted": (
        "Writes were restricted because the project reached its storage limit.",
        ("gross_bytes", "billable_bytes", "quota_bytes", "fraction"),
    ),
    "storage.released": (
        "Writes were restored after the project returned below its storage limit.",
        ("gross_bytes", "billable_bytes", "quota_bytes", "fraction"),
    ),
    # ADR-039. The statement text is shown back because this is the customer's
    # own SQL against their own database, and "who changed this schema" is the
    # question a schema surface makes worth answering. Result rows are never
    # recorded, so there is nothing tenant-data-shaped to leak here.
    # `requested_role` and `claim_keys` appear only on an impersonated
    # statement (slice 3). `claim_keys` is deliberately the keys rather than the
    # claims: the values are the customer's end users' identities. And
    # `requested_role` is what was asked for, not an observation of what ran --
    # a statement can change its own role, so a trail that promised the latter
    # would be promising something nothing measured.
    "project.sql.executed": (
        "SQL was run against this project from the dashboard.",
        ("statement", "statement_truncated", "commands", "storage_restricted", "statement_id",
         "requested_role", "claim_keys"),
    ),
    "project.sql.failed": (
        "SQL run against this project failed.",
        ("statement", "statement_truncated", "error", "statement_id", "requested_role",
         "claim_keys"),
    ),
    "realtime.enabled": ("Realtime was enabled for this project.", ()),
    "realtime.disabled": ("Realtime was disabled for this project.", ()),
    "realtime.slot_invalidated": (
        "Realtime stopped receiving changes because its replication slot was invalidated.",
        # `replayed_on_recovery` is the one a customer most needs: recovery
        # resumes from the present, and a report that omits it leaves them
        # assuming a backfill that never happened.
        ("reason", "replayed_on_recovery"),
    ),
    "realtime.slot_missing": (
        "A replication slot this project had is no longer on its node.",
        ("reason", "replayed_on_recovery"),
    ),
    "realtime.slot_restored": (
        "Realtime replication was restored; changes during the gap were not replayed.",
        ("reason", "replayed_on_recovery"),
    ),
}

MAX_PAGE = 100


class AuditEventOut(BaseModel):
    id: int
    event_type: str
    description: str
    # "platform" or "member". Never an operator's identity.
    actor: str
    actor_user_id: uuid.UUID | None
    detail: dict[str, Any]
    created_at: datetime


@router.get(
    "/projects/{project_ref}/audit-events",
    response_model=list[AuditEventOut],
    summary="What has happened to this project",
)
def list_audit_events(
    project_ref: str,
    principal: CurrentPrincipal,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    before_id: int | None = Query(default=None, ge=1),
) -> list[AuditEventOut]:
    """Newest first, keyset-paginated by id.

    Membership is enough to read this: an audit trail a member cannot see is one
    that explains an outage to nobody. Nothing here is a credential and nothing
    here identifies another tenant.
    """
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, project_ref)
        # Does-not-exist and not-a-member are the same answer, body included --
        # the rule the security review found broken on two other routers.
        if project is None or not principal.is_member_of(project.org_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
            )

        rows = db.query(
            conn,
            """
            SELECT id, event_type, actor_type, actor_user_id, detail_json, created_at
              FROM audit_events
             WHERE project_id = %s
               AND event_type = ANY(%s)
               AND (%s::bigint IS NULL OR id < %s::bigint)
             ORDER BY id DESC
             LIMIT %s
            """,
            (project.id, list(VISIBLE_EVENTS), before_id, before_id, limit),
        )

    out: list[AuditEventOut] = []
    for row in rows:
        description, keys = VISIBLE_EVENTS[row["event_type"]]
        detail = row["detail_json"] or {}
        out.append(
            AuditEventOut(
                id=row["id"],
                event_type=row["event_type"],
                description=description,
                # Coarse on purpose: which operator acted is not a customer's
                # business, and naming them invites pressure nobody should be
                # under. A member of their own organization is named, because
                # "who on my team did this" is the question they actually have.
                actor="member" if row["actor_type"] == "user" else "platform",
                actor_user_id=row["actor_user_id"] if row["actor_type"] == "user" else None,
                # Projected key by key. `detail_json` is free-form and written
                # by several subsystems; returning it whole would publish
                # whatever the next writer happens to put in it.
                detail={key: detail[key] for key in keys if key in detail},
                created_at=row["created_at"],
            )
        )
    return out
