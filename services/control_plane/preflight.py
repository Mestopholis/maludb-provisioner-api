"""Refuse a deployment that is wrong, rather than documenting how to be right.

`docs/DEPLOYMENT.md` is a runbook, and a runbook is a control held by whoever is
reading it. `AGENTS.md` records twice what happens to controls held that way.
This is the same list of things, asked of the deployment instead of the operator.

**Every check here is a mistake that has actually been made, or one whose
failure is silent.** Nothing is included because it seemed tidy:

- `plans sync` not run: creating a project answers 503 and the log says why at
  startup, which nobody is reading at signup time.
- the default `maludb.local` gateway domain: project API URLs are derived from
  it (ADR-008), so every project advertises a hostname that resolves nowhere.
- no healthy node: signup succeeds and the customer's first action fails.
- a gateway role that can still read `nodes.admin_ciphertext`: ADR-072, and the
  gateway refuses to start -- but finding that out from the control plane before
  installing the node is cheaper.
- Stripe configured with an unmapped plan: checkout answers 409 naming it, and
  only when a customer tries to pay.
- the maintenance pass never scheduled (launch slice 4): the webhook records a
  purchase and the pass applies it (ADR-053), so an unscheduled pass means a
  customer pays and nothing changes -- while every route still answers.
- a production signup that does not demand a challenge, or waves signups
  through when the challenge service is down: public signup is decided, and an
  account admitted by mistake is a database on a shared node.
- billing on with the dashboard address still the default: Stripe returns a
  customer who has just paid to that address.

**What it cannot check, and says so.** It runs on the control plane. It cannot
prove the internal listener is unreachable from the internet, that DNS resolves,
or that the gateway's certificate is valid -- those are properties of the
network, not of this database. A green preflight is not "the deployment is
correct"; it is "none of the things I can see are wrong". The output says that
rather than letting a checkmark imply more.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import psycopg

from services.control_plane import billing, config, db, gateway_grants, memory_worker_grants, models, nodes

# What `MALUDB_GATEWAY_DOMAIN` defaults to. Routes nothing.
PLACEHOLDER_DOMAIN = "maludb.local"

# What `MALUDB_DASHBOARD_URL` defaults to.
DEFAULT_DASHBOARD_URL = "https://app.maludb.org"

# How old the last finished maintenance run may be. ADR-053 describes purchases
# applying "seconds to a minute later", so the pass is meant to run about every
# minute; fifteen leaves room for a slow pass without letting "never scheduled"
# pass for "running". Which host runs it is still open (docs/OPEN-QUESTIONS.md),
# so this checks that it runs, not where.
MAINTENANCE_STALE_MINUTES = 15


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    # A failed check that is only a problem for part of a deployment -- billing
    # on a platform not taking money yet -- warns rather than fails.
    advisory: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, advisory: bool = False) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, advisory=advisory))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.advisory]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.advisory]

    @property
    def ok(self) -> bool:
        return not self.failures


def _check_plans(conn: psycopg.Connection, report: Report) -> None:
    plans = models.list_plans(conn, active_only=True)
    if not plans:
        report.add(
            "plan catalogue",
            False,
            "no active plans. `cp-manage plans sync` seeds them and nothing else "
            "does; without a free plan, creating a project answers 503",
        )
        return
    default = models.default_plan(conn)
    if default is None:
        report.add(
            "plan catalogue",
            False,
            f"{len(plans)} active plan(s) but no default: project creation has "
            "nothing to fall back on",
        )
        return
    report.add(
        "plan catalogue",
        True,
        f"{len(plans)} active, default {default.code}",
    )


def _check_gateway_domain(cfg: config.Config, report: Report) -> None:
    if cfg.gateway_domain == PLACEHOLDER_DOMAIN:
        report.add(
            "gateway domain",
            False,
            f"still the {PLACEHOLDER_DOMAIN} default. The hostname is the routing "
            "key (ADR-008) and project API URLs are derived from it, so every "
            "project would advertise a hostname that resolves nowhere",
        )
        return
    report.add("gateway domain", True, cfg.gateway_domain)


def _check_nodes(conn: psycopg.Connection, report: Report) -> None:
    rows = db.query(
        conn,
        """
        SELECT name, status, last_health_at, backup_stanza,
               last_health_at > now() - %s AS fresh
          FROM nodes
         ORDER BY name
        """,
        (nodes.HEALTH_STALE_AFTER,),
    )
    if not rows:
        report.add(
            "nodes",
            False,
            "none registered. Signup would succeed and the customer's first "
            "action -- creating a project -- would answer 503",
        )
        return

    placeable = [r for r in rows if r["status"] == nodes.PLACEABLE_STATUS and r["fresh"]]
    if not placeable:
        detail = "; ".join(
            f"{r['name']} is {r['status']}"
            + ("" if r["fresh"] else ", health stale")
            for r in rows
        )
        report.add("nodes", False, f"none can take a project: {detail}")
        return

    report.add(
        "nodes",
        True,
        f"{len(placeable)} of {len(rows)} placeable: "
        + ", ".join(r["name"] for r in placeable),
    )

    # Advisory rather than fatal: a node without a stanza serves traffic
    # perfectly and cannot be recovered, which is a decision an operator is
    # allowed to make and should not make silently.
    unbacked = [r["name"] for r in placeable if not r["backup_stanza"]]
    if unbacked:
        report.add(
            "node backups",
            False,
            "no pgBackRest stanza recorded for " + ", ".join(unbacked)
            + ". `cp-manage node backup-check --name <node> --stanza <stanza>` "
            "records one; until then these nodes cannot be recovered",
            advisory=True,
        )
    else:
        report.add("node backups", True, "every placeable node has a stanza")


def _check_gateway_role(conn: psycopg.Connection, report: Report) -> None:
    """ADR-072, asked before the node is built rather than after.

    Reads the intended role from `MALUDB_GATEWAY_DATABASE_URL` if it is set on
    this host; otherwise it can only say it was not checked. The gateway itself
    refuses to start when this is wrong, so this is an early warning rather than
    the enforcement.
    """
    dsn = os.environ.get("MALUDB_GATEWAY_DATABASE_URL", "").strip()
    if not dsn:
        # Not checked is not the same as passed, and printing it as a tick is
        # how a green run comes to mean less than it looks like.
        report.add(
            "gateway role",
            False,
            "MALUDB_GATEWAY_DATABASE_URL is not set on this host, so this was "
            "NOT checked. The gateway itself refuses to start in production when "
            "its role can read nodes.admin_ciphertext (ADR-072)",
            advisory=True,
        )
        return
    try:
        user = psycopg.conninfo.conninfo_to_dict(dsn).get("user")
    except psycopg.Error:
        report.add("gateway role", False, "MALUDB_GATEWAY_DATABASE_URL is not a valid DSN")
        return

    readable = db.query(
        conn,
        """
        SELECT a.attname AS column
          FROM pg_attribute a
         WHERE a.attrelid = 'nodes'::regclass
           AND a.attname = ANY(%s)
           AND has_column_privilege(%s, a.attrelid, a.attname, 'SELECT')
        """,
        (["admin_ciphertext", "admin_nonce", "admin_key_version"], user),
    )
    if readable:
        report.add(
            "gateway role",
            False,
            f"{user} can read " + ", ".join(r["column"] for r in readable)
            + " on nodes, so a compromise of the gateway yields every node's "
            "superuser DSN (ADR-072). Run `cp-manage gateway grant --role "
            f"{user} --node <node>`",
        )
        return

    # The second half of ADR-072, and a distinct failure: a role can be
    # perfectly narrowed and serve nothing, because the row policies resolve
    # `current_user` through `nodes.gateway_role` and an unmapped role matches
    # no row. That fails closed, which is right, and presents as every project
    # on the machine answering 404 with no error anywhere -- so it is worth
    # saying here, before the node is built, rather than at three in the
    # morning.
    reachable_secrets = [
        table for table in gateway_grants.UNREACHABLE_TABLES
        if db.one(conn, "SELECT to_regclass(%s) IS NOT NULL AND has_table_privilege(%s, %s, 'SELECT') AS yes",
                  (table, user, table))["yes"]
    ]
    if reachable_secrets:
        report.add(
            "gateway role",
            False,
            f"{user} can read " + ", ".join(reachable_secrets) + " -- customers' own provider API keys, "
            f"which nothing on the request path needs. Run `cp-manage gateway grant --role {user} --node <node>`",
        )
        return

    served = db.query(conn, "SELECT name FROM nodes WHERE gateway_role = %s", (user,))
    if not served:
        report.add(
            "gateway role",
            False,
            f"{user} cannot reach a node's admin credential, but is not mapped to any "
            "node either, so its row policies match nothing and every project on that "
            f"machine will answer 404 (ADR-072). Run `cp-manage gateway grant --role {user} "
            "--node <node>`",
        )
        return
    report.add(
        "gateway role",
        True,
        f"{user} cannot reach a node's admin credential, and sees only "
        + ", ".join(r["name"] for r in served),
    )


def _check_billing(conn: psycopg.Connection, cfg: config.Config, report: Report) -> None:
    if not cfg.stripe_secret_key:
        report.add(
            "billing", True, "not configured, so nothing is for sale. Every other route works"
        )
        return
    livemode = not cfg.stripe_secret_key.startswith("sk_test_")
    if not cfg.stripe_webhook_secret:
        report.add(
            "billing",
            False,
            "a secret key is set but no webhook secret, so nothing records what "
            "was paid for",
        )
        return
    unmapped = billing.unmapped_plans(conn, livemode=livemode)
    if unmapped:
        report.add(
            "billing",
            False,
            f"{'live' if livemode else 'test'} mode, and no price maps to "
            + ", ".join(unmapped)
            + ". Checkout answers 409 naming the plan, and only when a customer "
            "tries to pay",
        )
        return
    report.add("billing", True, f"{'LIVE' if livemode else 'test'} mode, every paid plan priced")


def _check_maintenance(conn: psycopg.Connection, report: Report) -> None:
    row = db.one(
        conn,
        "SELECT max(finished_at) AS finished, "
        "       max(finished_at) > now() - make_interval(mins => %s) AS fresh, "
        "       (SELECT failed FROM maintenance_runs WHERE finished_at IS NOT NULL "
        "         ORDER BY finished_at DESC LIMIT 1) AS last_failed "
        "  FROM maintenance_runs",
        (MAINTENANCE_STALE_MINUTES,),
    )
    if row is None or row["finished"] is None:
        report.add(
            "maintenance pass",
            False,
            "has never finished a run. It applies purchases (ADR-053), measures storage and "
            "ends failed-payment grace; schedule `cp-manage maintenance run` about every minute",
        )
        return
    if not row["fresh"]:
        report.add(
            "maintenance pass",
            False,
            f"last finished {row['finished'].isoformat(timespec='seconds')}, more than "
            f"{MAINTENANCE_STALE_MINUTES} minutes ago. Whatever schedules "
            "`cp-manage maintenance run` has stopped",
        )
        return
    if row["last_failed"]:
        report.add(
            "maintenance pass",
            False,
            f"running, but its last run reported {row['last_failed']} failure(s); "
            "its output names each pass and why",
            advisory=True,
        )
        return
    report.add("maintenance pass", True, f"last finished {row['finished'].isoformat(timespec='seconds')}")


def _check_signup_challenge(cfg: config.Config, report: Report) -> None:
    """Public signup is decided (2026-08-16); the challenge is what stands in front of it.

    Fatal in production, advisory elsewhere -- a development deployment runs
    without one on purpose.
    """
    problems = []
    if not cfg.captcha_required:
        problems.append("signups are not required to pass a challenge")
    elif not cfg.captcha_secret:
        problems.append("a challenge is required but no provider secret is configured, so every signup fails")
    if cfg.captcha_fail_open:
        problems.append(
            "MALUDB_CAPTCHA_FAIL_OPEN is set, so signups are waved through whenever the "
            "challenge service is unreachable"
        )
    if problems:
        report.add("signup challenge", False, "; ".join(problems), advisory=not cfg.is_production)
        return
    report.add("signup challenge", True, "required, configured, and fails closed")


def _check_dashboard_url(cfg: config.Config, report: Report) -> None:
    if cfg.dashboard_url.rstrip("/") != DEFAULT_DASHBOARD_URL:
        report.add("dashboard address", True, cfg.dashboard_url)
        return
    report.add(
        "dashboard address",
        False,
        f"still the default {DEFAULT_DASHBOARD_URL}. Stripe returns a customer who has just paid "
        "there, and password-reset links point there; set MALUDB_DASHBOARD_URL to this "
        "deployment's site",
        # Only fatal once money is involved: without billing it costs a reset link.
        advisory=not cfg.stripe_secret_key,
    )


def _check_memory_worker_role(conn: psycopg.Connection, report: Report) -> None:
    """ADR-079 memory slice 5c, asked of the group role the worker's login belongs to.

    Checked whether or not the worker runs on this host, because the thing checked
    is a control-plane role rather than a process: the group exists or it does not,
    and its privileges are what they are.
    """
    group = memory_worker_grants.GROUP_ROLE
    if db.one(conn, "SELECT 1 AS ok FROM pg_catalog.pg_roles WHERE rolname = %s", (group,)) is None:
        report.add(
            "memory worker role", False,
            f"{group} does not exist, so the memory worker can only run as a wider role, which it refuses in "
            "production. Create it and run `cp-manage memory-worker grant` (docs/DEPLOYMENT.md)",
            advisory=True,
        )
        return
    wider = memory_worker_grants.violations(conn, group)
    gateways = memory_worker_grants.gateway_members(conn)
    if wider or gateways:
        detail = []
        if wider:
            detail.append(f"{group} can read " + ", ".join(wider))
        if gateways:
            detail.append(f"gateway roles {gateways} are members of {group}")
        report.add("memory worker role", False, "; ".join(detail) + ". Run `cp-manage memory-worker grant`")
        return
    missing = [f"{table}.{column}" for table, columns in memory_worker_grants.READS.items() for column in columns
               if not db.one(conn, "SELECT has_column_privilege(%s, %s, %s, 'SELECT') AS ok",
                             (group, table, column))["ok"]]
    if missing:
        report.add("memory worker role", False,
                   f"{group} cannot read {', '.join(missing)}; a migration added what it needs since the grant "
                   "was applied. Re-run `cp-manage memory-worker grant`")
        return
    report.add("memory worker role", True, f"{group} reads only what the memory worker needs")


def run(conn: psycopg.Connection, cfg: config.Config) -> Report:
    """Everything this host can see. See the module docstring for what it cannot."""
    report = Report()

    # That `config.load()` returned at all is the key-material check:
    # `_read_secret_file` refuses a group- or world-readable file and refuses
    # material shorter than 32 bytes. Duplicating it here would be a second
    # implementation of a rule that already has one.
    report.add("key material", True, "loaded, and not group- or world-readable")

    _check_plans(conn, report)
    _check_gateway_domain(cfg, report)
    _check_nodes(conn, report)
    _check_gateway_role(conn, report)
    _check_memory_worker_role(conn, report)
    _check_billing(conn, cfg, report)
    _check_dashboard_url(cfg, report)
    _check_signup_challenge(cfg, report)
    _check_maintenance(conn, report)
    return report
