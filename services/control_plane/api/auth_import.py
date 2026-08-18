"""Importing migrated users into a tenant's Auth (Phase 08 slice 7, ADR-043).

**Why this is a route at all.** Everything else the migration tool writes goes
through `POST /v1/projects/{ref}/sql` as `mldb_<ref>_admin`. Auth cannot: the
tenant's `auth` schema is owned by `mldb_<ref>_auth` (bootstrap 007) and the
admin role holds only `USAGE` on the schema. Measured 2026-08-18 -- the console's
role is denied both `SELECT` and `INSERT` on `auth.users`.

The alternative was to grant the admin role access. That was rejected by the
repository owner: it would put every end user's bcrypt hash within reach of
anyone with console access, on every tier, permanently, in exchange for a
one-off operation -- and would let a customer forge or confirm accounts behind
GoTrue's back. So the credential stays platform-side and this route is the only
thing that uses it.

**Nothing a customer sends is executed.** The request carries JSON rows, never
SQL. Every statement is composed here with `psycopg.sql`, from an **allowlist**
of columns, into two named tables. That is what makes a privileged connection
safe to expose at all: the caller chooses values, never shape.

The allowlist is deliberately narrower than `auth.users`. Absent by choice:

- `is_super_admin`, which is not a customer's to set;
- `confirmation_token`, `recovery_token`, `email_change_token_*` and
  `reauthentication_token` -- in-flight secrets for flows that were happening on
  a *different* platform. Carrying half-finished flows across a cutover invites
  a token minted by Supabase being redeemed against MaluDB;
- `instance_id`, legacy and always zero;
- `confirmed_at`, which newer GoTrue generates and refuses to be told.

**Column drift is reported, not guessed at.** Supabase and this platform pin
different GoTrue versions, so a column in the source may not exist here. The
route intersects the allowlist with what the destination's `auth.users`
actually has and names what it dropped, rather than failing the batch or
silently discarding a field.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request, status
from psycopg import sql
from pydantic import BaseModel, Field

from services.control_plane import db, entitlements, ratelimit, sql_console
from services.control_plane.api import tenant_access
from services.control_plane.api.auth_dep import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["auth-import"])

AUTH_IMPORT_BUCKET = "auth-import"

# Bounded so one request cannot be a memory attack before any limit is
# consulted, and small enough that a failure costs one batch rather than a
# migration.
MAX_ROWS_PER_REQUEST = 500

# What may be written, and nothing else. See the module docstring for what is
# deliberately absent and why.
USER_COLUMNS = frozenset(
    {
        "id", "email", "encrypted_password",
        "email_confirmed_at", "last_sign_in_at",
        "raw_app_meta_data", "raw_user_meta_data",
        "created_at", "updated_at", "phone", "phone_confirmed_at",
    }
)

# **`role` and `aud` are set here, never by the caller**, and the reason is the
# finding the slice 7 security review turned up.
#
# `auth.users.role` is not application data. GoTrue copies it into the `role`
# claim of every access token it mints, PostgREST is configured with no
# `jwt-role-claim-key` override, so it reads that claim and issues
# `SET LOCAL ROLE` -- and `service_role` is created `BYPASSRLS` with `ALL` on
# every table in `public`. Allowlisting the column therefore let a caller choose
# the database role their token would map to. With `aud`, `encrypted_password`
# and `email_confirmed_at` alongside it -- all needed to make an account usable,
# all legitimately allowlisted -- one request planted an account that signs in
# over the public gateway and reads and writes the whole tenant past RLS.
#
# What made it worse than "a member already has access": a member's
# `service_role` API key is revoked by key rotation, which is the standard
# offboarding action. A password-authenticated account is not, and it does not
# appear in the API-key inventory. It survives the member leaving.
#
# GoTrue's own signup always writes `authenticated` (`GOTRUE_JWT_DEFAULT_GROUP_NAME`),
# so no legitimate row has any other value and nothing is lost by fixing it.
IMPOSED_USER_VALUES = {
    "role": "authenticated",
    # Must match `GOTRUE_JWT_AUD` (`auth_workers.py`) or the password grant
    # cannot find the user.
    "aud": "authenticated",
}

# Keys inside `raw_app_meta_data` that Supabase-shaped policies commonly treat
# as authorization -- `auth.jwt() -> 'app_metadata' ->> 'role'` is a documented
# pattern. GoTrue emits the column verbatim as the `app_metadata` claim.
#
# Carried rather than stripped: it is the customer's own data, their policies
# already trusted it on Supabase, and dropping it would break a migration
# silently. But it is *counted*, and the CLI says so, because importing it is an
# authorization decision the customer is making rather than a copy.
CLAIM_LIKE_KEYS = ("role", "roles", "claims_admin")

IDENTITY_COLUMNS = frozenset(
    {
        "id", "user_id", "provider", "provider_id", "identity_data",
        "last_sign_in_at", "created_at", "updated_at",
    }
)

# ADR-043: email and password only. An identity whose authenticator is a
# provider configuration migrates into an account nobody can sign in to, and the
# scanner already reports one as a blocker -- this is the same rule enforced
# where it cannot be skipped.
ALLOWED_PROVIDERS = frozenset({"email"})


class ImportIn(BaseModel):
    users: list[dict[str, Any]] = Field(default_factory=list)
    identities: list[dict[str, Any]] = Field(default_factory=list)


class ImportOut(BaseModel):
    users_inserted: int
    identities_inserted: int
    # Rows whose id was already present. A migration that is retried after a
    # network failure must not double-count or fail; `ON CONFLICT DO NOTHING`
    # makes the retry a no-op and this is how the caller sees that.
    users_skipped: int
    identities_skipped: int
    # Columns the caller sent that this platform's GoTrue does not have, named
    # rather than dropped in silence: the two sides pin different versions, and
    # a customer should hear that `banned_until` did not come across.
    dropped_columns: list[str]
    # Imported users whose `raw_app_meta_data` carries a key policies commonly
    # read as authorization. Carried, because it is the customer's own data --
    # but surfaced, because importing it is a decision rather than a copy.
    users_with_claim_metadata: int = 0


def _import_limit(allowed: entitlements.Entitlements) -> ratelimit.Limit:
    window = max(1, allowed.sql_console_timeout_ms // 1000)
    return ratelimit.Limit(max(4, allowed.sql_console_concurrent * 2), window)


@router.post(
    "/projects/{project_ref}/auth/import",
    response_model=ImportOut,
    summary="Import migrated Auth users into a project",
)
def import_auth(
    project_ref: str, body: ImportIn, request: Request, principal: CurrentPrincipal
) -> ImportOut:
    if len(body.users) > MAX_ROWS_PER_REQUEST or len(body.identities) > MAX_ROWS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"at most {MAX_ROWS_PER_REQUEST} users and identities per request",
        )

    for identity in body.identities:
        provider = identity.get("provider")
        if provider not in ALLOWED_PROVIDERS:
            # ADR-043, enforced where it cannot be skipped rather than only in
            # the scanner the customer runs.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"identity provider {provider!r} is not migrated. Only email and "
                    "password identities are carried at launch."
                ),
            )

    with db.connection() as conn:
        access = tenant_access.resolve(
            conn, project_ref, principal, request,
            bucket=AUTH_IMPORT_BUCKET, limit_for=_import_limit,
            as_auth_role=True, require_manage=True,
        )
        allowed = access.allowed

        try:
            result = _write(access, body)
        except sql_console.ConsoleError as exc:
            # **Audited on the way out too.** The first version recorded only
            # the success path, so a request that planted users and then failed
            # on identities left the users committed, answered 400, and wrote
            # nothing to `audit_events` -- on the one route that holds the
            # tenant's most privileged credential. An operator asking how a row
            # appeared in `auth.users` would have found no record the route ran.
            _audit_failure(conn, access.project.id, principal, str(exc))
            conn.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        _audit(conn, access.project.id, principal, result)
        conn.commit()

    del allowed
    return result


def _write(access: tenant_access.TenantAccess, body: ImportIn) -> ImportOut:
    """Compose and run the two inserts on the tenant's own auth schema."""
    try:
        # **Not autocommit.** The two inserts are one unit: users then
        # identities, an identity referencing a user. With autocommit the users
        # committed and a failing identities insert left the tenant half
        # written -- reproduced during the review with a foreign key that could
        # not resolve. `ON CONFLICT DO NOTHING` already makes a retry safe, so
        # there is nothing to gain from committing early.
        conn = psycopg.connect(
            access.dsn, connect_timeout=sql_console.CONNECT_TIMEOUT_SECONDS
        )
    except psycopg.Error as exc:
        # Never `str(exc)`: a connection error can echo the DSN.
        raise sql_console.ConsoleError("could not reach the project's database") from exc

    timer = None
    try:
        with conn.cursor() as cur:
            # The console bounds every statement it runs; a connection holding a
            # more privileged credential should not be the one without a
            # ceiling. Wall clock as well as the GUC, for ADR-017's reason.
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(access.allowed.sql_console_timeout_ms),),
            )
        timer = sql_console.cancel_after(conn, access.allowed.sql_console_timeout_ms / 1000)

        with conn.cursor() as cur:
            present = _destination_columns(cur)
            dropped: set[str] = set()

            users, users_dropped = _rows_for(body.users, USER_COLUMNS, present["users"])
            for row in users:
                for column, value in IMPOSED_USER_VALUES.items():
                    if column in present["users"]:
                        row[column] = value
            identities, identities_dropped = _rows_for(
                body.identities, IDENTITY_COLUMNS, present["identities"]
            )
            dropped |= users_dropped | identities_dropped

            inserted_users = _insert(cur, "users", users)
            inserted_identities = _insert(cur, "identities", identities)
        conn.commit()

        return ImportOut(
            users_inserted=inserted_users,
            identities_inserted=inserted_identities,
            users_skipped=len(body.users) - inserted_users,
            identities_skipped=len(body.identities) - inserted_identities,
            dropped_columns=sorted(dropped),
            users_with_claim_metadata=_claim_like(body.users),
        )
    except psycopg.errors.QueryCanceled as exc:
        conn.rollback()
        raise sql_console.ConsoleError("the import took too long and was cancelled") from exc
    except psycopg.Error as exc:
        # Rolled back explicitly rather than left to the close: nothing written
        # by a request that failed should survive it.
        conn.rollback()
        raise sql_console.ConsoleError(f"{exc.sqlstate}: {sql_console.first_line(exc)}") from exc
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def _destination_columns(cur) -> dict[str, set[str]]:
    """What this platform's GoTrue actually has, per table."""
    cur.execute(
        """
        SELECT c.relname AS table_name, a.attname AS column_name
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'auth' AND c.relname IN ('users', 'identities')
           AND a.attnum > 0 AND NOT a.attisdropped
           -- A generated column refuses to be told a value. `confirmed_at` is
           -- one in newer GoTrue.
           AND a.attgenerated = ''
        """
    )
    found: dict[str, set[str]] = {"users": set(), "identities": set()}
    for table_name, column_name in cur.fetchall():
        found[table_name].add(column_name)
    if not found["users"]:
        raise sql_console.ConsoleError(
            "this project has no auth.users table. Auth has not been configured for it yet."
        )
    return found


def _rows_for(
    rows: list[dict[str, Any]], allowed: frozenset[str], present: set[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Keep the allowlisted columns this destination has; name the rest."""
    writable = allowed & present
    dropped: set[str] = set()
    kept: list[dict[str, Any]] = []
    for row in rows:
        offered = set(row)
        dropped |= offered - writable
        kept.append({column: row[column] for column in offered & writable})
    return kept, dropped


def _insert(cur, table: str, rows: list[dict[str, Any]]) -> int:
    """One statement per table, composed here from validated column names.

    `ON CONFLICT (id) DO NOTHING`, so a migration retried after a network
    failure is a no-op rather than a duplicate-key error mid-cutover.
    """
    if not rows:
        return 0

    columns = sorted({column for row in rows for column in row})
    if not columns:
        return 0

    statement = sql.SQL(
        "INSERT INTO auth.{table} ({columns}) VALUES {values} "
        "ON CONFLICT (id) DO NOTHING"
    ).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        values=sql.SQL(", ").join(
            sql.SQL("({})").format(
                sql.SQL(", ").join(_value(row.get(column)) for column in columns)
            )
            for row in rows
        ),
    )
    cur.execute(statement)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _audit_failure(conn, project_id: uuid.UUID, principal, error: str) -> None:
    """That the route ran, and that it did not finish."""
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, 'user', %s, 'project.auth.import_failed', %s)",
        (project_id, principal.user.id, psycopg.types.json.Jsonb({"error": error[:500]})),
    )


def _claim_like(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        metadata = row.get("raw_app_meta_data")
        if isinstance(metadata, dict) and any(key in metadata for key in CLAIM_LIKE_KEYS):
            count += 1
    return count


def _value(value: Any):
    """One JSON value as a SQL literal.

    `raw_user_meta_data` and `identity_data` arrive as dictionaries, and
    `sql.Literal` cannot adapt one -- `cannot adapt type 'dict'`. Wrapping in
    `Jsonb` is what makes it a `jsonb` literal rather than a coincidence of
    string formatting, and it is exact: both sides store `jsonb`.
    """
    if isinstance(value, (dict, list)):
        return sql.Literal(psycopg.types.json.Jsonb(value))
    return sql.Literal(value)


def _audit(conn, project_id: uuid.UUID, principal, result: ImportOut) -> None:
    """Counts only. An audit row naming migrated end users would put the
    customer's own users' identities in a table the platform's operators read."""
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, 'user', %s, 'project.auth.imported', %s)",
        (
            project_id,
            principal.user.id,
            psycopg.types.json.Jsonb(
                {
                    "users_inserted": result.users_inserted,
                    "identities_inserted": result.identities_inserted,
                    "dropped_columns": result.dropped_columns,
                }
            ),
        ),
    )
