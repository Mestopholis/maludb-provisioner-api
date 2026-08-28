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

    # -- object storage (ADR-056) ------------------------------------------
    # Both hard ceilings under ADR-050: refused at the point of use, never
    # converted into a charge, never reported to a payment provider. Present on
    # every tier including free, because Storage over the gateway is API access
    # and therefore inside ADR-005 rather than against it.
    #
    # `object_storage_bytes` is bytes held and is measured by a maintenance
    # pass. `egress_bytes_per_month` is bytes served and is counted as they
    # pass -- a distinction that matters because the second is the one a
    # customer can have consumed *for* them: a public bucket is served to
    # whoever has the URL, and the project pays the ceiling either way.
    object_storage_bytes: int
    egress_bytes_per_month: int

    # -- email -------------------------------------------------------------
    emails_per_day: int
    emails_per_month: int
    email_custom_sending_domain: bool
    email_confirmations_required: bool

    # -- capability flags --------------------------------------------------
    direct_database_access: bool
    realtime_connections: int

    # -- recovery (ADR-068) ------------------------------------------------
    # Both are *promises*, not repository settings, and the distinction is the
    # whole of ADR-068. A pgBackRest repository retains per stanza -- per node
    # -- so nothing here can make one tenant's bytes outlive another's on the
    # same node. What a plan buys is how far back the platform will honour a
    # request, and the node's own retention has to be at least as long or the
    # promise is one the repository cannot keep.
    #
    # `backup_retention_days` is how far back a restore may be asked for at all.
    # `pitr_window_hours` is how far back a *point in time* may be named, and 0
    # means no PITR -- the `realtime_connections` convention rather than the
    # timeout one, because here zero is an absent capability rather than an
    # absent limit. A plan with 0 restores to the state of a backup; it does not
    # pick a second.
    backup_retention_days: int
    pitr_window_hours: int

    # -- platform-mediated SQL (ADR-039) -----------------------------------
    # Deliberately *not* `direct_database_access`. That one means "this project
    # gets a credential and a reachable port"; this one means "the platform will
    # run a statement on the project's behalf". ADR-039 turns on the two being
    # separable: free gets the second and never the first, and Phase 09 can move
    # the first without touching this.
    #
    # Every tier defaults to true, which makes the flag look decorative. It is
    # not: it is the switch an operator throws for a single abusive project
    # without changing that project's plan, and a capability with no off switch
    # is one an incident cannot contain.
    sql_console: bool
    sql_console_row_limit: int
    # A row cap bounds rows, not bytes. A hundred rows of a megabyte each is
    # within `sql_console_row_limit` on the free tier and is a hundred megabytes
    # held in a process every tenant shares, for as long as the response takes
    # to encode and the caller takes to read it -- so this is the ceiling that
    # matters for a shared process, and ADR-046 measured both halves. Spent
    # across a whole response: every result set of a multi-statement request,
    # and every catalogue of a schema snapshot, draw on the same budget.
    sql_console_max_bytes: int
    sql_console_concurrent: int
    # Separate from `statement_timeout_ms`, which the plan for this slice had
    # said to reuse. Reusing it produces an unbounded console on production,
    # whose statement timeout is deliberately UNLIMITED because "a long
    # analytical query is a legitimate workload at this tier" -- true of a
    # direct connection, false of a browser waiting on an HTTP response. This is
    # the ceiling the platform enforces by cancelling out of band, so it must be
    # a real number on every tier.
    sql_console_timeout_ms: int

    # -- what an organization may accumulate -------------------------------
    # Phase 07 slice 5, and an abuse control rather than a capacity one: a free
    # tier open to the public is farmed by creating projects, and each one is a
    # database, four roles and a slot on a node whether or not anybody ever
    # connects to it. Bounded per organization because that is where projects
    # live; bounding it per *user* would be defeated by an invitation.
    max_projects: int

    def pitr_hours_effective(self) -> int:
        """The PITR window actually honoured, which is never longer than retention.

        A deployment can write `pitr_window_hours: 720` next to
        `backup_retention_days: 7` in `plans.config_json`, and that pair is not
        a 30-day window -- it is a 7-day window and a misconfiguration. Taking
        the minimum fails closed; the inconsistency itself is reported by
        `cp-manage backup policy` rather than silently repaired here, because a
        value quietly rewritten is one nobody fixes.
        """
        return min(self.pitr_window_hours, self.backup_retention_days * 24)

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
        # ADR-056. 1 GiB held and 5 GiB served a month, which is Supabase's free
        # shape -- deliberately, because the customer this tier exists to reach
        # is one evaluating a migration, and a ceiling below what they already
        # have tells them the answer before they start.
        #
        # Larger than this tier's 500 MiB *database* quota, and that is not an
        # oversight: object bytes sit on ordinary disk in an object store
        # (ADR-055), while database bytes sit in a shared PostgreSQL cluster
        # where they cost buffer cache, backup time and WAL. The two resources
        # are not priced against each other.
        "object_storage_bytes": 1024 * 1024 * 1024,
        # Five times what the project may hold, so a free project can serve its
        # whole store a few times over in a month. A ceiling a normal user hits
        # by using the product normally is a churn event rather than a saved
        # dollar (ADR-050), and this is the tier with the least patience for
        # one.
        "egress_bytes_per_month": 5 * 1024 * 1024 * 1024,
        "emails_per_day": 100,
        "emails_per_month": 1_000,
        "email_custom_sending_domain": False,
        "email_confirmations_required": True,
        "direct_database_access": False,
        "realtime_connections": 0,
        # ADR-068. Free is backed up -- it is on a node, and a node is backed up
        # whole -- and slice 0 measured what that actually costs: a tenant at
        # the 24 MB floor is ~2.5 MB of repository after the measured 9.4:1
        # compression, because ADR-015 puts the same ~15 MB of `maludb_core` in
        # every tenant database and identical bytes compress to nothing. Seven
        # days of that is not a number worth charging for, and a tier told its
        # data is unrecoverable when the bytes are demonstrably in the
        # repository would be a lie told for a pricing reason.
        "backup_retention_days": 7,
        # No point in time. This is the half with a marginal cost: PITR is paid
        # for in archive rather than in backup -- `archive_timeout` forces a
        # segment per minute whether or not anyone is writing, and a tenant that
        # is writing costs about 120 MB of archive an hour -- and every request
        # is a ~3-minute scratch-cluster restore of the whole node. Free
        # recovers to the state of a backup, not to a second of its choosing.
        "pitr_window_hours": 0,
        # ADR-039. Free is the tier that has no other way to create a table, so
        # this is the whole of its schema surface rather than a convenience.
        "sql_console": True,
        # Supabase's SQL editor auto-limits selects to 100 rows. Matching it is
        # the compatible answer and also the right one: this is a dashboard
        # result grid, not an export path.
        "sql_console_row_limit": 100,
        # A dashboard result grid, not an export path -- the same reasoning as
        # the row limit above, applied to the axis the row limit does not cover.
        # Two projects times one concurrent statement is the most a free account
        # can have in flight, which bounds what a farmed signup can cost.
        "sql_console_max_bytes": 2 * 1024 * 1024,
        # One statement at a time. ADR-022 makes connections the binding
        # constraint on a shared node, and a free project running a second
        # statement before its first returns is a UI convenience the tier does
        # not owe.
        "sql_console_concurrent": 1,
        "sql_console_timeout_ms": 8_000,
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
        "object_storage_bytes": 25 * 1024 * 1024 * 1024,
        "egress_bytes_per_month": 100 * 1024 * 1024 * 1024,
        "emails_per_day": 5_000,
        "emails_per_month": 50_000,
        "email_custom_sending_domain": True,
        "email_confirmations_required": True,
        "direct_database_access": True,
        "realtime_connections": 200,
        "max_projects": 20,
        # Twice free's retention, and a week of it addressable to the second.
        # Seven days is the window that covers "we noticed on Monday what we did
        # on Tuesday", which is the shape of the incident PITR is bought for.
        "backup_retention_days": 14,
        "pitr_window_hours": 7 * 24,
        "sql_console": True,
        "sql_console_row_limit": 1_000,
        # Four times free, for ten times the rows.
        "sql_console_max_bytes": 8 * 1024 * 1024,
        "sql_console_concurrent": 3,
        "sql_console_timeout_ms": 30_000,
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
        "object_storage_bytes": 250 * 1024 * 1024 * 1024,
        # A terabyte a month. This is the number to lower first if ADR-055's
        # "start on the existing Proxmox hardware" turns out to bind on
        # bandwidth rather than on disk -- egress leaves through the node
        # (slice 0 measured that a signed URL is proxied, not redirected), so
        # this ceiling and the node's uplink are the same budget seen twice.
        "egress_bytes_per_month": 1024 * 1024 * 1024 * 1024,
        "emails_per_day": 50_000,
        "emails_per_month": 1_000_000,
        "email_custom_sending_domain": True,
        "email_confirmations_required": True,
        "direct_database_access": True,
        "realtime_connections": 2_000,
        "max_projects": 100,
        # A month, addressable to the second for the whole of it. This is the
        # number that sets the *node's* required retention: `backup policy`
        # compares it against `repo1-retention-full` and a node that keeps less
        # than the longest promise made by any offered plan fails its readiness
        # check rather than quietly under-delivering.
        "backup_retention_days": 30,
        "pitr_window_hours": 30 * 24,
        "sql_console": True,
        "sql_console_row_limit": 5_000,
        # Ten concurrent statements at this ceiling is the tier's worst case,
        # and it is the number to lower first if a control plane is sized
        # smaller than the plan assumes. `plans.config_json` overrides it
        # without a deploy.
        "sql_console_max_bytes": 32 * 1024 * 1024,
        "sql_console_concurrent": 10,
        # Not UNLIMITED, unlike this tier's `statement_timeout_ms`. The console
        # is answered inside an HTTP request and the platform holds the
        # connection for its duration; a query with no ceiling here is a held
        # connection with no ceiling, on the node ADR-022 says runs out of
        # connections before it runs out of memory. A production customer who
        # genuinely needs an hour-long query has a direct connection for it.
        "sql_console_timeout_ms": 60_000,
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


def _positive_int_from(config: dict[str, Any], key: str, default: int) -> int:
    """Like `_int_from`, but zero also falls back to the default.

    For most settings zero is a real value -- `test_zero_is_a_real_value_not_a_
    missing_one` exists to keep it that way, and PostgreSQL's own convention is
    that a zero timeout means no limit. That convention is exactly wrong for the
    SQL console's ceiling: the platform holds the connection for the life of the
    statement, so an operator who writes `sql_console_timeout_ms: 0` into
    `plans.config_json` would not be granting a generous limit, they would be
    removing the only per-statement control ADR-017 leaves standing.

    The asymmetry with `sql_console_row_limit` is deliberate. A row limit of
    zero returns nothing, which fails closed and harms only the person who set
    it. A timeout of zero fails open onto a shared node.
    """
    value = _int_from(config, key, default)
    return value if value > 0 else default


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
        object_storage_bytes=_int_from(limits, "object_storage_bytes", defaults["object_storage_bytes"]),
        egress_bytes_per_month=_int_from(
            limits, "egress_bytes_per_month", defaults["egress_bytes_per_month"]
        ),
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
        # `_int_from`, not `_positive_int_from`: zero is a real value for both.
        # Zero retention means the platform promises no restore, and zero PITR
        # means no point in time -- neither fails open, and free relies on the
        # second.
        backup_retention_days=_int_from(
            limits, "backup_retention_days", defaults["backup_retention_days"]
        ),
        pitr_window_hours=_int_from(limits, "pitr_window_hours", defaults["pitr_window_hours"]),
        max_projects=_int_from(limits, "max_projects", defaults["max_projects"]),
        # Plan-level like `direct_database_access`, because it says what kind of
        # plan this is rather than how much of something it gets.
        sql_console=_bool_from((config or {}), "sql_console", defaults["sql_console"]),
        sql_console_row_limit=_int_from(limits, "sql_console_row_limit", defaults["sql_console_row_limit"]),
        # `_positive_int_from`, like the timeout and unlike the row limit: a
        # zero row limit returns nothing and harms only whoever set it, while a
        # zero byte budget would remove the ceiling on a shared process.
        sql_console_max_bytes=_positive_int_from(
            limits, "sql_console_max_bytes", defaults["sql_console_max_bytes"]
        ),
        sql_console_concurrent=_int_from(limits, "sql_console_concurrent", defaults["sql_console_concurrent"]),
        sql_console_timeout_ms=_positive_int_from(
            limits, "sql_console_timeout_ms", defaults["sql_console_timeout_ms"]
        ),
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
