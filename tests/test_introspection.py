"""Reading a tenant's catalogue (Phase 08 slice 2), and what it must not read.

Slice 1's tests are written as attacks because the customer supplies the SQL.
Here they supply none, so the attacks that remain are the ones the *platform*
can commit on their behalf -- disclosing another tenant, disclosing the
platform's own internals, or writing on a path that has no business writing:

- `pg_roles` is cluster-scoped. The test that matters most here provisions two
  tenants and asserts that reading one's catalogue never names the other's
  roles, whose names are the other customer's API subdomain (ADR-008).
- `maludb_platform` is the platform's bookkeeping and is hidden.
- A managed function's body is not returned, which is the line
  `sql_console.first_line` already draws for error text.
- The snapshot's transaction really is read-only, proved by making it try to
  write rather than by reading the code that says it is.
"""

from __future__ import annotations

import functools

import pytest

from services.control_plane import db, introspection, sql_console
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_provisioning import ADMIN_DSN

# Generous on purpose: these tests are about the other ceilings.
MB = 16 * 1024 * 1024

pytestmark = requires_db
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")


# -- the pure parts, which need no node ------------------------------------


def test_the_role_allowlist_names_this_project_and_the_shared_three_only():
    """The cross-tenant control, asserted where it is decided.

    Everything downstream of this list is a `WHERE rolname = ANY(...)`, so if
    the list is right the query cannot disclose another tenant however
    `pg_roles` grows.
    """
    allowed = introspection.role_allowlist("abcd0001")
    assert set(allowed) == {
        "anon", "authenticated", "service_role",
        "mldb_abcd0001_admin", "mldb_abcd0001_authenticator", "mldb_abcd0001_auth",
        "mldb_abcd0001_executor", "mldb_abcd0001_replicator",
    }
    assert not any("efgh0002" in name for name in allowed)


def test_a_crafted_ref_cannot_widen_the_allowlist():
    """`TenantNames.for_ref` validates before deriving, so this raises rather
    than returning a list with a role name of the caller's choosing in it."""
    with pytest.raises(ValueError):
        introspection.role_allowlist("abcd0001'; DROP ROLE anon --")


def test_the_platform_schema_is_hidden_and_auth_is_not():
    """`auth` is visible on purpose: a customer writing an RLS policy needs to
    see `auth.users`, and what protects it is grants rather than concealment."""
    assert "maludb_platform" in introspection.HIDDEN_SCHEMAS
    assert "auth" not in introspection.HIDDEN_SCHEMAS


def test_every_catalogue_query_is_a_select():
    """The read-only claim, checked structurally as well as at runtime.

    A future edit that adds a `CREATE TEMP TABLE` to speed something up fails
    here as well as on the node, which matters because the node test needs a
    node and this one runs everywhere.
    """
    queries = [
        value for name, value in vars(introspection).items()
        if name.isupper() and isinstance(value, str) and "SELECT" in value
    ]
    assert len(queries) >= 9, "the query constants moved; this test is not finding them"
    for query in queries:
        first = next(line for line in query.splitlines() if line.strip())
        assert first.strip().startswith("SELECT"), query


def test_a_zero_timeout_is_refused_rather_than_treated_as_unlimited():
    with pytest.raises(ValueError, match="no ceiling"):
        introspection.snapshot(
            "postgresql://unused", run_as="r", project_ref="abcd0001", timeout_ms=0, max_bytes=MB
        )


# -- the route's gates -----------------------------------------------------


def test_a_project_you_do_not_belong_to_is_not_found_rather_than_forbidden(client, db_pool):  # noqa: ARG001
    created = client.post(
        "/v1/auth/signup", json={"email": "schema-outsider@example.com", "password": TEST_CREDENTIAL}
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/v1/auth/signin",
        json={"email": "schema-outsider@example.com", "password": TEST_CREDENTIAL},
    ).json()["token"]
    answered = client.get(
        "/v1/projects/somebodyelse/database/schema", headers={"Authorization": f"Bearer {token}"}
    )
    assert answered.status_code == 404
    assert answered.json()["detail"] == "project not found"


def test_an_unauthenticated_caller_never_reaches_the_database(client, db_pool):  # noqa: ARG001
    assert client.get("/v1/projects/anyref/database/schema").status_code == 401


# -- against a real tenant -------------------------------------------------


def _run(dsn: str, names, statement: str):
    return sql_console.execute(
        dsn, statement, run_as=names.admin, row_limit=100, max_bytes=MB, timeout_ms=10_000
    )


def _snapshot(dsn: str, names, ref: str, **kwargs) -> introspection.Snapshot:
    return introspection.snapshot(
        dsn, run_as=names.admin, project_ref=ref, timeout_ms=15_000,
        max_bytes=kwargs.pop("max_bytes", MB), **kwargs
    )


@requires_node
def test_a_customers_own_table_arrives_with_its_columns_and_constraints(tenant):
    """The happy path, and the shape a dashboard renders from."""
    ref = "intra001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, """
        CREATE TABLE public.items (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            owner text NOT NULL,
            note text DEFAULT 'none',
            CONSTRAINT owner_not_blank CHECK (owner <> '')
        );
        CREATE INDEX items_owner_idx ON public.items (owner);
        COMMENT ON TABLE public.items IS 'a customer table';
    """)

    snapshot = _snapshot(dsn, names, ref)
    items = next(t for t in snapshot.tables if t["name"] == "items")

    assert items["schema"] == "public"
    assert items["kind"] == "table"
    assert items["comment"] == "a customer table"
    # Created through the console, so the admin role owns it and it is the
    # customer's to alter. `auth.users` below is the other case.
    assert items["managed"] is False
    assert items["rls_enabled"] is False

    columns = {c["name"]: c for c in items["columns"]}
    assert list(columns) == ["id", "owner", "note"]
    assert columns["id"]["is_identity"] is True
    assert columns["owner"]["is_nullable"] is False
    assert columns["note"]["default_expression"] == "'none'::text"

    kinds = {c["kind"] for c in items["constraints"]}
    assert {"primary_key", "check"} <= kinds
    assert any(i["name"] == "items_owner_idx" and not i["is_primary"] for i in items["indexes"])
    assert any(i["is_primary"] for i in items["indexes"])


@requires_node
def test_an_rls_policy_arrives_readable_rather_than_as_catalogue_codes(tenant):
    """`polcmd = 'r'` means SELECT. A frontend that had to know that is
    reimplementing `information_schema` in TypeScript, which is what this
    endpoint exists to prevent."""
    ref = "intrb001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, """
        CREATE TABLE public.notes (id int, owner text);
        ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY;
        CREATE POLICY own_rows ON public.notes FOR SELECT TO authenticated
            USING (owner = auth.uid()::text);
    """)

    snapshot = _snapshot(dsn, names, ref)
    notes = next(t for t in snapshot.tables if t["name"] == "notes")
    assert notes["rls_enabled"] is True

    policy = notes["policies"][0]
    assert policy["name"] == "own_rows"
    assert policy["command"] == "select"
    assert policy["roles"] == ["authenticated"]
    assert "auth.uid()" in policy["using_expression"]


@requires_node
def test_the_snapshot_never_names_another_tenants_roles(tenant):
    """The disclosure this module's filtering exists for.

    `pg_roles` is cluster-scoped: the ADR-014 `CONNECT` lockdown stops a session
    reaching another tenant's *database* and does nothing about seeing its role
    rows from inside its own. A ref is the customer's API subdomain, so a role
    list passed through from the catalogue names every other customer on the
    node.
    """
    ref, other_ref = "intrc001", "intrd001"
    _, names, dsn = tenant(ref)
    _, other, _ = tenant(other_ref)

    snapshot = _snapshot(dsn, names, ref)
    listed = {role["name"] for role in snapshot.roles}

    # The other tenant's roles exist on this cluster right now, and are visible
    # in `pg_roles` from inside this tenant's own database.
    assert other.admin not in listed
    assert not any(other_ref in name for name in listed)

    # And the project's own, plus the three an RLS policy can target.
    assert {"anon", "authenticated", "service_role", names.admin} <= listed
    assert {r["name"] for r in snapshot.roles if r["is_shared"]} == {
        "anon", "authenticated", "service_role"
    }


@requires_node
def test_platform_bookkeeping_is_not_part_of_a_customers_schema(tenant):
    """`maludb_platform` holds bootstrap state and the extension-hardening
    functions. It is `REVOKE ALL ... FROM PUBLIC` in the database and absent
    from the answer here, which are two different controls and both wanted."""
    ref = "intre001"
    _, names, dsn = tenant(ref)

    snapshot = _snapshot(dsn, names, ref)
    listed = {schema["name"] for schema in snapshot.schemas}

    assert "public" in listed
    assert "auth" in listed, "hiding auth would make a migrated policy unexplainable"
    assert "maludb_platform" not in listed
    assert not any(name.startswith("pg_") for name in listed)
    assert "information_schema" not in listed
    assert not any(t["schema"] == "maludb_platform" for t in snapshot.tables)
    assert not any(f["schema"] == "maludb_platform" for f in snapshot.functions)


@requires_node
def test_a_managed_functions_body_is_withheld_and_the_customers_is_not(tenant):
    """The same line `sql_console.first_line` draws when it strips CONTEXT out
    of an error: a customer reads their own code, not the platform's."""
    ref = "intrf001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, "CREATE FUNCTION public.mine() RETURNS int LANGUAGE sql AS $$ SELECT 42 $$")

    snapshot = _snapshot(dsn, names, ref)
    by_name = {f"{f['schema']}.{f['name']}": f for f in snapshot.functions}

    mine = by_name["public.mine"]
    assert mine["managed"] is False
    assert "42" in mine["source"]
    assert mine["kind"] == "function"

    # auth.uid() is bootstrap 002's, owned by the platform rather than by the
    # customer's admin role.
    theirs = by_name["auth.uid"]
    assert theirs["managed"] is True
    assert theirs["source"] is None
    assert theirs["language"] == "sql"


@requires_node
def test_a_procedure_and_an_aggregate_do_not_break_the_response(tenant, client):
    """`pg_get_function_result` is null for a procedure, and a response model
    that required a string would answer 500 for any migrated schema containing
    one. Found by measuring the catalogue rather than by reading its docs."""
    ref = "intrn001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, """
        CREATE PROCEDURE public.noop() LANGUAGE sql AS $$ SELECT 1 $$;
        CREATE FUNCTION public.add_ints(int, int) RETURNS int LANGUAGE sql IMMUTABLE
            AS $$ SELECT $1 + $2 $$;
        CREATE AGGREGATE public.sum_ints(int) (sfunc = public.add_ints, stype = int);
    """)

    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]
    answered = client.get(
        f"/v1/projects/{ref}/database/schema", headers={"Authorization": f"Bearer {token}"}
    )
    assert answered.status_code == 200, answered.text

    kinds = {f["name"]: f for f in answered.json()["functions"]}
    assert kinds["noop"]["kind"] == "procedure"
    assert kinds["noop"]["returns"] is None
    assert kinds["sum_ints"]["kind"] == "aggregate"


@requires_node
def test_the_bootstrap_extensions_are_reported_and_the_available_ones_are_not(tenant):
    """What is installed is a fact about the customer's database. What *could*
    be installed is a node-wide list advertising a capability no customer has
    -- `CREATE EXTENSION` is refused for every tier today (negative test H) --
    and slice 4 is where that gets decided rather than implied."""
    ref = "intrg001"
    _, names, dsn = tenant(ref)

    snapshot = _snapshot(dsn, names, ref)
    installed = {e["name"] for e in snapshot.extensions}

    assert "pgcrypto" in installed
    assert all(set(e) == {"name", "schema", "installed_version"} for e in snapshot.extensions)


@requires_node
def test_the_snapshot_transaction_refuses_a_write(tenant, monkeypatch):
    """Proved by making it try, rather than by reading the code that says so.

    ADR-040 established that a read-only transaction is not a control against
    *submitted* SQL. It is a real one here, because nothing on this path can
    issue the `SET` that escapes it -- and this test is what keeps that
    difference honest if the queries ever stop being constants.
    """
    ref = "intrh001"
    _, names, dsn = tenant(ref)

    def write_instead(conn, **_kwargs):
        conn.execute("CREATE TABLE public.should_not_exist (id int)")
        return introspection.Snapshot()

    monkeypatch.setattr(introspection, "_read", write_instead)
    with pytest.raises(sql_console.ConsoleError, match="25006"):
        _snapshot(dsn, names, ref)

    # And the table really is absent, not merely un-committed.
    monkeypatch.undo()
    assert not any(t["name"] == "should_not_exist" for t in _snapshot(dsn, names, ref).tables)


@requires_node
def test_a_schema_filter_narrows_the_snapshot_and_is_never_composed(tenant):
    """The one caller-supplied value on this path. Bound as a parameter, so the
    worst a crafted filter can do is match no schema."""
    ref = "intri001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, "CREATE TABLE public.only_here (id int)")

    narrowed = _snapshot(dsn, names, ref, schemas=["public"])
    assert {s["name"] for s in narrowed.schemas} == {"public"}
    assert all(t["schema"] == "public" for t in narrowed.tables)

    hostile = _snapshot(dsn, names, ref, schemas=["public'; DROP TABLE only_here; --"])
    assert hostile.schemas == []
    assert hostile.tables == []
    assert any(t["name"] == "only_here" for t in _snapshot(dsn, names, ref).tables)


@requires_node
def test_a_capped_catalogue_says_which_one_was_capped(tenant, monkeypatch):
    """A truncation a dashboard cannot see is a partial list presented as a
    complete one."""
    ref = "intrj001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, "CREATE TABLE a (id int); CREATE TABLE b (id int); CREATE TABLE c (id int);")

    monkeypatch.setattr(introspection, "CATALOG_ROW_CAP", 2)
    snapshot = _snapshot(dsn, names, ref)

    assert "tables" in snapshot.truncated
    assert len(snapshot.tables) == 2


@requires_node
def test_a_row_cap_does_not_bound_a_function_body_and_the_byte_budget_does(tenant):
    """The row caps here were always the wrong axis for one catalogue.

    A function body is customer-authored text of no fixed size, and
    `CATALOG_ROW_CAP` of them is a row count this module considers reasonable
    and a response size it does not. Added 2026-08-19 with the same finding's
    fix in the console.
    """
    ref = "intrm001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, """
        CREATE FUNCTION public.bulky() RETURNS int LANGUAGE sql AS
            $$ SELECT 1 /* """ + "p" * 200_000 + """ */ $$;
    """)

    # Comfortably above the body, so the row cap is the only one in play.
    whole = _snapshot(dsn, names, ref, max_bytes=4 * 1024 * 1024)
    assert any(f["name"] == "bulky" for f in whole.functions)
    assert whole.truncated == []

    # Below it. The catalogue is named rather than silently short.
    bounded = _snapshot(dsn, names, ref, max_bytes=50_000)
    assert bounded.truncated != []
    assert not any(f["name"] == "bulky" for f in bounded.functions)


@requires_node
def test_the_route_answers_a_member_with_their_own_schema(client, tenant):
    """End to end: the gates, the executor credential out of the key ring, the
    tenant connection, and the response model."""
    ref = "intrk001"
    _, names, dsn = tenant(ref)
    _run(dsn, names, "CREATE TABLE public.widgets (id int PRIMARY KEY)")

    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]
    answered = client.get(
        f"/v1/projects/{ref}/database/schema", headers={"Authorization": f"Bearer {token}"}
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()

    widgets = next(t for t in body["tables"] if t["name"] == "widgets")
    # `schema_name` on the wire, because `schema` shadows a pydantic method.
    assert widgets["schema_name"] == "public"
    assert widgets["managed"] is False
    assert body["truncated"] == []
    assert {r["name"] for r in body["roles"]} >= {"anon", "authenticated", "service_role"}
    assert not any("maludb_platform" == s["name"] for s in body["schemas"])


@requires_node
def test_the_route_does_not_audit_a_read(client, tenant):
    """Slice 1 records every statement because a statement changes something. A
    dashboard page load does not, and recording those would bury the trail that
    answers 'who changed this schema' under the noise of people looking at it."""
    ref = "intrl001"
    project_id, _, _ = tenant(ref)

    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]
    assert client.get(
        f"/v1/projects/{ref}/database/schema", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200

    with db.connection() as conn:
        events = db.one(
            conn,
            "SELECT count(*) AS n FROM audit_events WHERE project_id = %s "
            "  AND event_type LIKE 'project.sql%%'",
            (project_id,),
        )
    assert events["n"] == 0


@requires_node
def test_reading_a_schema_does_not_spend_the_console_budget(client, tenant):
    """Separate buckets, asserted from the outside. Free resolves one statement
    per window; a dashboard that rendered a table list with it would leave the
    customer unable to run the statement they came to run."""
    ref = "intrm001"
    tenant(ref)

    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    get_schema = functools.partial(client.get, f"/v1/projects/{ref}/database/schema", headers=headers)

    assert get_schema().status_code == 200
    assert get_schema().status_code == 200

    ran = client.post(f"/v1/projects/{ref}/sql", json={"statement": "SELECT 1"}, headers=headers)
    assert ran.status_code == 200, ran.text
