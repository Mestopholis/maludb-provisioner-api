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

from services.control_plane import admin_grants, db, ratelimit, staff
from services.control_plane import config as config_module
from services.control_plane import logging as cp_logging
from services.control_plane.api import admin_reports, admin_session, admin_ui, health

log = logging.getLogger(__name__)

# The console's routes, and the only place they are mounted. Health so the listener can
# be probed like the other two; every other route requires a staff session.
ADMIN_ROUTERS = (
    health.router,
    admin_session.router,
    admin_reports.router,  # slice 3: sales, customers, usage, abuse, nodes, provisioning; read-only
    admin_ui.router,  # slice 4: the console's pages, a fixed map of files
)

# The console's pages load script and style from this origin only, and nothing may frame
# them. No inline script or style, so an injected fragment cannot run.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: config_module.AdminConfig = app.state.config
    db.init_pool(cfg.database_url)
    with db.connection() as conn:
        assert_narrowed(conn, environment=cfg.environment)
    log.info(
        "operator console started",
        extra={"extra_fields": {"environment": cfg.environment, "database": cfg.safe_database_dsn}},
    )
    try:
        yield
    finally:
        db.close_pool()


def assert_narrowed(conn, *, environment: str) -> None:
    """Refuse to serve in production as anything but a narrowed member of `cp_admin_console`.

    Asked of the database, as `memory_worker.assert_narrowed` does: can this role read a
    sealed column or a customer verifier, or write a staff credential? Is it the console
    at all -- a role that is not cannot record a staff sign-in (migration 0052), so every
    sign-in would fail? And is it also a memory worker, which `pg_roles` can answer?

    Whether it is also a gateway or health reporter is asked by `cp-manage admin-console
    grant` and preflight instead: those mappings live on `nodes`, whose row policy shows
    this role no rows, so the answer from here would always be "no".
    """
    role = conn.execute("SELECT current_user AS role").fetchone()
    role = role["role"] if isinstance(role, dict) else role[0]
    wider = admin_grants.violations(conn, role)
    member = conn.execute("SELECT public.is_admin_console() AS m").fetchone()
    member = member["m"] if isinstance(member, dict) else member[0]
    memory = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles o WHERE o.rolname IN ('cp_memory_worker', "
        "'cp_memory_embedder') AND pg_catalog.pg_has_role(current_user, o.oid, 'MEMBER')) AS m"
    ).fetchone()
    overlapping = [role] if (memory["m"] if isinstance(memory, dict) else memory[0]) else []
    conn.rollback()
    problems = []
    if wider:
        problems.append(f"the console's database role {role!r} can " + ", ".join(wider))
    if not member:
        problems.append(f"role {role!r} is not a member of {admin_grants.GROUP_ROLE}")
    if overlapping:
        problems.append(f"{role!r} is also a memory worker or query embedder")
    if not problems:
        return
    message = "; ".join(problems) + (
        ". Create a LOGIN role in cp_admin_console, run `cp-manage admin-console grant`, and point "
        "MALUDB_ADMIN_DATABASE_URL at it (docs/DEPLOYMENT.md 1.7)"
    )
    if environment == "production":
        raise RuntimeError(message)
    log.warning("%s -- refused in production; allowed here because MALUDB_ENV=%s", message, environment)


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
            response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
            response.headers["x-content-type-options"] = "nosniff"
            return response
        finally:
            cp_logging.request_id_var.reset(token)

    for router in ADMIN_ROUTERS:
        app.include_router(router)
    return app
