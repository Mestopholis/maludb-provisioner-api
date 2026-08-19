"""What is in a project's database (Phase 08 slice 2).

Public under ADR-037, on the same gates as slice 1's console
(`api/tenant_access.py`) and with no write path of any kind. The reasoning
about what is and is not disclosed lives in `introspection`, because it is a
property of the queries rather than of this router.

**One endpoint rather than nine.** `postgres-meta` splits tables, columns,
policies and the rest across separate routes, and copying that here would make
one dashboard page eight tenant connections and eight rate-limit tokens -- on a
plan whose whole console budget is one statement per window. So a page load is
one request, one connection, one snapshot, consistent with itself. `?schema=`
is how a large project narrows it.

**Nothing is audited here.** Slice 1 writes an audit event per statement because
a statement changes things; a read of one's own catalogue happens on every
dashboard page load, and recording those would bury the trail that answers "who
changed this schema" under the noise of people looking at it. Phase 07's audit
surface is allowlisted event by event, and this is not on the list deliberately.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.control_plane import db, entitlements, introspection, ratelimit, sql_console
from services.control_plane.api import tenant_access
from services.control_plane.api.auth_dep import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["sql"])

# Separate from `sql.CONSOLE_BUCKET`, which is the point: rendering a schema
# browser must not consume the ability to run a statement.
INTROSPECTION_BUCKET = "sql-introspection"

# A floor under the derived rate. Free resolves one concurrent statement per
# eight seconds, and a schema browser that allowed two page loads in that window
# would be unusable for the tier that has no other way into its own database.
# Reads are cheaper than statements, so the floor is on the read side only.
INTROSPECTION_FLOOR = 4

# More than this and the caller is not narrowing a query, they are sending a
# payload. The values are bound as parameters and cannot be SQL either way;
# what this bounds is the size of the request.
MAX_SCHEMA_FILTERS = 50


class Column(BaseModel):
    position: int
    name: str
    data_type: str
    is_nullable: bool
    default_expression: str | None
    is_identity: bool
    is_generated: bool
    comment: str | None


class Index(BaseModel):
    name: str
    is_unique: bool
    is_primary: bool
    is_valid: bool
    definition: str


class Constraint(BaseModel):
    name: str
    # primary_key | foreign_key | unique | check | exclusion | trigger
    kind: str
    definition: str


class Policy(BaseModel):
    name: str
    # select | insert | update | delete | all
    command: str
    permissive: bool
    roles: list[str]
    using_expression: str | None
    check_expression: str | None


class Table(BaseModel):
    schema_name: str
    name: str
    # table | partitioned_table | view | materialized_view | foreign_table
    kind: str
    rls_enabled: bool
    rls_forced: bool
    owner: str
    comment: str | None
    # The planner's estimate. Named as one because it is one: an exact count
    # means reading every table on a shared node to render a page.
    estimated_rows: int
    size_bytes: int
    # True when the platform owns this relation rather than the customer --
    # `auth.users` and the rest of what bootstrap created. A customer's SQL can
    # read it and cannot alter it, and a dashboard offering an edit button on it
    # would be offering a `42501`.
    managed: bool
    columns: list[Column]
    indexes: list[Index]
    constraints: list[Constraint]
    policies: list[Policy]


class Schema(BaseModel):
    name: str
    owner: str
    comment: str | None


class Function(BaseModel):
    schema_name: str
    name: str
    # function | procedure | aggregate | window
    kind: str
    arguments: str
    # Null for a procedure: `pg_get_function_result` has no RETURNS clause to
    # reconstruct, and a migrated Supabase schema with a `CREATE PROCEDURE` in
    # it is ordinary rather than exotic. Measured, not assumed.
    returns: str | None
    language: str
    security_definer: bool
    volatility: str
    owner: str
    comment: str | None
    managed: bool
    # Null for a managed function. The platform's `SECURITY DEFINER` bodies are
    # not the customer's to read, which is the same line `sql_console` draws
    # when it strips CONTEXT out of an error.
    source: str | None


class Extension(BaseModel):
    name: str
    schema_name: str
    installed_version: str


class Role(BaseModel):
    name: str
    can_login: bool
    connection_limit: int
    # anon, authenticated and service_role: cluster-wide names an RLS policy can
    # target (ADR-016), as opposed to this project's own roles.
    is_shared: bool


class SchemaOut(BaseModel):
    schemas: list[Schema]
    tables: list[Table]
    functions: list[Function]
    extensions: list[Extension]
    roles: list[Role]
    # The catalogues that hit a cap, by name -- their own row cap, or the
    # plan's `sql_console_max_bytes` spent across the whole snapshot. Empty in
    # every ordinary case; a dashboard that finds a name here should say so
    # rather than presenting a partial list as complete.
    truncated: list[str]


def _introspection_limit(allowed: entitlements.Entitlements) -> ratelimit.Limit:
    """Derived from the plan, like the console's, and deliberately looser."""
    window = max(1, allowed.sql_console_timeout_ms // 1000)
    return ratelimit.Limit(max(INTROSPECTION_FLOOR, allowed.sql_console_concurrent * 2), window)


@router.get(
    "/projects/{project_ref}/database/schema",
    response_model=SchemaOut,
    summary="What is in a project's database",
)
def get_schema(
    project_ref: str,
    request: Request,
    principal: CurrentPrincipal,
    schema: Annotated[
        list[str] | None, Query(description="Limit the snapshot to these schemas")
    ] = None,
) -> SchemaOut:
    if schema and len(schema) > MAX_SCHEMA_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"at most {MAX_SCHEMA_FILTERS} schema filters",
        )

    with db.connection() as conn:
        access = tenant_access.resolve(
            conn, project_ref, principal, request,
            bucket=INTROSPECTION_BUCKET, limit_for=_introspection_limit,
        )

    # Outside the control-plane connection: the tenant read can take as long as
    # the plan's ceiling allows, and holding a pool connection for it would let
    # a slow node exhaust the control plane rather than only itself.
    try:
        snapshot = introspection.snapshot(
            access.dsn,
            run_as=access.run_as,
            project_ref=access.project.project_ref,
            timeout_ms=access.allowed.sql_console_timeout_ms,
            max_bytes=access.allowed.sql_console_max_bytes,
            schemas=list(schema) if schema else None,
        )
    except sql_console.ConsoleError as exc:
        # 400 rather than 500: a cancel means the schema was too large to read
        # inside the plan's ceiling, and `?schema=` is the caller's fix.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return SchemaOut(
        schemas=[Schema(**row) for row in snapshot.schemas],
        tables=[Table(**_renamed(row)) for row in snapshot.tables],
        functions=[Function(**_renamed(row)) for row in snapshot.functions],
        extensions=[Extension(**_renamed(row)) for row in snapshot.extensions],
        roles=[Role(**row) for row in snapshot.roles],
        truncated=snapshot.truncated,
    )


def _renamed(row: dict[str, Any]) -> dict[str, Any]:
    """`schema` -> `schema_name`.

    Pydantic's `BaseModel.schema` is a method on the model class, so a field
    called `schema` shadows it and produces a warning today and a broken model
    at some future version. The catalogue's own word is `schema`; the wire name
    is `schema_name`, once, here, rather than at eight call sites.
    """
    if "schema" not in row:
        return row
    renamed = dict(row)
    renamed["schema_name"] = renamed.pop("schema")
    return renamed
