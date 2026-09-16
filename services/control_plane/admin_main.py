"""The operator console's application (ADR-082).

A third application on a third listener. The public application faces customers; the
internal one serves node-to-control-plane traffic; this one serves platform staff, bound
to a private interface and reached over the operator VPN.

Three properties, each asserted in `tests/test_admin_app.py` rather than trusted:

- **it serves exactly `ADMIN_ROUTERS`**, and neither of the other applications mounts
  any of them;
- **it holds no KEK and no platform pepper** -- `config.load_admin` reads neither, and
  the lifespan builds a `StaffKey`, never a `KeyRing`;
- **its import graph reaches no node credential and no provisioning work**, as ADR-038
  requires of the public application.

Network position is defence in depth: every route but health requires a staff session.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from services.control_plane import config as config_module
from services.control_plane import db, ratelimit, staff
from services.control_plane import logging as cp_logging
from services.control_plane.api import admin_session, health

log = logging.getLogger(__name__)

# The console's routes, and the only place they are mounted. Health so the listener can
# be probed like the other two; every other route requires a staff session.
ADMIN_ROUTERS = (
    health.router,
    admin_session.router,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: config_module.AdminConfig = app.state.config
    db.init_pool(cfg.database_url)
    log.info(
        "operator console started",
        extra={"extra_fields": {"environment": cfg.environment, "database": cfg.safe_database_dsn}},
    )
    try:
        yield
    finally:
        db.close_pool()


def create_admin_app(cfg: config_module.AdminConfig | None = None) -> FastAPI:
    """The operator console. Bind it to a private address; see deploy/maludb-control-plane-admin.service."""
    cfg = cfg or config_module.load_admin()
    cp_logging.configure()

    app = FastAPI(
        title="MaluDB Operator Console API",
        version="0.1.0",
        description="Platform staff only (ADR-082). Not part of the customer API contract.",
        lifespan=lifespan,
        docs_url="/admin/docs" if cfg.docs_enabled else None,
        redoc_url=None,
        openapi_url="/admin/openapi.json" if cfg.docs_enabled else None,
    )
    app.state.config = cfg
    app.state.surface = "admin"
    # Built here rather than in the lifespan, so a wrong or KEK-identical key fails the
    # process at construction, before it listens.
    app.state.staff_key = staff.StaffKey(cfg.staff_key)
    app.state.limiter = ratelimit.LocalLimiter()

    @app.middleware("http")
    async def correlation_ids(request: Request, call_next) -> Response:  # noqa: ANN001
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = cp_logging.request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            # Nothing on the console belongs in a shared cache or a frame on another site.
            response.headers["cache-control"] = "no-store"
            response.headers["x-frame-options"] = "DENY"
            response.headers["referrer-policy"] = "no-referrer"
            return response
        finally:
            cp_logging.request_id_var.reset(token)

    for router in ADMIN_ROUTERS:
        app.include_router(router)
    return app
