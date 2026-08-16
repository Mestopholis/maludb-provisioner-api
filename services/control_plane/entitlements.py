"""What a project's plan allows.

One place answers "what is this project entitled to". Before this, the same
question was asked three different ways: the email quota read
`plans.config_json` inline, provisioning took a `plan_settings` dict from
whichever caller happened to build one, and the PostgREST pool size was a module
constant. Three readers means three defaults and three behaviours when a value
is missing, which is how a plan limit ends up enforced in one place and ignored
in another.

Two rules, both of which exist because the alternative fails open:

- **A missing or unusable value falls back to a documented default, never to
  "unlimited".** `plans.config_json` is operator-supplied; the same reasoning
  `nodes.py` applies to node capacity applies here, and in the same direction.
- **Every default is a real number.** `specs/plans-and-limits.yaml` shipped with
  every value null, meaning "not yet approved". Null read as no-limit would make
  the free tier unbounded, so nulls resolve to the defaults below and the spec
  now carries them too.

`AGENTS.md` forbids hard-coding production plan limits in application logic. The
numbers here are defaults that a plan's `config_json` overrides -- the mechanism
is configuration-driven even where the starting values are in code, and a
deployment that disagrees changes a row rather than a release.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from services.control_plane import db

# PostgreSQL's own convention: 0 means no limit. Used for the timeout settings
# so "unlimited" is expressible without a None that every caller must handle.
UNLIMITED = 0


@dataclass(frozen=True)
class Entitlements:
    """A project's resolved allowances. Every field is a concrete value."""

    plan_code: str

    # -- API surface -------------------------------------------------------
    api_requests_per_window: int
    api_window_seconds: int
    concurrent_api_requests: int

    # -- database ----------------------------------------------------------
    database_connections: int
    postgrest_pool_size: int
    statement_timeout_ms: int
    lock_timeout_ms: int
    idle_in_transaction_timeout_ms: int
    work_mem_mb: int
    temp_file_limit_mb: int
    max_parallel_workers_per_gather: int
    database_storage_bytes: int

    # -- email -------------------------------------------------------------
    emails_per_day: int
    emails_per_month: int
    email_custom_sending_domain: bool
    email_confirmations_required: bool

    # -- capability flags --------------------------------------------------
    direct_database_access: bool
    realtime_connections: int

    # -- what an organization may accumulate -------------------------------
    # Phase 07 slice 5, and an abuse control rather than a capacity one: a free
    # tier open to the public is farmed by creating projects, and each one is a
    # database, four roles and a slot on a node whether or not anybody ever
    # connects to it. Bounded per organization because that is where projects
    # live; bounding it per *user* would be defeated by an invitation.
    max_projects: int

    def postgres_settings(self) -> dict[str, str]:
        """The GUCs provisioning applies to the project's login roles.

        ADR-017 is explicit that these are defaults for well-behaved clients
        rather than enforcement -- most are session-settable by anything holding
        direct SQL. They are worth setting anyway, and worth not describing as a
        control.

        A zero timeout is omitted rather than written as `0`: PostgreSQL reads 0
        as no limit, and an explicit no-limit on a role would override a
        stricter cluster default rather than inheriting it.
        """
        settings: dict[str, str] = {}
        if self.statement_timeout_ms:
            settings["statement_timeout"] = f"{self.statement_timeout_ms}ms"
        if self.lock_timeout_ms:
            settings["lock_timeout"] = f"{self.lock_timeout_ms}ms"
        if self.idle_in_transaction_timeout_ms:
            settings["idle_in_transaction_session_timeout"] = (
                f"{self.idle_in_transaction_timeout_ms}ms"
            )
        if self.work_mem_mb:
            settings["work_mem"] = f"{self.work_mem_mb}MB"
        if self.temp_file_limit_mb:
            settings["temp_file_limit"] = f"{self.temp_file_limit_mb}MB"
        # Not guarded: 0 is a meaningful value here -- it disables parallel
        # query for a plan, which is exactly what the free tier wants.
        settings["max_parallel_workers_per_gather"] = str(self.max_parallel_workers_per_gather)
        return settings

    def connection_limits(self) -> dict[str, int]:
        """Per-role CONNECTION LIMIT, in the shape `create_roles` expects.

        The auth role gets a small fixed allowance rather than the project's:
        GoTrue opens a handful of connections and does not scale with the
        customer's traffic, and giving it the full allowance would let Auth
        exhaust what the Data API needs.
        """
        return {"authenticator": self.database_connections, "auth": AUTH_ROLE_CONNECTIONS}


AUTH_ROLE_CONNECTIONS = 5

# Defaults per plan. Deliberately generous on the paid tiers and deliberately
# tight on free: the free tier is the one sharing a node with everyone else, and
# ADR-022 measured connections rather than memory as the binding constraint.
#
# These are starting values, not approved public limits -- `AGENTS.md` requires
# them to be overridable, and `plans.config_json` is what overrides them.
DEFAULTS: dict[str, dict[str, Any]] = {
    "free": {
        "api_requests_per_window": 300,
        "api_window_seconds": 60,
        "concurrent_api_requests": 10,
        "database_connections": 10,
        "postgrest_pool_size": 3,
        "statement_timeout_ms": 8_000,
        "lock_timeout_ms": 3_000,
        "idle_in_transaction_timeout_ms": 30_000,
        "work_mem_mb": 4,
        "temp_file_limit_mb": 256,
        # Parallel query multiplies a single query's cost across a shared node.
        "max_parallel_workers_per_gather": 0,
        # Net of the ~23 MB maludb_core baseline (ADR-015), which is why this is
        # not a round 500 MB: a quota that counts the extension is smaller than
        # the number printed on it.
        "database_storage_bytes": 500 * 1024 * 1024,
        "emails_per_day": 100,
        "emails_per_month": 1_000,
        "email_custom_sending_domain": False,
        "email_confirmations_required": True,
        "direct_database_access": False,
        "realtime_connections": 0,
        # Enough to try the platform properly -- an app and a scratch copy --
        # and few enough that farming costs an account per pair rather than
        # being free once one account exists.
        "max_projects": 2,
    },
    "starter": {
        "api_requests_per_window": 3_000,
        "api_window_seconds": 60,
        "concurrent_api_requests": 50,
        "database_connections": 30,
        "postgrest_pool_size": 6,
        "statement_timeout_ms": 30_000,
        "lock_timeout_ms": 10_000,
        "idle_in_transaction_timeout_ms": 120_000,
        "work_mem_mb": 8,
        "temp_file_limit_mb": 2_048,
        "max_parallel_workers_per_gather": 2,
        "database_storage_bytes": 8 * 1024 * 1024 * 1024,
        "emails_per_day": 5_000,
        "emails_per_month": 50_000,
        "email_custom_sending_domain": True,
        "email_confirmations_required": True,
        "direct_database_access": True,
        "realtime_connections": 200,
        "max_projects": 20,
    },
    "production": {
        "api_requests_per_window": 30_000,
        "api_window_seconds": 60,
        "concurrent_api_requests": 200,
        "database_connections": 90,
        "postgrest_pool_size": 12,
        # No statement timeout by default on production: a long analytical query
        # is a legitimate workload at this tier, and ADR-009 puts the real
        # protection at the gateway and in node scheduling rather than here.
        "statement_timeout_ms": UNLIMITED,
        "lock_timeout_ms": 30_000,
        "idle_in_transaction_timeout_ms": 300_000,
        "work_mem_mb": 16,
        "temp_file_limit_mb": 16_384,
        "max_parallel_workers_per_gather": 4,
        "database_storage_bytes": 100 * 1024 * 1024 * 1024,
        "emails_per_day": 50_000,
        "emails_per_month": 1_000_000,
        "email_custom_sending_domain": True,
        "email_confirmations_required": True,
        "direct_database_access": True,
        "realtime_connections": 2_000,
        "max_projects": 100,
    },
}

# What an unknown plan code resolves to. The tightest tier, because a project
# whose plan we cannot identify should not be handed production allowances.
FALLBACK_PLAN = "free"


def _int_from(config: dict[str, Any], key: str, default: int) -> int:
    """Read an integer, falling back on anything unusable.

    `plans.config_json` is operator-supplied, so the same reasoning `nodes.py`
    applies to node capacity applies here: a malformed value must not raise
    mid-request, and must not be read as unlimited.
    """
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    # NaN and infinity survive the isinstance check and then raise inside int().
    # `nan < 0` is also False, so the sign check below does not catch them.
    if isinstance(value, float) and not math.isfinite(value):
        return default
    if value < 0:
        return default
    return int(value)


def _bool_from(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key)
    return value if isinstance(value, bool) else default


def resolve(plan_code: str | None, config: dict[str, Any] | None) -> Entitlements:
    """Merge a plan's stored configuration over the defaults for its tier.

    Pure, so the merge is testable without a database -- which matters because
    the interesting cases are all about malformed input.
    """
    code = plan_code if plan_code in DEFAULTS else FALLBACK_PLAN
    defaults = DEFAULTS[code]
    limits = (config or {}).get("limits")
    if not isinstance(limits, dict):
        limits = {}

    return Entitlements(
        plan_code=code,
        api_requests_per_window=_int_from(limits, "api_requests_per_window", defaults["api_requests_per_window"]),
        api_window_seconds=_int_from(limits, "api_window_seconds", defaults["api_window_seconds"]),
        concurrent_api_requests=_int_from(limits, "concurrent_api_requests", defaults["concurrent_api_requests"]),
        database_connections=_int_from(limits, "database_connections", defaults["database_connections"]),
        postgrest_pool_size=_int_from(limits, "postgrest_pool_size", defaults["postgrest_pool_size"]),
        statement_timeout_ms=_int_from(limits, "statement_timeout_ms", defaults["statement_timeout_ms"]),
        lock_timeout_ms=_int_from(limits, "lock_timeout_ms", defaults["lock_timeout_ms"]),
        idle_in_transaction_timeout_ms=_int_from(
            limits, "idle_in_transaction_timeout_ms", defaults["idle_in_transaction_timeout_ms"]
        ),
        work_mem_mb=_int_from(limits, "work_mem_mb", defaults["work_mem_mb"]),
        temp_file_limit_mb=_int_from(limits, "temp_file_limit_mb", defaults["temp_file_limit_mb"]),
        max_parallel_workers_per_gather=_int_from(
            limits, "max_parallel_workers_per_gather", defaults["max_parallel_workers_per_gather"]
        ),
        database_storage_bytes=_int_from(limits, "database_storage_bytes", defaults["database_storage_bytes"]),
        emails_per_day=_int_from(limits, "emails_per_day", defaults["emails_per_day"]),
        emails_per_month=_int_from(limits, "emails_per_month", defaults["emails_per_month"]),
        email_custom_sending_domain=_bool_from(
            limits, "email_custom_sending_domain", defaults["email_custom_sending_domain"]
        ),
        email_confirmations_required=_bool_from(
            limits, "email_confirmations_required", defaults["email_confirmations_required"]
        ),
        direct_database_access=_bool_from(
            (config or {}), "direct_database_access", defaults["direct_database_access"]
        ),
        realtime_connections=_int_from(limits, "realtime_connections", defaults["realtime_connections"]),
        max_projects=_int_from(limits, "max_projects", defaults["max_projects"]),
    )


def for_project(conn: psycopg.Connection, project_id: uuid.UUID) -> Entitlements:
    """A project's entitlements, resolved from its plan.

    A project with no plan resolves to the fallback tier rather than raising:
    the caller is usually on a request path, and refusing to serve because a
    plan row is missing turns a bookkeeping gap into an outage.
    """
    row = db.one(
        conn,
        "SELECT p.code, p.config_json FROM projects pr "
        " LEFT JOIN plans p ON p.id = pr.plan_id WHERE pr.id = %s",
        (project_id,),
    )
    if row is None:
        return resolve(None, None)
    return resolve(row["code"], row["config_json"])
