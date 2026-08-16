"""A real Realtime instance, started the way the node starts one.

Phase 06 slice 5. Everything in this file runs against upstream
`supabase/realtime` in a container, a tenant provisioned by the platform's own
code, and a cluster with `wal_level = logical`. Slice 3's tests proxied to a
stub, which could not disagree with what the gateway sent it; the point of
these is that the real server can.

Three properties are worth the cost of the container:

- **Postgres Changes actually arrive**, through the replicator credential and
  the two slots the server creates for itself.
- **The container cannot reach the node's loopback.** That is the containment
  the whole arrangement rests on, and a control nobody exercises is a comment.
- **An instance serves its own tenant and no other.** Each has exactly one
  registered, so a connection carrying another project's hostname is refused
  rather than served from the wrong database.

Needs the Realtime cluster (`scripts/realtime-test-cluster.sh`, which also
builds the data address), Podman, and the pinned image. CI sets
MALUDB_REQUIRE_REALTIME_SERVER so an absent one fails the run rather than
skipping it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket as socket_module
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest
import websockets.sync.client as ws_client

from services.control_plane import db, provisioning, realtime
from services.control_plane import realtime_workers as rtw
from tests.conftest import requires_db
from tests.test_realtime_enablement import (  # noqa: F401 - pytest resolves these as fixtures
    REALTIME_DSN,
    node,
    tenant,
)

DATA_HOST = os.environ.get("MALUDB_REALTIME_DB_HOST", "").strip()
DATA_PORT = int(os.environ.get("MALUDB_REALTIME_DB_PORT", "5433"))
IMAGE = os.environ.get("MALUDB_REALTIME_IMAGE", "docker.io/supabase/realtime:v2.110.0")

# Outside `workers.PORT_RANGE`, so an allocated worker port can never land on
# one of these, and high enough not to collide with a developer's own services.
TEST_PORT_BASE = 24401


def _image_present() -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(  # noqa: S603 - fixed executable, list arguments
        ["podman", "image", "exists", IMAGE], check=False  # noqa: S607
    ).returncode == 0


pytestmark = [
    requires_db,
    pytest.mark.skipif(
        not REALTIME_DSN,
        reason="MALUDB_REALTIME_NODE_DSN is unset; build one with scripts/realtime-test-cluster.sh",
    ),
    pytest.mark.skipif(
        not DATA_HOST,
        reason=(
            "MALUDB_REALTIME_DB_HOST is unset. A Realtime container is given no route to the "
            "node's loopback, so it needs a data address; scripts/realtime-test-cluster.sh "
            "builds one and prints the export."
        ),
    ),
    pytest.mark.skipif(
        not _image_present(),
        reason=f"podman or the pinned image is missing: podman pull {IMAGE}",
    ),
]


class PodmanSupervisor:
    """Starts the container the way the unit does, without systemd.

    Tests cannot `systemctl start`, and a supervisor that started something
    *else* would leave the interesting arguments -- the network mode above all
    -- untested. So this runs `realtime_workers.podman_args`, which
    `test_realtime_workers.py` asserts is the same command line the unit runs.
    """

    def __init__(self, settings_for, config_dir: Path) -> None:
        self._settings_for = settings_for
        self._config_dir = config_dir
        self._processes: dict[str, subprocess.Popen] = {}

    def start(self, project_ref: str) -> None:
        self.stop(project_ref)
        self._processes[project_ref] = subprocess.Popen(  # noqa: S603 - argv from podman_args
            rtw.podman_args(self._settings_for(project_ref), config_dir=self._config_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self, project_ref: str) -> None:
        """SIGTERM the `podman run` process, exactly as the unit's KillSignal does.

        `podman rm -f` on its own leaves the attached `podman run` waiting, which
        is worth knowing: a supervisor that only removed the container would
        report a stop that had not happened.
        """
        process = self._processes.pop(project_ref, None)
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        names = rtw.RealtimeNames.for_ref(project_ref)
        subprocess.run(  # noqa: S603 - fixed executable, list arguments
            ["podman", "rm", "-f", "--ignore", names.container],  # noqa: S607
            check=False, capture_output=True,
        )

    def is_active(self, project_ref: str) -> bool:
        process = self._processes.get(project_ref)
        return process is not None and process.poll() is None


@dataclasses.dataclass
class Instance:
    project_id: uuid.UUID
    ref: str
    settings: rtw.RealtimeSettings
    supervisor: PodmanSupervisor
    startup_seconds: float


@pytest.fixture
def realtime_config(app_config):
    """Configuration pointing at the test cluster's data address."""
    return dataclasses.replace(
        app_config,
        realtime_db_host=DATA_HOST,
        realtime_db_port=DATA_PORT,
        realtime_image=IMAGE,
        realtime_memory_max="512m",
    )


def _admin_conn():
    return psycopg.connect(REALTIME_DSN, row_factory=psycopg.rows.dict_row)


def _connect_to(database: str):
    parsed = psycopg.conninfo.conninfo_to_dict(REALTIME_DSN)
    parsed["dbname"] = database
    return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)


@pytest.fixture
def running_instance(tenant, key_ring, realtime_config, tmp_path):  # noqa: F811 - fixture
    """A provisioned tenant with Realtime enabled and its server running."""
    made: list[Instance] = []

    def make(ref: str) -> Instance:
        project_id = tenant(ref)
        with db.connection() as conn, _admin_conn() as admin:
            realtime.enable(
                conn, admin, project_id=project_id, key_ring=key_ring,
                tenant_connect=_connect_to, metadata_connect=_connect_to,
            )
            # A fixed port rather than an allocated one, so a developer's
            # occupied port range cannot fail this for an unrelated reason.
            db.execute(
                conn, "UPDATE projects SET realtime_port = %s WHERE id = %s",
                (TEST_PORT_BASE + len(made), project_id),
            )
            conn.commit()

        def settings_for(_project_ref: str) -> rtw.RealtimeSettings:
            with db.connection() as conn:
                password = provisioning.load_credential(
                    conn, project_id=project_id,
                    credential_type=rtw.METADATA_CREDENTIAL_TYPE, key_ring=key_ring,
                )
                return rtw.settings_for(
                    conn, project_id=project_id, key_ring=key_ring,
                    config=realtime_config, metadata_password=password,
                )

        supervisor = PodmanSupervisor(settings_for, tmp_path)
        with db.connection() as conn:
            elapsed = rtw.start_worker(
                conn, project_id=project_id, key_ring=key_ring, config=realtime_config,
                supervisor=supervisor, config_dir=tmp_path,
            )
        instance = Instance(
            project_id=project_id, ref=ref, settings=settings_for(ref),
            supervisor=supervisor, startup_seconds=elapsed,
        )
        made.append(instance)
        return instance

    yield make

    for instance in made:
        with db.connection() as conn, _admin_conn() as admin:
            try:
                rtw.teardown(
                    conn, admin, project_id=instance.project_id, key_ring=key_ring,
                    supervisor=instance.supervisor,
                )
            finally:
                instance.supervisor.stop(instance.ref)


# --------------------------------------------------------------------------
# The container's reach
# --------------------------------------------------------------------------


def _curl_exit(container: str, url: str) -> str:
    probe = subprocess.run(  # noqa: S603 - fixed executable, list arguments
        ["podman", "exec", container, "curl", "-s", "-o", "/dev/null",  # noqa: S607
         "-w", "%{exitcode}", "--max-time", "4", url],
        check=False, capture_output=True, text=True,
    )
    return probe.stdout.strip()


def test_the_container_cannot_reach_the_nodes_loopback(running_instance):
    """The containment this whole arrangement rests on.

    Measured the way the problem was found: with `allow_host_loopback` the
    instance reached `127.0.0.1:5432` -- a different cluster, carrying tenants
    this project has nothing to do with -- and every loopback-bound worker
    besides. A tenant's PostgREST answers anonymous requests through
    `db-anon-role` to anything that can open its port, so that reach is not
    theoretical.

    curl's exit code is the assertion: 7 is "could not connect", 52 is "empty
    reply", which is what PostgreSQL says to an HTTP request.
    """
    instance = running_instance("rtsv0001")
    container = instance.settings.names.container

    assert _curl_exit(container, f"http://10.0.2.2:{DATA_PORT}/") == "7", (
        "the container reached the node's loopback; with that reach a compromised "
        "Realtime instance can read every other tenant's anon-visible data"
    )
    # The address it *is* meant to reach still works, or the assertion above
    # would pass on a container with no network at all.
    assert _curl_exit(container, f"http://{DATA_HOST}:{DATA_PORT}/") == "52", (
        "the data address is not reachable from the container"
    )


# --------------------------------------------------------------------------
# Registration and delivery
# --------------------------------------------------------------------------


def test_the_tenant_is_registered_and_the_platform_records_it(running_instance):
    instance = running_instance("rtsv0002")
    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT realtime_worker_state, realtime_registered_at FROM projects WHERE id = %s",
            (instance.project_id,),
        )
    assert row["realtime_worker_state"] == "RUNNING"
    assert row["realtime_registered_at"] is not None, (
        "an instance running with no tenant registered accepts a handshake and then refuses "
        "every channel, which is the silent failure this phase keeps designing against"
    )
    # Measured at 9.0s on the development node: a BEAM boot plus the server's own
    # migrations, against PostgREST's 320 ms and GoTrue's 175-268 ms. That is the
    # number `maintenance.REALTIME_IDLE_MINUTES` is set from -- a wake this
    # expensive is paid by a client opening a socket, not by a retryable request.
    # A generous bound, so this fails on a regression rather than on a slow host.
    assert instance.startup_seconds < 30, (
        f"Realtime took {instance.startup_seconds:.1f}s to become ready; the sleep policy is "
        "sized on roughly nine"
    )


def test_postgres_changes_reach_a_subscriber(running_instance):
    """The phase's point, from a client to a tenant table and back.

    Not the official client -- that is `test_realtime_compat.py`, through the
    gateway. This is the server and the tenant alone, so a failure here means
    the platform's configuration and a failure there means the gateway.
    """
    instance = running_instance("rtsv0003")
    delivered = _subscribe_and_insert(instance)
    assert delivered["data"]["type"] == "INSERT"
    assert delivered["data"]["record"]["body"] == "slice 5"

    # ADR-034's two slots, created by the server rather than by the platform,
    # and named with the suffix the platform reserved capacity against.
    with psycopg.connect(REALTIME_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slot_name, plugin FROM pg_replication_slots WHERE database = %s",
                (f"mldb_{instance.ref}",),
            )
            slots = {row["slot_name"]: row["plugin"] for row in cur.fetchall()}
    assert set(slots) == set(realtime.slot_names_for(instance.ref))
    assert slots[f"supabase_realtime_replication_slot_{instance.ref}"] == "wal2json", (
        "without wal2json a client subscribes successfully and no event is ever delivered"
    )


def test_an_instance_serves_only_its_own_tenant(running_instance):
    """Cross-tenant isolation at the server, not at the gateway.

    Slice 3 proved a key for one project cannot open a socket for another. This
    is the other half, and it needs a real server: each instance has exactly one
    tenant registered, so a connection carrying a different project's hostname
    finds nothing rather than being served from the wrong database.

    The token is this project's own and genuinely valid, which is what makes the
    test about the tenant lookup rather than about the signature.
    """
    instance = running_instance("rtsv0004")
    token = _tenant_token(instance)

    with pytest.raises(Exception) as refused:  # noqa: PT011 - the library raises several types
        with _open_socket(instance, token, host_ref="rtsv0005") as socket:
            socket.recv(timeout=5)
    assert "401" in str(refused.value) or "403" in str(refused.value) or "404" in str(refused.value), (
        f"expected the server to refuse an unknown tenant, got {refused.value!r}"
    )


def test_a_slept_instance_leaves_the_project_intact(running_instance):
    """Stopping is sleep, not teardown.

    ADR-005 and ADR-022: a slept project stays a project. Here that means the
    tenant stays registered and the slots stay reserved -- only the ~146 MB goes
    back, which is the largest single allocation a node can reclaim.
    """
    instance = running_instance("rtsv0006")
    with db.connection() as conn:
        rtw.stop_worker(conn, project_id=instance.project_id, supervisor=instance.supervisor)
        row = db.one(
            conn,
            "SELECT realtime_worker_state, realtime_enabled, realtime_registered_at, "
            "       realtime_port FROM projects WHERE id = %s",
            (instance.project_id,),
        )
    assert row["realtime_worker_state"] == "STOPPED"
    assert row["realtime_enabled"] is True
    assert row["realtime_registered_at"] is not None
    assert row["realtime_port"] is not None


def test_teardown_removes_the_metadata_database(running_instance, key_ring):
    """Turning Realtime off reduces what exists, not only what is reachable.

    The metadata database holds this project's replicator credential, encrypted
    under a key derived from a secret the platform still has. Leaving it behind
    leaves a copy of the most valuable credential the platform issues on a node
    with no further use for it.
    """
    instance = running_instance("rtsv0007")
    names = rtw.RealtimeNames.for_ref(instance.ref)
    assert _exists("pg_database", "datname", names.metadata_database)

    with db.connection() as conn, _admin_conn() as admin:
        rtw.teardown(
            conn, admin, project_id=instance.project_id, key_ring=key_ring,
            supervisor=instance.supervisor,
        )

    assert not _exists("pg_database", "datname", names.metadata_database)
    assert not _exists("pg_roles", "rolname", names.metadata_role)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _exists(catalogue: str, column: str, value: str) -> bool:
    with psycopg.connect(REALTIME_DSN) as conn:
        with conn.cursor() as cur:
            # Catalogue and column are literals in this file, never input.
            cur.execute(f"SELECT 1 FROM {catalogue} WHERE {column} = %s", (value,))  # noqa: S608
            return cur.fetchone() is not None


def _tenant_token(instance: Instance, role: str = "service_role") -> str:
    import jwt as pyjwt

    now = int(time.time())
    return pyjwt.encode(
        {"role": role, "iat": now, "exp": now + 900},
        instance.settings.jwt_secret,
        algorithm="HS256",
    )


def _open_socket(instance: Instance, token: str, *, host_ref: str | None = None):
    """Connect as the gateway would: tenant name in the URI, socket to loopback.

    The hostname and the address are two different things, and conflating them
    is the bug `sockets.open_upstream` documents: upstream resolves the tenant
    from the first label of the `Host` header, and the connection still has to
    reach a loopback port.
    """
    ref = host_ref or instance.ref
    uri = f"ws://{ref}.maludb.local/socket/websocket?apikey={token}&vsn=1.0.0"
    return ws_client.connect(
        uri,
        sock=socket_module.create_connection(("127.0.0.1", instance.settings.port), timeout=20),
        open_timeout=20,
    )


def _subscribe_and_insert(instance: Instance) -> dict:
    """Join a channel, write a row, and return the change that came back."""
    database = f"mldb_{instance.ref}"
    with _connect_to(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS public.notes (id serial primary key, body text)")
        conn.execute("ALTER TABLE public.notes REPLICA IDENTITY FULL")
        try:
            conn.execute("ALTER PUBLICATION supabase_realtime ADD TABLE public.notes")
        except psycopg.errors.DuplicateObject:
            pass

    token = _tenant_token(instance)
    with _open_socket(instance, token) as socket:
        socket.send(json.dumps({
            "topic": "realtime:notes",
            "event": "phx_join",
            "payload": {
                "config": {
                    "postgres_changes": [{"event": "*", "schema": "public", "table": "notes"}],
                    "private": False,
                },
                "access_token": token,
            },
            "ref": "1",
        }))

        deadline = time.monotonic() + 40
        inserted = False
        while time.monotonic() < deadline:
            message = json.loads(socket.recv(timeout=10))
            if message.get("event") == "system" and not inserted:
                inserted = True
                with _connect_to(database) as conn:
                    conn.execute("INSERT INTO public.notes (body) VALUES ('slice 5')")
            if message.get("event") == "postgres_changes":
                return message["payload"]
    raise AssertionError(
        "no change was delivered. A client reports SUBSCRIBED and then receives nothing when "
        "the slot name collides or wal2json is missing -- both silent from the client's side."
    )
