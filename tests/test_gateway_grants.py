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
from tests.conftest import requires_db

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
