"""ADR-056's two ceilings: bytes held, bytes served, and the plan behind both.

**Object** storage accounting. `tests/test_storage.py` is the *database* quota
and its ADR-040 write restriction; `tests/test_object_storage.py` is the tenant
`storage` schema slice 1 built. This file is the third of the three and shares
only the word.

The classification is pure and tested as such. The two things that are not pure
are the ones worth the setup:

- **The plan-change path.** Phase 09 opened by finding an entitlement that was
  applied once at provisioning and therefore never reached a project that
  changed plan. Both ceilings here re-read the plan on every evaluation, and
  that is asserted rather than assumed — twice, once per resource, because they
  read it through different functions.
- **The counter.** Egress is the platform's first persisted usage figure, so
  the tests cover what a counter gets wrong: accumulation, month boundaries, and
  the arithmetic that would hand a project unlimited egress if it went
  negative.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest

from services.control_plane import db, entitlements, object_storage
from tests.conftest import requires_db
from tests.test_provisioning import ADMIN_DSN, _provision_core, _tenant_admin_dsn

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

GB = 1024 * 1024 * 1024
MB = 1024 * 1024


# -- classification --------------------------------------------------------


def test_the_three_states_and_where_their_boundaries_are():
    quota = 10 * GB
    assert object_storage.classify(0, quota).state == object_storage.OK
    assert object_storage.classify(7 * GB, quota).state == object_storage.OK
    # 80% is where the warning starts, inclusive.
    assert object_storage.classify(8 * GB, quota).state == object_storage.WARNING
    assert object_storage.classify(quota - 1, quota).state == object_storage.WARNING
    # At the ceiling, not merely past it: a project that has exactly filled its
    # quota has no room for the next byte.
    assert object_storage.classify(quota, quota).state == object_storage.EXCEEDED
    assert object_storage.classify(quota * 2, quota).state == object_storage.EXCEEDED


def test_a_missing_or_nonsensical_quota_is_exceeded_rather_than_unlimited():
    """`entitlements`' rule, one layer out: an operator-supplied plan whose
    limit is missing must not resolve to no limit.

    `UNLIMITED` is 0 in that module and means something to PostgreSQL for the
    timeouts. It means nothing here, and a zero-byte ceiling read as infinite is
    the failure that costs money rather than the one that annoys a customer.
    """
    assert object_storage.classify(0, 0).state == object_storage.EXCEEDED
    assert object_storage.classify(0, -1).state == object_storage.EXCEEDED
    assert object_storage.classify(5, 0).fraction == 0.0


def test_negative_usage_is_floored_rather_than_reported():
    assert object_storage.classify(-500, 10 * GB).used_bytes == 0


def test_remaining_never_goes_negative():
    assert object_storage.classify(12 * GB, 10 * GB).remaining_bytes == 0
    assert object_storage.classify(4 * GB, 10 * GB).remaining_bytes == 6 * GB


# -- the entitlements themselves -------------------------------------------


def test_every_tier_has_both_ceilings_and_none_of_them_is_zero():
    """ADR-056 puts Storage on every tier including free. A tier that resolved
    to a zero ceiling would have Storage in name and refuse the first upload."""
    for code in entitlements.DEFAULTS:
        allowed = entitlements.resolve(code, None)
        assert allowed.object_storage_bytes > 0, code
        assert allowed.egress_bytes_per_month > 0, code


def test_a_plans_config_overrides_the_default():
    """`AGENTS.md` forbids hard-coding production plan limits in application
    logic. The numbers in `DEFAULTS` are defaults; a row is what decides."""
    allowed = entitlements.resolve("free", {"limits": {"object_storage_bytes": 42 * GB}})
    assert allowed.object_storage_bytes == 42 * GB
    # And the one not named still falls back rather than vanishing.
    assert allowed.egress_bytes_per_month == entitlements.DEFAULTS["free"]["egress_bytes_per_month"]


def test_egress_is_larger_than_what_a_project_may_hold_on_every_tier():
    """Not arithmetic for its own sake. A monthly egress ceiling below the
    storage ceiling means a project cannot serve its own files once, which is
    ADR-050's churn event rather than a saved dollar."""
    for code in entitlements.DEFAULTS:
        allowed = entitlements.resolve(code, None)
        assert allowed.egress_bytes_per_month > allowed.object_storage_bytes, code


# -- measuring bytes held --------------------------------------------------


@pytest.fixture
def tenant(admin_conn, key_ring, project_factory):
    """A provisioned project and a connection factory for its database."""

    def build(ref: str):
        project_id = project_factory(ref)
        names, _ = _provision_core(project_id, admin_conn, key_ring, ref)

        def tenant_connect(database: str):
            return psycopg.connect(_tenant_admin_dsn(database), autocommit=True)

        return project_id, names, tenant_connect

    return build


def _fake_storage_objects(tenant_connect, database: str, sizes: list[int]) -> None:
    """The two columns this module reads, and nothing else.

    Deliberately not upstream's real schema: measuring is about the metadata
    shape, not about `storage-api`'s 63 migrations, and tying these tests to the
    pinned image would make the accounting untestable without Podman. The
    version that *does* use the real schema is below, gated on the image.
    """
    with tenant_connect(database) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS storage")
        conn.execute("DROP TABLE IF EXISTS storage.objects")
        conn.execute("CREATE TABLE storage.objects (id bigserial PRIMARY KEY, metadata jsonb)")
        for size in sizes:
            conn.execute(
                "INSERT INTO storage.objects (metadata) VALUES (%s)",
                (psycopg.types.json.Jsonb({"size": size, "mimetype": "text/plain"}),),
            )


@requires_node
def test_a_tenant_with_no_storage_schema_measures_zero(tenant):
    """Every project until the storage worker first serves it, which is every
    project between slice 1 and slice 3. Not an error: a project that has never
    used Storage is using none of it, and raising would fail the maintenance
    pass for the whole fleet in the meantime."""
    _, names, tenant_connect = tenant("oa000001")
    with tenant_connect(names.database) as conn:
        conn.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        assert object_storage.measure_objects(conn) == 0


@requires_node
def test_object_bytes_come_from_the_metadata_the_tenant_recorded(tenant):
    _, names, tenant_connect = tenant("oa000002")
    _fake_storage_objects(tenant_connect, names.database, [100, 250, 1_000])
    with tenant_connect(names.database) as conn:
        assert object_storage.measure_objects(conn) == 1_350


@requires_node
def test_a_single_object_over_two_gibibytes_does_not_break_the_sum(tenant):
    """Upstream's own `storage.get_size_by_bucket()` casts each row to `int` —
    four bytes. One 3 GiB file overflows that cast and takes the whole aggregate
    with it, which would turn a large upload into a fleet-wide measurement
    failure. This module casts to bigint and reads the same column."""
    _, names, tenant_connect = tenant("oa000003")
    big = 3 * GB
    _fake_storage_objects(tenant_connect, names.database, [big, big])
    with tenant_connect(names.database) as conn:
        assert object_storage.measure_objects(conn) == 2 * big


@requires_node
def test_evaluate_records_the_measurement_and_its_state(tenant):
    project_id, names, tenant_connect = tenant("oa000004")
    _fake_storage_objects(tenant_connect, names.database, [200 * MB])

    with db.connection() as conn:
        _set_plan_limit(conn, project_id, object_storage_bytes=1 * GB)
        usage = object_storage.evaluate(
            conn, project_id=project_id, tenant_connect=tenant_connect
        )
        assert usage.state == object_storage.OK
        row = db.one(
            conn,
            "SELECT object_bytes, object_measured_at, object_storage_state FROM projects "
            " WHERE id = %s",
            (project_id,),
        )
    assert row["object_bytes"] == 200 * MB
    assert row["object_measured_at"] is not None
    assert row["object_storage_state"] == object_storage.OK


@requires_node
def test_going_over_records_the_state_and_audits_once(tenant):
    """The audit event is written on the transition, not on every pass. A pass
    running every few minutes must not write one every few minutes."""
    project_id, names, tenant_connect = tenant("oa000005")
    _fake_storage_objects(tenant_connect, names.database, [900 * MB])

    with db.connection() as conn:
        _set_plan_limit(conn, project_id, object_storage_bytes=500 * MB)
        first = object_storage.evaluate(conn, project_id=project_id, tenant_connect=tenant_connect)
        second = object_storage.evaluate(conn, project_id=project_id, tenant_connect=tenant_connect)
        assert first.state == second.state == object_storage.EXCEEDED

        events = db.query(
            conn,
            "SELECT event_type FROM audit_events WHERE project_id = %s ORDER BY created_at",
            (project_id,),
        )
        exceeded = [e for e in events if e["event_type"] == "object_storage.exceeded"]
        assert len(exceeded) == 1, "a repeated pass wrote a second audit event"

        marked = db.one(
            conn, "SELECT object_exceeded_at FROM projects WHERE id = %s", (project_id,)
        )
    assert marked["object_exceeded_at"] is not None


@requires_node
def test_nothing_is_revoked_in_the_tenant_when_a_project_is_over(tenant):
    """The difference from `storage.py` that the state name exists to signal.

    Database storage restriction revokes INSERT and UPDATE inside the tenant.
    Object bytes arrive through the Storage API, so the ceiling is enforced
    where the request is and the database is untouched. A pass that appeared to
    enforce and did not would be worse than one that plainly does not.
    """
    project_id, names, tenant_connect = tenant("oa000006")
    _fake_storage_objects(tenant_connect, names.database, [900 * MB])

    with tenant_connect(names.database) as conn:
        before = _table_grants(conn)
    with db.connection() as conn:
        _set_plan_limit(conn, project_id, object_storage_bytes=1)
        assert (
            object_storage.evaluate(
                conn, project_id=project_id, tenant_connect=tenant_connect
            ).state
            == object_storage.EXCEEDED
        )
    with tenant_connect(names.database) as conn:
        assert _table_grants(conn) == before


def _table_grants(conn) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges "
            " WHERE table_schema = 'public' ORDER BY 1, 2, 3"
        )
        return set(cur.fetchall())


# -- what the measured figure is worth -------------------------------------


@requires_node
def test_a_customer_who_can_reach_service_role_can_under_report_held_bytes(tenant):
    """Measured, and pinned here so the admission stays a fact in the suite
    rather than a caveat in a comment — the treatment ADR-040 gave the
    equivalent hole in database storage.

    `metadata->>'size'` is the tenant's own record, and `service_role` holds
    `ALL` on `storage.objects` (upstream migration 0046) and carries
    `BYPASSRLS`. `services/control_plane/api/tenant_access.py` already records
    that the session user on an impersonating connection is the authenticator, a
    member of all three shared names, so a request can `SET ROLE service_role`
    in one line of its own SQL. ADR-039 puts that surface on **every tier**.

    So a customer can rewrite the column this quota is measured from, and the
    platform will believe them.

    **Unlike ADR-040's admission, re-measuring does not self-correct.** That one
    is a loop — a customer re-grants `INSERT`, the next pass revokes it again.
    This one re-reads the same forged column and gets the same answer forever.
    The thing that closes it is a measurement taken from the object store, which
    is not customer-writable and which no code can take until slice 3 has an
    endpoint to ask. Recorded in `docs/RESOURCE-GOVERNANCE.md` and carried to
    slice 3 in the plan.

    `anon` and `authenticated` cannot do this: they hold the same grants but not
    `BYPASSRLS`, so row-level security with no policy in place stops them. That
    half is asserted too, because it is the half that could regress quietly.
    """
    project_id, names, tenant_connect = tenant("oa000016")
    _fake_storage_objects(tenant_connect, names.database, [900 * MB])

    with db.connection() as conn:
        _set_plan_limit(conn, project_id, object_storage_bytes=500 * MB)
        assert (
            object_storage.evaluate(
                conn, project_id=project_id, tenant_connect=tenant_connect
            ).state
            == object_storage.EXCEEDED
        )

    # The forgery, as the platform's own impersonation surface would reach it.
    with tenant_connect(names.database) as conn:
        conn.execute("ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY")
        conn.execute("GRANT USAGE ON SCHEMA storage TO anon, authenticated, service_role")
        conn.execute(
            "GRANT ALL ON storage.objects TO anon, authenticated, service_role"
        )
        conn.execute("SET ROLE service_role")
        conn.execute("UPDATE storage.objects SET metadata = jsonb_set(metadata, '{size}', '0')")
        conn.execute("RESET ROLE")

    with db.connection() as conn:
        after = object_storage.evaluate(
            conn, project_id=project_id, tenant_connect=tenant_connect
        )
    assert after.used_bytes == 0
    assert after.state == object_storage.OK, (
        "if this now fails, something has closed the hole -- update the docs "
        "in docs/RESOURCE-GOVERNANCE.md and the plan rather than this assertion"
    )

    # And the half that must not regress: without BYPASSRLS, RLS with no policy
    # stops the same statement dead.
    with tenant_connect(names.database) as conn:
        conn.execute("UPDATE storage.objects SET metadata = jsonb_set(metadata, '{size}', '123')")
        conn.execute("SET ROLE authenticated")
        with conn.cursor() as cur:
            cur.execute("UPDATE storage.objects SET metadata = jsonb_set(metadata, '{size}', '0')")
            assert cur.rowcount == 0, "authenticated rewrote object metadata past RLS"
        conn.execute("RESET ROLE")

    with db.connection() as conn:
        assert (
            object_storage.evaluate(
                conn, project_id=project_id, tenant_connect=tenant_connect
            ).used_bytes
            == 123
        )


# -- the lesson Phase 09 opened with ---------------------------------------


def _set_plan_limit(conn, project_id: uuid.UUID, **limits) -> None:
    """Point the project at a plan whose `config_json` carries these limits."""
    code = f"oa-{uuid.uuid4().hex[:8]}"
    plan = db.one(
        conn,
        "INSERT INTO plans (code, name, config_json) VALUES (%s, 'Object test', %s) "
        "RETURNING id",
        (code, psycopg.types.json.Jsonb({"limits": limits})),
    )["id"]
    db.execute(conn, "UPDATE projects SET plan_id = %s WHERE id = %s", (plan, project_id))
    conn.commit()


@requires_node
def test_a_plan_change_reaches_the_held_bytes_ceiling_on_the_next_pass(tenant):
    """Phase 09's opening measurement, not repeated.

    An entitlement applied once at provisioning is one a plan change never
    reaches. `evaluate` re-reads the plan every time, so a project that upgrades
    stops being `exceeded` with nothing else done to it — no backfill, no
    re-provision, no operator.
    """
    project_id, names, tenant_connect = tenant("oa000007")
    _fake_storage_objects(tenant_connect, names.database, [900 * MB])

    with db.connection() as conn:
        _set_plan_limit(conn, project_id, object_storage_bytes=500 * MB)
        assert (
            object_storage.evaluate(
                conn, project_id=project_id, tenant_connect=tenant_connect
            ).state
            == object_storage.EXCEEDED
        )

        _set_plan_limit(conn, project_id, object_storage_bytes=10 * GB)
        after = object_storage.evaluate(
            conn, project_id=project_id, tenant_connect=tenant_connect
        )
        assert after.state == object_storage.OK

        row = db.one(
            conn, "SELECT object_exceeded_at FROM projects WHERE id = %s", (project_id,)
        )
        released = db.query(
            conn,
            "SELECT 1 FROM audit_events WHERE project_id = %s AND event_type = %s",
            (project_id, "object_storage.released"),
        )
    assert row["object_exceeded_at"] is None, "the exceeded marker outlived the condition"
    assert len(released) == 1


def test_a_plan_change_reaches_the_egress_ceiling_immediately(db_pool):  # noqa: ARG001
    """The same property for the other resource, and it reads the plan through
    a different function — so it is asserted separately rather than inferred.

    Immediately rather than on the next pass: egress is judged per request, so
    there is no pass to wait for. A project that upgrades mid-month gets the
    larger ceiling at once and keeps the bytes it has already served, which is
    the only arrangement that neither punishes an upgrade nor rewards a
    downgrade.
    """
    with db.connection() as conn:
        project_id = _bare_project(conn, "oa000008")
        _set_plan_limit(conn, project_id, egress_bytes_per_month=1 * GB)
        object_storage.record_egress(conn, project_id=project_id, bytes_served=2 * GB)
        conn.commit()
        assert (
            object_storage.egress_usage(conn, project_id=project_id).state
            == object_storage.EXCEEDED
        )

        _set_plan_limit(conn, project_id, egress_bytes_per_month=100 * GB)
        after = object_storage.egress_usage(conn, project_id=project_id)
        assert after.state == object_storage.OK
        assert after.used_bytes == 2 * GB, "an upgrade must not erase what was already served"


# -- counting bytes served -------------------------------------------------


def _bare_project(conn, ref: str) -> uuid.UUID:
    """A project row and nothing else. Egress accounting touches no node."""
    from services.control_plane import identity

    _, org = identity.create_user_with_personal_org(
        conn, email=f"{ref}@example.com", password="correct-horse-battery-staple"  # noqa: S106
    )
    plan = db.one(
        conn,
        "INSERT INTO plans (code, name) VALUES (%s, 'Egress test') "
        "ON CONFLICT (code) DO UPDATE SET name = 'Egress test' RETURNING id",
        (f"plan-{ref}",),
    )["id"]
    project_id = uuid.uuid4()
    db.execute(
        conn,
        "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
        "VALUES (%s, %s, %s, %s, %s, 'ACTIVE')",
        (project_id, org, ref, ref, plan),
    )
    conn.commit()
    return project_id


def test_egress_accumulates_rather_than_overwriting(db_pool):  # noqa: ARG001
    with db.connection() as conn:
        project_id = _bare_project(conn, "oa000009")
        assert object_storage.record_egress(conn, project_id=project_id, bytes_served=100) == 100
        assert object_storage.record_egress(conn, project_id=project_id, bytes_served=250) == 350
        conn.commit()
        assert object_storage.egress_used(conn, project_id=project_id) == 350


def test_a_batch_and_a_sequence_of_singles_agree(db_pool):  # noqa: ARG001
    """The signature takes a total because the caller is the gateway, on the
    path ADR-026 published a throughput number for: it accumulates in process
    and flushes. A flush of N must equal N flushes of one, or the buffering
    slice 4 adds would change the number."""
    with db.connection() as conn:
        batched = _bare_project(conn, "oa000010")
        singly = _bare_project(conn, "oa000011")
        object_storage.record_egress(conn, project_id=batched, bytes_served=6_000)
        for _ in range(6):
            object_storage.record_egress(conn, project_id=singly, bytes_served=1_000)
        conn.commit()
        assert object_storage.egress_used(conn, project_id=batched) == (
            object_storage.egress_used(conn, project_id=singly)
        )


def test_a_non_positive_amount_cannot_give_a_project_free_egress(db_pool):  # noqa: ARG001
    """Egress does not un-happen. The `bytes >= 0` constraint on the table
    exists so a subtraction bug could not hand a project unlimited egress for
    the rest of the month, and this is the layer above it."""
    with db.connection() as conn:
        project_id = _bare_project(conn, "oa000012")
        object_storage.record_egress(conn, project_id=project_id, bytes_served=500)
        assert object_storage.record_egress(conn, project_id=project_id, bytes_served=-400) == 500
        assert object_storage.record_egress(conn, project_id=project_id, bytes_served=0) == 500
        conn.commit()
        assert object_storage.egress_used(conn, project_id=project_id) == 500


def test_the_period_is_the_utc_calendar_month():
    assert object_storage.period_start(
        dt.datetime(2026, 8, 24, 17, 30, tzinfo=dt.UTC)
    ) == dt.date(2026, 8, 1)
    # The last instant of a month and the first of the next are different
    # periods, and the boundary is UTC's rather than the server's.
    assert object_storage.period_start(
        dt.datetime(2026, 8, 31, 23, 59, 59, tzinfo=dt.UTC)
    ) == dt.date(2026, 8, 1)
    assert object_storage.period_start(
        dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.UTC)
    ) == dt.date(2026, 9, 1)


def test_a_naive_timestamp_is_read_as_utc_rather_than_refused():
    """A ValueError here would turn a mis-typed timestamp into a refused
    download. Every caller in this repository passes an aware datetime or
    nothing; this is about the one that does not."""
    assert object_storage.period_start(dt.datetime(2026, 8, 24, 17, 30)) == dt.date(2026, 8, 1)


def test_a_new_month_starts_from_zero_and_the_old_one_is_still_readable(db_pool):  # noqa: ARG001
    """What a row per period buys over a counter that resets in place: an
    answer to "what did this project serve last month", and no reset job that
    can fail to run."""
    august = dt.datetime(2026, 8, 15, tzinfo=dt.UTC)
    september = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
    with db.connection() as conn:
        project_id = _bare_project(conn, "oa000013")
        object_storage.record_egress(
            conn, project_id=project_id, bytes_served=4 * GB, moment=august
        )
        object_storage.record_egress(
            conn, project_id=project_id, bytes_served=100, moment=september
        )
        conn.commit()

        assert object_storage.egress_used(conn, project_id=project_id, moment=september) == 100
        assert object_storage.egress_used(conn, project_id=project_id, moment=august) == 4 * GB

        history = object_storage.egress_history(conn, project_id=project_id)
    assert [row["period_start"] for row in history] == [dt.date(2026, 9, 1), dt.date(2026, 8, 1)]


def test_a_project_that_has_served_nothing_reports_zero_not_unknown(db_pool):  # noqa: ARG001
    """Egress is counted, not measured, so an absent row means none rather than
    "nobody has looked" — the opposite of the storage figures, which spend a
    null on exactly that."""
    with db.connection() as conn:
        project_id = _bare_project(conn, "oa000014")
        assert object_storage.egress_used(conn, project_id=project_id) == 0
        assert object_storage.egress_usage(conn, project_id=project_id).state == object_storage.OK


def test_egress_rows_go_when_the_project_does(db_pool):  # noqa: ARG001
    """`ON DELETE CASCADE`. A counter that outlived its project would be a row
    nothing can ever read and nothing will ever clean up."""
    with db.connection() as conn:
        project_id = _bare_project(conn, "oa000015")
        object_storage.record_egress(conn, project_id=project_id, bytes_served=10)
        conn.commit()
        db.execute(conn, "DELETE FROM projects WHERE id = %s", (project_id,))
        conn.commit()
        left = db.query(
            conn, "SELECT 1 FROM project_egress WHERE project_id = %s", (project_id,)
        )
    assert left == []
