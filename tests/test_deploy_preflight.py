"""What `cp-manage deploy preflight` refuses, and what it lets through.

A check that can only fail is as useless as one that can only pass, so every
case here is asserted in both directions: the misconfiguration is caught, and
the corrected deployment goes green.

The list is not arbitrary. Each check is a mistake this repository has actually
made or one whose failure is silent until a customer hits it -- unsynced plans,
the placeholder gateway domain, no placeable node, an unmapped Stripe price, a
gateway role that can still reach a node's superuser credential.
"""

from __future__ import annotations

import dataclasses

import pytest
from psycopg.types.json import Jsonb

from services.control_plane import config as config_module
from services.control_plane import db, preflight
from tests.conftest import requires_db

pytestmark = requires_db


def _cfg(**overrides) -> config_module.Config:
    """A Config without reading the environment.

    Built by replacing fields on a minimal instance rather than by calling
    `load()`, so these tests do not depend on what the developer has exported.
    """
    base = config_module.Config(
        environment="production",
        database_url="postgresql://x/y",
        gateway_domain="example.com",
        database_domain="db.example.com",
        docs_enabled=False,
        kek=b"k" * 32,
        token_pepper=b"p" * 32,
    )
    return dataclasses.replace(base, **overrides)


def _plan(code: str, *, active: bool = True) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, is_active, config_json) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (code) DO UPDATE SET is_active = EXCLUDED.is_active",
            (code, code, active, Jsonb({})),
        )
        conn.commit()


def _node(name: str = "node-01", *, status: str = "active", fresh: bool = True,
          stanza: str | None = "maludb-node-01") -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            """
            INSERT INTO nodes (name, hostname, internal_host, node_pool, status,
                               last_health_at, backup_stanza)
            VALUES (%s,%s,%s,'shared',%s,
                    CASE WHEN %s THEN now() ELSE now() - interval '1 day' END, %s)
            ON CONFLICT (name) DO UPDATE
               SET status = EXCLUDED.status,
                   last_health_at = EXCLUDED.last_health_at,
                   backup_stanza = EXCLUDED.backup_stanza
            """,
            (name, f"{name}.example.com", "10.0.0.20", status, fresh, stanza),
        )
        conn.commit()


def _run(cfg=None):
    with db.connection() as conn:
        return preflight.run(conn, cfg or _cfg())


def _named(report, name):
    return next(c for c in report.checks if c.name == name)


# -- the plan catalogue ----------------------------------------------------


def test_an_unsynced_catalogue_fails(db_pool):  # noqa: ARG001
    """`plans sync` seeds the table and nothing else does.

    Without it, creating a project answers 503 and the only warning is a log
    line at startup that nobody reads at signup time.
    """
    report = _run()
    check = _named(report, "plan catalogue")
    assert not check.ok
    assert "plans sync" in check.detail
    assert not report.ok


def test_a_synced_catalogue_passes(db_pool):  # noqa: ARG001
    _plan("free")
    assert _named(_run(), "plan catalogue").ok


# -- the gateway domain ----------------------------------------------------


def test_the_placeholder_domain_fails(db_pool):  # noqa: ARG001
    """ADR-008 makes the hostname the routing key; the default routes nothing."""
    check = _named(_run(_cfg(gateway_domain=preflight.PLACEHOLDER_DOMAIN)), "gateway domain")
    assert not check.ok
    assert "resolves nowhere" in check.detail


def test_a_real_domain_passes(db_pool):  # noqa: ARG001
    assert _named(_run(_cfg(gateway_domain="maludb.example")), "gateway domain").ok


# -- nodes -----------------------------------------------------------------


def test_no_node_fails(db_pool):  # noqa: ARG001
    """Signup would work and the customer's first action would not."""
    check = _named(_run(), "nodes")
    assert not check.ok
    assert "503" in check.detail


@pytest.mark.parametrize(
    ("status", "fresh", "expected"),
    [("draining", True, "draining"), ("active", False, "health stale")],
)
def test_a_node_that_cannot_take_a_project_fails(status, fresh, expected, db_pool):  # noqa: ARG001
    _node(status=status, fresh=fresh)
    check = _named(_run(), "nodes")
    assert not check.ok
    assert expected in check.detail


def test_a_healthy_node_passes(db_pool):  # noqa: ARG001
    _node()
    assert _named(_run(), "nodes").ok


def test_a_node_without_a_backup_stanza_warns_rather_than_fails(db_pool):  # noqa: ARG001
    """Serving without backups is a decision an operator may make.

    It must not be one they make silently, so it warns -- and a warning does not
    make `report.ok` false, because refusing to launch over it would be this
    tool overriding a choice that is not its own.
    """
    _plan("free")
    _node(stanza=None)
    report = _run()
    check = _named(report, "node backups")
    assert not check.ok
    assert check.advisory
    assert check in report.warnings
    assert check not in report.failures


# -- the gateway role (ADR-072) --------------------------------------------


def test_an_unset_gateway_dsn_warns_rather_than_claiming_a_pass(monkeypatch, db_pool):  # noqa: ARG001
    """"Not checked" printed as a tick is how a green run stops meaning anything."""
    monkeypatch.delenv("MALUDB_GATEWAY_DATABASE_URL", raising=False)
    check = _named(_run(), "gateway role")
    assert not check.ok
    assert check.advisory
    assert "NOT checked" in check.detail


def test_a_gateway_role_that_reaches_node_admin_columns_fails(monkeypatch, db_pool):  # noqa: ARG001
    """The control-plane role itself is the worst case, and a realistic one.

    A gateway configured with the control plane's DSN works perfectly and can
    recover every node's superuser DSN, which is exactly what ADR-072 exists to
    stop -- so pointing the check at that role must fail.
    """
    with db.connection() as conn:
        current = db.one(conn, "SELECT current_user AS u")["u"]
    monkeypatch.setenv("MALUDB_GATEWAY_DATABASE_URL", f"postgresql://{current}@127.0.0.1/x")
    check = _named(_run(), "gateway role")
    assert not check.ok
    assert not check.advisory, "this is a failure, not an advisory"
    assert "superuser DSN" in check.detail


# -- billing ---------------------------------------------------------------


def test_billing_absent_is_not_a_failure(db_pool):  # noqa: ARG001
    """A platform not taking money yet still serves every other route."""
    assert _named(_run(_cfg(stripe_secret_key=None)), "billing").ok


def test_a_secret_key_without_a_webhook_secret_fails(db_pool):  # noqa: ARG001
    """Nothing would record what was paid for."""
    # noqa S106: a literal Stripe *test* key prefix, not a credential.
    check = _named(
        _run(_cfg(stripe_secret_key="sk_test_x", stripe_webhook_secret=None)),  # noqa: S106
        "billing",
    )
    assert not check.ok
    assert "webhook" in check.detail


# -- launch slice 4 --------------------------------------------------------


def _ready_cfg(**overrides):
    """A production config with every launch-slice-4 setting right."""
    ready = {
        "captcha_required": True,
        "captcha_secret": "s",  # noqa: S105 - test fixture
        "captcha_fail_open": False,
        "dashboard_url": "https://example.com",
    }
    return _cfg(**{**ready, **overrides})


def _maintenance_run(*, minutes_ago: int = 1, failed: int = 0, finished: bool = True) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO maintenance_runs (started_at, finished_at, passes, failed) "
            "VALUES (now() - make_interval(mins => %s), "
            "        CASE WHEN %s THEN now() - make_interval(mins => %s) END, 10, %s)",
            (minutes_ago + 1, finished, minutes_ago, failed),
        )
        conn.commit()


def test_a_maintenance_pass_that_never_ran_fails(db_pool):  # noqa: ARG001
    """ADR-053: the webhook records a purchase and the pass applies it."""
    check = _named(_run(_ready_cfg()), "maintenance pass")
    assert not check.ok and not check.advisory
    assert "never finished" in check.detail


def test_a_run_that_died_before_finishing_is_not_a_run(db_pool):  # noqa: ARG001
    _maintenance_run(finished=False)
    assert not _named(_run(_ready_cfg()), "maintenance pass").ok


def test_a_stale_maintenance_pass_fails_and_a_recent_one_passes(db_pool):  # noqa: ARG001
    _maintenance_run(minutes_ago=preflight.MAINTENANCE_STALE_MINUTES + 5)
    stale = _named(_run(_ready_cfg()), "maintenance pass")
    assert not stale.ok and "stopped" in stale.detail
    _maintenance_run(minutes_ago=1)
    assert _named(_run(_ready_cfg()), "maintenance pass").ok


def test_a_recent_pass_with_failures_warns(db_pool):  # noqa: ARG001
    _maintenance_run(failed=2)
    check = _named(_run(_ready_cfg()), "maintenance pass")
    assert not check.ok and check.advisory


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"captcha_required": False}, "not required"),
        ({"captcha_secret": None}, "no provider secret"),
        ({"captcha_fail_open": True}, "waved through"),
    ],
)
def test_a_production_signup_challenge_that_is_off_misconfigured_or_fails_open_fails(overrides, expected, db_pool):  # noqa: ARG001
    check = _named(_run(_ready_cfg(**overrides)), "signup challenge")
    assert not check.ok and not check.advisory
    assert expected in check.detail


def test_the_signup_challenge_only_warns_outside_production(db_pool):  # noqa: ARG001
    check = _named(_run(_ready_cfg(environment="development", captcha_required=False)), "signup challenge")
    assert not check.ok and check.advisory


def test_a_complete_signup_challenge_passes(db_pool):  # noqa: ARG001
    assert _named(_run(_ready_cfg()), "signup challenge").ok


def test_the_default_dashboard_address_fails_only_once_billing_is_on(db_pool):  # noqa: ARG001
    """Stripe returns a customer who has just paid to it."""
    without_billing = _named(_run(_ready_cfg(dashboard_url=preflight.DEFAULT_DASHBOARD_URL)), "dashboard address")
    assert not without_billing.ok and without_billing.advisory
    with_billing = _named(
        _run(_ready_cfg(dashboard_url=preflight.DEFAULT_DASHBOARD_URL, stripe_secret_key="sk_live_x")),  # noqa: S106 - test fixture
        "dashboard address",
    )
    assert not with_billing.ok and not with_billing.advisory
    assert _named(_run(_ready_cfg()), "dashboard address").ok


# -- the exit contract -----------------------------------------------------


def test_warnings_alone_do_not_make_the_report_fail(db_pool):  # noqa: ARG001
    """Exit 2 -- ready, with something to read -- has to be distinguishable."""
    _plan("free")
    _node(stanza=None)
    _maintenance_run()
    report = _run(_ready_cfg())
    assert report.ok, [c.detail for c in report.failures]
    assert report.warnings
