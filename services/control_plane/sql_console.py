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
- **A row cap bounds rows, not bytes.** `SELECT repeat('x', 1000000) FROM
  generate_series(1, 100)` returns exactly the free tier's hundred rows,
  reports itself untruncated, and costs the control plane ~200 MB of resident
  memory for a 100 MB response body -- measured 2026-08-19. Every declared
  limit was respected. So the fetch carries a byte budget too, set by the plan,
  and spent across every result set in one response.

  **What that budget does and does not bound, measured rather than assumed.**
  libpq buffers a whole result set before the first row can be refused, so the
  ~200 MB transient is unchanged by it: 202 MB peak with a 100 MiB budget, 203
  MB with a 2 MiB one. What changes is what is still held when the connection
  closes -- 100.0 MB against 2.0 MB of live Python objects -- and that is the
  half whose duration a caller controls, because it is held while the response
  is serialised and read. Streaming would bound the other half (+5 MB against
  +101 MB, measured with `Cursor.stream`) and cannot be used here: it is the
  extended protocol, and this route takes multi-statement text. ADR-046 records
  the residual and the options for closing it.

Impersonation (slice 3) changes which role logs in, not which controls apply.
A request that names `anon`, `authenticated` or `service_role` connects as
`mldb_<ref>_authenticator` -- the role PostgREST already uses, whose whole
purpose is `SET ROLE` into those three -- rather than as the executor. That is
what makes `RESET ROLE` land somewhere harmless: the authenticator is a member
of the three shared names and of nothing else, so a session cannot climb back to
`mldb_<ref>_admin` inside the request. Reusing the Data API's own role is also
the point of the feature: a policy debugged here is debugged as the application
will meet it.
"""

from __future__ import annotations

import json
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


# Rows are pulled in batches rather than in one `fetchmany(row_limit + 1)`, so
# the byte budget is consulted before the whole result set is resident. Small
# enough that a batch of wide rows is not itself the overshoot, large enough
# that a 5,000-row result is fifty round trips through the driver's buffer
# rather than five thousand.
FETCH_CHUNK = 100

# What one value of a type with no length is counted as: numbers, booleans,
# timestamps, uuids, NULL. The estimate exists to stop a response growing
# without bound, not to predict its serialised size to the byte.
SCALAR_BYTES = 16

# Nested containers are walked this far and no further. A jsonb value can nest
# arbitrarily, and a recursive size estimate is one more thing a customer's own
# data could drive into a `RecursionError`.
MAX_NESTING = 8


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


@dataclass
class Budget:
    """Bytes one response may materialise, spent across everything it fetches.

    Per response rather than per result set, because the response is one JSON
    document held in memory in its entirety: a statement returning ten result
    sets of the row cap each is ten times the cost of one, and a per-set budget
    would not notice.

    `spend` refuses rather than clamps. A value that does not fit is left out
    entirely and the caller reports a truncation -- returning half a row's text
    would be a corruption dressed as a limit.
    """

    remaining: int
    exhausted: bool = False

    def spend(self, amount: int) -> bool:
        if amount > self.remaining:
            self.exhausted = True
            return False
        self.remaining -= amount
        return True


def approx_bytes(value: Any, depth: int = 0) -> int:
    """A cheap estimate of what one returned value costs to hold.

    Cheap on purpose: it is charged against every value of every row, so it
    must not itself be a second pass over the data. Text and bytes are measured
    exactly because they are what makes a result large; everything else is
    counted flat.

    It under-counts Python's own per-object overhead and the JSON encoder's
    copy, so a budget spent here is less memory than the response will actually
    take, not more. That direction is deliberate -- the number is a ceiling on
    tenant data, and the multiplier above it belongs to the plan's sizing.
    """
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        return len(value)
    if depth < MAX_NESTING:
        if isinstance(value, dict):
            return sum(
                approx_bytes(k, depth + 1) + approx_bytes(v, depth + 1)
                for k, v in value.items()
            ) or SCALAR_BYTES
        if isinstance(value, (list, tuple)):
            return sum(approx_bytes(v, depth + 1) for v in value) or SCALAR_BYTES
    return SCALAR_BYTES


def fetch_bounded(
    cur: psycopg.Cursor, *, row_limit: int, budget: Budget
) -> tuple[list[dict[str, Any]], bool]:
    """Rows up to the plan's caps, and whether either cap stopped the fetch.

    One `truncated` for both, because they answer the same question -- you are
    not seeing all of it -- and a client that must render "showing the first N"
    does not act differently on which limit bit.

    The row that overruns the budget is discarded rather than returned, so the
    ceiling holds for the response as well as for the fetch.
    """
    rows: list[dict[str, Any]] = []
    while True:
        # One past the cap, so "there was more" is observed rather than
        # inferred from a full page.
        want = row_limit + 1 - len(rows)
        batch = cur.fetchmany(min(FETCH_CHUNK, want))
        if not batch:
            return rows, False
        for row in batch:
            if len(rows) >= row_limit:
                return rows, True
            if not budget.spend(sum(approx_bytes(value) for value in row.values())):
                return rows, True
            rows.append(row)


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


def cancel_after(conn: psycopg.Connection, seconds: float) -> threading.Timer:
    """The control ADR-017 leaves standing.

    `Connection.cancel()` is documented safe to call from another thread and
    sends the request over its own connection, which is what makes this
    independent of anything the running statement has done to its session.

    Public since slice 2: `introspection` holds a tenant connection too, and its
    queries are platform-authored but still run on a shared node against a
    schema of the customer's shape. A catalogue query is not exempt from taking
    too long.
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
    max_bytes: int,
    timeout_ms: int,
    claims: dict[str, Any] | None = None,
) -> list[Result]:
    """Run `statement` as `run_as` and return every result set it produced.

    `claims` is the impersonation half (slice 3): the JSON PostgREST would have
    put in `request.jwt.claims` for a request carrying that JWT, which is what
    `auth.uid()`, `auth.role()` and `auth.email()` read. It is set on the
    session, never composed -- the GUC name is a literal here and the value is
    bound -- and it is not a privilege: any session can set a custom GUC, and
    what bounds an impersonated statement is the role it runs as.

    There is deliberately no read-only mode here. The first version of this
    slice put the session in a read-only transaction to hold a storage-restricted
    project, and a probe on 2026-08-17 showed the submitted text escapes it:
    `SET default_transaction_read_only = off` is accepted inside a read-only
    session, and the next statement writes. The comment claiming otherwise was
    wrong.

    Storage restriction lives in grants instead (ADR-040), where it applies to
    the console and to paid direct SQL by the same mechanism and needs no
    special case on this path.

    `max_bytes` is spent across every result set the statement produced, not
    per set. A result cut short by it is marked `truncated` exactly as one cut
    short by `row_limit` is; see `fetch_bounded`.
    """
    if timeout_ms <= 0:  # pragma: no cover - entitlements refuses a zero
        raise ValueError("timeout_ms must be positive; a zero ceiling is no ceiling")
    if max_bytes <= 0:  # pragma: no cover - entitlements refuses a zero
        raise ValueError("max_bytes must be positive; a zero ceiling is no ceiling")

    budget = Budget(max_bytes)

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
            if claims is not None:
                # Before SET ROLE for the same reason as the timeouts, and
                # matching what bootstrap 002's helpers read: `request.jwt.claims`
                # plural, JSON, which is what PostgREST 14 sets. The legacy
                # `request.jwt.claim.<name>` form is deliberately not written --
                # a tenant's helpers coalesce both only if GoTrue's own migration
                # got that far, and setting the modern one is what the platform
                # can guarantee.
                setup.execute(
                    "SELECT set_config('request.jwt.claims', %s, false)", (json.dumps(claims),)
                )
            # An identifier, so it is composed rather than parameterised. The
            # value comes from `TenantNames`, which validates the ref against a
            # strict alphabet before deriving any name from it.
            setup.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(run_as)))

        timer = cancel_after(conn, timeout_ms / 1000)
        results: list[Result] = []
        with conn.cursor() as cur:
            cur.execute(statement)  # type: ignore[arg-type]
            while True:
                result = Result(row_count=cur.rowcount, command=cur.statusmessage)
                if cur.description is not None:
                    result.columns = [column.name for column in cur.description]
                    result.rows, result.truncated = fetch_bounded(
                        cur, row_limit=row_limit, budget=budget
                    )
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
        raise ConsoleError(f"{exc.sqlstate}: {first_line(exc)}") from exc
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def first_line(exc: psycopg.Error) -> str:
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
