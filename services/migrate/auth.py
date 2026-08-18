"""Reading a Supabase project's users, for import (Phase 08 slice 7, ADR-043).

Email and password only. An identity whose only authenticator is a provider
configuration migrates into an account nobody can sign in to, which is worse
than not migrating it -- the scanner already reports one as a blocker, and the
import route refuses one that reaches it anyway.

**Password hashes come across.** GoTrue stores bcrypt in
`auth.users.encrypted_password` on both sides, so a migrated user signs in with
the password they already had. That is most of the value of migrating auth at
all: the alternative is every user of the customer's application discovering at
cutover that they must reset.

What is deliberately not read: `confirmation_token`, `recovery_token`, the
`email_change_*` tokens and `reauthentication_token`. Those are in-flight
secrets for flows that were happening on *Supabase*, and carrying them across
would let a token minted there be redeemed here. A user midway through a
password reset starts it again, which is the correct outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

# What the import route accepts. Kept here as well so the CLI sends nothing it
# knows will be dropped -- the route names what it discarded, but a tool that
# knowingly sends rejects is a tool whose warnings get ignored.
USER_COLUMNS = (
    "id", "aud", "role", "email", "encrypted_password",
    "email_confirmed_at", "last_sign_in_at",
    "raw_app_meta_data", "raw_user_meta_data",
    "created_at", "updated_at", "phone", "phone_confirmed_at",
)

IDENTITY_COLUMNS = (
    "id", "user_id", "provider", "provider_id", "identity_data",
    "last_sign_in_at", "created_at", "updated_at",
)

# One request's worth. The route caps at 500 of each.
BATCH_ROWS = 200


class AuthError(RuntimeError):
    """Auth could not be read. Never carries the DSN."""


@dataclass
class AuthExport:
    users: list[dict[str, Any]] = field(default_factory=list)
    identities: list[dict[str, Any]] = field(default_factory=list)
    # Users whose identity is an external provider, counted rather than carried.
    external_identities: int = 0

    def batches(self, size: int = BATCH_ROWS):
        """Users first, then identities: an identity references a user."""
        for start in range(0, len(self.users), size):
            yield {"users": self.users[start:start + size], "identities": []}
        for start in range(0, len(self.identities), size):
            yield {"users": [], "identities": self.identities[start:start + size]}


def _available(conn: psycopg.Connection, table: str, wanted: tuple[str, ...]) -> list[str]:
    """The wanted columns this *source* actually has.

    Supabase and this platform pin different GoTrue versions, and the source may
    be older or newer than either. Asking the catalogue rather than assuming is
    the difference between a migration and a `42703`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'auth' AND c.relname = %s
               AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (table,),
        )
        present = {row[0] for row in cur.fetchall()}
    return [column for column in wanted if column in present]


def read(conn: psycopg.Connection) -> AuthExport:
    """Every email/password user and identity, as JSON-ready values."""
    export = AuthExport()

    user_columns = _available(conn, "users", USER_COLUMNS)
    if not user_columns:
        raise AuthError("the source has no auth.users table to migrate")

    columns = psycopg.sql.SQL(", ").join(
        psycopg.sql.Identifier(column) for column in user_columns
    )
    # Soft-deleted users are not migrated. The column is recent enough that an
    # older source may not have it, so its absence means "this version had no
    # soft delete" rather than an error.
    where = (
        psycopg.sql.SQL(" WHERE deleted_at IS NULL")
        if _has_column(conn, "users", "deleted_at")
        else psycopg.sql.SQL("")
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            psycopg.sql.SQL("SELECT {columns} FROM auth.users{where}").format(
                columns=columns, where=where
            )
        )
        export.users = [_jsonable(row) for row in cur.fetchall()]

    identity_columns = _available(conn, "identities", IDENTITY_COLUMNS)
    if identity_columns:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                psycopg.sql.SQL("SELECT {columns} FROM auth.identities").format(
                    columns=psycopg.sql.SQL(", ").join(
                        psycopg.sql.Identifier(column) for column in identity_columns
                    )
                )
            )
            for row in cur.fetchall():
                if row.get("provider") != "email":
                    # ADR-043. Counted so the report can say how many accounts
                    # will need another way in, rather than dropping them
                    # silently.
                    export.external_identities += 1
                    continue
                export.identities.append(_jsonable(row))

    # An identity whose user was not carried would violate the foreign key.
    migrated = {user["id"] for user in export.users}
    export.identities = [i for i in export.identities if i.get("user_id") in migrated]
    return export


def _has_column(conn: psycopg.Connection, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'auth' AND c.relname = %s AND a.attname = %s
               AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (table, column),
        )
        return cur.fetchone() is not None


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """Values a JSON body can carry, without losing what the type meant.

    Timestamps become ISO strings and uuids their text form -- both of which
    PostgreSQL reads back into the same value through the column's input
    function, the same property slice 6c's copier relies on. `dict` and `list`
    stay as they are: `raw_user_meta_data` is `jsonb` on both sides and passing
    it through as JSON is exact.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (str, int, float, bool, dict, list)):
            out[key] = value
        else:
            out[key] = str(value)
    return out
