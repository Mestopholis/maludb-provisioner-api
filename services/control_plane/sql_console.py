"""Running a customer's SQL on their behalf (ADR-039).

The security boundary here is **not** this module. It is `mldb_<ref>_admin`,
which Phase 02 and the direct-SQL slice bounded: it cannot reach another tenant,
install an extension, grant extension functions to `anon`, own or drop `public`,
or hold `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS` or `REPLICATION`.
`tests/test_direct_sql.py` pins all of that. What this module adds is a caller,
not a privilege.

What it *is* responsible for is the layer ADR-017 proved the database cannot
provide. Five of the six per-statement controls are `context = user`, and a
tenant with direct SQL ran `SET statement_timeout = 0` successfully during that
verification. So the ceiling here is enforced by the platform holding the
connection and cancelling it out of band, and the GUCs below are set as a
courtesy to well-behaved clients rather than as the control.

Three things follow from customer SQL being arbitrary text:

- **`RESET ROLE` is reachable and is not contained.** It returns the session to
  the executor role, which is a member of the admin role and can `SET ROLE`
  back. That is the intended ceiling; see `specs/tenant-role-model.md`.
- **The row cap is applied while fetching, not by appending `LIMIT`.** Appending
  is defeated by the submitted text ending in its own `LIMIT`, by a trailing
  comment, or by a second statement.
- **The timeout is wall clock, not `statement_timeout`.** A statement that sets
  its own timeout to zero is still cancelled.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

# How long to wait for the TCP connect and authentication, separately from the
# statement's own ceiling. A node that is unreachable must fail the request
# rather than hold a worker for the statement timeout.
CONNECT_TIMEOUT_SECONDS = 5


class ConsoleError(RuntimeError):
    """A failure that is safe to return to the caller.

    Never carries driver text for a *connection* failure, which can contain the
    DSN and therefore the executor password. Statement errors are different and
    are returned deliberately: a customer debugging their own SQL needs
    PostgreSQL's message, and it is their own database's message.
    """


@dataclass
class Result:
    """One statement's outcome.

    `rows` is empty for a statement that returns none -- DDL, or an `UPDATE`
    without `RETURNING`. `row_count` is PostgreSQL's own count and is reported
    even then, which is how a customer sees that their `DELETE` did something.
    """

    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = -1
    truncated: bool = False
    command: str | None = None


def executor_dsn(*, host: str, port: int, database: str, role: str, password: str) -> str:
    """Built from parts, never by substituting into another DSN.

    `tests/test_provisioning.py` records why: string replacement against the
    admin DSN left the admin password in place, and the resulting authentication
    failure looked like a lockdown working.
    """
    return (
        f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def _cancel_after(conn: psycopg.Connection, seconds: float) -> threading.Timer:
    """The control ADR-017 leaves standing.

    `Connection.cancel()` is documented safe to call from another thread and
    sends the request over its own connection, which is what makes this
    independent of anything the running statement has done to its session.
    """
    timer = threading.Timer(seconds, conn.cancel)
    timer.daemon = True
    timer.start()
    return timer


def execute(
    dsn: str,
    statement: str,
    *,
    run_as: str,
    row_limit: int,
    timeout_ms: int,
    read_only: bool = False,
) -> list[Result]:
    """Run `statement` as `run_as` and return every result set it produced.

    `read_only` puts the session in a read-only transaction, which is how a
    storage-restricted project is refused its writes. PostgreSQL enforces it,
    so it holds against any statement rather than against the ones a parser
    happened to recognise.
    """
    if timeout_ms <= 0:  # pragma: no cover - entitlements refuses a zero
        raise ValueError("timeout_ms must be positive; a zero ceiling is no ceiling")

    try:
        conn = psycopg.connect(
            dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS, row_factory=dict_row
        )
    except psycopg.Error as exc:
        # Deliberately not `str(exc)`: a connection error can echo the DSN.
        log.warning("sql console could not reach the tenant database: %s", exc.sqlstate)
        raise ConsoleError("could not reach the project's database") from exc

    timer: threading.Timer | None = None
    try:
        with conn.cursor() as setup:
            # Applied before SET ROLE. ADR-017's first finding is that role
            # settings do not survive into a SET ROLE, so these are set on the
            # session explicitly rather than left on the role.
            # `set_config` rather than `SET`, because `SET` takes no bind
            # parameter -- it is parsed as a utility statement, and `SET
            # statement_timeout = $1` is a syntax error rather than a
            # substitution. The alternative is composing the number into the
            # statement, which works and is one edit away from composing
            # something that is not a number.
            setup.execute("SELECT set_config('statement_timeout', %s, false)", (str(timeout_ms),))
            setup.execute(
                "SELECT set_config('idle_in_transaction_session_timeout', %s, false)",
                (str(timeout_ms),),
            )
            # An identifier, so it is composed rather than parameterised. The
            # value comes from `TenantNames`, which validates the ref against a
            # strict alphabet before deriving any name from it.
            setup.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(run_as)))
            if read_only:
                # Session-level, so it survives the statement opening its own
                # transaction. A customer's `SET default_transaction_read_only
                # = off` is refused: changing it requires a superuser when the
                # session is already read-only.
                setup.execute("SELECT set_config('default_transaction_read_only', 'on', false)")
                setup.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

        timer = _cancel_after(conn, timeout_ms / 1000)
        results: list[Result] = []
        with conn.cursor() as cur:
            cur.execute(statement)  # type: ignore[arg-type]
            while True:
                result = Result(row_count=cur.rowcount, command=cur.statusmessage)
                if cur.description is not None:
                    result.columns = [column.name for column in cur.description]
                    # One past the cap, so "there was more" is observed rather
                    # than inferred from a full page.
                    fetched = cur.fetchmany(row_limit + 1)
                    result.truncated = len(fetched) > row_limit
                    result.rows = fetched[:row_limit]
                results.append(result)
                if not cur.nextset():
                    break
        return results
    except psycopg.errors.QueryCanceled as exc:
        raise ConsoleError(f"statement cancelled after {timeout_ms} ms") from exc
    except psycopg.Error as exc:
        # The customer's own database answering about the customer's own SQL.
        # Returned so they can act on it; `sqlstate` first because it is the
        # part a client can branch on.
        raise ConsoleError(f"{exc.sqlstate}: {_first_line(exc)}") from exc
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def _first_line(exc: psycopg.Error) -> str:
    """PostgreSQL's message without its CONTEXT and QUERY blocks.

    Those repeat the submitted statement back, which is the customer's own text
    and harmless -- but they also carry the platform's internal function bodies
    when the failure came from a bootstrap trigger, and those are not the
    customer's to read.
    """
    message = str(exc).strip()
    return message.splitlines()[0] if message else "statement failed"


def audit_detail(statement: str, *, results: list[Result], limit: int = 2_000) -> dict[str, Any]:
    """What is recorded about a statement, and what deliberately is not.

    The statement text is stored. It is the customer's own SQL against their own
    database, it is what makes the audit trail answer "who changed this schema",
    and Phase 07's audit surface is already allowlisted event by event.

    It is truncated, because an unbounded write of caller-supplied text into an
    operator-read table is a denial-of-service on the people reading it. Result
    *rows* are never recorded: they are tenant data, and an audit table is the
    wrong place for a copy of it.
    """
    return {
        "statement": statement[:limit],
        "statement_truncated": len(statement) > limit,
        "result_count": len(results),
        "commands": [r.command for r in results if r.command][:10],
    }


def new_statement_id() -> uuid.UUID:
    return uuid.uuid4()
