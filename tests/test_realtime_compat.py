"""Postgres Changes, with the official client, through the gateway.

Phase 06's first acceptance criterion, and the only test in the repository that
proves it: `@supabase/supabase-js` subscribes to a channel over
`wss://<ref>/realtime/v1/websocket`, a row is written to the tenant database,
and the client receives the change. Everything in the path is real -- a tenant
provisioned by the platform's own code, its own Realtime instance in a
container, the gateway authenticating the socket against a project API key, and
a hostname that actually resolves, because the hostname *is* the routing key
(ADR-008).

It has its own project ref, and therefore its own `/etc/hosts` entry, rather
than reusing the Phase 03 compatibility suite's. That suite's tenant lives on
the ordinary test node, which is `wal_level = replica` and where logical
decoding cannot work at all; Realtime needs the prepared cluster.

The gateway is given a supervisor and `wake_sleeping=True`, so what this
exercises is also the wake path: the project's instance is not running when the
client connects. That is what a customer's first connection after an idle hour
actually does.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import psycopg
import pytest
import uvicorn

from services.control_plane import api_keys, db, realtime
from services.control_plane import config as cp_config
from services.control_plane import realtime_workers as rtw
from services.gateway.app import Gateway, create_app
from tests.conftest import TEST_KEK, TEST_PEPPER, requires_db
from tests.test_realtime_enablement import REALTIME_DSN
from tests.test_realtime_server import (  # noqa: F401 - pytest resolves the fixtures
    DATA_HOST,
    DATA_PORT,
    IMAGE,
    PodmanSupervisor,
    _connect_to,
    _image_present,
    node,
    realtime_config,
    tenant,
)

# Its own ref, its own hosts entry, its own ports. Fixed so the environment can
# be prepared once rather than mutated by a running test.
COMPAT_REF = "rtcp0001"
GATEWAY_PORT = 28111
REALTIME_PORT = 24411

COMPAT_DIR = os.path.join(os.path.dirname(__file__), "compat")


def _resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return True


pytestmark = [
    requires_db,
    pytest.mark.skipif(not REALTIME_DSN, reason="MALUDB_REALTIME_NODE_DSN is unset"),
    pytest.mark.skipif(
        not DATA_HOST,
        reason="MALUDB_REALTIME_DB_HOST is unset; scripts/realtime-test-cluster.sh prints it",
    ),
    pytest.mark.skipif(not _image_present(), reason=f"podman or {IMAGE} is missing"),
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
    pytest.mark.skipif(
        not os.path.exists(os.path.join(COMPAT_DIR, "node_modules")),
        reason="run `npm install` in tests/compat",
    ),
    pytest.mark.skipif(
        not _resolves(f"{COMPAT_REF}.maludb.local"),
        reason=f"add '127.0.0.1 {COMPAT_REF}.maludb.local' to /etc/hosts",
    ),
]


@pytest.fixture
def compat_stack(tenant, key_ring, realtime_config, tmp_path, monkeypatch):  # noqa: F811 - fixtures
    """A tenant with Realtime enabled, a gateway in front of it, and a key.

    The instance is deliberately **not** started here. The gateway holds a
    supervisor and wakes it on the first connection, which is both the path a
    customer takes after an idle hour and the part of slice 5 with the most
    moving pieces.
    """
    # The gateway wakes the worker itself and passes no config directory, so the
    # module's default is what it writes to. Pointed at a scratch directory
    # here; production is /etc/maludb/realtime, owned by the node.
    monkeypatch.setattr(rtw, "CONFIG_DIR", tmp_path)

    project_id = tenant(COMPAT_REF)

    with db.connection() as conn, psycopg.connect(
        REALTIME_DSN, row_factory=psycopg.rows.dict_row
    ) as admin:
        realtime.enable(
            conn, admin, project_id=project_id, key_ring=key_ring,
            tenant_connect=_connect_to, metadata_connect=_connect_to,
        )
        db.execute(
            conn, "UPDATE projects SET realtime_port = %s WHERE id = %s",
            (REALTIME_PORT, project_id),
        )
        issued = api_keys.create(
            conn, project_id=project_id, key_type=api_keys.PUBLISHABLE,
            pepper=TEST_PEPPER, key_ring=key_ring,
        )
        conn.commit()

    with _connect_to(f"mldb_{COMPAT_REF}") as tenant_conn:
        tenant_conn.execute(
            "CREATE TABLE IF NOT EXISTS public.notes (id serial primary key, body text)"
        )
        tenant_conn.execute("ALTER TABLE public.notes REPLICA IDENTITY FULL")
        # Exactly what a customer runs on Supabase to choose what replicates.
        try:
            tenant_conn.execute("ALTER PUBLICATION supabase_realtime ADD TABLE public.notes")
        except psycopg.errors.DuplicateObject:
            pass
        # Realtime decides what a subscriber may see with
        # `has_column_privilege(<the token's role>, ...)`, so a table the
        # anonymous role cannot select is a table whose changes are delivered to
        # nobody -- silently, which is upstream's behaviour and not an error the
        # client can see. A customer gets this from the bootstrap's default
        # privileges by creating the table as their own admin role; this fixture
        # creates it on the platform's connection, so the grant is explicit.
        tenant_conn.execute("GRANT SELECT ON public.notes TO anon, authenticated")

    supervisor = PodmanSupervisor(
        key_ring=key_ring, config=realtime_config, config_dir=tmp_path
    )
    gateway_config = cp_config.Config(
        environment="test",
        database_url=os.environ["MALUDB_CONTROL_PLANE_DATABASE_URL"],
        gateway_domain="maludb.local",
        docs_enabled=False,
        kek=TEST_KEK,
        token_pepper=TEST_PEPPER,
        realtime_db_host=DATA_HOST,
        realtime_db_port=DATA_PORT,
        realtime_image=IMAGE,
    )
    gateway = Gateway(
        config=gateway_config, key_ring=key_ring,
        realtime_supervisor=supervisor, wake_sleeping=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(gateway), host="127.0.0.1", port=GATEWAY_PORT, log_level="error"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    yield {
        "project_id": project_id,
        "url": f"http://{COMPAT_REF}.maludb.local:{GATEWAY_PORT}",
        "key": issued.plaintext,
        "supervisor": supervisor,
    }

    server.should_exit = True
    with db.connection() as conn, psycopg.connect(
        REALTIME_DSN, row_factory=psycopg.rows.dict_row
    ) as admin:
        try:
            rtw.teardown(
                conn, admin, project_id=project_id, key_ring=key_ring, supervisor=supervisor
            )
        finally:
            supervisor.stop(COMPAT_REF)


def test_the_official_client_receives_postgres_changes(compat_stack):
    """The acceptance criterion, end to end.

    The Node process subscribes and prints `subscribed`; only then is a row
    written, because a change written before the subscription is a change there
    was nothing to deliver. The client then prints what it received.
    """
    client = subprocess.Popen(  # noqa: S603 - fixed argv
        [shutil.which("node") or "node", os.path.join(COMPAT_DIR, "realtime.mjs")],  # noqa: S607
        cwd=COMPAT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "MALUDB_URL": compat_stack["url"], "MALUDB_KEY": compat_stack["key"]},
    )
    try:
        subscribed = _await_line(client, "subscribed", timeout=180)
        assert subscribed["ok"], f"the client could not subscribe: {subscribed}"

        # Written repeatedly rather than once, and the reason is upstream's
        # ordering rather than flakiness: SUBSCRIBED reports the *channel* join,
        # and the Postgres Changes binding is confirmed a moment later, when the
        # server has written the subscription and its replication slot has
        # caught up. A single row in that gap is delivered to nobody, which is
        # indistinguishable from a broken pipeline.
        change = _insert_until_delivered(client)
    finally:
        client.terminate()
        try:
            client.wait(timeout=10)
        except subprocess.TimeoutExpired:
            client.kill()

    assert change["ok"], f"no change reached the official client: {change}"
    assert change["type"] == "INSERT"
    assert change["table"] == "notes"
    assert change["body"] == "through the gateway"


def _insert_until_delivered(client: subprocess.Popen, *, timeout: float = 60) -> dict:
    """Write rows while waiting for one to come back.

    Written from a thread rather than between reads, because reading the
    client's output blocks: a harness that inserted once and then waited would
    be holding the only thing that could notice it had waited too long. The
    repetition itself is for upstream's ordering -- SUBSCRIBED reports the
    channel join, and the Postgres Changes binding is confirmed a moment later,
    when the server has written the subscription and its slot has caught up. A
    single row in that gap is delivered to nobody, which from the client's side
    is indistinguishable from a broken pipeline.
    """
    stop = threading.Event()

    def write() -> None:
        while not stop.is_set():
            with _connect_to(f"mldb_{COMPAT_REF}") as conn:
                conn.execute("INSERT INTO public.notes (body) VALUES ('through the gateway')")
            stop.wait(2)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        return _await_line(client, "postgres_changes", timeout=timeout)
    finally:
        stop.set()
        writer.join(timeout=5)


def test_the_wake_happened_and_was_recorded(compat_stack):
    """The instance was not running when the client connected.

    Which makes the previous test a proof of the wake path too, and this the
    assertion that says so rather than leaving it implied.
    """
    project_id = compat_stack["project_id"]
    with db.connection() as conn:
        before = db.one(
            conn,
            "SELECT realtime_worker_state FROM projects WHERE id = %s",
            (project_id,),
        )
    assert before["realtime_worker_state"] == "STOPPED"

    client = subprocess.Popen(  # noqa: S603 - fixed argv
        [shutil.which("node") or "node", os.path.join(COMPAT_DIR, "realtime.mjs")],  # noqa: S607
        cwd=COMPAT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "MALUDB_URL": compat_stack["url"], "MALUDB_KEY": compat_stack["key"]},
    )
    try:
        assert _await_line(client, "subscribed", timeout=180)["ok"]
    finally:
        client.terminate()
        try:
            client.wait(timeout=10)
        except subprocess.TimeoutExpired:
            client.kill()

    with db.connection() as conn:
        after = db.one(
            conn,
            "SELECT realtime_worker_state, realtime_registered_at, "
            "       realtime_worker_last_active_at FROM projects WHERE id = %s",
            (project_id,),
        )
    assert after["realtime_worker_state"] == "RUNNING"
    assert after["realtime_registered_at"] is not None
    # Without this the sleep policy would reclaim a project that is being used.
    assert after["realtime_worker_last_active_at"] is not None


def _await_line(client: subprocess.Popen, name: str, *, timeout: float) -> dict:
    """Read the client's JSON lines until the named one arrives."""
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while time.monotonic() < deadline:
        line = client.stdout.readline()
        if not line:
            stderr = client.stderr.read()
            raise AssertionError(
                f"the client exited before printing {name!r}. It printed {seen}\n{stderr}"
            )
        line = line.strip()
        if line.startswith("{"):
            row = json.loads(line)
            seen.append(row)
            if row.get("name") == name:
                return row
    raise AssertionError(f"the client never printed {name!r} within {timeout:.0f}s")
