"""The gateway's database role, and what it must not be able to read (ADR-072).

The finding: the gateway is internet-facing, runs on a node, holds the KEK
because waking a worker needs it, and connected with the control plane's own
database credentials. `nodes.admin_dsn()` needs exactly those four things plus a
`node_id` it can read and an AAD derived from it -- so one compromised public
listener yielded the PostgreSQL superuser DSN of every node on the platform.

ADR-038 forbids this and enforces it with an import-graph test over the control
plane's public routers. That test never saw the gateway, which is a second
internet-facing application built afterwards.

**An import-graph test is the wrong instrument here.** The gateway legitimately
imports `workers`, `auth_workers`, `realtime_workers`, `storage_workers`,
`provisioning`, `entitlements` and `object_storage`; forbidding a module would
forbid the job. ADR-072's mechanism is a database privilege instead, so these
tests ask the database.
"""

from __future__ import annotations

import psycopg
import pytest

from services.control_plane import db, gateway_grants
from services.gateway import main as gateway_main
from tests.conftest import TEST_CREDENTIAL, requires_db

GATEWAY_ROLE = "mldb_test_gateway_role"


@pytest.fixture
def gateway_role(db_pool):  # noqa: ARG001 - the pool must exist before db.connection()
    """A real role carrying the real permission model.

    Skips rather than fails where the control-plane role cannot create one:
    `CREATE ROLE` needs CREATEROLE or superuser and the schema owner has
    neither by default, which is the same reason `cp-manage gateway grant`
    grants to a role an operator made rather than making it itself.
    """
    with db.connection() as conn:
        try:
            conn.execute(f'DROP ROLE IF EXISTS "{GATEWAY_ROLE}"')
            conn.execute(f'CREATE ROLE "{GATEWAY_ROLE}" NOLOGIN')
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip(
                "the control-plane role cannot CREATE ROLE, so the grant model "
                "cannot be applied to a real role here"
            )

        for statement in gateway_grants.statements(GATEWAY_ROLE):
            conn.execute(statement)
        conn.commit()
    try:
        yield GATEWAY_ROLE
    finally:
        with db.connection() as conn:
            # Explicit revokes, not `DROP OWNED BY`: that is documented to
            # revoke privileges granted to a role and did not clear these, so
            # `DROP ROLE` failed on "privileges for table ..." for every table.
            for statement in gateway_grants.revocations(GATEWAY_ROLE):
                conn.execute(statement)
            conn.execute(f'DROP ROLE IF EXISTS "{GATEWAY_ROLE}"')
            conn.commit()


def _can_select(conn, role: str, column: str) -> bool:
    row = db.one(
        conn,
        "SELECT has_column_privilege(%s, 'nodes'::regclass, %s, 'SELECT') AS ok",
        (role, column),
    )
    return bool(row["ok"])


@requires_db
def test_the_grant_model_hides_what_recovers_a_node_superuser(gateway_role):
    """The whole point, asserted against the catalogue rather than the statements.

    Any one of these three columns being readable is enough: `admin_dsn()` needs
    the ciphertext, the nonce and the key version, and a role that can read all
    three plus hold the KEK owns every database on the platform.
    """
    with db.connection() as conn:
        readable = [
            c for c in gateway_grants.NODE_ADMIN_COLUMNS if _can_select(conn, gateway_role, c)
        ]
    assert readable == [], (
        f"the gateway role can still read {readable} on nodes, which is what "
        "nodes.admin_dsn() needs -- ADR-072 is not in force"
    )


@requires_db
def test_the_gateway_role_can_still_do_its_job(gateway_role):
    """A narrowing that broke the gateway would be reverted, so prove it does not.

    `workers.py` locks a node row while allocating a port
    (`SELECT id FROM nodes WHERE id = %s FOR UPDATE`), and PostgreSQL requires
    UPDATE on at least one column for that lock -- which is why the model grants
    UPDATE on `last_health_at` rather than on the table.
    """
    with db.connection() as conn:
        assert _can_select(conn, gateway_role, "id"), "the gateway cannot see a node's id"
        row = db.one(
            conn,
            "SELECT has_column_privilege(%s, 'nodes'::regclass, %s, 'UPDATE') AS ok",
            (gateway_role, gateway_grants.NODE_LOCKABLE_COLUMNS[0]),
        )
        assert row["ok"], "the gateway cannot take the row lock port allocation needs"

        for table in ("projects", "plans"):
            has = db.one(
                conn,
                "SELECT has_table_privilege(%s, %s, 'SELECT') AS ok",
                (gateway_role, table),
            )
            assert has["ok"], f"the gateway cannot read {table}, which it serves every request from"


# -- the startup refusal ---------------------------------------------------


class _Refusing:
    """A connection whose probe is denied: a correctly narrowed role."""

    def execute(self, _sql):
        raise psycopg.errors.InsufficientPrivilege("permission denied for table nodes")

    def rollback(self):
        pass


class _Permitting:
    """A connection whose probe succeeds: the control plane's own credentials."""

    def execute(self, _sql):
        return None

    def rollback(self):
        pass


def test_a_gateway_that_can_read_node_admin_columns_refuses_to_start_in_production():
    """Configuration is not the check; the privilege is.

    A gateway pointed at the control plane's DSN works perfectly and is fully
    exposed, so asserting that MALUDB_GATEWAY_DATABASE_URL is set would pass
    exactly the deployment that has to fail.
    """
    with pytest.raises(RuntimeError, match="superuser DSN of every node"):
        gateway_main.assert_narrowed(_Permitting(), environment="production")


def test_a_narrowed_gateway_starts():
    gateway_main.assert_narrowed(_Refusing(), environment="production")


def test_outside_production_it_warns_rather_than_refuses(caplog):
    """The suite and a developer's laptop run one role against one database.

    Refusing there would most likely be "fixed" by pasting in the production
    DSN, which is the opposite of what this exists to encourage.
    """
    with caplog.at_level("WARNING"):
        gateway_main.assert_narrowed(_Permitting(), environment="development")
    assert any("ADR-072" in r.getMessage() for r in caplog.records), caplog.text


# -- the row narrowing (ADR-072 point 2) -----------------------------------
#
# The column model above stops a gateway recovering another *node's* superuser
# DSN. It does nothing about project rows: the gateway holds the KEK, so every
# project row it can read is that project's database password and JWT signing
# key. These assert the second half, and they assert it by becoming the role --
# `SET ROLE`, not `has_table_privilege` -- because row-level security is not a
# privilege the catalogue can be asked about. A policy that is enabled and wrong
# looks identical to one that is right until somebody reads a row through it.


@pytest.fixture
def two_nodes_two_projects(db_pool):  # noqa: ARG001 - the pool must exist first
    """One project on each of two nodes, and the ids to tell them apart."""
    import uuid

    from services.control_plane import identity

    made = {}
    with db.connection() as conn:
        for slug in ("alpha", "beta"):
            node = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, node_pool, status) "
                "VALUES (%s,%s,%s,'shared','active') ON CONFLICT (name) DO UPDATE "
                "SET status='active' RETURNING id",
                (f"rls-{slug}", f"{slug}.example", f"{slug}.internal"),
            )["id"]
            _, org = identity.create_user_with_personal_org(
                conn, email=f"rls-{slug}-{uuid.uuid4().hex[:8]}@example.com",
                password=TEST_CREDENTIAL,
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES (%s,'Test') "
                "ON CONFLICT (code) DO UPDATE SET name='Test' RETURNING id",
                (f"rls-plan-{slug}",),
            )["id"]
            project_id = uuid.uuid4()
            ref = f"rls{slug}"
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, "
                "status, node_id, database_name) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s)",
                (project_id, org, ref, ref, plan, node, f"mldb_{ref}"),
            )
            # A credential row, because that is the thing whose exposure the
            # decision is actually about -- not the project's name.
            db.execute(
                conn,
                "INSERT INTO project_credentials (id, project_id, credential_type, role_name, "
                "ciphertext, nonce, key_version) "
                "SELECT %s, %s, 'database_password', %s, %s, %s, ek.key_version "
                "  FROM encryption_keys ek ORDER BY ek.key_version LIMIT 1",
                (uuid.uuid4(), project_id, f"mldb_{ref}_client", b"ciphertext", b"nonce-nonce-"),
            )
            made[slug] = {"node_id": node, "project_id": project_id, "ref": ref}
        conn.commit()
    yield made
    with db.connection() as conn:
        # Inside out: `project_credentials.project_id` and `projects.node_id`
        # both restrict, so the rows have to go in dependency order.
        names = ["rls-alpha", "rls-beta"]
        db.execute(
            conn,
            "DELETE FROM project_credentials WHERE project_id IN "
            "  (SELECT id FROM projects WHERE node_id IN "
            "     (SELECT id FROM nodes WHERE name = ANY(%s)))",
            (names,),
        )
        db.execute(
            conn,
            "DELETE FROM projects WHERE node_id IN (SELECT id FROM nodes WHERE name = ANY(%s))",
            (names,),
        )
        db.execute(conn, "DELETE FROM nodes WHERE name = ANY(%s)", (names,))
        conn.commit()


def _as_gateway(conn, role: str):
    """Become the gateway role for the rest of this transaction.

    `SET LOCAL` so the reset is the rollback, and a rollback is what every one
    of these tests does anyway.
    """
    conn.execute(f'SET LOCAL ROLE "{role}"')


@requires_db
def test_a_gateway_sees_only_its_own_node_projects(gateway_role, two_nodes_two_projects):
    """The claim in one query.

    Note what is *not* asserted: that the beta row is absent from a filtered
    query. RLS is not a WHERE clause the caller can forget -- the row is not
    there for any query, which is why this reads the whole table.
    """
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        _as_gateway(conn, gateway_role)
        refs = {r["project_ref"] for r in db.query(conn, "SELECT project_ref FROM projects")}
        conn.rollback()

    assert alpha["ref"] in refs, "the gateway cannot see the projects on its own node"
    assert beta["ref"] not in refs, (
        "the gateway can read a project placed on another node; with the KEK that is "
        "that project's database password and signing key (ADR-072)"
    )


@requires_db
def test_a_gateway_cannot_read_another_node_project_credentials(
    gateway_role, two_nodes_two_projects
):
    """The row that matters, reached the way an attacker would reach it.

    `project_credentials` is keyed on the project rather than the node, so it is
    covered by its own policy rather than by the one on `projects`. A narrowing
    that covered only the table with `node_id` on it would pass the test above
    and leave this open.
    """
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        _as_gateway(conn, gateway_role)
        rows = db.query(
            conn,
            "SELECT project_id FROM project_credentials WHERE project_id = ANY(%s)",
            ([alpha["project_id"], beta["project_id"]],),
        )
        conn.rollback()

    seen = {r["project_id"] for r in rows}
    assert alpha["project_id"] in seen, "the gateway cannot decrypt its own tenants"
    assert beta["project_id"] not in seen, (
        "the gateway can read another node's project credentials"
    )


@requires_db
def test_a_gateway_can_read_its_own_node_storage_root_and_not_another(
    gateway_role, two_nodes_two_projects
):
    """A narrowing that breaks the job gets reverted, so prove this one does not.

    `storage_workers.ensure_node_secret` reads `nodes.storage_secret_ciphertext`
    on the request that registers a project with the shared worker, and that is
    a gateway path (`app.py`, "a project that is not registered is simply one
    whose next Storage request registers it").

    ADR-072's first slice granted `nodes(id)` alone, so a correctly narrowed
    gateway answered 500 on every Storage request -- and nothing caught it,
    because the suite runs the gateway as the schema owner, which is exempt from
    all of this. The column is granted now and the *row* policy is what keeps it
    honest: its own node's root, never another's.
    """
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        # All three columns together: `nodes_storage_secret_complete` refuses a
        # half-sealed root, which is the constraint doing its job.
        for node_id, root in ((alpha["node_id"], b"alpha-root"), (beta["node_id"], b"beta-root")):
            db.execute(
                conn,
                "UPDATE nodes SET storage_secret_ciphertext = %s, storage_secret_nonce = %s, "
                "storage_secret_key_version = (SELECT min(key_version) FROM encryption_keys) "
                "WHERE id = %s",
                (root, b"nonce-nonce-", node_id),
            )
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        _as_gateway(conn, gateway_role)
        rows = db.query(
            conn, "SELECT id, storage_secret_ciphertext AS root FROM nodes WHERE id = ANY(%s)",
            ([alpha["node_id"], beta["node_id"]],),
        )
        conn.rollback()

    got = {r["id"]: bytes(r["root"]) for r in rows}
    assert got.get(alpha["node_id"]) == b"alpha-root", (
        "the gateway cannot read its own node's storage root, so every Storage "
        "request on a narrowed deployment fails"
    )
    assert beta["node_id"] not in got, "the gateway can read another node's storage root"


@requires_db
def test_a_gateway_cannot_seal_a_new_storage_root(gateway_role, two_nodes_two_projects):
    """Reading the root is a gateway path; creating one is node preparation.

    A process that can write it can, on a node where it is absent, mint a root
    the running container does not hold -- which leaves every tenant already
    registered on that node unreadable at once. `ensure_node_secret` warns about
    exactly that, and this keeps the public listener out of it.
    """
    alpha = two_nodes_two_projects["alpha"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        _as_gateway(conn, gateway_role)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE nodes SET storage_secret_ciphertext = %s WHERE id = %s",
                (b"minted-by-the-gateway", alpha["node_id"]),
            )
        conn.rollback()


@requires_db
def test_a_gateway_cannot_pin_its_own_node(gateway_role, two_nodes_two_projects):
    """ADR-075's refusal compares a node's pins with what it was checked to provide.
    A gateway that could write its own node's pin could match whatever version the
    node runs and put it back into placement. Its own node, on purpose: another
    node's rows are already out of reach by the row policy, so that would not show
    the grant is what refuses."""
    alpha = two_nodes_two_projects["alpha"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        db.execute(
            conn,
            "INSERT INTO node_extension_pins (node_id, extension, version, set_by) "
            "VALUES (%s, 'vector', '0.8.4', 'operator') ON CONFLICT DO NOTHING",
            (alpha["node_id"],),
        )
        conn.commit()
        _as_gateway(conn, gateway_role)
        seen = db.query(conn, "SELECT extension FROM node_extension_pins WHERE node_id = %s",
                        (alpha["node_id"],))
        assert [r["extension"] for r in seen] == ["vector"], "the gateway cannot read its own pins"
        for statement, params in (
            ("UPDATE node_extension_pins SET version = '9.9.9' WHERE node_id = %s", (alpha["node_id"],)),
            ("DELETE FROM node_extension_pins WHERE node_id = %s", (alpha["node_id"],)),
            ("INSERT INTO node_extension_pins (node_id, extension, version, set_by) "
             "VALUES (%s, 'maludb_core', '0.104.0', 'gateway')", (alpha["node_id"],)),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
            conn.rollback()
            _as_gateway(conn, gateway_role)
        conn.rollback()


@requires_db
def test_a_gateway_cannot_write_a_row_for_another_node_project(
    gateway_role, two_nodes_two_projects
):
    """USING hides rows; WITH CHECK is what stops one being created.

    Without the second half a gateway could insert an audit event, or an egress
    measurement, attributed to a project on a machine it has nothing to do with.
    """
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        _as_gateway(conn, gateway_role)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
                "VALUES (%s, 'system', 'forged', '{}'::jsonb)",
                (beta["project_id"],),
            )
        conn.rollback()


@requires_db
def test_a_gateway_mapped_to_no_node_sees_nothing(gateway_role, two_nodes_two_projects):
    """The failure direction, asserted rather than assumed.

    An unmapped role resolves to NULL and `node_id = NULL` is never true, so the
    gateway sees nothing. That is the safe direction and the whole reason this
    is keyed on `current_user`: the alternative -- a session variable naming the
    node -- would be set by the very process the decision treats as hostile.
    """
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = NULL WHERE gateway_role = %s",
                   (gateway_role,))
        _as_gateway(conn, gateway_role)
        assert db.one(conn, "SELECT count(*) AS n FROM projects")["n"] == 0
        assert db.one(conn, "SELECT count(*) AS n FROM project_credentials")["n"] == 0
        conn.rollback()


@requires_db
def test_the_control_plane_still_sees_every_row(gateway_role, two_nodes_two_projects):
    """The other half of "does not disturb the control plane's own access".

    PostgreSQL exempts a table's owner from its policies unless FORCE ROW LEVEL
    SECURITY is set. That exemption is the reason this narrowing could be added
    to a live schema at all, so it is asserted rather than trusted -- a stray
    FORCE would break provisioning, billing and every maintenance pass at once,
    and would do it quietly, as rows that stopped existing.
    """
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        conn.commit()
        refs = {r["project_ref"] for r in db.query(conn, "SELECT project_ref FROM projects")}

    assert {alpha["ref"], beta["ref"]} <= refs, (
        "the control-plane role lost rows to a policy it must be exempt from"
    )


@requires_db
def test_a_temp_table_cannot_impersonate_the_nodes_table(gateway_role, two_nodes_two_projects):
    """The escalation the pinned search_path exists to stop.

    `gateway_node_id()` reads `nodes`. PostgreSQL searches the temporary schema
    before the rest of the search_path unless pg_temp is named explicitly, so an
    unqualified reference would let a gateway create a temp table called `nodes`
    with a `gateway_role` column and name whichever node it liked. The function
    qualifies the table *and* pins the path with pg_temp last; this proves it.

    Same class of mistake as upstream storage migration 0011's unqualified
    function, which is why it is tested rather than reasoned about.
    """
    alpha, beta = two_nodes_two_projects["alpha"], two_nodes_two_projects["beta"]
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, alpha["node_id"]))
        _as_gateway(conn, gateway_role)
        try:
            conn.execute(
                "CREATE TEMP TABLE nodes (id BIGINT, gateway_role TEXT)"
            )
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip("this role cannot create temp tables, so the attack is unavailable")
        conn.execute("INSERT INTO pg_temp.nodes VALUES (%s, current_user)", (beta["node_id"],))
        refs = {r["project_ref"] for r in db.query(conn, "SELECT project_ref FROM projects")}
        conn.rollback()

    assert beta["ref"] not in refs, (
        "a temp table named `nodes` redirected the policy at another node -- "
        "gateway_node_id() is resolving its table name at execution time"
    )


# -- the startup refusal, second half --------------------------------------


class _Unmapped:
    """A connection whose role resolves to no node."""

    def cursor(self):
        return _Cursor(None)

    def rollback(self):
        pass


class _Mapped:
    def cursor(self):
        return _Cursor(7)

    def rollback(self):
        pass


class _Cursor:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql):
        return None

    def fetchone(self):
        return (self._value,)


def test_an_unmapped_gateway_refuses_to_start_in_production():
    """Fails closed is right and unreadable, so it is said out loud.

    Without this the deployment symptom is a healthy process, no error, and
    every tenant on the machine answering 404.
    """
    with pytest.raises(RuntimeError, match="not mapped to any node"):
        gateway_main._assert_node_identity(_Unmapped(), environment="production")


def test_a_mapped_gateway_starts():
    gateway_main._assert_node_identity(_Mapped(), environment="production")


def test_an_unmapped_gateway_outside_production_warns(caplog):
    with caplog.at_level("WARNING"):
        gateway_main._assert_node_identity(_Unmapped(), environment="development")
    assert any("not mapped to any node" in r.getMessage() for r in caplog.records), caplog.text


@requires_db
def test_every_project_or_node_keyed_table_carries_the_gateway_policy(db_pool):  # noqa: ARG001
    """ADR-072 point 2 does not hold itself; this does.

    Migration 0031 put a row policy on every table keyed to a project or a node.
    The gateway's permission model is a denylist, so the next migration that adds
    such a table without the policy makes that table readable across every node
    by every gateway -- silently, because nothing the gateway does today reads
    it. Phase 12 slice 1 added `extension_upgrades` and had to remember. This
    remembers instead.
    """
    with db.connection() as conn:
        rows = db.query(
            conn,
            """
            SELECT c.relname AS table,
                   c.relrowsecurity AS rls,
                   EXISTS (SELECT 1 FROM pg_policies p
                            WHERE p.schemaname = 'public' AND p.tablename = c.relname
                              AND p.policyname = 'gateway_own_node') AS policy
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
               AND EXISTS (SELECT 1 FROM pg_attribute a
                            WHERE a.attrelid = c.oid AND NOT a.attisdropped
                              AND a.attname IN ('project_id', 'node_id'))
             ORDER BY c.relname
            """,
        )
    uncovered = [r["table"] for r in rows if not (r["rls"] and r["policy"])]
    assert rows, "found no project- or node-keyed tables at all; the query is wrong"
    assert uncovered == [], (
        f"{uncovered} are keyed to a project or a node but lack the gateway_own_node "
        "policy, so every gateway can read them across nodes (ADR-072 point 2)"
    )
