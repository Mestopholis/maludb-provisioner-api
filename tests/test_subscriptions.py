"""What has been paid for, kept apart from what is enforced (Phase 09 slice 3).

ADR-048. The property under test throughout is a negative one and it is easy to
lose: **nothing in `subscriptions` changes an entitlement.** Recording a
payment, a failed payment, a cancellation and an upgrade all leave
`projects.plan_id` exactly where they found it, and only `reconcile` moves it --
through `plan_change`, which is the operation that owns a node.

That separation is acceptance criterion 3 and it is what makes a provider swap
survivable, so most of what follows asserts the *absence* of an effect. A suite
that only checked that reconciliation works would pass just as well against an
implementation that applied plans directly from a webhook, which is the design
this one exists to rule out.

Most of these need no node: the state machine, the ordering guard, the
cross-tenant constraint and the drift report are all control-plane facts. The
handful that reach a node are marked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from services.control_plane import db, plan_change, subscriptions
from tests.conftest import requires_db
from tests.test_direct_sql import paid_project  # noqa: F401 - fixture
from tests.test_provisioning import ADMIN_DSN, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _plan(code: str, config: dict | None = None) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, is_active, config_json) VALUES (%s, %s, TRUE, %s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json, "
            "                                 is_active = EXCLUDED.is_active",
            (code, code.title(), psycopg.types.json.Jsonb(config or {})),
        )
        conn.commit()


def _free() -> None:
    """The default plan. `entitled_plan_code` resolves to it by code, so a
    deployment without one is an error rather than a guess -- asserted below."""
    _plan("free", {"direct_database_access": False})


def _plan_code(project_id) -> str:
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT pl.code FROM projects pr JOIN plans pl ON pl.id = pr.plan_id "
            " WHERE pr.id = %s",
            (project_id,),
        )["code"]


def _set_plan(project_id, code: str) -> None:
    """Put a project on a plan *without* going through `plan_change`.

    Deliberately the raw UPDATE: these tests need a project sitting on a paid
    plan with no node behind it, which is exactly the state slice 1's operation
    refuses to create. It is also the state the fleet is in today.
    """
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET plan_id = (SELECT id FROM plans WHERE code = %s) WHERE id = %s",
            (code, project_id),
        )
        conn.commit()


def _entitled(project_id) -> str:
    with db.connection() as conn:
        return subscriptions.entitled_plan_code(conn, project_id)


# -- a subscription changes no entitlement ----------------------------------


def test_recording_a_subscription_changes_no_entitlement(placed_project):
    """The property the whole slice exists for. A paid subscription on a free
    project leaves the project free until somebody reconciles it."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00001")
    _set_plan(project_id, "free")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)

    assert _plan_code(project_id) == "free"
    assert _entitled(project_id) == "pro"


def test_a_cancellation_changes_no_entitlement_either(placed_project):
    """The direction that matters more. A cancellation that downgraded on the
    spot would be a customer's plan changing inside a webhook handler."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00002")
    _set_plan(project_id, "pro")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        subscriptions.record_state(
            conn, project_id=project_id, state="canceled", as_of=T0 + timedelta(days=1)
        )

    assert _plan_code(project_id) == "pro"
    assert _entitled(project_id) == "free"


# -- what each state entitles ------------------------------------------------


@pytest.mark.parametrize(
    ("state", "entitles_the_plan"),
    [
        ("incomplete", False),
        ("trialing", True),
        ("active", True),
        ("past_due", True),
        ("canceled", False),
    ],
)
def test_each_state_entitles_the_plan_or_the_free_tier(placed_project, state, entitles_the_plan):
    """`past_due` being True here is the slice's one opinion about failed
    payment, and it is a default rather than an answer: how long it lasts is the
    third `## Billing` question and slice 5's business. What is settled is that
    a payment failing is not, by itself, a downgrade -- which is the direction
    acceptance criterion 4 demands."""
    _free()
    _plan("pro")
    project_id = placed_project(f"subs{state[:4]:0<4}")

    with db.connection() as conn:
        # Created in the target state where that is legal, which is every one
        # but `canceled` -- a subscription cannot be created dead, so that one
        # is reached the way a real one is.
        opening = "active" if state == "canceled" else state
        subscriptions.create(conn, project_id=project_id, plan_code="pro",
                             state=opening, as_of=T0)
        if state != opening:
            subscriptions.record_state(
                conn, project_id=project_id, state=state, as_of=T0 + timedelta(hours=1)
            )

    assert _entitled(project_id) == ("pro" if entitles_the_plan else "free")


def test_a_project_with_no_subscription_is_entitled_to_the_free_tier(placed_project):
    """Not a fallback. Every project is entitled to the free tier by existing,
    which is why this is the same answer a canceled subscription gives."""
    _free()
    project_id = placed_project("sub00003")
    _set_plan(project_id, "free")

    assert _entitled(project_id) == "free"
    with db.connection() as conn:
        assert subscriptions.for_project(conn, project_id) is None


def test_a_deployment_with_no_free_plan_is_an_error_rather_than_a_guess(placed_project):
    """`models.default_plan` returns None and this refuses. Guessing would mean
    resolving to whatever plan sorts first, which could be the most expensive
    one -- the reason `default_plan` looks up 'free' by code."""
    _plan("pro")
    project_id = placed_project("sub00004")
    _set_plan(project_id, "pro")
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM plans WHERE code = 'free'")
        conn.commit()

    with pytest.raises(subscriptions.SubscriptionError, match="no plan called 'free'"):
        _entitled(project_id)


# -- the ordering guard ------------------------------------------------------


def test_a_fact_older_than_the_one_on_record_is_refused(placed_project):
    """The replay defence, and the reason `state_as_of` is a column rather than
    slice 4's problem. Providers retry and deliver out of order, so a `canceled`
    can arrive after the `active` that superseded it; ordered by arrival that
    downgrades a paying customer."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00005")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        subscriptions.record_state(
            conn, project_id=project_id, state="active", as_of=T0 + timedelta(days=2)
        )
        with pytest.raises(subscriptions.SubscriptionError, match="stale"):
            subscriptions.record_state(
                conn, project_id=project_id, state="canceled", as_of=T0 + timedelta(days=1)
            )

    assert _entitled(project_id) == "pro"


def test_the_same_moment_is_accepted_because_a_redelivery_is_idempotent(placed_project):
    """Equal rather than strictly newer, on purpose. A provider redelivering the
    current truth carries the same timestamp, and refusing it would turn an
    ordinary retry into an operator's problem. Exact duplicates are slice 4's
    event-id idempotency, which is a different control."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00006")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        again = subscriptions.record_state(
            conn, project_id=project_id, state="active", as_of=T0
        )

    assert again.state == "active"
    assert again.state_as_of == T0


def test_the_ordering_guard_is_in_the_update_and_not_only_in_the_read(placed_project):
    """The check in `record_state` reads a row and then writes it, so two
    deliveries arriving together both pass it and the later commit wins
    regardless of which fact is newer -- which is the out-of-order downgrade the
    column exists to prevent. Simulated here by writing a newer fact underneath
    a call that has already read the older one.
    """
    _free()
    _plan("pro")
    project_id = placed_project("sub00029")

    with db.connection() as conn:
        created = subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        # The interleaving: another writer lands a later fact between this
        # caller's read and its write.
        db.execute(
            conn, "UPDATE subscriptions SET state_as_of = %s WHERE id = %s",
            (T0 + timedelta(days=5), created.id),
        )
        conn.commit()

        with pytest.raises(subscriptions.SubscriptionError, match="stale"):
            subscriptions.record_state(
                conn, project_id=project_id, state="canceled",
                as_of=T0 + timedelta(days=1),
                # Sidestep the read-time check so the WHERE clause is what is
                # under test; without the clause this call would succeed.
                plan_code=None,
            )

    assert _entitled(project_id) == "pro"


def test_a_second_live_subscription_is_refused_by_the_index_not_only_the_check(placed_project):
    """The pair that will actually race is an operator and slice 4's webhook
    handler. Both would pass a check-then-insert, and the loser would leave the
    project with two live subscriptions entitling different plans."""
    _free()
    _plan("pro")
    _plan("team")
    project_id = placed_project("sub00030")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        org = db.one(conn, "SELECT org_id FROM projects WHERE id = %s", (project_id,))["org_id"]
        # The insert a caller that had already passed the check would issue.
        with pytest.raises(psycopg.errors.UniqueViolation):
            db.execute(
                conn,
                "INSERT INTO subscriptions (id, org_id, project_id, plan_code, state, "
                "                           state_as_of) VALUES (%s, %s, %s, 'team', 'active', %s)",
                (uuid.uuid4(), org, project_id, T0),
            )
        conn.rollback()
        assert subscriptions.for_project(conn, project_id).plan_code == "pro"


# -- the state machine -------------------------------------------------------


def test_a_canceled_subscription_is_terminal(placed_project):
    """Reviving one would overwrite the record of what was sold. A customer who
    comes back gets a new row, which the partial unique index permits."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00007")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        subscriptions.record_state(
            conn, project_id=project_id, state="canceled", as_of=T0 + timedelta(days=1)
        )
        # There is no live subscription to transition any more, which is how
        # terminality is enforced rather than by a check somebody could skip.
        with pytest.raises(subscriptions.SubscriptionError, match="no live subscription"):
            subscriptions.record_state(
                conn, project_id=project_id, state="active", as_of=T0 + timedelta(days=2)
            )


def test_a_returning_customer_gets_a_new_row_and_keeps_the_old_one(placed_project):
    _free()
    _plan("pro")
    project_id = placed_project("sub00008")

    with db.connection() as conn:
        first = subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        subscriptions.record_state(
            conn, project_id=project_id, state="canceled", as_of=T0 + timedelta(days=1)
        )
        second = subscriptions.create(
            conn, project_id=project_id, plan_code="pro", as_of=T0 + timedelta(days=30)
        )
        past = subscriptions.history(conn, project_id)

    assert first.id != second.id
    assert len(past) == 2
    assert {row["state"] for row in past} == {"canceled", "active"}


def test_an_active_subscription_cannot_go_back_to_incomplete(placed_project):
    """A subscription that has been paid for cannot un-happen. The transition
    map is small enough to read; this pins that it is consulted."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00009")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        with pytest.raises(subscriptions.SubscriptionError, match="cannot go from active"):
            subscriptions.record_state(
                conn, project_id=project_id, state="incomplete", as_of=T0 + timedelta(days=1)
            )


def test_a_project_gets_one_live_subscription(placed_project):
    _free()
    _plan("pro")
    _plan("team")
    project_id = placed_project("sub00010")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        with pytest.raises(subscriptions.SubscriptionError, match="already has a live"):
            subscriptions.create(conn, project_id=project_id, plan_code="team", as_of=T0)


def test_a_subscription_cannot_be_created_canceled(placed_project):
    _free()
    _plan("pro")
    project_id = placed_project("sub00011")

    with db.connection() as conn:
        with pytest.raises(subscriptions.SubscriptionError, match="cannot be created canceled"):
            subscriptions.create(
                conn, project_id=project_id, plan_code="pro", state="canceled", as_of=T0
            )


def test_an_upgrade_moves_the_plan_on_the_same_subscription(placed_project):
    """A webhook carries the whole current truth rather than a delta, so state
    and plan arrive together. Still applies nothing."""
    _free()
    _plan("pro")
    _plan("team")
    project_id = placed_project("sub00012")
    _set_plan(project_id, "pro")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        updated = subscriptions.record_state(
            conn, project_id=project_id, state="active", plan_code="team",
            as_of=T0 + timedelta(days=1),
        )

    assert updated.plan_code == "team"
    assert _entitled(project_id) == "team"
    assert _plan_code(project_id) == "pro"


# -- refusals ---------------------------------------------------------------


def test_a_plan_the_catalogue_does_not_offer_is_refused_at_the_write(placed_project):
    """`plan_change` would refuse it later anyway. Failing here is cheaper: a
    subscription naming a plan that does not exist can never reconcile and
    would be reported as drift forever."""
    _free()
    project_id = placed_project("sub00013")

    with db.connection() as conn:
        with pytest.raises(subscriptions.SubscriptionError, match="no active plan"):
            subscriptions.create(conn, project_id=project_id, plan_code="invented", as_of=T0)


def test_a_retired_plan_is_refused_the_same_way_a_misspelled_one_is(placed_project):
    _free()
    _plan("legacy")
    project_id = placed_project("sub00014")
    with db.connection() as conn:
        db.execute(conn, "UPDATE plans SET is_active = FALSE WHERE code = 'legacy'")
        conn.commit()
        with pytest.raises(subscriptions.SubscriptionError, match="no active plan"):
            subscriptions.create(conn, project_id=project_id, plan_code="legacy", as_of=T0)


def test_a_deleting_project_cannot_be_subscribed(placed_project):
    _free()
    _plan("pro")
    project_id = placed_project("sub00015")
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status = 'DELETING' WHERE id = %s", (project_id,))
        conn.commit()
        with pytest.raises(subscriptions.SubscriptionError, match="being deleted"):
            subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)


# -- cross-tenant ------------------------------------------------------------


def test_the_paying_organization_is_read_from_the_project(placed_project):
    """Not taken as an argument, which is the control. A caller that could name
    the org could pay for one organization's project out of another's
    subscription -- and then move it between plans."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00016")

    with db.connection() as conn:
        created = subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        owner = db.one(conn, "SELECT org_id FROM projects WHERE id = %s", (project_id,))["org_id"]

    assert created.org_id == owner


def test_the_database_refuses_a_subscription_naming_another_organization(placed_project):
    """Defence in depth, and it is the half that survives a future caller.
    Migration 0020's composite foreign key means no code path -- a webhook
    handler, a backfill script, a psql session -- can attach one organization's
    payment to another organization's project."""
    _free()
    _plan("pro")
    mine = placed_project("sub00017")
    theirs = placed_project("sub00018")

    with db.connection() as conn:
        other_org = db.one(
            conn, "SELECT org_id FROM projects WHERE id = %s", (theirs,)
        )["org_id"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute(
                conn,
                "INSERT INTO subscriptions (id, org_id, project_id, plan_code, state, "
                "                           state_as_of) VALUES (%s, %s, %s, 'pro', 'active', %s)",
                (uuid.uuid4(), other_org, mine, T0),
            )
        conn.rollback()


# -- the drift report --------------------------------------------------------


def test_a_paid_project_nobody_is_paying_for_is_reported_as_unbilled(placed_project):
    """The finding this report exists for, and every project on the platform is
    one the day it ships: `project set-plan` moves a project to a paid plan and
    takes no money, which was correct while there was nowhere to record that
    money had been taken."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00019")
    _set_plan(project_id, "pro")

    with db.connection() as conn:
        found = subscriptions.drift(conn)

    assert [(d.project_ref, d.direction) for d in found] == [("sub00019", "unbilled")]
    assert found[0].entitled_plan_code == "free"


def test_a_subscription_whose_plan_never_reached_the_project_is_reported(placed_project):
    _free()
    _plan("pro")
    project_id = placed_project("sub00020")
    _set_plan(project_id, "free")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        found = subscriptions.drift(conn)

    assert [(d.project_ref, d.direction) for d in found] == [("sub00020", "diverged")]
    assert found[0].entitled_plan_code == "pro"


def test_a_reconciled_project_is_not_reported(placed_project):
    """The property that makes the report worth reading: it must be empty in the
    ordinary case, or nobody will look at it."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00021")
    _set_plan(project_id, "pro")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        assert subscriptions.drift(conn) == []


def test_a_free_project_with_no_subscription_is_not_reported(placed_project):
    _free()
    project_id = placed_project("sub00022")
    _set_plan(project_id, "free")

    with db.connection() as conn:
        assert subscriptions.drift(conn) == []


def test_a_deleted_project_is_not_reported(placed_project):
    """A project being torn down is not an operator's billing question, and
    leaving it in the report is how a report becomes noise."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00023")
    _set_plan(project_id, "pro")
    with db.connection() as conn:
        db.execute(
            conn, "UPDATE projects SET status = 'DELETING' WHERE id = %s", (project_id,)
        )
        conn.commit()
        assert subscriptions.drift(conn) == []


# -- the customer's own trail ------------------------------------------------


def test_the_subscription_is_visible_in_the_customers_audit_trail(placed_project):
    """Two events for one purchase -- the billing fact and the plan change that
    carried it out -- because the two halves can diverge and a customer looking
    at one should be able to find the other."""
    from services.control_plane.api import audit

    _free()
    _plan("pro")
    project_id = placed_project("sub00024")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        subscriptions.record_state(
            conn, project_id=project_id, state="past_due", as_of=T0 + timedelta(days=1)
        )
        rows = db.query(
            conn,
            "SELECT event_type, detail_json FROM audit_events WHERE project_id = %s "
            "ORDER BY id",
            (project_id,),
        )

    assert [row["event_type"] for row in rows] == [
        subscriptions.CREATED, subscriptions.STATE_CHANGED,
    ]
    for row in rows:
        assert row["event_type"] in audit.VISIBLE_EVENTS
        allowed = set(audit.VISIBLE_EVENTS[row["event_type"]][1])
        assert set(row["detail_json"]) <= allowed


def test_the_trail_carries_no_amount_provider_or_customer_identifier(placed_project):
    """Costs nothing to promise now, and is the promise slice 4 will be tempted
    to break when it has a webhook body in its hand."""
    _free()
    _plan("pro")
    project_id = placed_project("sub00025")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="pro", as_of=T0)
        rows = db.query(
            conn,
            "SELECT detail_json FROM audit_events WHERE project_id = %s", (project_id,),
        )

    forbidden = {"amount", "currency", "provider", "customer_id", "customer", "email",
                 "invoice", "price_id", "subscription_id"}
    for row in rows:
        assert not forbidden & set(row["detail_json"])


# -- reconciliation, which is the only half that acts ------------------------


@requires_node
@requires_maludb_core
def test_reconcile_moves_the_project_onto_the_plan_being_paid_for(paid_project, admin_conn):  # noqa: F811
    """The seam, exercised end to end. Note what the assertion is about: the
    plan moved *and* the change went through `plan_change`, which is what keeps
    billing from ever writing to a node itself."""
    _free()
    _plan("paid-tier", {"direct_database_access": True, "limits": {"work_mem_mb": 32}})
    project_id, _, _ = paid_project("sub00026")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="paid-tier", as_of=T0)
        before = plan_change.identity(conn, project_id)
        result = subscriptions.reconcile(conn, admin_conn, project_id=project_id)
        after = plan_change.identity(conn, project_id)
        recorded = db.query(
            conn, "SELECT state, to_plan_code FROM plan_changes WHERE project_id = %s",
            (project_id,),
        )

    assert result.changed
    assert _plan_code(project_id) == "paid-tier"
    assert after == before, "ADR-006: a plan change keeps the database"
    assert recorded == [{"state": "APPLIED", "to_plan_code": "paid-tier"}]


@requires_node
@requires_maludb_core
def test_reconciling_twice_is_uneventful(paid_project, admin_conn):  # noqa: F811
    """The whole point of a reconciler. A second run must record no plan change
    at all, not an APPLIED one that changed nothing."""
    _free()
    _plan("idem-tier", {"direct_database_access": True})
    project_id, _, _ = paid_project("sub00027")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="idem-tier", as_of=T0)
        subscriptions.reconcile(conn, admin_conn, project_id=project_id)
        second = subscriptions.reconcile(conn, admin_conn, project_id=project_id)
        count = db.one(
            conn, "SELECT count(*) AS n FROM plan_changes WHERE project_id = %s", (project_id,)
        )["n"]

    assert not second.changed
    assert second.change is None
    assert count == 1


@requires_node
@requires_maludb_core
def test_a_cancellation_reconciles_to_free_without_destroying_data(paid_project, admin_conn):  # noqa: F811
    """Acceptance criterion 4, at the one place slice 3 can assert it. A
    cancellation is the most destructive thing this slice can cause, and what it
    causes is a downgrade: the database, its schema and its rows survive, and
    direct access stops."""
    _free()
    _plan("cancel-tier", {"direct_database_access": True})
    project_id, names, _ = paid_project("sub00028")

    with db.connection() as conn:
        subscriptions.create(conn, project_id=project_id, plan_code="cancel-tier", as_of=T0)
        subscriptions.reconcile(conn, admin_conn, project_id=project_id)

    with psycopg.connect(_tenant_dsn_for(names.database)) as tenant:
        tenant.execute("CREATE TABLE IF NOT EXISTS survives (id int)")
        tenant.execute("INSERT INTO survives VALUES (1)")
        tenant.commit()

    with db.connection() as conn:
        subscriptions.record_state(
            conn, project_id=project_id, state="canceled", as_of=T0 + timedelta(days=1)
        )
        subscriptions.reconcile(conn, admin_conn, project_id=project_id)

    assert _plan_code(project_id) == "free"
    exists = admin_conn.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (names.database,)
    ).fetchone()
    assert exists, "a cancellation must not drop the tenant database"
    with psycopg.connect(_tenant_dsn_for(names.database)) as tenant:
        assert tenant.execute("SELECT count(*) FROM survives").fetchone()[0] == 1
    # `admin_conn` is a dict_row connection, per conftest.
    assert not admin_conn.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (names.client,)
    ).fetchone()["rolcanlogin"], "a canceled subscription must revoke direct access"


def _tenant_dsn_for(database: str) -> str:
    """The platform's own superuser connection to a tenant, for assertions.

    Not a customer's route in: this is the test harness reading the tenant
    database to prove its contents survived, and it uses the node admin DSN the
    suite already requires.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    parsed["dbname"] = database
    return psycopg.conninfo.make_conninfo(**parsed)
