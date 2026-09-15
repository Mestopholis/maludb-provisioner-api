"""`cp-manage node credential set`: the only way a deployment stores a node's provisioning DSN.

Found by the deployment rehearsal: `node register` takes no DSN and `nodes.set_admin_dsn`
was called only from tests, so a node registered by following docs/DEPLOYMENT.md could
never be provisioned onto. What is held here:

- **the DSN is read from stdin**, never an argument, and never printed -- not on success,
  not in a connection error;
- **it is checked before it is stored**: a DSN that does not connect, or connects as a
  role that is not a superuser, stores nothing;
- **what is stored is what provisioning reads back** through `nodes.admin_dsn`.
"""

from __future__ import annotations

import argparse
import io
import pathlib

import psycopg
import pytest

from services.control_plane import crypto, db, manage, nodes
from tests.conftest import NODE_ADMIN_DSN, TEST_KEK, requires_db

pytestmark = requires_db

UNREACHABLE = "postgresql://postgres:never-printed-7f3a@127.0.0.1:1/postgres"  # noqa: S105 - not a real secret


@pytest.fixture
def cli(db_pool, app_config, monkeypatch):
    monkeypatch.setattr("services.control_plane.config.load", lambda: app_config)
    with db.connection() as conn:
        nodes.register_node(conn, name="cred-node", hostname="cred.test", internal_host="10.0.9.7",
                            node_pool="shared", capacity={})
        conn.commit()

    def run(dsn: str, *, name: str = "cred-node", no_verify: bool = False) -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(dsn + "\n"))
        return manage._cmd_node_credential_set(argparse.Namespace(name=name, no_verify=no_verify))

    return run


def _stored(name: str = "cred-node") -> str | None:
    ring = crypto.KeyRing(TEST_KEK)
    with db.connection() as conn:
        ring.load(conn)
        row = db.one(conn, "SELECT id, admin_ciphertext FROM nodes WHERE name = %s", (name,))
        if row["admin_ciphertext"] is None:
            return None
        return nodes.admin_dsn(conn, node_id=row["id"], key_ring=ring)


def test_the_command_takes_no_dsn_argument():
    source = pathlib.Path(manage.__file__).read_text()
    block = source[source.index("credential_set = credential.add_parser("):source.index("credential_set.set_defaults")]
    assert "--dsn" not in block, "a DSN argument lands in shell history and in ps"


def test_an_unverified_credential_is_stored_and_read_back_and_never_printed(cli, capsys):
    assert cli(UNREACHABLE, no_verify=True) == 0
    assert _stored() == UNREACHABLE
    out = capsys.readouterr()
    assert "not verified" in out.out
    assert "never-printed-7f3a" not in out.out + out.err


def test_a_dsn_that_does_not_connect_stores_nothing_and_does_not_print_its_password(cli, capsys):
    assert cli(UNREACHABLE) == 1
    assert _stored() is None
    out = capsys.readouterr()
    assert "could not connect" in out.err and "nothing stored" in out.err
    assert "never-printed-7f3a" not in out.out + out.err


def test_an_empty_or_malformed_dsn_is_refused(cli, capsys):
    assert cli("") == 2
    assert cli("host=127.0.0.1 port='unterminated") == 2
    assert _stored() is None


def test_an_unregistered_node_is_refused(cli, capsys):
    assert cli(UNREACHABLE, name="no-such-node", no_verify=True) == 1
    assert "register it first" in capsys.readouterr().err


def test_a_role_that_is_not_a_superuser_stores_nothing(cli, capsys, migrated_database):
    parts = psycopg.conninfo.conninfo_to_dict(migrated_database)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        superuser = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        if superuser:  # then log in as an ordinary role made for this
            conn.execute("DROP ROLE IF EXISTS cred_probe")
            conn.execute("CREATE ROLE cred_probe LOGIN PASSWORD 'cred-probe-only'")
            parts.update(user="cred_probe", password="cred-probe-only")  # noqa: S106 - test role
    try:
        assert cli(psycopg.conninfo.make_conninfo(**parts)) == 1
        assert "is not a superuser" in capsys.readouterr().err
        assert _stored() is None
    finally:
        if superuser:
            with psycopg.connect(migrated_database, autocommit=True) as conn:
                conn.execute("DROP ROLE cred_probe")


@pytest.mark.skipif(not NODE_ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")
def test_a_superuser_dsn_is_verified_and_stored(cli, capsys):
    assert cli(NODE_ADMIN_DSN) == 0
    out = capsys.readouterr().out
    assert "(superuser)" in out and "stored" in out
    assert _stored() == NODE_ADMIN_DSN
    parts = psycopg.conninfo.conninfo_to_dict(NODE_ADMIN_DSN)
    if parts.get("password") and parts["password"] != parts.get("user"):  # a test cluster may reuse the name
        assert parts["password"] not in out


def test_the_runbook_stores_the_credential_before_anything_reaches_the_node():
    runbook = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md").read_text()
    section = runbook[runbook.index("### 2.2 Register it"):runbook.index("### 2.3")]
    order = [section.index(step) for step in (
        "cp-manage node register", "cp-manage node credential set", "cp-manage node backup-check",
        "cp-manage node extension-check",
    )]
    assert order == sorted(order)
