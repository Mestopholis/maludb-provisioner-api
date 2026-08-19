"""The connection details a paid project is sold (ADR-047, Phase 09 slice 2).

Public under ADR-037. This is the sharpest surface the control plane has: it
returns a long-lived credential that opens a real PostgreSQL connection from
the internet, and it is the only route in the platform that does.

What bounds it:

- **The plan.** `direct_database_access` is the capability, and a project
  without it gets a refusal rather than a credential. ADR-005 has said since
  Phase 01 that a free project receives no connection credentials and no
  reachable port; this is the route that would have broken that promise if it
  were written without the check.
- **The organization role.** A manager, not any member. Reading the credential
  is taking custody of the project's database, and `viewer` exists precisely so
  that seeing a project is not the same as holding it.
- **The audit trail.** Both routes record, and both events are allowlisted, so
  "who took our database password, and when" is a question a customer can
  answer for themselves rather than by asking support.

**The credential returned is `mldb_<ref>_client`'s, never the admin role's**
(ADR-047). That is the whole reason the role exists: rotating this does not
touch the identity the platform acts under, and revoking it does not break the
customer's SQL console.

**Rotation does not use a node credential**, because ADR-038 keeps those out of
this application entirely. It connects as the client role and changes its own
password -- measured 2026-08-19: a client session arrives in the admin role,
`SET ROLE NONE` returns it to the client role (`RESET ROLE` does not, because
the role setting *is* the session default), an ordinary role may change its own
password, and the same session is refused `42501` when it tries to change the
admin role's. So the capability this route needs is exactly the capability the
customer already has, and nothing was widened to provide it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from services.control_plane import (
    db,
    entitlements,
    models,
    provisioning,
    ratelimit,
    sql_console,
)
from services.control_plane.api import tenant_access
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager
from services.control_plane.api.limit_dep import enforce as enforce_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["database"])

CREDENTIAL_BUCKET = "database-credential"

VIEWED = "project.database.credential_viewed"
ROTATED = "project.database.credential_rotated"

# Its own bucket and its own number. Reading a credential is not a statement
# and should not spend the console's allowance, and a route that hands out a
# password is worth limiting more tightly than one that runs a query: a
# compromised session should not be able to enumerate an organization's
# projects' credentials at speed.
CREDENTIAL_LIMIT = ratelimit.Limit(10, 60)


class ConnectionOut(BaseModel):
    host: str
    port: int
    database: str
    user: str
    password: str
    # Composed here rather than left to a client. `sql_console.executor_dsn`
    # exists because string substitution into a DSN went wrong once already,
    # and a password with a `/` or `@` in it is exactly the case a hand-rolled
    # concatenation gets wrong.
    connection_string: str
    # So a dashboard can say "rotated 3 days ago" without asking again, and so
    # a customer can tell a credential they rotated from one they did not.
    issued_at: datetime | None


def _no_store(response: Response) -> None:
    """A credential must not sit in a shared cache or a browser's history.

    `no-store` rather than `no-cache`: the latter permits storage and requires
    revalidation, which is not the same promise.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _entitled_project(conn, project_ref: str, principal, request: Request):
    """Resolve, authorize and check the capability, in that order.

    The order is the point. A non-member gets `404` before anything reveals
    whether the project exists -- a project ref is the customer's API subdomain
    (ADR-008), so confirming one confirms a target. Only a caller who has
    already proved membership can reach the `403`s below, so those disclose
    nothing they did not already know.
    """
    project = models.get_project_by_ref(conn, project_ref)
    if project is None or not principal.is_member_of(project.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    require_manager(principal, project.org_id)

    row = db.one(
        conn,
        "SELECT pr.status, pr.database_name, n.internal_host, n.db_port, "
        "       pl.code AS plan_code, pl.config_json "
        "  FROM projects pr "
        "  JOIN plans pl ON pl.id = pr.plan_id "
        "  LEFT JOIN nodes n ON n.id = pr.node_id "
        " WHERE pr.id = %s",
        (project.id,),
    )
    if row is None or row["status"] not in tenant_access.SERVING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the project is not ready to serve connections",
        )

    allowed = entitlements.resolve(row["plan_code"], row["config_json"])
    if not allowed.direct_database_access:
        # 403 rather than 404: the caller is a manager of a project they can
        # see, and telling them the capability is not on their plan is the
        # answer to the question they asked. ADR-005 is what makes it a refusal
        # rather than a provisioning step.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this project's plan does not include direct database access",
        )
    if row["internal_host"] is None or row["db_port"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the project is not ready to serve connections",
        )

    enforce_limit(request, bucket=CREDENTIAL_BUCKET, limit=CREDENTIAL_LIMIT, subject=str(project.id))
    return project, row, allowed


def _credential(conn, request: Request, project_id: uuid.UUID) -> tuple[str, datetime | None]:
    """The application's own key ring, not one built from the environment.

    `request.app.state.key_ring` is what every other route that unwraps a
    credential uses. Building a second one from `config.load()` reads whatever
    KEK the process was started with, which is the same value in production and
    a different one under a test application -- so the mistake is invisible
    where it matters and loud where it does not.
    """
    secret = provisioning.load_credential(
        conn, project_id=project_id, credential_type="db_client",
        key_ring=request.app.state.key_ring,
    )
    row = db.one(
        conn,
        "SELECT created_at FROM project_credentials "
        " WHERE project_id = %s AND credential_type = 'db_client' AND revoked_at IS NULL",
        (project_id,),
    )
    return secret, row["created_at"] if row else None


def _database_domain() -> str:
    from services.control_plane import config

    return config.load().database_domain


def _audit(conn, project_id: uuid.UUID, principal, event_type: str) -> None:
    """Recorded with no detail at all, deliberately.

    There is nothing about a credential that is safe to put in a table an
    operator reads, and the useful facts -- which project, which user, when --
    are the columns rather than the payload.
    """
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, 'user', %s, %s, '{}'::jsonb)",
        (project_id, principal.user.id, event_type),
    )


@router.get(
    "/projects/{project_ref}/database/connection",
    response_model=ConnectionOut,
    summary="The project's direct PostgreSQL connection details",
)
def get_connection(
    project_ref: str, request: Request, response: Response, principal: CurrentPrincipal
) -> ConnectionOut:
    with db.connection() as conn:
        project, row, _ = _entitled_project(conn, project_ref, principal, request)
        password, issued_at = _credential(conn, request, project.id)
        _audit(conn, project.id, principal, VIEWED)
        conn.commit()

    _no_store(response)
    return _connection_out(
        row, project.project_ref, project.database_name, password, issued_at
    )


@router.post(
    "/projects/{project_ref}/database/connection/rotate",
    response_model=ConnectionOut,
    summary="Replace the project's direct PostgreSQL password",
)
def rotate_connection(
    project_ref: str, request: Request, response: Response, principal: CurrentPrincipal
) -> ConnectionOut:
    """A new password, effective immediately, without a node credential.

    Stored before it is applied, inside a transaction that is rolled back if
    the node refuses. The alternative order leaves the platform holding a
    password the node no longer accepts, which locks the customer out of their
    own database -- and this route exists for the case where they have just
    discovered their old one is public.

    The remaining window is a control-plane commit failing after the node has
    accepted the change. `cp-manage project rotate-client-credential` is the
    recovery, and it works from node admin rather than from the credential.
    """
    with db.connection() as conn:
        project, row, _ = _entitled_project(conn, project_ref, principal, request)
        current, _ = _credential(conn, request, project.id)
        names = provisioning.TenantNames.for_ref(project.project_ref)
        new_password = provisioning.generate_password()

        provisioning.store_credential(
            conn, project_id=project.id, credential_type="db_client",
            role_name=names.client, secret=new_password,
            key_ring=request.app.state.key_ring,
        )
        try:
            _apply_password(row, names, current=current, new_password=new_password)
        except psycopg.Error as exc:
            conn.rollback()
            # `from None`, and the sqlstate logged rather than the exception.
            # This path composes a DSN *containing the current password*, and a
            # psycopg connection error can echo the DSN it failed on -- so a
            # chained cause is a password one `exc_info` away from a log file.
            # The same discipline `sql_console` applies to its own connect.
            log.warning(
                "rotating a client credential failed: %s", exc.sqlstate or "no sqlstate"
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="the project's database did not accept a new password",
            ) from None
        _audit(conn, project.id, principal, ROTATED)
        conn.commit()
        issued = datetime.now(tz=None)

    _no_store(response)
    return _connection_out(
        row, project.project_ref, project.database_name, new_password, issued
    )


def _apply_password(row, names: provisioning.TenantNames, *, current: str, new_password: str) -> None:
    """Change the client role's password as the client role.

    `SET ROLE NONE`, not `RESET ROLE`. The client role arrives in the admin
    role because that is its session default for `role`, so `RESET` returns it
    there; `SET ROLE NONE` returns to the session user, which is the only
    identity permitted to change this password. Measured, along with the fact
    that the same session is refused `42501` for the admin role's.
    """
    # The *internal* address, because this connection is the control plane's
    # own. The per-project name the customer is given is a public DNS record
    # that need not resolve from in here, and resolving it would make an
    # operator-facing operation depend on customer-facing DNS.
    dsn = sql_console.executor_dsn(
        host=row["internal_host"], port=row["db_port"],
        database=names.database, role=names.client, password=current,
    )
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=sql_console.CONNECT_TIMEOUT_SECONDS
    ) as conn:
        conn.execute("SET ROLE NONE")
        conn.execute(
            psycopg.sql.SQL("ALTER ROLE {role} PASSWORD {password}").format(
                role=psycopg.sql.Identifier(names.client),
                password=psycopg.sql.Literal(new_password),
            )
        )


def _connection_out(
    row, project_ref: str, database: str, password: str, issued_at: datetime | None
) -> ConnectionOut:
    """A per-project host, never the node's.

    `<ref>.<database_domain>` follows ADR-008's shape for the API URL, and for
    the same two reasons plus one. It hides which node a project is on, which
    `docs/CONTROL-PLANE.md` already treats as something not to publish. It
    survives ADR-006's background move to another node, which a connection
    string naming the node would not -- and the customer would discover that as
    an outage rather than be told. And it is a name the platform can repoint.
    """
    host = f"{project_ref}.{_database_domain()}"
    user = f"{database}_client"
    return ConnectionOut(
        host=host,
        port=row["db_port"],
        database=database,
        user=user,
        password=password,
        connection_string=sql_console.executor_dsn(
            host=host, port=row["db_port"],
            database=database, role=user, password=password,
        ),
        issued_at=issued_at,
    )
