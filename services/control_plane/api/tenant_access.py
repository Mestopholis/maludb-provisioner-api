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

# The roles a request may ask to be run as (slice 3). Exactly the three shared
# Supabase names, because those are exactly what `mldb_<ref>_authenticator` is a
# member of -- asking for anything else would be asking for a role the
# impersonating connection cannot reach anyway.
#
# Checked here as well as in the route's request model. The model is the
# contract; this is the boundary, and a function that picks which credential to
# unwrap should not trust its caller to have validated the input that decides.
#
# **What this list decides is which credential is unwrapped -- not what the
# resulting session may do, and it must never be used for the second.**
# `SET ROLE` is authorized against the *session user*, and the session user on
# an impersonating connection is the authenticator, a member of all three shared
# names. So a request that asks for `anon` can issue `SET ROLE service_role` in
# its own text and get there: no `RESET ROLE`, no grant of its own. Measured
# 2026-08-17.
#
# The first version of this slice refused to impersonate `service_role` while a
# project was storage-restricted. It read as a control and was bypassable by
# exactly that route, in one line of the customer's own SQL. ADR-041 moved the
# restriction into grants instead, where it binds whichever of the three the
# session ends up in.
IMPERSONATABLE_ROLES = provisioning.SHARED_ROLES


@dataclass(frozen=True)
class TenantAccess:
    """A resolved, authorized route into one tenant's database."""

    project: models.Project
    allowed: entitlements.Entitlements
    dsn: str
    # The role the platform enters once connected. Never the login role itself,
    # which is a way in and not a set of privileges
    # (specs/tenant-role-model.md).
    run_as: str
    # True when `run_as` is one of the shared Supabase names rather than the
    # project's admin role -- i.e. the caller asked to be someone else. Carried
    # so the route can say so in the answer and in the audit trail without
    # re-deriving it from the role name.
    impersonating: bool
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
    impersonate: str | None = None,
) -> TenantAccess:
    """Authorize the caller and build a connection string for their project.

    The limit arrives as a function of the plan rather than as a value, because
    every limit on this surface is derived from the plan and the plan is not
    known until step 2. Passing a computed `Limit` would mean resolving
    entitlements in the caller, before the membership check -- which is the one
    ordering this module exists to keep.

    The bucket is per-surface: browsing a schema must not spend the budget for
    running a statement.

    `impersonate` changes which role logs in, and that is the whole of slice 3's
    security design. Without it: the executor, entering `mldb_<ref>_admin`. With
    it: `mldb_<ref>_authenticator`, entering one of the three shared names. The
    second connection cannot reach the admin role at all -- the authenticator is
    a member of `anon`, `authenticated` and `service_role` and of nothing else --
    so `RESET ROLE` in the submitted text lands on a role that holds no more than
    what was asked for. Nesting a `SET ROLE` inside the admin role would not have
    achieved that, because slice 1 established that the reset is reachable.
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

    storage_restricted = row["storage_restricted_at"] is not None

    if impersonate is None:
        login_role, credential, run_as = "executor", "db_executor", f"{project.database_name}_admin"
    else:
        if impersonate not in IMPERSONATABLE_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"role must be one of {', '.join(IMPERSONATABLE_ROLES)}",
            )
        login_role, credential, run_as = "authenticator", "db_authenticator", impersonate

    password = _credential(conn, project.id, request, credential_type=credential)
    return TenantAccess(
        project=project,
        allowed=allowed,
        dsn=sql_console.executor_dsn(
            host=row["internal_host"],
            port=row["db_port"],
            database=project.database_name,
            role=f"{project.database_name}_{login_role}",
            password=password,
        ),
        run_as=run_as,
        impersonating=impersonate is not None,
        storage_restricted=storage_restricted,
    )


def _credential(
    conn, project_id: uuid.UUID, request: Request, *, credential_type: str
) -> str:
    """The one secret these routes handle. Never logged, never returned.

    A project provisioned before ADR-039 has no executor credential, and the
    repair is an operator running `cp-manage project backfill-executor` rather
    than a route minting one: creating a role needs node admin credentials,
    which ADR-038 keeps out of this process entirely. The authenticator's
    credential needs no such backfill -- every project has had one since Phase
    02, which is most of why impersonation reuses that role rather than
    inventing a fourth.
    """
    try:
        return provisioning.load_credential(
            conn,
            project_id=project_id,
            credential_type=credential_type,
            key_ring=request.app.state.key_ring,
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="this project's SQL console is not configured yet",
        ) from exc
