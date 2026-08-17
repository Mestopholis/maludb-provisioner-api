"""Running SQL against a project, for every tier (ADR-039).

Public under ADR-037: the dashboard calls this. It holds a per-project executor
credential and must never reach `nodes.admin_dsn` -- ADR-038's import-graph test
fails if a future edit wires one in.

The gates -- membership, entitlement, readiness, rate limit, in that order and
for the reasons recorded there -- moved to `api/tenant_access.py` in slice 2,
when introspection became their second caller.

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
from services.control_plane.api import tenant_access
from services.control_plane.api.auth_dep import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["sql"])

# Separate from the introspection bucket on purpose: reading a schema is not
# spending a statement, and a dashboard that rendered a table list must not have
# used up a free project's one statement per window to do it.
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


def _console_limit(allowed: entitlements.Entitlements) -> ratelimit.Limit:
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
        access = tenant_access.resolve(
            conn, project_ref, principal, request,
            bucket=CONSOLE_BUCKET, limit_for=_console_limit,
        )
        allowed = access.allowed

        statement_id = sql_console.new_statement_id()
        try:
            results = sql_console.execute(
                access.dsn,
                body.statement,
                run_as=access.run_as,
                row_limit=allowed.sql_console_row_limit,
                timeout_ms=allowed.sql_console_timeout_ms,
            )
        except sql_console.ConsoleError as exc:
            _audit(conn, access.project.id, principal, "project.sql.failed",
                   {**sql_console.audit_detail(body.statement, results=[]),
                    "statement_id": str(statement_id), "error": str(exc)})
            conn.commit()
            # 400: the statement is the thing that was wrong, and a customer
            # retrying it unchanged will fail identically.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        _audit(conn, access.project.id, principal, "project.sql.executed",
               {**sql_console.audit_detail(body.statement, results=results),
                "statement_id": str(statement_id),
                "storage_restricted": access.storage_restricted})
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
            storage_restricted=access.storage_restricted,
        )


def _audit(conn, project_id: uuid.UUID, principal, event_type: str, detail: dict) -> None:
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, 'user', %s, %s, %s)",
        (project_id, principal.user.id, event_type, psycopg.types.json.Jsonb(detail)),
    )
