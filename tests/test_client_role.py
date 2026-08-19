"""Paid direct database access (ADR-047, Phase 09 slice 2).

The capability was half-built for two phases: `mldb_<ref>_admin` had a stored
password and a `LOGIN` attribute the plan flipped, and **no route ever returned
that password to anybody**. This slice delivers it — and does not deliver the
admin role's, because handing over the identity the platform acts under makes
rotation a platform outage and revocation indistinguishable from breaking the
customer's SQL console.

So the positives here are few and the negatives are the deliverable. What a
credential that opens a real PostgreSQL connection from the internet must not
do is the whole of the test list: negative tests S to Y in
`specs/tenant-role-model.md`.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from services.control_plane import db, plan_apply, provisioning
from tests.conftest import requires_db
from tests.test_direct_sql import paid_project  # noqa: F401 - fixture
from tests.test_plan_apply import _entitlements_for, _set_plan_config
from tests.test_provisioning import ADMIN_DSN, _tenant_dsn, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture


def _as_client(names, passwords):
    return psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"]))


def _role(admin_conn, name: str) -> dict:
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
            "       rolbypassrls, rolreplication, rolinherit "
            "  FROM pg_roles WHERE rolname = %s",
            (name,),
        )
        return cur.fetchone()


# -- what the role is ------------------------------------------------------


@requires_node
@requires_maludb_core
def test_S_the_client_roles_memberships_are_exactly_the_admin_role(paid_project, admin_conn):  # noqa: F811
    """Negative test S. One membership is the whole of its privilege, so a
    second one is a privilege nobody decided to grant."""
    _, names, _ = paid_project("cli00001")
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "  JOIN pg_roles r ON r.oid = m.roleid "
            "  JOIN pg_roles c ON c.oid = m.member "
            " WHERE c.rolname = %s",
            (names.client,),
        )
        assert {row["rolname"] for row in cur.fetchall()} == {names.admin}


@requires_node
@requires_maludb_core
def test_U_the_client_role_holds_no_dangerous_attribute(paid_project, admin_conn):  # noqa: F811
    """Negative test U. The same list `tests/test_direct_sql.py` pins for the
    admin role, because a member of it must not exceed it."""
    _, names, _ = paid_project("cli00002")
    role = _role(admin_conn, names.client)
    assert role["rolsuper"] is False
    assert role["rolcreatedb"] is False
    assert role["rolcreaterole"] is False
    assert role["rolbypassrls"] is False
    assert role["rolreplication"] is False
    # NOINHERIT: privileges are arrived at through the session default, not
    # held ambiently by a role that has not asked.
    assert role["rolinherit"] is False


@requires_node
@requires_maludb_core
def test_V_no_shared_role_is_a_member_of_the_client_role(paid_project, admin_conn):  # noqa: F811
    """Negative test V. ADR-016 is one-directional: shared names may be granted
    to a tenant role, never the reverse -- the reverse would make every tenant
    on the node a member of this one."""
    _, names, _ = paid_project("cli00003")
    with admin_conn.cursor(row_factory=dict_row) as cur:
        for shared in ("anon", "authenticated", "service_role"):
            cur.execute("SELECT pg_has_role(%s, %s, 'member') AS member", (shared, names.client))
            assert cur.fetchone()["member"] is False, shared


@requires_node
@requires_maludb_core
def test_X_the_admin_role_cannot_log_in_on_any_tier(paid_project, admin_conn):  # noqa: F811
    """Negative test X, and the point of the whole ADR. The admin role's
    password is never issued, so it must never be a door."""
    _, names, _ = paid_project("cli00004", direct_access=True)
    assert _role(admin_conn, names.admin)["rolcanlogin"] is False
    assert provisioning.has_direct_sql_access(admin_conn, names) is True


@requires_node
@requires_maludb_core
def test_W_a_free_project_has_the_role_and_it_cannot_log_in(paid_project, admin_conn):  # noqa: F811
    """Negative test W. Created on every tier so an upgrade is an attribute
    change rather than a new credential -- and NOLOGIN until the plan says
    otherwise, which is ADR-005 as an attribute rather than as a promise."""
    _, names, passwords = paid_project("cli00005", direct_access=False)

    assert _role(admin_conn, names.client)["rolcanlogin"] is False
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"]))


# -- what a connection through it does --------------------------------------


@requires_node
@requires_maludb_core
def test_a_client_session_arrives_in_the_admin_role(paid_project):  # noqa: F811
    """The measurement ADR-047 turns on. Without the session default, objects a
    customer creates over their connection are owned by the client role, and
    `ALTER DEFAULT PRIVILEGES` only affects objects created by the role it
    names -- which is how Phase 08 produced a table the customer's own data API
    could not read."""
    _, names, passwords = paid_project("cli00006")
    with _as_client(names, passwords) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT current_user, session_user")
        row = cur.fetchone()
    assert row["current_user"] == names.admin
    assert row["session_user"] == names.client


@requires_node
@requires_maludb_core
def test_a_table_made_over_the_connection_is_owned_like_every_other_table(paid_project):  # noqa: F811
    """The property that matters and that a customer would never think to
    check: a direct connection and the SQL console produce the same result."""
    _, names, passwords = paid_project("cli00007")
    with _as_client(names, passwords) as conn, conn.cursor(row_factory=dict_row) as cur:
        conn.execute("CREATE TABLE public.made_directly (id int)")
        conn.commit()
        cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'made_directly'")
        assert cur.fetchone()["tableowner"] == names.admin


@requires_node
@requires_maludb_core
def test_T_a_client_role_cannot_reach_another_tenants_database(paid_project):  # noqa: F811
    """Negative test T, and the one that would matter most if it failed."""
    _, mine, my_passwords = paid_project("cli00008")
    _, theirs, _ = paid_project("cli00009")

    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(theirs.database, mine.client, my_passwords["client"]))


@requires_node
@requires_maludb_core
def test_the_client_role_cannot_change_the_admin_roles_password(paid_project):  # noqa: F811
    """What makes self-service rotation safe: the capability the rotation route
    needs is one the customer already has, and it stops exactly there."""
    _, names, passwords = paid_project("cli00010")
    with _as_client(names, passwords) as conn:
        conn.execute("SET ROLE NONE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                psycopg.sql.SQL("ALTER ROLE {r} PASSWORD 'nope'").format(
                    r=psycopg.sql.Identifier(names.admin)
                )
            )


@requires_node
@requires_maludb_core
def test_Y_a_downgrade_closes_the_connection_and_leaves_the_console_working(
    paid_project, admin_conn, key_ring,  # noqa: F811
):
    """Negative test Y, and the reason the roles are separate at all. Revoking
    direct access and breaking mediated SQL would be the same operation if the
    customer connected as the admin role."""
    project_id, names, passwords = paid_project("cli00011")
    with _as_client(names, passwords) as conn:
        conn.execute("SELECT 1")

    _set_plan_config(project_id, {"direct_database_access": False})
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))

    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"]))

    # And the console's own role is untouched, which is the half that would
    # have broken if direct access were the admin role's LOGIN attribute.
    executor_password = _load(project_id, "db_executor", key_ring)
    with psycopg.connect(
        _tenant_dsn(names.database, names.executor, executor_password)
    ) as conn:
        conn.execute(
            psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(names.admin))
        )
        conn.execute("SELECT 1")


def _load(project_id, credential_type: str, key_ring) -> str:
    """Through the suite's own key ring, not the deployment's: the test KEK is
    not the one `config.load()` would find."""
    with db.connection() as conn:
        return provisioning.load_credential(
            conn, project_id=project_id, credential_type=credential_type, key_ring=key_ring
        )
