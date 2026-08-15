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


@router.get("/plans", response_model=list[PlanOut], summary="List active plans and their limits")
def list_plans(principal: CurrentPrincipal) -> list[PlanOut]:
    with db.connection() as conn:
        return [
            PlanOut(code=p.code, name=p.name, limits=p.config.get("limits", {}))
            for p in models.list_plans(conn, active_only=True)
        ]
