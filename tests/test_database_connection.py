"""The route that hands over a database credential (ADR-047, slice 2).

The only route in the platform that returns a secret which opens a real
PostgreSQL connection from the internet. So the refusals come first and there
are more of them than there are successes.

One thing asserted here that is easy to miss: the host returned is
`<ref>.<database_domain>`, a per-project name, and never the node's own
hostname. A node hostname in a customer's connection string names which node
they share, which `docs/CONTROL-PLANE.md` already treats as something the audit
trail must not publish -- and it breaks the moment ADR-006's background move to
another node happens, which the customer's application would discover as an
outage rather than be told.
"""

from __future__ import annotations

import psycopg
import pytest

from services.control_plane import db, plan_apply, provisioning
from tests.conftest import TEST_CREDENTIAL, node_host_and_port, requires_db
from tests.test_plan_apply import _entitlements_for, _set_plan_config
from tests.test_provisioning import ADMIN_DSN, _tenant_dsn, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

CONNECTION = "/v1/projects/{ref}/database/connection"
ROTATE = "/v1/projects/{ref}/database/connection/rotate"


def _token(client, ref: str) -> str:
    return client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]


def _headers(client, ref: str) -> dict:
    return {"Authorization": f"Bearer {_token(client, ref)}"}


def _entitle(project_id, *, direct: bool) -> None:
    _set_plan_config(project_id, {"direct_database_access": direct})


def _apply(admin_conn, names, project_id) -> None:
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))


# -- refusals --------------------------------------------------------------


def test_an_unauthenticated_caller_gets_nothing(client, db_pool):  # noqa: ARG001
    assert client.get(CONNECTION.format(ref="anyref")).status_code == 401
    assert client.post(ROTATE.format(ref="anyref")).status_code == 401


def test_a_non_member_cannot_tell_the_project_exists(client, db_pool):  # noqa: ARG001
    """404 rather than 403, and the same body as a missing project: a project
    ref is the customer's API subdomain (ADR-008), so confirming one is real
    confirms a target."""
    created = client.post(
        "/v1/auth/signup", json={"email": "db-outsider@example.com", "password": TEST_CREDENTIAL}
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/v1/auth/signin",
        json={"email": "db-outsider@example.com", "password": TEST_CREDENTIAL},
    ).json()["token"]

    answered = client.get(
        CONNECTION.format(ref="somebodyelse"), headers={"Authorization": f"Bearer {token}"}
    )
    assert answered.status_code == 404
    assert answered.json()["detail"] == "project not found"


@requires_node
@requires_maludb_core
def test_a_free_project_is_refused_its_own_credential(client, tenant, admin_conn):
    """ADR-005 as a route rather than as a promise. The credential exists in
    `project_credentials` for every tier -- so that an upgrade is an attribute
    change rather than a new secret -- and this is what stops it being handed
    over before the plan grants it."""
    ref = "dbcn0001"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=False)
    _apply(admin_conn, names, project_id)

    answered = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref))

    assert answered.status_code == 403
    assert "plan" in answered.json()["detail"]


@requires_node
@requires_maludb_core
def test_a_member_who_is_not_a_manager_is_refused(client, tenant, admin_conn):
    """Reading this is taking custody of the project's database. `viewer`
    exists so that seeing a project is not the same as holding it."""
    ref = "dbcn0002"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    client.post(
        "/v1/auth/signup", json={"email": "db-viewer@example.com", "password": TEST_CREDENTIAL}
    )
    with db.connection() as conn:
        org = db.one(
            conn, "SELECT org_id FROM projects WHERE project_ref = %s", (ref,)
        )["org_id"]
        user = db.one(
            conn, "SELECT id FROM users WHERE email = %s", ("db-viewer@example.com",)
        )["id"]
        db.execute(
            conn,
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'viewer')",
            (org, user),
        )
        conn.commit()
    token = client.post(
        "/v1/auth/signin",
        json={"email": "db-viewer@example.com", "password": TEST_CREDENTIAL},
    ).json()["token"]

    answered = client.get(
        CONNECTION.format(ref=ref), headers={"Authorization": f"Bearer {token}"}
    )
    assert answered.status_code == 403


# -- the credential --------------------------------------------------------


@requires_node
@requires_maludb_core
def test_a_manager_gets_a_credential_that_actually_connects(client, tenant, admin_conn):
    """The capability, end to end. Connected against the node's real address
    rather than the returned host, which is a DNS record this suite does not
    own -- what is being asserted is that the *credential* works."""
    ref = "dbcn0003"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    answered = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref))
    assert answered.status_code == 200, answered.text
    body = answered.json()

    assert body["user"] == names.client
    assert body["database"] == names.database
    with psycopg.connect(
        _tenant_dsn(names.database, body["user"], body["password"])
    ) as conn:
        conn.execute("SELECT 1")


@requires_node
@requires_maludb_core
def test_the_host_is_the_projects_own_name_and_never_the_nodes(client, tenant, admin_conn):
    """A node hostname in a connection string names which node a customer
    shares, and breaks when ADR-006's background move happens."""
    ref = "dbcn0004"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    body = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref)).json()

    with db.connection() as conn:
        node = db.one(
            conn,
            "SELECT n.hostname, n.internal_host FROM nodes n "
            "  JOIN projects p ON p.node_id = n.id WHERE p.project_ref = %s",
            (ref,),
        )
    assert body["host"].startswith(f"{ref}.")
    assert body["host"] != node["hostname"]
    assert body["host"] != node["internal_host"]
    assert node["hostname"] not in body["connection_string"]


@requires_node
@requires_maludb_core
def test_the_response_is_not_cacheable(client, tenant, admin_conn):
    """A credential must not sit in a shared cache or a browser's history."""
    ref = "dbcn0005"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    answered = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref))

    assert answered.headers["cache-control"] == "no-store"


@requires_node
@requires_maludb_core
def test_the_connection_string_escapes_what_it_interpolates(client, tenant, admin_conn):
    """Composed by the platform rather than left to a client, because a
    password containing `/` or `@` is exactly what a hand-rolled concatenation
    gets wrong -- `sql_console.executor_dsn` exists because that happened."""
    ref = "dbcn0006"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    body = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref)).json()

    parsed = psycopg.conninfo.conninfo_to_dict(body["connection_string"])
    assert parsed["user"] == names.client
    assert parsed["password"] == body["password"]
    assert parsed["dbname"] == names.database


# -- rotation --------------------------------------------------------------


@requires_node
@requires_maludb_core
def test_rotation_replaces_the_password_and_the_old_one_stops_working(client, tenant, admin_conn):
    """The route exists for the case where a customer has just discovered
    their password is public, so "immediately" is the requirement."""
    ref = "dbcn0007"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    first = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref)).json()
    rotated = client.post(ROTATE.format(ref=ref), headers=_headers(client, ref))
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()

    assert second["password"] != first["password"]
    with psycopg.connect(_tenant_dsn(names.database, names.client, second["password"])) as conn:
        conn.execute("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(names.database, names.client, first["password"]))


@requires_node
@requires_maludb_core
def test_rotation_leaves_the_stored_credential_matching_the_node(client, tenant, admin_conn, key_ring):
    """The failure this ordering exists to avoid: the platform holding a
    password the node no longer accepts locks the customer out of their own
    database."""
    ref = "dbcn0008"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    body = client.post(ROTATE.format(ref=ref), headers=_headers(client, ref)).json()

    with db.connection() as conn:
        stored = provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_client", key_ring=key_ring
        )
    assert stored == body["password"]
    # And what the next GET returns is the same one, not a third.
    again = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref)).json()
    assert again["password"] == body["password"]


@requires_node
@requires_maludb_core
def test_a_free_project_cannot_rotate_what_it_may_not_read(client, tenant, admin_conn):
    """Otherwise the refusal above is a lock on the front door with the back
    one open: rotation would still prove the credential exists, and would
    invalidate whatever the platform had stored."""
    ref = "dbcn0009"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=False)
    _apply(admin_conn, names, project_id)

    assert client.post(ROTATE.format(ref=ref), headers=_headers(client, ref)).status_code == 403


# -- the trail -------------------------------------------------------------


@requires_node
@requires_maludb_core
def test_both_routes_are_recorded_where_the_customer_can_read_them(client, tenant, admin_conn):
    """"Who took our database password, and when" is a question a customer
    should be able to answer without asking support."""
    from services.control_plane.api import audit, database

    ref = "dbcn0010"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref))
    client.post(ROTATE.format(ref=ref), headers=_headers(client, ref))

    with db.connection() as conn, conn.cursor() as cur:
        # The pool sets dict_row, so rows come back as mappings.
        cur.execute(
            "SELECT event_type, detail_json FROM audit_events WHERE project_id = %s",
            (project_id,),
        )
        events = {row["event_type"]: row["detail_json"] for row in cur.fetchall()}

    assert database.VIEWED in events
    assert database.ROTATED in events
    # Recorded with no detail at all: there is nothing about a credential that
    # is safe to put in a table an operator reads.
    assert events[database.VIEWED] == {}
    assert database.VIEWED in audit.VISIBLE_EVENTS
    assert database.ROTATED in audit.VISIBLE_EVENTS


@requires_node
@requires_maludb_core
def test_no_response_or_trail_ever_contains_the_admin_roles_password(
    client, tenant, admin_conn, key_ring
):
    """The whole of ADR-047 in one assertion: the secret handed over is the
    one minted to be given away, not the one the platform acts under."""
    ref = "dbcn0011"
    project_id, names, _ = tenant(ref)
    _entitle(project_id, direct=True)
    _apply(admin_conn, names, project_id)

    with db.connection() as conn:
        admin_password = provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_admin", key_ring=key_ring
        )

    body = client.get(CONNECTION.format(ref=ref), headers=_headers(client, ref)).text

    assert admin_password not in body
    assert names.admin not in body


def test_node_address_is_available_for_the_suite():
    """The fixtures above lean on it; failing here is clearer than failing
    inside a connection attempt."""
    host, port = node_host_and_port()
    assert host and port
