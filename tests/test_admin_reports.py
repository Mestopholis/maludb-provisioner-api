"""The operator console's sales and customer reports (ADR-082 slice 3a).

Every report is called **as `cp_admin_console`, in production mode**, against seeded
customers, subscriptions and billing events -- so a query that needs a column or a row
the role was not granted fails here, and the allowlist in `admin_grants` stays exactly as
wide as the reports.

Also held: the reports carry platform records and never a secret (no password hash, no
token, no key material, no amount), every route needs a staff session, and opening one
organization writes `staff.view` while lists do not.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from services.control_plane import config as config_module
from services.control_plane import db, identity
from services.control_plane.admin_main import create_admin_app
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_admin_grants import PASSWORD, STAFF_KEY, _code, console_dsn, seed  # noqa: F401 - fixtures

pytestmark = requires_db

HEADERS = {"X-MaluDB-Staff": "1"}
NOW = datetime.now(UTC)
ROUTES = ("/admin/v1/overview", "/admin/v1/sales", "/admin/v1/billing-events", "/admin/v1/customers",
          "/admin/v1/usage", "/admin/v1/abuse", "/admin/v1/nodes", "/admin/v1/provisioning")


@pytest.fixture
def customers(db_pool):
    """Two organizations: one paying and a payment failed; one free. Returned by name."""
    with db.connection() as conn:
        plans = {code: db.one(conn, "INSERT INTO plans (code, name) VALUES (%s, %s) ON CONFLICT (code) "
                                    "DO UPDATE SET name = EXCLUDED.name RETURNING id", (code, code.title()))["id"]
                 for code in ("free", "starter")}
        acme_owner, acme = identity.create_user_with_personal_org(
            conn, email="founder@acme.test", password=TEST_CREDENTIAL, display_name="Acme")
        _, hobby = identity.create_user_with_personal_org(conn, email="hobbyist@example.test",
                                                          password=TEST_CREDENTIAL)
        teammate, _ = identity.create_user_with_personal_org(conn, email="dev@acme.test", password=TEST_CREDENTIAL)
        db.execute(conn, "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'developer')",
                   (acme, teammate.id))
        projects = {}
        for ref, org, plan, status in (("acmeprod", acme, "starter", "ACTIVE"), ("acmedev1", acme, "starter",
                                       "ACTIVE"), ("hobby001", hobby, "free", "PROVISIONED"),
                                       ("broken01", hobby, "free", "FAILED")):
            projects[ref] = uuid.uuid4()
            db.execute(conn, "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                             "database_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                       (projects[ref], org, ref, ref.title(), plans[plan], status, 12_345_678))
        for ref, state, since in (("acmeprod", "active", NOW - timedelta(days=30)),
                                  ("acmedev1", "past_due", NOW - timedelta(days=3))):
            db.execute(conn, "INSERT INTO subscriptions (id, org_id, project_id, plan_code, state, state_as_of, "
                             "state_since, provider, provider_subscription_id, provider_customer_id, "
                             "reconciled_state, reconciled_plan_code) VALUES (%s, %s, %s, 'starter', %s, %s, %s, "
                             "'stripe', %s, 'cus_acme', 'active', 'starter')",
                       (uuid.uuid4(), acme, projects[ref], state, since, since, f"sub_{ref}"))
        for outcome, event_type in (("applied", "checkout.session.completed"), ("failed", "invoice.payment_failed")):
            db.execute(conn, "INSERT INTO billing_events (id, provider, event_id, event_type, livemode, event_at, "
                             "outcome, project_id, note) VALUES (%s, 'stripe', %s, %s, false, now(), %s, %s, %s)",
                       (uuid.uuid4(), f"evt_{outcome}", event_type, outcome, projects["acmedev1"],
                        f"{outcome} for a test"))
        conn.commit()
    return {"acme": acme, "hobby": hobby, "acme_owner": acme_owner}


def _as_console(dsn, migrated_database, calls):
    """Run `calls(client)` against the console connected as its own role, then restore the suite's pool."""
    cfg = config_module.AdminConfig(environment="production", database_url=dsn, staff_key=STAFF_KEY,
                                    billing_grace_days=14)
    app = create_admin_app(cfg)
    db.close_pool()
    try:
        with TestClient(app, base_url="https://testserver") as client:
            return calls(client)
    finally:
        db.close_pool()
        db.init_pool(migrated_database)


def _signed_in(client, seed):  # noqa: F811 - fixture value
    response = client.post("/admin/v1/session", headers=HEADERS,
                           json={"email": "ops@example.com", "password": PASSWORD, "code": _code(seed)})
    assert response.status_code == 200, response.text


def test_every_report_answers_as_the_console_role(console_dsn, seed, customers, migrated_database):  # noqa: F811
    def calls(client):
        _signed_in(client, seed)
        out = {route: client.get(route) for route in ROUTES}
        out["search"] = client.get("/admin/v1/customers", params={"q": "ACME.test"})
        out["detail"] = client.get(f"/admin/v1/customers/{customers['acme']}")
        out["missing"] = client.get(f"/admin/v1/customers/{uuid.uuid4()}")
        out["past_due"] = client.get("/admin/v1/sales", params={"state": "past_due"})
        out["failed_events"] = client.get("/admin/v1/billing-events", params={"outcome": "failed"})
        return out

    out = _as_console(console_dsn, migrated_database, calls)
    for name, response in out.items():
        if name != "missing":
            assert response.status_code == 200, (name, response.text)
    assert out["missing"].status_code == 404

    overview = out["/admin/v1/overview"].json()
    assert overview["organizations"] >= 3 and overview["projects"] == 4
    assert overview["projects_serving"] == 3 and overview["projects_failed"] == 1
    assert {p["plan_code"]: p["projects"] for p in overview["projects_by_plan"]} == {"starter": 2, "free": 2}
    assert overview["in_grace"] == 1 and overview["events_7d_unhandled"] == 1

    sales = out["/admin/v1/sales"].json()
    assert {s["project_ref"] for s in sales["subscriptions"]} == {"acmeprod", "acmedev1"}
    assert [g["project_ref"] for g in sales["in_grace"]] == ["acmedev1"]
    assert [p["project_ref"] for p in sales["pending_reconciliation"]] == ["acmedev1"], "past_due not yet applied"
    assert [s["state"] for s in out["past_due"].json()["subscriptions"]] == ["past_due"]
    assert [e["outcome"] for e in out["failed_events"].json()] == ["failed"]

    # By member address as well as by name: the teammate's own personal organization matches too.
    found = {c["display_name"]: c for c in out["search"].json()}
    assert set(found) == {"Acme", "dev@acme.test"}
    acme = found["Acme"]
    assert acme["owners"] == ["founder@acme.test"]
    assert acme["members"] == 2 and acme["projects"] == 2 and acme["paying_subscriptions"] == 2

    detail = out["detail"].json()
    assert [m["email"] for m in detail["members"]] == ["founder@acme.test", "dev@acme.test"]
    assert {p["project_ref"] for p in detail["projects"]} == {"acmeprod", "acmedev1"}
    assert len(detail["billing_events"]) == 2

    with db.connection() as conn:
        views = db.query(conn, "SELECT actor_id, org_id, detail_json FROM audit_events "
                               " WHERE event_type = 'staff.view'")
    assert len(views) == 1, "one view of one organization; lists and 404s record nothing"
    assert views[0]["org_id"] == customers["acme"] and views[0]["actor_id"].startswith("staff:")


def test_no_report_carries_a_secret(console_dsn, seed, customers, migrated_database):  # noqa: F811
    with db.connection() as conn:
        hash_prefix = db.one(conn, "SELECT substr(password_hash, 1, 20) AS h FROM users LIMIT 1")["h"]

    def calls(client):
        _signed_in(client, seed)
        texts = [client.get(route).text for route in ROUTES]
        texts.append(client.get(f"/admin/v1/customers/{customers['acme']}").text)
        return texts

    for text in _as_console(console_dsn, migrated_database, calls):
        assert hash_prefix not in text and "$argon2" not in text
        assert not re.search(r"mldb_(sess|pat|staff|secret|publishable)_", text)
        assert not re.search(r'"(password_hash|token_hash|ciphertext|nonce|amount|unit_amount)"', text)


@pytest.mark.parametrize("route", [*ROUTES, "/admin/v1/customers/00000000-0000-0000-0000-000000000000"])
def test_every_report_needs_a_staff_session(migrated_database, db_pool, route):
    cfg = config_module.AdminConfig(environment="test", database_url=migrated_database, staff_key=STAFF_KEY)
    with TestClient(create_admin_app(cfg), base_url="https://testserver") as client:
        assert client.get(route).status_code == 401


# -- usage and abuse (slice 3b) ------------------------------------------------------


def _pressure(customers_fixture):
    """Push acmedev1 past its database ceiling, hobby001 to 90% of egress and its email day."""
    from services.control_plane import entitlements

    free = entitlements.resolve("free", {})
    with db.connection() as conn:
        ids = {r["project_ref"]: r["id"] for r in db.query(conn, "SELECT id, project_ref FROM projects")}
        db.execute(conn, "UPDATE projects SET database_bytes = %s, storage_state = 'restricted', "
                         "database_measured_at = now() WHERE project_ref = 'acmedev1'",
                   (entitlements.resolve("starter", {}).database_storage_bytes * 2,))
        db.execute(conn, "UPDATE projects SET database_bytes = NULL WHERE project_ref = 'broken01'")
        db.execute(conn, "INSERT INTO project_egress (project_id, period_start, bytes) "
                         "VALUES (%s, date_trunc('month', now() AT TIME ZONE 'UTC')::date, %s)",
                   (ids["hobby001"], int(free.egress_bytes_per_month * 0.9)))
        for _ in range(3):
            db.execute(conn, "INSERT INTO email_events (project_id, event_type, recipient_hash, occurred_at) "
                             "VALUES (%s, 'sent', '\\x00', now())", (ids["hobby001"],))
        conn.commit()


def test_usage_and_abuse_answer_as_the_console_role(console_dsn, seed, customers, migrated_database):  # noqa: F811
    _pressure(customers)

    def calls(client):
        _signed_in(client, seed)
        return {
            "usage": client.get("/admin/v1/usage"),
            "usage_free": client.get("/admin/v1/usage", params={"plan": "free"}),
            "abuse": client.get("/admin/v1/abuse"),
            "abuse_high": client.get("/admin/v1/abuse", params={"min_percent": 95}),
            "bad_plan": client.get("/admin/v1/usage", params={"plan": "no spaces allowed"}),
        }

    out = _as_console(console_dsn, migrated_database, calls)
    for name in ("usage", "usage_free", "abuse", "abuse_high"):
        assert out[name].status_code == 200, (name, out[name].text)
    assert out["bad_plan"].status_code == 422

    usage = out["usage"].json()
    assert usage[0]["project_ref"] == "acmedev1", "highest pressure first"
    top = usage[0]
    assert top["peak_meter"] == "database" and top["database"]["percent"] == 200.0
    assert top["database"]["state"] == "restricted" and top["database"]["measured_at"]
    by_ref = {row["project_ref"]: row for row in usage}
    hobby = by_ref["hobby001"]
    assert hobby["egress"]["percent"] == 90.0 and hobby["egress"]["state"] == "warning"
    assert hobby["email_day"]["used"] == 3 and hobby["email_day"]["state"] == "ok"
    assert by_ref["broken01"]["database"]["percent"] is None, "never measured is not zero"

    assert {row["plan_code"] for row in out["usage_free"].json()} == {"free"}
    abuse = [row["project_ref"] for row in out["abuse"].json()]
    assert abuse[0] == "hobby001" and "acmedev1" not in abuse, "the free plan by default"
    assert out["abuse_high"].json() == [], "nothing on free is at 95%"
    assert "recipient_hash" not in out["usage"].text


def test_a_used_zero_ceiling_is_flagged_not_infinite(console_dsn, seed, customers, migrated_database):  # noqa: F811
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET config_json = %s WHERE code = 'free'",
                   (Jsonb({"limits": {"emails_per_day": 0}}),))
        ids = {r["project_ref"]: r["id"] for r in db.query(conn, "SELECT id, project_ref FROM projects")}
        db.execute(conn, "INSERT INTO email_events (project_id, event_type, recipient_hash, occurred_at) "
                         "VALUES (%s, 'sent', '\\x00', now())", (ids["hobby001"],))
        conn.commit()

    def calls(client):
        _signed_in(client, seed)
        return client.get("/admin/v1/abuse")

    response = _as_console(console_dsn, migrated_database, calls)
    assert response.status_code == 200, response.text
    hobby = next(row for row in response.json() if row["project_ref"] == "hobby001")
    assert hobby["email_day"]["over_zero_ceiling"] is True and hobby["email_day"]["percent"] is None
    assert hobby["email_day"]["state"] == "exceeded" and hobby["peak_percent"] is None
    assert response.json()[0]["project_ref"] == "hobby001", "a used zero ceiling outranks everything"


# -- nodes and provisioning (slice 3c) -----------------------------------------------


def _nodes_and_jobs(customers_fixture):
    """Two nodes -- one healthy with room, one stale -- and projects failed, stuck and freshly requested."""
    with db.connection() as conn:
        healthy = db.one(conn, "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, capacity_json, "
                               "metrics_json, last_health_at) VALUES ('node-a', 'secret-host.internal', "
                               "'10.9.9.9', 'shared', 'active', %s, %s, now()) RETURNING id",
                         (Jsonb({"max_projects": 50, "max_warm_projects": 10}),
                          Jsonb({"free_disk_bytes": 900 * 1024**3})))["id"]
        db.execute(conn, "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
                         "VALUES ('node-b', 'b.internal', '10.9.9.10', 'shared', 'active', "
                         "now() - interval '1 hour')")
        ids = {r["project_ref"]: r["id"] for r in db.query(conn, "SELECT id, project_ref FROM projects")}
        db.execute(conn, "UPDATE projects SET node_id = %s, worker_state = 'RUNNING' WHERE project_ref = 'acmeprod'",
                   (healthy,))
        db.execute(conn, "UPDATE projects SET node_id = %s, failed_at = now() WHERE project_ref = 'broken01'",
                   (healthy,))
        db.execute(conn, "INSERT INTO provisioning_jobs (id, project_id, state, attempt, error_code, error_detail) "
                         "VALUES (%s, %s, 'FAILED', 3, 'bootstrap_failed', 'connection to secret-host.internal "
                         "refused')", (uuid.uuid4(), ids["broken01"]))
        stuck, fresh = uuid.uuid4(), uuid.uuid4()
        plan = db.one(conn, "SELECT id FROM plans WHERE code = 'free'")["id"]
        for pid, ref, age in ((stuck, "stuck001", "1 hour"), (fresh, "fresh001", "1 minute")):
            db.execute(conn, "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                             "requested_at) VALUES (%s, %s, %s, %s, %s, 'DATABASE_CREATING', "
                             "now() - %s::interval)",
                       (pid, customers_fixture["hobby"], ref, ref, plan, age))
        conn.commit()


def test_nodes_and_provisioning_answer_as_the_console_role(console_dsn, seed, customers, migrated_database):  # noqa: F811
    _nodes_and_jobs(customers)

    def calls(client):
        _signed_in(client, seed)
        return {"nodes": client.get("/admin/v1/nodes"), "provisioning": client.get("/admin/v1/provisioning")}

    out = _as_console(console_dsn, migrated_database, calls)
    assert out["nodes"].status_code == 200, out["nodes"].text
    assert out["provisioning"].status_code == 200, out["provisioning"].text

    by_name = {n["name"]: n for n in out["nodes"].json()}
    a, b = by_name["node-a"], by_name["node-b"]
    assert a["projects"] == 2 and a["max_projects"] == 50 and a["warm_projects"] == 1
    assert a["free_disk_bytes"] == 900 * 1024**3 and not a["health_stale"]
    assert a["projected_connections"] > 0, "a warm project's pool counts against the node"
    # No extension pins were set, so placement refuses both -- and says why.
    assert a["accepting"] is False and "extension pin" in (a["refusal"] or "")
    assert b["health_stale"] and b["refusal"] == "no health report in the last 5 minutes"

    report = out["provisioning"].json()
    failed = {p["project_ref"]: p for p in report["failed"]}
    assert set(failed) == {"broken01"}
    assert failed["broken01"]["error_code"] == "bootstrap_failed" and failed["broken01"]["attempt"] == 3
    assert failed["broken01"]["node_name"] == "node-a"
    assert [p["project_ref"] for p in report["stuck"]] == ["stuck001"], "fresh001 is only a minute old"

    for response in out.values():
        assert "secret-host.internal" not in response.text and "10.9.9.9" not in response.text
