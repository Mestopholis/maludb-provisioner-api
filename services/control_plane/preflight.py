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

import hmac
import os
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg

from services.control_plane import (
    admin_grants,
    backup,
    billing,
    config,
    db,
    gateway_grants,
    memory_worker_grants,
    models,
    node_reporter,
    nodes,
)

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

# How old the control plane's last `node backup-check` may be. It reads cluster settings that change
# only with a restart, so a week; the node's own repository report is held to a day (ADR-086).
NODE_BACKUP_CHECK_STALE = timedelta(days=7)


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


def _check_nodes(conn: psycopg.Connection, report: Report, *, production: bool = False) -> None:
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

    _check_node_backups(conn, report, placeable, production=production)


def _check_node_backups(conn: psycopg.Connection, report: Report, placeable: list[dict], *,
                        production: bool) -> None:
    """Rehearsal finding 21: a recorded stanza *name* is not a working backup.

    What counts is the last `node backup-check`: that it passed, that it is not stale, and --
    where the node records its own repository report (ADR-086) -- that the report behind it is
    recent. A node whose check failed used to read "every placeable node has a stanza".
    Fatal in production; a development node without backups is a choice an operator may make.
    """
    names = [r["name"] for r in placeable]
    rows = {r["name"]: r for r in db.query(
        conn,
        """
        SELECT name, backup_stanza, backup_recorder_role,
               (capacity_json->>'backup_ready')::boolean AS ready,
               metrics_json->'backup_failures' AS failures,
               (metrics_json->>'backup_checked_at')::timestamptz AS checked_at,
               (metrics_json->>'backup_repository_checked_at')::timestamptz AS reported_at,
               now() AS now
          FROM nodes WHERE name = ANY(%s)
        """,
        (names,),
    )}
    problems = []
    for name in names:
        row = rows[name]
        if not row["backup_stanza"]:
            problems.append(f"{name}: no stanza recorded; "
                            f"`cp-manage node backup-check --name {name} --stanza <stanza>`")
        elif row["ready"] is not True:
            first = (row["failures"] or ["never checked"])[0]
            problems.append(f"{name}: its last backup-check did not pass ({first})")
        elif row["checked_at"] is None or row["now"] - row["checked_at"] > NODE_BACKUP_CHECK_STALE:
            problems.append(f"{name}: backup-check last ran {row['checked_at'] or 'never'}; re-run it")
        elif row["backup_recorder_role"] and (
                row["reported_at"] is None or row["now"] - row["reported_at"] > backup.REPOSITORY_REPORT_MAX_AGE):
            problems.append(f"{name}: the node has not reported its repository since {row['reported_at'] or 'ever'}; "
                            "is maludb-node-backup-check.timer running?")
    if problems:
        report.add("node backups", False, "; ".join(problems) + ". Until then these nodes cannot be recovered",
                   advisory=not production)
    else:
        report.add("node backups", True, "every placeable node passed a recent backup-check")


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
    # Any privilege, not only SELECT: on the staff tables the danger is INSERT -- a gateway
    # that can write a staff session has operator access (ADR-082).
    reachable_secrets = [
        table for table in gateway_grants.UNREACHABLE_TABLES
        if db.one(conn, "SELECT to_regclass(%s) IS NOT NULL AND has_table_privilege(%s, %s, "
                  "'SELECT, INSERT, UPDATE, DELETE') AS yes",
                  (table, user, table))["yes"]
    ]
    if reachable_secrets:
        report.add(
            "gateway role",
            False,
            f"{user} holds privileges on " + ", ".join(reachable_secrets) + " -- customers' own provider API "
            "keys and the platform staff accounts, which nothing on the request path needs and which would "
            f"give a compromised gateway operator access. Run `cp-manage gateway grant --role {user} --node <node>`",
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


NODE_MAINTENANCE_STALE_MINUTES = 10


def _check_node_maintenance(conn: psycopg.Connection, report: Report) -> None:
    """ADR-083: every active node sleeps its own idle workers, on a timer, as its gateway.

    Without it a free project's workers never sleep, which is the whole of ADR-022's density. Read
    from `node_maintenance_runs`, which only a node's own gateway role can write for that node.
    """
    rows = db.query(
        conn,
        """
        SELECT n.name,
               coalesce((SELECT max(r.finished_at) FROM node_maintenance_runs r WHERE r.node_id = n.id)
                        > now() - make_interval(mins => %s), false) AS fresh,
               (SELECT r.failed FROM node_maintenance_runs r WHERE r.node_id = n.id AND r.finished_at IS NOT NULL
                 ORDER BY r.finished_at DESC LIMIT 1) AS last_failed
          FROM nodes n WHERE n.status = 'active' ORDER BY n.name
        """,
        (NODE_MAINTENANCE_STALE_MINUTES,),
    )
    if not rows:
        return
    stale = [r["name"] for r in rows if not r["fresh"]]
    if stale:
        report.add("node maintenance", False,
                   f"{', '.join(stale)}: no node maintenance run in the last {NODE_MAINTENANCE_STALE_MINUTES} minutes, "
                   "so idle workers there never sleep. Install maludb-node-maintenance.timer on the node "
                   "(docs/DEPLOYMENT.md 2.6)")
        return
    failing = [f"{r['name']} ({r['last_failed']})" for r in rows if r["last_failed"]]
    if failing:
        report.add("node maintenance", False,
                   f"running, but the last run failed to sleep workers on {', '.join(failing)}; "
                   "`journalctl -u maludb-node-maintenance` names each", advisory=True)
        return
    report.add("node maintenance", True, "every active node sleeps its idle workers")


def _check_email(cfg: config.Config, report: Report) -> None:
    """Free slice 2: the platform can send mail.

    Two things fail silently without it. A platform user who forgets their password is told to
    check an inbox nothing reaches (`password_reset.send` refuses, but the route answers the same
    either way on purpose), and a project's Auth sends no confirmation -- which the gateway now
    refuses in production rather than hide, so Auth stops working instead. Fatal in production.
    """
    missing = [name for name, value in (("MALUMAIL_API", cfg.malumail_api_key),
                                        ("MALUDB_PLATFORM_EMAIL_FROM", cfg.platform_email_from)) if not value]
    if missing:
        report.add("email", False,
                   f"{' and '.join(missing)} not set: platform password resets send nothing, and project Auth "
                   "cannot start in production. The nodes also need MALUDB_EMAIL_HOOK_BASE_URL and "
                   "MALUDB_PLATFORM_EMAIL_FROM in gateway.env (docs/DEPLOYMENT.md)",
                   advisory=not cfg.is_production)
        return
    report.add("email", True, f"platform mail sends from {cfg.platform_email_from} through MaluMail")


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


def _bucket_unreachable(cfg: config.Config) -> str | None:
    """Why the platform bucket cannot be reached from here, or None if it can."""
    from services.control_plane import object_storage

    try:
        object_storage._client(cfg).head_bucket(Bucket=cfg.storage_s3_bucket)
    except Exception as exc:  # noqa: BLE001 - the type is the diagnosis; the message may carry a URL
        return type(exc).__name__
    return None


def _check_object_store(
    conn: psycopg.Connection, cfg: config.Config, report: Report, *, probe=_bucket_unreachable
) -> None:
    """Free-tier slice 4: Storage is a launch feature, and every way it is half-configured is quiet.

    The control plane needs the object store itself, not only the node: it
    measures held bytes against the store rather than against metadata a
    customer holding `service_role` can rewrite, and it deletes a deleted
    project's objects. Without an endpoint both quietly fall back or skip. A node
    with no sealed storage root has never been prepared, and its first Storage
    request answers 503 naming node preparation.
    """
    if not cfg.storage_s3_endpoint:
        report.add(
            "object store",
            False,
            "MALUDB_STORAGE_S3_ENDPOINT is unset, so this deployment offers no Storage: held bytes "
            "are never measured against the store and deleted projects' files are never removed",
            advisory=True,
        )
        return
    missing = [
        name for name, value in (
            ("MALUDB_STORAGE_DB_HOST", cfg.storage_db_host),
            ("MALUDB_STORAGE_S3_ACCESS_KEY", cfg.storage_s3_access_key),
            ("MALUDB_STORAGE_S3_SECRET_KEY", cfg.storage_s3_secret_key),
        ) if not value
    ]
    if missing:
        report.add("object store", False, f"an endpoint is set but {', '.join(missing)} is not",
                   advisory=not cfg.is_production)
        return
    unreachable = probe(cfg)
    if unreachable is not None:
        report.add(
            "object store",
            False,
            f"bucket {cfg.storage_s3_bucket} at {cfg.storage_s3_endpoint} did not answer ({unreachable}). "
            "Check the node's object store and that its firewall admits this host (docs/DEPLOYMENT.md §2.7)",
            advisory=not cfg.is_production,
        )
        return
    unprepared = [
        row["name"] for row in db.query(
            conn,
            "SELECT name FROM nodes WHERE status = 'active' AND storage_secret_ciphertext IS NULL "
            "ORDER BY name",
        )
    ]
    if unprepared:
        report.add(
            "object store",
            False,
            f"bucket reachable, but {', '.join(unprepared)} never prepared for Storage: run "
            "`cp-manage node storage-prepare` (docs/DEPLOYMENT.md §2.7)",
            advisory=not cfg.is_production,
        )
        return
    report.add("object store", True, f"bucket {cfg.storage_s3_bucket} reachable; every active node prepared")


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


def _check_memory_embedder_role(conn: psycopg.Connection, report: Report) -> None:
    """ADR-079 memory slice 6a. Absent is advisory: search by text is then simply not offered."""
    group = memory_worker_grants.EMBEDDER_GROUP_ROLE
    if db.one(conn, "SELECT 1 AS ok FROM pg_catalog.pg_roles WHERE rolname = %s", (group,)) is None:
        report.add("memory embedder role", False,
                   f"{group} does not exist, so memory search by text cannot run narrowed. Create it and run "
                   "`cp-manage memory-worker grant` if search by text is offered", advisory=True)
        return
    wider = memory_worker_grants.embedder_violations(conn, group)
    gateways = memory_worker_grants.gateway_members(conn, group)
    missing = [f"{table}.{column}" for table, columns in memory_worker_grants.EMBEDDER_READS.items()
               for column in columns
               if not db.one(conn, "SELECT has_column_privilege(%s, %s, %s, 'SELECT') AS ok",
                             (group, table, column))["ok"]]
    if wider or gateways or missing:
        detail = [part for part in (
            f"{group} can read {', '.join(wider)}" if wider else "",
            f"gateway roles {gateways} are members of {group}" if gateways else "",
            f"{group} cannot read {', '.join(missing)}" if missing else "",
        ) if part]
        report.add("memory embedder role", False, "; ".join(detail) + ". Run `cp-manage memory-worker grant`")
        return
    report.add("memory embedder role", True, f"{group} reads only what the query embedder needs")


def _check_node_reporters(conn: psycopg.Connection, report: Report) -> None:
    """ADR-080. Every active node needs something reporting its health, or placement stops choosing it
    five minutes after the last manual report. Missing is advisory -- `cp-manage node health` still
    works -- but a reporter holding more than the one function is a failure."""
    active = db.query(conn, "SELECT name, health_reporter_role FROM nodes WHERE status = 'active' ORDER BY name")
    if not active:
        return
    unmapped = [n["name"] for n in active if not n["health_reporter_role"]]
    problems = []
    for node in active:
        role = node["health_reporter_role"]
        if not role:
            continue
        if db.one(conn, "SELECT 1 AS ok FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)) is None:
            problems.append(f"{node['name']}: reporter role {role} does not exist")
            continue
        wider = node_reporter.wider_than_the_model(conn, role)
        if wider:
            problems.append(f"{node['name']}: {role} also holds {', '.join(wider)}")
        elif not node_reporter.can_report(conn, role):
            problems.append(f"{node['name']}: {role} cannot execute report_node_health")
    if problems:
        report.add("node health reporters", False, "; ".join(problems) + ". Run `cp-manage node reporter grant`")
    elif unmapped:
        report.add("node health reporters", False,
                   f"no reporter for {', '.join(unmapped)}; nothing on the node keeps its health fresh "
                   "(docs/DEPLOYMENT.md, 2.5)", advisory=True)
    else:
        report.add("node health reporters", True, "every active node has a reporter holding only report_node_health")


def _check_admin_console(cfg: config.Config, report: Report) -> None:
    """ADR-082: the console listens privately, and its staff key is not the KEK.

    Read from this host's environment: preflight runs where `cp-manage` runs, which is
    where the console's environment file is installed. Unconfigured is advisory -- a
    deployment need not run the console at all.
    """
    import ipaddress

    bind = os.environ.get("MALUDB_ADMIN_BIND", "").strip()
    if not bind:
        report.add("operator console", False, "not configured (MALUDB_ADMIN_BIND unset); nothing to check",
                   advisory=True)
        return
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        report.add("operator console", False,
                   f"MALUDB_ADMIN_BIND={bind!r} is not an IP address; bind it to a private address")
        return
    if address.is_unspecified or not (address.is_private or address.is_loopback):
        report.add("operator console", False,
                   f"MALUDB_ADMIN_BIND={bind} is a wildcard or public address. The console serves platform "
                   "staff and belongs on a private address reached over the operator VPN (ADR-082)")
        return
    try:
        staff_key = config.staff_key_material()
    except config.ConfigError as exc:
        report.add("operator console", False, f"the staff key cannot be loaded: {exc}")
        return
    if hmac.compare_digest(staff_key, cfg.kek):
        report.add("operator console", False,
                   "the staff key is the KEK's material. The console would then hold what opens every node "
                   "and project secret; generate separate material (docs/SECRETS.md, ADR-082)")
        return
    report.add("operator console", True, f"listens on private {bind}; staff key is separate from the KEK")


def _check_admin_console_role(conn: psycopg.Connection, report: Report) -> None:
    """ADR-082 slice 2. Absent is advisory: a deployment need not run the console."""
    group = admin_grants.GROUP_ROLE
    if db.one(conn, "SELECT 1 AS ok FROM pg_catalog.pg_roles WHERE rolname = %s", (group,)) is None:
        report.add("operator console role", False,
                   f"{group} does not exist, so the operator console cannot run narrowed, which it refuses in "
                   "production. Create it and run `cp-manage admin-console grant` if the console is deployed",
                   advisory=True)
        return
    wider = admin_grants.violations(conn, group)
    overlapping = admin_grants.overlaps(conn, group)
    if wider or overlapping:
        detail = []
        if wider:
            detail.append(f"{group} can " + ", ".join(wider))
        if overlapping:
            detail.append(f"{overlapping} are console roles and also a gateway, reporter or memory worker")
        report.add("operator console role", False, "; ".join(detail) + ". Run `cp-manage admin-console grant`")
        return
    missing = [
        f"{verb} {table}.{column}"
        for verb, model in (("SELECT", admin_grants.READS), ("UPDATE", admin_grants.UPDATES),
                            ("INSERT", admin_grants.INSERTS))
        for table, columns in model.items() for column in columns
        if not db.one(conn, "SELECT has_column_privilege(%s, %s, %s, %s) AS ok", (group, table, column, verb))["ok"]
    ]
    if missing:
        report.add("operator console role", False,
                   f"{group} lacks {', '.join(missing)}; a migration added what it needs since the grant was "
                   "applied. Re-run `cp-manage admin-console grant`")
        return
    report.add("operator console role", True, f"{group} reaches only what the operator console needs")


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
    _check_nodes(conn, report, production=cfg.is_production)
    _check_gateway_role(conn, report)
    _check_memory_worker_role(conn, report)
    _check_memory_embedder_role(conn, report)
    _check_node_reporters(conn, report)
    _check_billing(conn, cfg, report)
    _check_dashboard_url(cfg, report)
    _check_signup_challenge(cfg, report)
    _check_email(cfg, report)
    _check_maintenance(conn, report)
    _check_node_maintenance(conn, report)
    _check_object_store(conn, cfg, report)
    _check_admin_console(cfg, report)
    _check_admin_console_role(conn, report)
    return report
