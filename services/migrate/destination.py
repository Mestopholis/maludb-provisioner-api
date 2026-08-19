"""Writing to a MaluDB project, through the door every client uses (ADR-042).

The CLI has **no privileged path**. It applies a schema by calling
`POST /v1/projects/{ref}/sql` with the customer's own platform token, which is
the same route a dashboard would call and is bounded by the same ceiling: the
statement runs as `mldb_<ref>_admin`, entered from the executor role, under the
plan's timeout and row cap (ADR-039). That is what keeps slice 1's containment
meaningful -- a migration tool that needed more privilege than the console would
have been an argument for giving the console more.

**The token is the customer's and is treated like the source DSN**: read from
`MALUDB_TOKEN` by preference, never written to output, never included in an
error. A failed request reports the API's own `detail`, which is the customer's
database talking about the customer's SQL.

**429 is expected, not exceptional.** The console's rate limit is per project
and deliberately tight -- one statement per eight seconds on free -- so a
migration of several batches will meet it. `Retry-After` is honoured rather than
guessed at, and the wait is reported, because a customer watching a cutover
should see "waiting for the rate limit" rather than a stalled cursor.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from services.migrate import report

DEFAULT_BASE_URL = "https://api.maludb.com"
TOKEN_ENV = "MALUDB_TOKEN"  # noqa: S105 - the variable's name, not a token

# A batch refused for rate is retried this many times before the migration
# stops. Bounded rather than infinite: a cutover that cannot make progress
# should say so while somebody is still watching.
MAX_RATE_RETRIES = 10

# What a `Retry-After` is trusted up to. A header asking for an hour is a
# reason to stop and tell the customer, not to sleep through their window.
MAX_RETRY_AFTER_SECONDS = 120


class DestinationError(RuntimeError):
    """The destination refused something. Never carries the token."""


@dataclass
class Applied:
    """What actually ran, for the report and for the freeze measurement."""

    batches: int = 0
    statements: int = 0
    seconds: float = 0.0
    rate_limited_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


class Destination:
    """One MaluDB project, reachable over HTTP.

    `transport` is injected so the tests can drive a real control-plane
    application rather than a mock: the assertions that matter -- that a dumped
    schema applies, that RLS policies survive, that the destination ends up with
    the objects the source had -- are only worth making against the real route.
    It takes `(method, url, json, headers)` and returns anything with
    `status_code` and `.json()`, which is what both httpx and a test client are.
    """

    def __init__(
        self,
        project_ref: str,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project_ref = project_ref
        self._token = token
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._sleep = sleep

    # -- the wire ---------------------------------------------------------

    def _send(self, method: str, path: str, payload: dict | None = None):
        # The token goes in a header and nowhere else -- not into a URL, not
        # into a message, not into the report.
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._transport is not None:
            return self._transport(method, path, payload, headers)

        import httpx

        return httpx.request(
            method, f"{self.base_url}{path}", json=payload, headers=headers, timeout=120
        )

    def _request(self, path: str, payload: dict) -> tuple[int, dict]:
        response = self._send("POST", path, payload)
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body

    def execute(self, sql: str) -> dict:
        """Run one batch, waiting out the rate limit rather than failing on it."""
        waited = 0.0
        for attempt in range(MAX_RATE_RETRIES + 1):
            status, body = self._request(f"/v1/projects/{self.project_ref}/sql", {"statement": sql})
            if status == 200:
                body["_rate_limited_seconds"] = waited
                return body
            if status == 429 and attempt < MAX_RATE_RETRIES:
                delay = self._retry_after(body)
                waited += delay
                self._sleep(delay)
                continue
            raise DestinationError(self._explain(status, body))
        raise DestinationError(
            f"the project's rate limit refused {MAX_RATE_RETRIES} attempts in a row"
        )

    @staticmethod
    def _retry_after(body: dict) -> float:
        # The route sends `Retry-After`, but a body-only failure should still
        # back off rather than spin.
        raw = body.get("retry_after") if isinstance(body, dict) else None
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            delay = 5.0
        return max(1.0, min(delay, MAX_RETRY_AFTER_SECONDS))

    @staticmethod
    def _explain(status: int, body: dict) -> str:
        detail = body.get("detail") if isinstance(body, dict) else None
        if status == 401:
            return "the platform token was refused (401). Check MALUDB_TOKEN."
        if status == 404:
            return (
                "no such project, or it does not belong to you (404). Check the project ref "
                "and that the token belongs to a member of its organization."
            )
        if status == 403:
            return f"the destination refused this (403): {detail or 'no detail given'}"
        if status == 409:
            return "the project is not ready to serve SQL yet (409). Wait for provisioning."
        if status == 503:
            return (
                "the project's SQL console is not configured yet (503). An operator needs to "
                "run `cp-manage project backfill-executor` for it."
            )
        return f"the destination refused a statement ({status}): {detail or 'no detail given'}"

    # -- what a migration does with it ------------------------------------

    def install_extensions(self, names: list[str]) -> Applied:
        """`CREATE EXTENSION IF NOT EXISTS` for each, in one request.

        `IF NOT EXISTS` because provisioning already installed several of them
        (ADR-015), and a migration re-run must not fail on what it already did.
        Names are quoted because `uuid-ossp` needs it -- the extension this
        whole path exists for.
        """
        if not names:
            return Applied()
        statements = [f'CREATE EXTENSION IF NOT EXISTS "{name}";' for name in sorted(names)]
        return self.apply(["\n".join(statements)], statement_count=len(statements))

    def apply(self, batches: list[str], *, statement_count: int | None = None) -> Applied:
        applied = Applied()
        started = time.monotonic()
        for batch in batches:
            body = self.execute(batch)
            applied.batches += 1
            applied.rate_limited_seconds += body.get("_rate_limited_seconds", 0.0)
        applied.statements = statement_count if statement_count is not None else 0
        applied.seconds = time.monotonic() - started
        return applied

    def import_auth(self, payload: dict) -> dict:
        """One batch of migrated users or identities (slice 7).

        A separate route from the console because the console's role cannot
        write `auth.users` at all -- and granting it that would put every end
        user's password hash within reach of console access. The platform holds
        the auth credential and composes the statements; this sends values.
        """
        status_code, body = self._request(
            f"/v1/projects/{self.project_ref}/auth/import", payload
        )
        if status_code != 200:
            raise DestinationError(self._explain(status_code, body))
        return body

    def count_rows(self, table: str) -> int:
        """What the destination actually holds, through the same route.

        The row-count check ADR-044 relies on is worthless if both sides of the
        comparison are client-side counters -- see `data.CopyReport.mismatches`.
        `ONLY` matches how the copier reads, so a partitioned parent is not
        counted twice.
        """
        from psycopg import sql

        # `sql.Identifier`, not an f-string: the name comes from the source
        # catalogue, and hand-rolled quoting is what the rest of this package
        # deliberately avoids.
        schema, _, name = table.partition(".")
        quoted = sql.Identifier(schema, name).as_string()
        body = self.execute(f"SELECT count(*) AS n FROM ONLY {quoted};")  # noqa: S608
        for result in reversed(body.get("results", [])):
            for row in result.get("rows", []):
                if "n" in row:
                    return int(row["n"])
        raise DestinationError(f"the destination did not answer a row count for {table}")

    def schema_snapshot(self) -> dict:
        """The destination's own catalogue, for the after-the-fact comparison.

        Slice 2's introspection route. Used to check that what was applied is
        what arrived rather than trusting that no error meant success.

        A truncated snapshot is refused rather than returned. The route caps
        each catalogue's rows and, since ADR-046, the bytes of the whole
        document -- and a comparison run against a partial catalogue would
        report a missing object for every one the cap left out, or worse, agree
        because both sides were cut at the same place.
        """
        response = self._send("GET", f"/v1/projects/{self.project_ref}/database/schema")
        if response.status_code != 200:
            raise DestinationError(
                f"could not read the destination's schema ({response.status_code})"
            )
        body = response.json()
        truncated = body.get("truncated") or []
        if truncated:
            # Sanitised on the way to an error the CLI prints: these are
            # catalogue names the *server* chose, and slice 8's review settled
            # that anything crossing into a terminal is sanitised where it is
            # printed rather than where it is trusted.
            names = ", ".join(sorted(report.sanitise(name) for name in truncated))
            raise DestinationError(
                f"the destination's schema was returned truncated ({names}); "
                "narrow it with a schema filter or raise the project's "
                "sql_console_max_bytes"
            )
        return body
