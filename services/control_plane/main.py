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

from services.control_plane import captcha, crypto, db, models, ratelimit
from services.control_plane import config as config_module
from services.control_plane import logging as cp_logging
from services.control_plane.api import (
    api_keys,
    audit,
    auth,
    auth_import,
    database,
    health,
    hooks,
    organizations,
    plans,
    projects,
    schema,
    sql,
    usage,
)

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

    # A catalogue with no default plan is a control plane that cannot create a
    # project: `default_plan` looks for the code `free`, finds nothing, and the
    # route answers 503. Nothing seeds this table -- `cp-manage plans sync`
    # does, as a bring-up step -- so an operator should hear about it here
    # rather than from the first customer who tries.
    with db.connection() as conn:
        if models.default_plan(conn) is None:
            log.warning(
                "the plan catalogue has no default plan; projects cannot be created "
                "until `cp-manage plans sync` has been run"
            )

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


# ADR-037. The classification, in one place, as data rather than as an argument
# spread across two factory functions.
#
# A router reaches the internet by being named here and in no other way, which
# is the property worth having: the failure mode of forgetting is a route that
# is unreachable from outside, not one that is reachable and should not be.
# `tests/test_control_plane_surfaces.py` asserts the served route set matches
# this tuple exactly, so adding a router without deciding fails the suite.
PUBLIC_ROUTERS = (
    health.router,        # each listener needs its own liveness answer
    auth.router,          # signup is public at launch; the rest is a user's own account
    organizations.router,
    plans.router,         # authenticated: an entitlement catalogue, not a price list
    projects.router,
    api_keys.router,   # a project's keys and the URL they are used against
    usage.router,      # what a project has used, and asking for a bigger plan
    audit.router,      # what has happened to it, allowlisted event by event
    sql.router,        # ADR-039: SQL the platform runs on the project's behalf
    schema.router,     # read-only: what is in the project's database
    # ADR-047. The only route that returns a credential opening a real
    # PostgreSQL connection from the internet: manager-only, refused unless the
    # plan grants direct access, audited on both sides.
    database.router,
    # ADR-043 slice 7. The one route that connects as the tenant's auth role,
    # because the console's role cannot write auth.users and granting it that
    # would expose every end user's password hash to console access.
    auth_import.router,
)

# Everything, including what must never be public. The internal application is
# what operators and platform components reach, on a listener that is not bound
# to a public interface.
INTERNAL_ROUTERS = (*PUBLIC_ROUTERS, hooks.router)


def create_app(cfg: config_module.Config | None = None) -> FastAPI:
    """The internal application: every router, including the private ones.

    Kept as the name `uvicorn --factory` and the tests already use. What it
    builds is the internal surface, which is what a single-listener deployment
    was serving all along -- so nothing that used it silently loses a route.
    Bind it to a private interface; `create_public_app` is what faces the world.
    """
    return _build(cfg, routers=INTERNAL_ROUTERS, surface="internal")


def create_public_app(cfg: config_module.Config | None = None) -> FastAPI:
    """The internet-facing application: only what `PUBLIC_ROUTERS` names.

    ADR-037 and ADR-038. Two things must stay true of it, and neither is
    guaranteed by this function alone: it serves no route that administers the
    platform, and it holds no path to a node's superuser credentials. The first
    is asserted from the route table; the second is asserted from the import
    graph, because it is a property of what the code can reach rather than of
    what it currently calls.
    """
    return _build(cfg, routers=PUBLIC_ROUTERS, surface="public")


def _build(
    cfg: config_module.Config | None, *, routers: tuple, surface: str
) -> FastAPI:
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
        title=f"MaluDB Control Plane API ({surface})",
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
    app.state.surface = surface

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

    # One limiter per application, on app.state so a test can supply its own
    # clock rather than sleeping through a window.
    app.state.limiter = ratelimit.LocalLimiter()
    # One verifier per application. NullVerifier unless a provider is
    # configured; `captcha_required` is what decides whether a route may rely
    # on it, so an unconfigured deployment refuses rather than accepting all.
    app.state.captcha = captcha.build(cfg)

    for router in routers:
        app.include_router(router)
    return app
