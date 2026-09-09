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

from services.control_plane import billing, config, db, models, nodes

# What `MALUDB_GATEWAY_DOMAIN` defaults to. Routes nothing.
PLACEHOLDER_DOMAIN = "maludb.local"


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
            f"{user}`",
        )
        return
    report.add("gateway role", True, f"{user} cannot reach a node's admin credential")


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
    _check_billing(conn, cfg, report)
    return report
