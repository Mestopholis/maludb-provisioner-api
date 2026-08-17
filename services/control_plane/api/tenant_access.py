"""The preflight every route that opens a tenant connection has to pass.

Extracted from `api/sql.py` in Phase 08 slice 2, when introspection became the
second caller and impersonation was visibly going to be the third. Three copies
of a security preflight is three places to forget a check, and the order of the
checks below is itself part of the design:

1. **Membership**, because everything after it discloses something. A project
   that is not yours answers 404 with the same body as one that does not exist.
2. **The entitlement**, because a project whose console is switched off should
   not have its readiness probed.
3. **Readiness**, because a project still provisioning has a database that may
   not exist and roles that may not be created.
4. **The rate limit**, last of the gates, so a caller who is going to be refused
   for a reason they can fix is told that reason rather than a 429.

Then, and only then, the executor credential is loaded. It is the one secret
these routes handle: never logged, never returned, and never used to build a DSN
by substituting into another one -- `sql_console.executor_dsn` builds from parts
because `tests/test_provisioning.py` records what string replacement did.

ADR-038 note: this module reaches a *tenant's* credential and must never reach
`nodes.admin_dsn`. The import-graph test in `tests/test_control_plane_surfaces.py`
is what keeps that true as this file grows callers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from services.control_plane import db, entitlements, models, provisioning, ratelimit, sql_console
from services.control_plane.api import limit_dep
from services.control_plane.api.usage import _project_for

# The pair `api_keys.authenticate` and `workers` already gate on: a database
# that exists, with its roles created.
SERVING_STATUSES = ("PROVISIONED", "ACTIVE")


@dataclass(frozen=True)
class TenantAccess:
    """A resolved, authorized route into one tenant's database."""

    project: models.Project
    allowed: entitlements.Entitlements
    dsn: str
    # The role the platform enters once connected. Never the executor, which is
    # a way in and not a set of privileges (specs/tenant-role-model.md).
    run_as: str
    # True when the project is over its storage quota. Reported, not enforced
    # here: ADR-040 put the restriction in grants on `run_as`, so it covers this
    # path and paid direct SQL by one mechanism. What survives is being able to
    # explain a `42501` rather than leaving a customer to guess.
    storage_restricted: bool


def resolve(
    conn,
    project_ref: str,
    principal,
    request: Request,
    *,
    bucket: str,
    limit_for: Callable[[entitlements.Entitlements], ratelimit.Limit],
) -> TenantAccess:
    """Authorize the caller and build a connection string for their project.

    The limit arrives as a function of the plan rather than as a value, because
    every limit on this surface is derived from the plan and the plan is not
    known until step 2. Passing a computed `Limit` would mean resolving
    entitlements in the caller, before the membership check -- which is the one
    ordering this module exists to keep.

    The bucket is per-surface: browsing a schema must not spend the budget for
    running a statement.
    """
    project = _project_for(conn, project_ref, principal)
    allowed = entitlements.for_project(conn, project.id)
    if not allowed.sql_console:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this project's SQL console is disabled",
        )

    row = db.one(
        conn,
        """
        SELECT p.status, p.storage_restricted_at, n.internal_host, n.db_port
          FROM projects p JOIN nodes n ON n.id = p.node_id
         WHERE p.id = %s
        """,
        (project.id,),
    )
    if row is None or row["status"] not in SERVING_STATUSES:
        # 409 rather than 404: the caller has already been told this project is
        # theirs, so the honest answer is that it is not ready yet.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is not ready to serve SQL",
        )

    # Per project rather than per caller. The resource being protected is the
    # tenant's database and the node under it, and two members of one
    # organization hammering it cost the node the same as one member twice.
    limit_dep.enforce(request, bucket=bucket, limit=limit_for(allowed), subject=str(project.id))

    password = _executor_password(conn, project.id, request)
    return TenantAccess(
        project=project,
        allowed=allowed,
        dsn=sql_console.executor_dsn(
            host=row["internal_host"],
            port=row["db_port"],
            database=project.database_name,
            role=f"{project.database_name}_executor",
            password=password,
        ),
        run_as=f"{project.database_name}_admin",
        storage_restricted=row["storage_restricted_at"] is not None,
    )


def _executor_password(conn, project_id: uuid.UUID, request: Request) -> str:
    """The one secret these routes handle. Never logged, never returned.

    A project provisioned before ADR-039 has no executor credential, and the
    repair is an operator running `cp-manage project backfill-executor` rather
    than a route minting one: creating a role needs node admin credentials,
    which ADR-038 keeps out of this process entirely.
    """
    try:
        return provisioning.load_credential(
            conn,
            project_id=project_id,
            credential_type="db_executor",
            key_ring=request.app.state.key_ring,
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="this project's SQL console is not configured yet",
        ) from exc
