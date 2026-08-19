"""Running a customer's SQL on their behalf (ADR-039), and what it cannot do.

Phase 08 slice 1. This is the first route that takes a customer's own text and
executes it against a database, so almost everything here is written as an
attack rather than as a happy path. The three that matter most:

- `RESET ROLE;` in the submitted SQL. It is reachable and it is *not* blocked,
  because the admin role is the customer's intended ceiling inside their own
  database. What is asserted is that it reaches no more than that.
- `SET statement_timeout = 0`. ADR-017 established that a session can do this,
  so the ceiling is a wall-clock cancel from outside the session rather than a
  GUC, and the test proves the GUC is not what is holding.
- A row cap the submitted text cannot raise by ending in its own `LIMIT`.
- A row cap that is not a memory cap. Added 2026-08-19 by the review this
  slice never got: a hundred rows of a megabyte each is inside every declared
  limit and is a hundred megabytes of *shared* control-plane memory.

The negatives on the executor role itself are tests K to N in
`specs/tenant-role-model.md`, which gate this slice's merge.
"""

from __future__ import annotations

import functools

import psycopg
import pytest

from services.control_plane import db, provisioning, sql_console, storage
from tests.conftest import requires_db
from tests.test_provisioning import ADMIN_DSN, _provision, _tenant_dsn

# Generous on purpose: these tests are about the other ceilings.
MB = 16 * 1024 * 1024

pytestmark = requires_db
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret


# -- the pure parts, which need no node ------------------------------------


def test_a_dsn_is_built_from_parts_and_escapes_what_it_interpolates():
    """`tests/test_provisioning.py` records why this is not string substitution:
    replacing into the admin DSN left the admin password in place, and the
    authentication failure that produced looked like a lockdown working."""
    dsn = sql_console.executor_dsn(
        host="10.0.0.4", port=5433, database="mldb_abc",
        role="mldb_abc_executor",
        password="p@ss word/with?specials",  # noqa: S106 - the point is the escaping
    )
    assert dsn.startswith("postgresql://mldb_abc_executor:")
    assert "@10.0.0.4:5433/mldb_abc" in dsn
    # The password's own separators must not become DSN structure.
    assert "p%40ss%20word%2Fwith%3Fspecials" in dsn


def test_a_zero_timeout_is_refused_rather_than_treated_as_unlimited():
    """Belt and braces with `entitlements._positive_int_from`. Zero reaching
    here would mean no ceiling at all, on a connection the platform holds."""
    with pytest.raises(ValueError, match="no ceiling"):
        sql_console.execute("postgresql://unused", "SELECT 1", run_as="r", row_limit=1, max_bytes=1024, timeout_ms=0)


def test_a_zero_byte_budget_is_refused_for_the_same_reason_a_zero_timeout_is():
    with pytest.raises(ValueError, match="no ceiling"):
        sql_console.execute(
            "postgresql://unused", "SELECT 1", run_as="r", row_limit=1,
            max_bytes=0, timeout_ms=1_000,
        )


def test_the_size_estimate_measures_text_and_does_not_recurse_forever():
    """Two properties, because the estimate is charged against tenant data.

    Text is what makes a result large, so it is counted exactly. And a jsonb
    value nests as deeply as whoever wrote it liked -- a recursive estimate
    with no floor is a `RecursionError` a customer can post into the control
    plane, which is the shape of bug this whole change exists to close.
    """
    assert sql_console.approx_bytes("x" * 1000) == 1000
    assert sql_console.approx_bytes(b"x" * 1000) == 1000
    assert sql_console.approx_bytes(None) == sql_console.SCALAR_BYTES

    nested: object = "leaf"
    for _ in range(5_000):
        nested = {"k": nested}
    assert sql_console.approx_bytes(nested) > 0


def test_the_budget_refuses_a_value_rather_than_returning_half_of_it():
    budget = sql_console.Budget(100)
    assert budget.spend(60) is True
    assert budget.spend(60) is False
    assert budget.exhausted is True
    # And the refusal did not spend it: what fits afterwards still fits.
    assert budget.spend(40) is True


def test_the_audit_detail_keeps_the_statement_and_never_the_rows():
    """The statement is the customer's own SQL and is what makes the trail
    answer 'who changed this schema'. Result rows are tenant data and an audit
    table is the wrong place for a copy of them."""
    results = [sql_console.Result(columns=["secret"], rows=[{"secret": "tenant-data"}], row_count=1)]
    detail = sql_console.audit_detail("SELECT secret FROM t", results=results)
    assert detail["statement"] == "SELECT secret FROM t"
    assert "tenant-data" not in repr(detail)


def test_a_long_statement_is_truncated_and_says_so():
    """An unbounded write of caller-supplied text into an operator-read table is
    a denial of service on the people reading it."""
    detail = sql_console.audit_detail("x" * 5_000, results=[], limit=100)
    assert len(detail["statement"]) == 100
    assert detail["statement_truncated"] is True


# -- the route's gates -----------------------------------------------------


def _account(client, email: str) -> tuple[str, str]:
    created = client.post("/v1/auth/signup", json={"email": email, "password": TEST_CREDENTIAL})
    assert created.status_code == 201, created.text
    token = client.post(
        "/v1/auth/signin", json={"email": email, "password": TEST_CREDENTIAL}
    ).json()["token"]
    return token, created.json()["organizations"][0]["org_id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_a_project_you_do_not_belong_to_is_not_found_rather_than_forbidden(client, db_pool):  # noqa: ARG001
    """The same answer, body included, as a project that does not exist. A ref
    is the customer's API subdomain (ADR-008), so confirming one is real
    confirms a target."""
    token, _ = _account(client, "outsider@example.com")
    answered = client.post(
        "/v1/projects/somebodyelse/sql", json={"statement": "SELECT 1"}, headers=_auth(token)
    )
    assert answered.status_code == 404
    assert answered.json()["detail"] == "project not found"


def test_an_unauthenticated_caller_never_reaches_the_database(client, db_pool):  # noqa: ARG001
    assert client.post("/v1/projects/anyref/sql", json={"statement": "SELECT 1"}).status_code == 401


# -- the executor role: specs/tenant-role-model.md tests K to N ------------


@pytest.fixture
def console_project(admin_conn, key_ring, project_factory):
    """A provisioned tenant with the ADR-039 executor role."""

    def make(ref: str):
        project_id = project_factory(ref)
        with db.connection() as conn:
            node = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, node_pool, status) "
                "VALUES ('sc-node','sc.example','sc.internal','shared','active') "
                "ON CONFLICT (name) DO UPDATE SET status='active' RETURNING id",
            )["id"]
            db.execute(conn, "UPDATE projects SET node_id = %s WHERE id = %s", (node, project_id))
            conn.commit()
        names, _ = _provision(project_id, admin_conn, key_ring, ref)
        password = provisioning.generate_password()
        provisioning.create_executor_role(admin_conn, names, password=password)
        provisioning.grant_executor_connect(admin_conn, names)
        admin_conn.commit()
        return names, password

    return make


@requires_node
def test_K_the_executor_is_a_member_of_the_admin_role_and_nothing_else(console_project, admin_conn):
    """Its single membership is the whole of its privilege. A second one is the
    failure this role exists to make visible, and would not be noticed by any
    test that only checked what the console can do."""
    names, _ = console_project("exeka001")
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "  JOIN pg_roles r ON r.oid = m.roleid "
            "  JOIN pg_roles e ON e.oid = m.member "
            " WHERE e.rolname = %s",
            (names.executor,),
        )
        assert {row["rolname"] for row in cur.fetchall()} == {names.admin}


@requires_node
def test_L_the_executor_is_never_granted_to_a_shared_role(console_project, admin_conn):
    """ADR-016's exception is one-directional. Granting this role *to* `anon`
    would make every tenant's `anon` on the node a member of one project's
    executor, which is a cross-tenant escalation rather than an untidy grant."""
    names, _ = console_project("exekb001")
    with admin_conn.cursor() as cur:
        for shared in provisioning.SHARED_ROLES:
            cur.execute(
                "SELECT pg_has_role(%s, %s, 'member') AS is_member", (shared, names.executor)
            )
            assert cur.fetchone()["is_member"] is False, f"{shared} is a member of {names.executor}"


@requires_node
def test_M_the_executor_cannot_reach_another_tenants_database(console_project, admin_conn):  # noqa: ARG001
    """The ADR-014 lockdown, re-asserted for a role that did not exist when it
    was written."""
    first, password = console_project("exekc001")
    second, _ = console_project("exekd001")
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        psycopg.connect(
            _tenant_dsn(second.database, first.executor, password), connect_timeout=5
        ).close()


@requires_node
def test_N_the_executor_holds_no_escalating_attribute(console_project, admin_conn):
    names, _ = console_project("exeke001")
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication, rolinherit "
            "  FROM pg_roles WHERE rolname = %s",
            (names.executor,),
        )
        row = cur.fetchone()
    assert not any(
        row[attr] for attr in
        ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls", "rolreplication")
    ), row
    # NOINHERIT: the admin role's privileges are reached by an explicit SET ROLE
    # rather than held ambiently, so a session that has not asked has not got.
    assert row["rolinherit"] is False


# -- what the submitted statement cannot do -------------------------------


@requires_node
def test_reset_role_reaches_the_admin_role_and_no_further(console_project):
    """Deliberately not asserted as 'the reset is blocked'. It is not blocked,
    and writing the test that way would encode a guarantee the design does not
    make. The admin role is the ceiling either way; what matters is that the
    session cannot end up anywhere above it."""
    names, password = console_project("exerr001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    results = sql_console.execute(
        dsn,
        "RESET ROLE; SELECT current_user AS who, "
        "  pg_has_role(current_user, 'pg_read_all_data', 'member') AS reads_everything;",
        run_as=names.admin, row_limit=10, max_bytes=MB, timeout_ms=5_000,
    )
    row = [r for r in results if r.rows][-1].rows[0]
    assert row["who"] == names.executor
    assert row["reads_everything"] is False


@requires_node
def test_a_statement_that_disables_its_own_timeout_is_still_cancelled(console_project):
    """ADR-017's finding, turned into the reason the ceiling lives outside the
    session. `statement_timeout` is `context = user`; the cancel is not."""
    names, password = console_project("exect001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    with pytest.raises(sql_console.ConsoleError, match="cancelled"):
        sql_console.execute(
            dsn, "SET statement_timeout = 0; SELECT pg_sleep(30);",
            run_as=names.admin, row_limit=10, max_bytes=MB, timeout_ms=1_500,
        )


@requires_node
def test_the_row_cap_cannot_be_raised_by_the_submitted_text(console_project):
    """Applied while fetching rather than by appending `LIMIT`, which the text
    defeats by ending in its own -- or in a comment, or in a second statement."""
    names, password = console_project("exerl001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    results = sql_console.execute(
        dsn, "SELECT g FROM generate_series(1, 500) g LIMIT 400",
        run_as=names.admin, row_limit=5, max_bytes=MB, timeout_ms=10_000,
    )
    assert len(results[-1].rows) == 5
    assert results[-1].truncated is True


@requires_node
def test_the_row_cap_is_not_a_memory_cap_and_the_byte_budget_is(console_project):
    """The finding, as the measurement that produced it.

    A hundred rows of a megabyte each is *exactly* the free tier's row limit,
    reports itself untruncated, and was measured at ~200 MB of resident memory
    in the control plane for a 100 MB response body. Every declared limit was
    respected. Asserted here as bytes returned rather than as RSS, because RSS
    is the allocator's business and the ceiling is ours.
    """
    names, password = console_project("exemb001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    statement = "SELECT repeat('x', 1000000) AS c FROM generate_series(1, 100)"

    results = sql_console.execute(
        dsn, statement, run_as=names.admin, row_limit=100,
        max_bytes=4 * 1024 * 1024, timeout_ms=20_000,
    )
    returned = sum(len(row["c"]) for row in results[-1].rows)
    assert returned <= 4 * 1024 * 1024
    # Fewer than the row cap allowed, and said so rather than looking complete.
    assert len(results[-1].rows) < 100
    assert results[-1].truncated is True


@requires_node
def test_the_byte_budget_is_spent_across_a_whole_response_not_per_statement(console_project):
    """Otherwise a statement returning ten result sets costs ten times the
    ceiling, which is the same bug with an extra semicolon in front of it."""
    names, password = console_project("exemb002")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    results = sql_console.execute(
        dsn,
        "SELECT repeat('a', 100000) AS c FROM generate_series(1, 10);"
        "SELECT repeat('b', 100000) AS c FROM generate_series(1, 10);",
        run_as=names.admin, row_limit=100,
        max_bytes=1_200_000, timeout_ms=20_000,
    )
    with_rows = [r for r in results if r.columns]
    assert len(with_rows) == 2
    returned = sum(len(row["c"]) for result in with_rows for row in result.rows)
    assert returned <= 1_200_000
    # The first statement spent most of the budget; the second is what notices.
    assert with_rows[-1].truncated is True


@requires_node
def test_a_single_value_larger_than_the_budget_is_dropped_rather_than_cut(console_project):
    """A half-returned value would be a corruption dressed as a limit: the
    client cannot tell a truncated string from a short one."""
    names, password = console_project("exemb003")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    results = sql_console.execute(
        dsn, "SELECT repeat('x', 2000000) AS c",
        run_as=names.admin, row_limit=100, max_bytes=1024, timeout_ms=20_000,
    )
    assert results[-1].rows == []
    assert results[-1].truncated is True


@requires_node
def test_a_storage_restricted_project_is_refused_writes_but_can_still_shrink(console_project):
    """ADR-040, and the reason it is grants rather than a read-only session.

    The first version of this slice held a restricted project by putting the
    session in a read-only transaction. A probe on 2026-08-17 showed the
    submitted text walks out of that -- `SET default_transaction_read_only =
    off` is accepted inside a read-only session and the next statement writes --
    so the control moved to where Phase 05 already applies one.

    `DELETE` surviving is the point rather than an accident: a project that
    cannot shrink cannot recover, and on free there is no other way in.
    """
    names, password = console_project("exesr001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    run = functools.partial(
        sql_console.execute, dsn, run_as=names.admin, row_limit=10, max_bytes=MB,
        timeout_ms=10_000,
    )
    run("CREATE TABLE bulky (id int); INSERT INTO bulky VALUES (1), (2);")

    with psycopg.connect(_tenant_dsn(names.database, names.executor, password)) as tenant:
        tenant.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(names.admin)))
        storage.restrict(tenant)

    with pytest.raises(sql_console.ConsoleError, match="42501"):
        run("INSERT INTO bulky VALUES (3)")
    with pytest.raises(sql_console.ConsoleError, match="42501"):
        run("UPDATE bulky SET id = 9")

    # Reads and shrinks are untouched, which is what makes the restriction
    # recoverable rather than terminal.
    assert run("SELECT count(*) AS n FROM bulky")[-1].rows == [{"n": 2}]
    run("DELETE FROM bulky WHERE id = 1")
    assert run("SELECT count(*) AS n FROM bulky")[-1].rows == [{"n": 1}]


@requires_node
def test_the_restriction_is_a_default_that_a_table_owner_can_re_grant_around(console_project):
    """The limitation, asserted rather than left as a caveat in a comment.

    A role that owns a table holds GRANT OPTION on it implicitly, so the
    customer whose INSERT was revoked can grant it back. ADR-040 says so and
    this is the test that keeps that admission true -- if a future change made
    the restriction genuinely enforcing, this test fails and the ADR needs
    rewriting rather than the test deleting.
    """
    names, password = console_project("exerg001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    run = functools.partial(
        sql_console.execute, dsn, run_as=names.admin, row_limit=10, max_bytes=MB,
        timeout_ms=10_000,
    )
    run("CREATE TABLE owned (id int)")
    with psycopg.connect(_tenant_dsn(names.database, names.executor, password)) as tenant:
        tenant.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(names.admin)))
        storage.restrict(tenant)

    with pytest.raises(sql_console.ConsoleError, match="42501"):
        run("INSERT INTO owned VALUES (1)")

    run("GRANT INSERT ON owned TO CURRENT_USER; INSERT INTO owned VALUES (1);")
    assert run("SELECT count(*) AS n FROM owned")[-1].rows == [{"n": 1}]


@requires_node
def test_ddl_through_the_console_tells_postgrest_to_reload(console_project):
    """Bootstrap 006 installs the event triggers and names 'the dashboard SQL
    editor' among the callers it was written for, in Phase 00. Nothing had ever
    exercised that path from a route, and a table a customer creates being
    invisible to their own Data API is a platform fault rather than theirs."""
    names, password = console_project("exedd001")
    dsn = sql_console.executor_dsn(
        host=_host(), port=_port(), database=names.database,
        role=names.executor, password=password,
    )
    listener = psycopg.connect(_tenant_dsn(names.database, names.executor, password))
    try:
        listener.autocommit = True
        listener.execute("LISTEN pgrst")
        sql_console.execute(
            dsn, "CREATE TABLE reload_me (id int)",
            run_as=names.admin, row_limit=10, max_bytes=MB, timeout_ms=10_000,
        )
        notifications = []
        generator = listener.notifies(timeout=10, stop_after=1)
        notifications.extend(generator)
        assert notifications and notifications[0].payload == "reload schema"
    finally:
        listener.close()


def _host() -> str:
    from urllib.parse import urlsplit

    return urlsplit(ADMIN_DSN).hostname or "127.0.0.1"


def _port() -> int:
    from urllib.parse import urlsplit

    return urlsplit(ADMIN_DSN).port or 5432
