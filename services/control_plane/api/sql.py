"""Running SQL against a project, for every tier (ADR-039).

Public under ADR-037: the dashboard calls this. It holds a per-project executor
credential and must never reach `nodes.admin_dsn` -- ADR-038's import-graph test
fails if a future edit wires one in.

The gates -- membership, entitlement, readiness, rate limit, in that order and
for the reasons recorded there -- moved to `api/tenant_access.py` in slice 2,
when introspection became their second caller.

**Impersonation (slice 3) is a lower ceiling, not a sandbox.** A request naming
`anon`, `authenticated` or `service_role` is run on a connection as
`mldb_<ref>_authenticator` rather than as the executor, so `RESET ROLE` in the
submitted text cannot climb to the admin role. What it does not do is stop the
customer reaching the admin role *at all* -- they need only send the next
request without a role, which is their own database and their own right. The
value is fidelity: this is the same role, the same `SET ROLE` and the same
`request.jwt.claims` their application meets through PostgREST, so a policy
that returns nothing here returns nothing there.

**Storage restriction is not a check here.** ADR-040 put it in grants, on the
same role this runs statements as, so it applies to this path and to paid direct
SQL by one mechanism. The first version of this slice held a restricted project
in a read-only session instead, and a probe showed the submitted text escapes
that with `SET default_transaction_read_only = off`. What survives is reporting
the state, so a dashboard can explain a `42501` rather than leaving a customer
to guess why their own table refused them.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from services.control_plane import db, entitlements, ratelimit, sql_console
from services.control_plane.api import tenant_access
from services.control_plane.api.auth_dep import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["sql"])

# Separate from the introspection bucket on purpose: reading a schema is not
# spending a statement, and a dashboard that rendered a table list must not have
# used up a free project's one statement per window to do it.
CONSOLE_BUCKET = "sql-console"

# An access token's payload is a few hundred bytes; this is room for an
# unusually rich claim set and not for a payload.
MAX_CLAIMS_BYTES = 8_192


class StatementIn(BaseModel):
    # Bounded so a single request cannot be a memory attack before any limit is
    # consulted. Generous enough for a real migration file, which is what
    # Phase 08's later slices will send through here.
    statement: str = Field(min_length=1, max_length=1_000_000)
    # Slice 3. Absent means the project's own admin role, which is what a
    # customer writing DDL wants. Present means run this as my application's
    # users would meet it -- the failure mode Phase 00 finding 7 and ADR-018
    # keep producing is a policy that returns nothing rather than `42501`, and
    # no amount of reading the policy tells you which rows it would have hidden.
    #
    # `Literal` rather than a validator, so the three names are in the OpenAPI
    # contract a frontend builds its role selector from.
    role: Literal["anon", "authenticated", "service_role"] | None = None
    # The JSON PostgREST would have put in `request.jwt.claims` for a request
    # carrying that JWT. Not a token: nothing is verified, because there is
    # nothing to verify against -- the customer is asserting "suppose a user
    # with these claims", which is the question they are trying to answer.
    claims: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _claims_need_a_role(self) -> StatementIn:
        # `auth.uid()` read while running as the admin role answers whatever was
        # set and means nothing, because no policy is being applied to that
        # role. Refusing is better than answering a question the caller did not
        # mean to ask.
        if self.claims is not None and self.role is None:
            raise ValueError("claims require a role to impersonate")
        # A GUC value is memory in the backend, and this one is caller-supplied.
        # Bounded well above any real claim set: a Supabase access token's
        # payload is a few hundred bytes.
        if self.claims is not None and len(json.dumps(self.claims)) > MAX_CLAIMS_BYTES:
            raise ValueError(f"claims must serialise to at most {MAX_CLAIMS_BYTES} bytes")
        return self


class ResultOut(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    # PostgreSQL's own count. -1 where it does not report one, which is what the
    # driver returns and is more honest than coercing it to zero.
    row_count: int
    # True when the plan's caps stopped the fetch -- more rows than
    # `sql_console_row_limit`, or more bytes than `sql_console_max_bytes` spent
    # across this response. The rows above are the first of them, not a sample.
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
    # The role the session entered before the statement ran. Named for what it
    # is: a statement can `SET ROLE` or `RESET ROLE` its own way to any role the
    # connection's session user is a member of, so calling this "ran as" would
    # claim an observation the platform never made. Echoed because "this
    # returned no rows" and "this returned no rows having asked for anon" are
    # different findings and a result set does not say which.
    requested_role: str


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
            impersonate=body.role,
        )
        allowed = access.allowed
        claims = _claims_for(body)

        statement_id = sql_console.new_statement_id()
        try:
            results = sql_console.execute(
                access.dsn,
                body.statement,
                run_as=access.run_as,
                row_limit=allowed.sql_console_row_limit,
                max_bytes=allowed.sql_console_max_bytes,
                timeout_ms=allowed.sql_console_timeout_ms,
                claims=claims,
            )
        except sql_console.ConsoleError as exc:
            _audit(conn, access.project.id, principal, "project.sql.failed",
                   {**sql_console.audit_detail(body.statement, results=[]),
                    **_impersonation_detail(access, claims),
                    "statement_id": str(statement_id), "error": str(exc)})
            conn.commit()
            # 400: the statement is the thing that was wrong, and a customer
            # retrying it unchanged will fail identically.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        _audit(conn, access.project.id, principal, "project.sql.executed",
               {**sql_console.audit_detail(body.statement, results=results),
                **_impersonation_detail(access, claims),
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
            requested_role=access.run_as,
        )


def _claims_for(body: StatementIn) -> dict[str, Any] | None:
    """What goes into `request.jwt.claims`, and the one thing added to it.

    `role` is defaulted to the role being impersonated when the caller did not
    supply it. Supabase's own access tokens always carry it, `auth.role()` reads
    it, and a policy calling that function against a claim set without one would
    answer `NULL` for a session that is demonstrably `authenticated` -- a
    difference between the console and production that exists only because the
    console filled the form in by hand. An explicit `role` claim is left alone:
    a customer testing what a mismatched token does is asking a real question.
    """
    if body.role is None:
        return None
    claims = dict(body.claims or {})
    claims.setdefault("role", body.role)
    return claims


def _impersonation_detail(access: tenant_access.TenantAccess, claims: dict | None) -> dict:
    """What the trail records about an impersonated statement.

    The claim *keys*, never their values. A claim set is where an end user's id
    and email live, and those belong to the customer's users rather than in a
    table the platform's operators read -- the same line `audit_detail` draws
    when it records the statement but never the rows it returned.
    """
    if not access.impersonating:
        return {}
    return {"requested_role": access.run_as, "claim_keys": sorted(claims or {})}


def _audit(conn, project_id: uuid.UUID, principal, event_type: str, detail: dict) -> None:
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, 'user', %s, %s, %s)",
        (project_id, principal.user.id, event_type, psycopg.types.json.Jsonb(detail)),
    )
