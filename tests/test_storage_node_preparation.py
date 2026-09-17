"""Preparing a node for Storage: the files a node runs, and the command that fills them.

Until free-tier slice 4 nothing in production prepared a node for Storage. The
worker, the gateway's registration and the maintenance passes all existed; the
object store was a test script, the data address vanished on reboot, the
metadata database and `storage.env` were written only by test fixtures, and the
storage unit had never started a container -- `ProtectHome=true` hides the
rootless runtime directory. These tests hold the pieces that closed that gap.
"""

from __future__ import annotations

import io
import json
import pathlib
import types

import psycopg
import pytest

from services.control_plane import storage_workers as sw
from tests.conftest import requires_db, storage_env_config

DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"
OBJECT_STORE_UNIT = DEPLOY / "maludb-object-store.service"
STORAGE_UNIT = DEPLOY / "maludb-storage.service"
FIREWALL = DEPLOY / "object-store-firewall.nft"


def _directives(path: pathlib.Path) -> str:
    """The unit without comments, continuations folded."""
    text = path.read_text().replace("\\\n", " ")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _exec_start(path: pathlib.Path) -> list[str]:
    for line in _directives(path).splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1].split()
    raise AssertionError(f"{path.name} has no ExecStart")


# -- the object store --------------------------------------------------------


def test_only_the_s3_gateway_leaves_the_data_address():
    """The master, volume server and filer authenticate nothing: the volume
    server serves any blob by id and the filer reads and writes every object. So
    they bind the data address, and only the S3 gateway gets a second one."""
    args = _exec_start(OBJECT_STORE_UNIT)
    assert "-ip.bind=${MALUDB_OBJECT_STORE_DATA_ADDRESS}" in args
    assert "-ip=${MALUDB_OBJECT_STORE_DATA_ADDRESS}" in args
    assert "-s3.ip.bind=${MALUDB_OBJECT_STORE_S3_ADDRESS}" in args
    binds = [a for a in args if ".bind=" in a]
    assert binds == ["-ip.bind=${MALUDB_OBJECT_STORE_DATA_ADDRESS}",
                     "-s3.ip.bind=${MALUDB_OBJECT_STORE_S3_ADDRESS}"], binds
    assert not any("0.0.0.0" in a for a in args)  # noqa: S104 - asserting its absence
    # A second, unrelated API on the S3 address, on by default in 4.x.
    assert "-s3.port.iceberg=0" in args
    assert "-s3.config=/etc/maludb/object-store/s3.json" in args


def test_the_firewall_is_loaded_as_root_before_the_store_listens():
    text = _directives(OBJECT_STORE_UNIT)
    assert "ExecStartPre=+/usr/sbin/nft -f /etc/maludb/object-store/firewall.nft" in text
    users = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("User=")]
    assert users == ["maludb-objects"], users
    for directive in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=tmpfs",
                      "PrivateTmp=true", "RestrictNamespaces=true"):
        assert directive in text, directive
    # Addresses only; the credential is the identities file, read by path.
    assert "EnvironmentFile=/etc/maludb/object-store/object-store.env" in text
    env = (DEPLOY / "object-store.env.example").read_text()
    assert "SECRET" not in env.upper() and "KEY=" not in env.upper()


def test_the_firewall_admits_s3_and_its_grpc_port_from_the_control_plane_only():
    """S3 authenticates; its gRPC port (S3 + 10000) does not."""
    rules = [line.strip() for line in FIREWALL.read_text().splitlines()
             if line.strip() and not line.strip().startswith("#")]
    body = "\n".join(rules)
    assert 'iif "lo" accept' in body
    assert "ip daddr $DATA_ADDRESS drop" in body
    assert "tcp dport { 8333, 18333 } ip saddr $CONTROL_PLANE accept" in body
    assert "tcp dport { 8333, 18333 } drop" in body
    # Order is the policy: loopback before the drops, the admit before its drop.
    assert (body.index('iif "lo" accept') < body.index("ip daddr $DATA_ADDRESS drop")
            < body.index("ip saddr $CONTROL_PLANE accept") < body.index("tcp dport { 8333, 18333 } drop"))
    # Replaced whole on load, and never a flush of anyone else's rules.
    assert "delete table inet maludb_object_store" in body
    assert "flush ruleset" not in body


def test_the_data_address_is_the_one_the_suite_and_the_gateway_use():
    network = (DEPLOY / "10-maludb-data.network").read_text()
    assert "Address=10.91.0.1/32" in network
    assert "Kind=dummy" in (DEPLOY / "10-maludb-data.netdev").read_text()
    assert "DATA_ADDRESS=\"${STORAGE_DATA_ADDRESS:-10.91.0.1}\"" in (
        DEPLOY.parent / "scripts" / "storage-test-cluster.sh"
    ).read_text()
    assert "define DATA_ADDRESS = 10.91.0.1" in FIREWALL.read_text()
    assert "MALUDB_OBJECT_STORE_DATA_ADDRESS=10.91.0.1" in (DEPLOY / "object-store.env.example").read_text()


# -- the storage worker's unit -------------------------------------------------


def test_the_storage_unit_leaves_the_rootless_runtime_reachable():
    """Found starting the unit for the first time: every form of ProtectHome=
    also covers /run/user, and ReadWritePaths= does not reopen it, so Podman
    failed with `lstat /run/user/<uid>: permission denied`."""
    text = _directives(STORAGE_UNIT)
    assert "ProtectHome" not in text
    assert "InaccessiblePaths=/home /root" in text
    assert "ReadWritePaths=/run/user /var/lib/maludb-api" in text
    assert "After=postgresql.service network-online.target maludb-object-store.service" in text


# -- the identities file ---------------------------------------------------------


def test_the_identities_file_is_one_platform_identity():
    rendered = json.loads(sw.render_identities("maludb-platform", "s" * 48))
    assert rendered == {"identities": [{
        "name": "maludb-platform",
        "credentials": [{"accessKey": "maludb-platform", "secretKey": "s" * 48}],
        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
    }]}


@pytest.mark.parametrize("access, secret", [(None, "x"), ("x", None), ("", "x")])
def test_the_identities_file_refuses_an_unset_credential(access, secret):
    with pytest.raises(sw.StorageWorkerError):
        sw.render_identities(access, secret)


# -- the command -------------------------------------------------------------------


def test_the_command_refuses_to_print_credentials_to_a_terminal(monkeypatch, capsys):
    from services.control_plane import manage

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdout", _Tty())
    code = manage._cmd_node_storage_prepare(types.SimpleNamespace(name="node-01", what="env"))
    assert code == 2
    assert "refusing" in capsys.readouterr().err


class _NoNode:
    """An admin connection that fails the test if anything reaches it."""

    def __getattr__(self, name):
        raise AssertionError(f"the node was touched ({name}) before the configuration was checked")


@requires_db
def test_an_unprepared_control_plane_seals_nothing(placed_project, key_ring):
    """A root sealed for a node that never gets a worker is a root nobody holds."""
    from services.control_plane import db

    placed_project("stprep01")
    config = types.SimpleNamespace(
        storage_db_host=None, storage_s3_endpoint=None,
        storage_s3_access_key=None, storage_s3_secret_key=None,
    )
    with db.connection() as conn:
        node_id = db.one(conn, "SELECT id FROM nodes WHERE name = 'wk-node'")["id"]
        with pytest.raises(sw.StorageWorkerError, match="MALUDB_STORAGE_DB_HOST"):
            sw.prepare_node(conn, node_id=node_id, key_ring=key_ring, config=config,
                            admin_conn=_NoNode(), metadata_connect=_NoNode())
        assert sw.node_secret(conn, node_id=node_id, key_ring=key_ring) is None


@requires_db
def test_preparing_twice_seals_once_and_renders_the_same_file(placed_project, key_ring, admin_conn):
    from services.control_plane import db
    from tests.test_provisioning import _tenant_admin_dsn

    config = storage_env_config()
    if not (config.storage_db_host and config.storage_s3_endpoint and config.storage_s3_access_key):
        config = types.SimpleNamespace(**{
            **vars(config),
            "storage_db_host": "10.91.0.1",
            "storage_s3_endpoint": "http://10.91.0.1:8333",
            "storage_s3_access_key": "maludb-platform",
            "storage_s3_secret_key": "s" * 48,
        })
    placed_project("stprep02")
    admin_conn.autocommit = True

    def connect(database):
        return psycopg.connect(_tenant_admin_dsn(database), autocommit=True)

    with db.connection() as conn:
        node_id = db.one(conn, "SELECT id FROM nodes WHERE name = 'wk-node'")["id"]
        first = sw.prepare_node(conn, node_id=node_id, key_ring=key_ring, config=config,
                                admin_conn=admin_conn, metadata_connect=connect)
        second = sw.prepare_node(conn, node_id=node_id, key_ring=key_ring, config=config,
                                 admin_conn=admin_conn, metadata_connect=connect)
    assert sw.render_env(first) == sw.render_env(second)

    row = admin_conn.execute(
        "SELECT has_database_privilege('public', %s, 'CONNECT') AS open",
        (sw.METADATA_DATABASE,),
    ).fetchone()
    assert row["open"] is False, "every tenant role on the node could open the tenant registry"
