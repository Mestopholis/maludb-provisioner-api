"""Control-plane FastAPI application.

ADR-024: FastAPI is authoritative for the API contract.
specs/control-plane-api.yaml is regenerated from this app and CI fails on
drift, so the spec stays reviewable without being hand-maintained.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from services.control_plane import config as config_module
from services.control_plane import crypto, db
from services.control_plane import logging as cp_logging
from services.control_plane.api import auth, health, hooks, organizations, plans, projects

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: config_module.Config = app.state.config
    db.init_pool(cfg.database_url)

    # Load the key ring before serving. If the KEK cannot unwrap the stored
    # DEKs the process fails here rather than at the first request that needs
    # to decrypt something (ADR-023 fail-closed).
    key_ring = crypto.KeyRing(cfg.kek)
    with db.connection() as conn:
        key_ring.load(conn)
    app.state.key_ring = key_ring

    log.info(
        "control plane started",
        # safe_database_dsn drops credentials; never log database_url directly.
        extra={"extra_fields": {"environment": cfg.environment, "database": cfg.safe_database_dsn}},
    )
    try:
        yield
    finally:
        db.close_pool()


# True since slice 2: every data route depends on CurrentPrincipal, so an
# unauthenticated request is rejected before any handler runs. The guard below
# stays as a standing check -- if this is ever flipped back without removing
# the dependencies, production startup fails loudly rather than silently
# serving unauthenticated traffic.
AUTHENTICATION_ENFORCED = True


class InsecureConfiguration(RuntimeError):
    """Raised when the application would start in an unsafe state."""


def create_app(cfg: config_module.Config | None = None) -> FastAPI:
    # Fails closed if key material or the database URL is missing (ADR-023).
    cfg = cfg or config_module.load()

    if cfg.is_production and not AUTHENTICATION_ENFORCED:
        raise InsecureConfiguration(
            "refusing to start in production: no authentication is enforced on "
            "control-plane routes. Authentication lands in Phase 01 slice 2 "
            "(docs/ACCOUNTS.md, ADR-021). Until then this service may run only "
            "in development or test."
        )

    cp_logging.configure()

    app = FastAPI(
        title="MaluDB Control Plane API",
        version="0.1.0",
        description=(
            "Platform-user credentials only. Unrelated to project API keys or to "
            "end-user tokens issued by a tenant's Auth service."
        ),
        lifespan=lifespan,
        # ADR-024: documentation routes are gated so production does not
        # publish a map of the admin surface.
        docs_url="/docs" if cfg.docs_enabled else None,
        redoc_url="/redoc" if cfg.docs_enabled else None,
        openapi_url="/openapi.json" if cfg.docs_enabled else None,
    )
    app.state.config = cfg

    @app.middleware("http")
    async def correlation_ids(request: Request, call_next) -> Response:  # noqa: ANN001
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = cp_logging.request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            cp_logging.request_id_var.reset(token)

    app.include_router(health.router)
    app.include_router(hooks.router)
    app.include_router(auth.router)
    app.include_router(organizations.router)
    app.include_router(plans.router)
    app.include_router(projects.router)
    return app
