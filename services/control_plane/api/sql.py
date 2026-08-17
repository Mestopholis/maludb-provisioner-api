"""Running SQL against a project, for every tier (ADR-039).

Public under ADR-037: the dashboard calls this. It holds a per-project executor
credential and must never reach `nodes.admin_dsn` -- ADR-038's import-graph test
fails if a future edit wires one in.

The order of the checks below is the design. Membership first, because
everything after it discloses something; the entitlement next, because a project
whose console is switched off should not have its readiness probed; and the rate
limit last of the gates, so a caller who is going to be refused for a reason
they can fix is told that reason rather than a 429.

**Storage restriction is not a check here.** ADR-040 put it in grants, on the
same role this runs statements as, so it applies to this path and to paid direct
SQL by one mechanism. The first version of this slice held a restricted project
in a read-only session instead, and a probe showed the submitted text escapes
that with `SET default_transaction_read_only = off`. What survives is reporting
the state, so a dashboard can explain a `42501` rather than leaving a customer
to guess why their own table refused them.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from services.control_plane import db, entitlements, ratelimit, sql_console
from services.control_plane.api import limit_dep
from services.control_plane.api.auth_dep import CurrentPrincipal
from services.control_plane.api.usage import _project_for

router = APIRouter(prefix="/v1", tags=["sql"])

# Per project rather than per caller. The resource being protected is the
# tenant's database and the node under it, and two members of one organization
# hammering the console cost the node the same as one member doing it twice.
CONSOLE_BUCKET = "sql-console"


class StatementIn(BaseModel):
    # Bounded so a single request cannot be a memory attack before any limit is
    # consulted. Generous enough for a real migration file, which is what
    # Phase 08's later slices will send through here.
    statement: str = Field(min_length=1, max_length=1_000_000)


class ResultOut(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    # PostgreSQL's own count. -1 where it does not report one, which is what the
    # driver returns and is more honest than coercing it to zero.
    row_count: int
    # True when the statement produced more rows than the plan's cap. The rows
    # above are the first `sql_console_row_limit` of them, not a sample.
    truncated: bool
    command: str | None


class ExecutionOut(BaseModel):
    statement_id: uuid.UUID
    results: list[ResultOut]
    # Echoed so a dashboard can say "showing 100 of more" without knowing the
    # plan, and so a truncated result is explainable rather than mysterious.
    row_limit: int
    # True when the project is over its storage quota. The statement still ran;
    # what changes is that ADR-040 has revoked INSERT and UPDATE from the role
    # it ran as, so a write will have come back as `42501 permission denied`.
    # Surfaced so a dashboard can explain that error rather than leaving a
    # customer to guess why their own table refused them.
    storage_restricted: bool


def _console_limit(request: Request, allowed: entitlements.Entitlements) -> ratelimit.Limit:
    """One statement per concurrent slot per timeout window.

    Derived from the plan rather than configured separately: a tier that allows
    one statement at a time and cancels at eight seconds is describing its own
    rate, and a second number to keep in step with it would drift.
    """
    window = max(1, allowed.sql_console_timeout_ms // 1000)
    return ratelimit.Limit(allowed.sql_console_concurrent, window)


@router.post(
    "/projects/{project_ref}/sql",
    response_model=ExecutionOut,
    summary="Run SQL against a project's database",
)
def execute_sql(
    project_ref: str, body: StatementIn, request: Request, principal: CurrentPrincipal
) -> ExecutionOut:
    with db.connection() as conn:
        # 404 for both "no such project" and "not your project", body included.
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
        # The same pair `api_keys.authenticate` and `workers` already gate on: a
        # database that exists, with its roles created.
        if row is None or row["status"] not in ("PROVISIONED", "ACTIVE"):
            # A project still provisioning has a database that may not exist and
            # roles that may not be created. 409 rather than 404: the caller has
            # already been told this project is theirs.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="project is not ready to serve SQL",
            )

        # Reported, not enforced here. ADR-040 put the restriction in grants,
        # which cover this path and paid direct SQL by one mechanism -- so there
        # is no special case to apply, only a state worth naming in the answer.
        storage_restricted = row["storage_restricted_at"] is not None

        limit_dep.enforce(
            request,
            bucket=CONSOLE_BUCKET,
            limit=_console_limit(request, allowed),
            subject=str(project.id),
        )

        password = _executor_password(conn, project.id, request)
        dsn = sql_console.executor_dsn(
            host=row["internal_host"],
            port=row["db_port"],
            database=project.database_name,
            role=f"{project.database_name}_executor",
            password=password,
        )

        statement_id = sql_console.new_statement_id()
        try:
            results = sql_console.execute(
                dsn,
                body.statement,
                run_as=f"{project.database_name}_admin",
                row_limit=allowed.sql_console_row_limit,
                timeout_ms=allowed.sql_console_timeout_ms,
            )
        except sql_console.ConsoleError as exc:
            _audit(conn, project.id, principal, "project.sql.failed",
                   {**sql_console.audit_detail(body.statement, results=[]),
                    "statement_id": str(statement_id), "error": str(exc)})
            conn.commit()
            # 400: the statement is the thing that was wrong, and a customer
            # retrying it unchanged will fail identically.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        _audit(conn, project.id, principal, "project.sql.executed",
               {**sql_console.audit_detail(body.statement, results=results),
                "statement_id": str(statement_id),
                "storage_restricted": storage_restricted})
        conn.commit()

        return ExecutionOut(
            statement_id=statement_id,
            results=[
                ResultOut(
                    columns=r.columns, rows=r.rows, row_count=r.row_count,
                    truncated=r.truncated, command=r.command,
                )
                for r in results
            ],
            row_limit=allowed.sql_console_row_limit,
            storage_restricted=storage_restricted,
        )


def _executor_password(conn, project_id: uuid.UUID, request: Request) -> str:
    """The one secret this route handles. Never logged, never returned.

    A project provisioned before ADR-039 has no executor credential, and the
    repair is an operator running `cp-manage project backfill-executor` rather
    than this route minting one: creating a role needs node admin credentials,
    which ADR-038 keeps out of this process entirely.
    """
    from services.control_plane import provisioning

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


def _audit(conn, project_id: uuid.UUID, principal, event_type: str, detail: dict) -> None:
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, 'user', %s, %s, %s)",
        (project_id, principal.user.id, event_type, psycopg.types.json.Jsonb(detail)),
    )
