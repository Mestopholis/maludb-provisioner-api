"""Liveness and readiness.

Readiness checks the database because a control plane that cannot reach its
own database cannot provision. It reports no version or internal detail --
docs/API-GATEWAY.md forbids exposing internal node and database names to
clients, and the same reasoning applies to an unauthenticated probe.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from services.control_plane import db

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str


@router.get("/healthz", response_model=Health, summary="Liveness probe")
def healthz() -> Health:
    return Health(status="ok")


@router.get("/readyz", response_model=Health, summary="Readiness probe")
def readyz(response: Response) -> Health:
    try:
        with db.connection() as conn:
            db.one(conn, "SELECT 1 AS ok")
    except Exception:  # noqa: BLE001 - any failure means not ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="unavailable")
    return Health(status="ready")
