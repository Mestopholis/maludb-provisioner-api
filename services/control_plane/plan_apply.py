"""Making a plan true on the node it was sold against (Phase 09 slice 0).

Every entitlement resolves through `entitlements.for_project`, which reads the
plan row. Where that happens per request -- the gateway's rate limits, the
storage quota, the console's ceilings -- changing a plan is instant. Where it
happened once, during provisioning, changing a plan does nothing at all, and
nothing says so:

    instant      api_requests_per_window, concurrent_api_requests,
                 database_storage_bytes, emails_per_day, sql_console_*,
                 max_projects, realtime_connections
    never        statement_timeout_ms, lock_timeout_ms,
                 idle_in_transaction_timeout_ms, work_mem_mb,
                 temp_file_limit_mb, max_parallel_workers_per_gather,
                 direct_database_access, database_connections

`jobs.py` writes the second group during provisioning and, before this module,
nothing re-applied it. That is not a hypothesis: `cp-manage project direct-sql`
exists because of it and says so in its own help -- "an upgrade taking effect
before the next provisioning run".

Realtime was the one exception and the pattern this copies:
`maintenance._realtime_past_its_plan` already finds projects whose plan no
longer allows Realtime and turns it off. One entitlement of ten reconciled.

**This module does not decide anything.** It reads the plan, reads the node,
and reports or corrects the difference. What a project's plan *is* remains
slice 1's business, and what a customer paid remains slice 4's.

Why applying is explicit rather than part of the maintenance pass: an operator
can revoke a paid project's direct SQL during an incident with `cp-manage
project direct-sql --disable`, and its plan still says the project is entitled
to it. A reconciler running on a timer would undo that within the hour, which
is a control cancelling a control. So the pass *reports* drift and an operator
*applies* it -- and the report distinguishes the two directions, because a
plan that grants what the node withholds is an unfulfilled upgrade, while a
node that grants what the plan withholds is either an incident measure or a
privilege nobody is paying for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from services.control_plane import entitlements, provisioning

# What a divergence is about, which decides how it is corrected and how it
# should be read.
SETTING = "setting"
LOGIN = "login"
CONNECTION_LIMIT = "connection_limit"

# Which way it points. `WITHHELD` is a project not getting what its plan says;
# `EXCESS` is a project getting more than its plan says.
WITHHELD = "withheld"
EXCESS = "excess"


@dataclass(frozen=True)
class Divergence:
    """One thing the node says that the plan does not, or the other way round."""

    role: str
    kind: str
    setting: str | None
    expected: str
    observed: str
    direction: str

    def __str__(self) -> str:
        what = self.setting or self.kind
        return f"{self.role}: {what} is {self.observed}, plan says {self.expected}"


@dataclass
class RoleState:
    """What the node currently says about one tenant role."""

    name: str
    exists: bool = False
    can_login: bool = False
    connection_limit: int = -1
    settings: dict[str, str] = field(default_factory=dict)


@dataclass
class Report:
    project_ref: str
    plan_code: str
    divergences: list[Divergence] = field(default_factory=list)
    corrected: list[Divergence] = field(default_factory=list)
    # Roles the plan expects and the node does not have. A project mid-provision
    # or one provisioned before a role existed -- `mldb_<ref>_executor` was
    # added in Phase 08 -- and not something this module invents on the fly.
    missing_roles: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.divergences and not self.missing_roles


def read_roles(admin_conn: psycopg.Connection, names: provisioning.TenantNames) -> dict[str, RoleState]:
    """The node's own account of the tenant's login roles.

    `pg_db_role_setting` is filtered to this tenant's database, because the
    settings are written `IN DATABASE` and a row for another database would be
    read as this one's.
    """
    wanted = provisioning.settings_roles(names)
    states = {name: RoleState(name=name) for name in wanted}

    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolconnlimit FROM pg_roles WHERE rolname = ANY(%s)",
            (list(wanted),),
        )
        for row in cur.fetchall():
            state = states[row["rolname"]]
            state.exists = True
            state.can_login = bool(row["rolcanlogin"])
            state.connection_limit = int(row["rolconnlimit"])

        cur.execute(
            "SELECT r.rolname, s.setconfig "
            "  FROM pg_db_role_setting s "
            "  JOIN pg_roles r ON r.oid = s.setrole "
            "  JOIN pg_database d ON d.oid = s.setdatabase "
            " WHERE r.rolname = ANY(%s) AND d.datname = %s",
            (list(wanted), names.database),
        )
        for row in cur.fetchall():
            states[row["rolname"]].settings = _parsed(row["setconfig"])

    return states


def _parsed(setconfig: list[str] | None) -> dict[str, str]:
    """`['statement_timeout=8s', ...]` as a mapping.

    A value containing `=` keeps it: `search_path = a, b` is legal and
    splitting on every separator would corrupt one bootstrap already writes.
    """
    out: dict[str, str] = {}
    for entry in setconfig or []:
        name, _, value = entry.partition("=")
        out[name.strip()] = value.strip()
    return out


def divergences(
    allowed: entitlements.Entitlements,
    names: provisioning.TenantNames,
    observed: dict[str, RoleState],
) -> tuple[list[Divergence], list[str]]:
    """What the node and the plan disagree about, and which roles are absent.

    Settings the plan does not name are ignored rather than reported. A GUC on
    a role that the plan has nothing to say about is somebody's deliberate act
    -- bootstrap 007 writes `search_path` on the auth role -- and a reconciler
    that removed what it did not recognise would undo it.
    """
    found: list[Divergence] = []
    missing: list[str] = []
    settings = allowed.postgres_settings()

    for role, state in observed.items():
        if not state.exists:
            missing.append(role)
            continue

        for setting, expected in settings.items():
            actual = state.settings.get(setting)
            if actual == str(expected):
                continue
            found.append(
                Divergence(
                    role=role, kind=SETTING, setting=setting,
                    expected=str(expected), observed=actual or "unset",
                    # Unset means the tenant is running on the cluster default,
                    # which for `temp_file_limit` is no limit at all -- so an
                    # absent setting is the project having more than its plan
                    # grants, not less.
                    direction=EXCESS if actual is None else WITHHELD,
                )
            )

    admin = observed.get(names.admin)
    if admin is not None and admin.exists:
        if admin.can_login != allowed.direct_database_access:
            found.append(
                Divergence(
                    role=names.admin, kind=LOGIN, setting=None,
                    expected=str(allowed.direct_database_access),
                    observed=str(admin.can_login),
                    direction=EXCESS if admin.can_login else WITHHELD,
                )
            )
        expected_limit = allowed.database_connections if allowed.direct_database_access else 0
        if admin.connection_limit != expected_limit:
            found.append(
                Divergence(
                    role=names.admin, kind=CONNECTION_LIMIT, setting=None,
                    expected=str(expected_limit), observed=str(admin.connection_limit),
                    # -1 is PostgreSQL's "no limit", and any higher number than
                    # the plan's is likewise the project having more.
                    direction=EXCESS
                    if admin.connection_limit < 0 or admin.connection_limit > expected_limit
                    else WITHHELD,
                )
            )

    authenticator = observed.get(names.authenticator)
    if authenticator is not None and authenticator.exists:
        expected_limit = allowed.connection_limits()["authenticator"]
        if authenticator.connection_limit != expected_limit:
            found.append(
                Divergence(
                    role=names.authenticator, kind=CONNECTION_LIMIT, setting=None,
                    expected=str(expected_limit),
                    observed=str(authenticator.connection_limit),
                    direction=EXCESS
                    if authenticator.connection_limit < 0
                    or authenticator.connection_limit > expected_limit
                    else WITHHELD,
                )
            )

    return found, missing


def inspect(
    admin_conn: psycopg.Connection,
    names: provisioning.TenantNames,
    allowed: entitlements.Entitlements,
) -> Report:
    """Read-only. What a drift report is built from."""
    observed = read_roles(admin_conn, names)
    found, missing = divergences(allowed, names, observed)
    return Report(
        project_ref=names.project_ref, plan_code=allowed.plan_code,
        divergences=found, missing_roles=missing,
    )


def apply(
    admin_conn: psycopg.Connection,
    names: provisioning.TenantNames,
    allowed: entitlements.Entitlements,
) -> Report:
    """Re-assert the plan on the node, and report what that changed.

    Idempotent, and deliberately unconditional: it writes every setting rather
    than only the diverging ones. A conditional write would make the correction
    depend on the comparison being right, and the comparison is the newer of
    the two. The report still says what *had* diverged, which is what an
    operator needs to see.

    Never mints a credential. `set_direct_sql_access` flips `LOGIN` on a role
    whose password was stored at provisioning, so a customer who already has it
    does not receive a different one on upgrade -- and a downgrade followed by
    a later upgrade restores the same credential rather than breaking whatever
    the customer had configured.
    """
    report = inspect(admin_conn, names, allowed)
    if report.missing_roles:
        # Refused rather than half-applied. A missing role is a project that is
        # mid-provision or was provisioned before that role existed, and both
        # want `cp-manage project retry` or `backfill-executor` -- not this.
        return report

    provisioning.apply_plan_settings(admin_conn, names, settings=allowed.postgres_settings())
    provisioning.set_direct_sql_access(
        admin_conn, names,
        enabled=allowed.direct_database_access,
        connection_limit=allowed.database_connections,
    )
    admin_conn.execute(
        sql.SQL("ALTER ROLE {role} CONNECTION LIMIT {limit}").format(
            role=sql.Identifier(names.authenticator),
            limit=sql.Literal(int(allowed.connection_limits()["authenticator"])),
        )
    )
    admin_conn.commit()

    report.corrected = list(report.divergences)
    return report


def project_rows(conn: psycopg.Connection, *, node_id: int | None = None) -> list[dict[str, Any]]:
    """Projects worth comparing: provisioned, not deleted, on a node.

    A project still being provisioned has no settled node state to compare
    against, and one being deleted is not worth reporting on.
    """
    query = (
        "SELECT pr.id, pr.project_ref, pr.node_id, pl.code AS plan_code, pl.config_json "
        "  FROM projects pr JOIN plans pl ON pl.id = pr.plan_id "
        " WHERE pr.deleted_at IS NULL AND pr.node_id IS NOT NULL "
        "   AND pr.status IN ('ACTIVE', 'PROVISIONED', 'PAUSED', 'API_CONFIGURING', "
        "                     'ROUTING_CONFIGURING')"
    )
    params: tuple[Any, ...] = ()
    if node_id is not None:
        query += " AND pr.node_id = %s"
        params = (node_id,)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query + " ORDER BY pr.project_ref", params)
        return cur.fetchall()
