"""Plan catalogue.

Plan limits are configuration/entitlement data, never hard-coded logic
(docs/BILLING-AND-PLANS.md). This endpoint reads them; it does not interpret
them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from services.control_plane import db, models
from services.control_plane.api.auth_dep import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["plans"])


class PlanOut(BaseModel):
    code: str
    name: str
    limits: dict[str, Any]
    # Whether a customer may put a new project on this plan without an operator.
    # `POST /v1/organizations/{id}/projects` accepts the default plan and nothing
    # else -- naming a paid one used to grant its entitlements to an unbilled
    # project, and the refusal is deliberately the same 404 an unknown code gets,
    # so it is not a probe for which plans exist and which are merely forbidden.
    #
    # The console had no way to know that and offered the whole catalogue: three
    # options, two of which could only ever answer "unknown plan". Saying it here
    # is this endpoint's job -- it is the entitlement catalogue (ADR-037), so
    # "may this caller choose it" belongs beside "what does it grant" -- and it
    # keeps the one rule in the control plane rather than copied into a page as
    # the string "free".
    self_serve: bool = False


@router.get("/plans", response_model=list[PlanOut], summary="List active plans and their limits")
def list_plans(principal: CurrentPrincipal) -> list[PlanOut]:
    with db.connection() as conn:
        default = models.default_plan(conn)
        return [
            PlanOut(
                code=p.code,
                name=p.name,
                limits=p.config.get("limits", {}),
                self_serve=default is not None and p.code == default.code,
            )
            for p in models.list_plans(conn, active_only=True)
        ]
