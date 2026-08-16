"""The Realtime socket surface, written from the attacker's side.

Phase 06 slice 3. The gateway had never proxied a WebSocket before this, and a
defect here is the same class of problem as a defect in the request path: a
cross-tenant read by anyone on the internet. So the tests are the same shape as
`tests/test_gateway.py` -- present a valid key against the wrong project, forge
a Host, exhaust the connection limit, keep using a revoked key -- rather than a
happy path with edge cases bolted on.

The upstream is a recording WebSocket server rather than a real Realtime, for
the reason the HTTP tests use a recorder rather than a real PostgREST: what is
under test is *what the gateway forwards and what it refuses*, and a stub can be
asked precisely that. It also means these run anywhere, which matters because
upstream Realtime ships as a container image and this host has no container
runtime -- see `docs/REALTIME.md`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from urllib.parse import parse_qsl

import jwt
import psycopg
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from services.control_plane import (
    api_keys,
    db,
    identity,
    provisioning,
    realtime_workers,
    workers,
)
from services.gateway import limits, sockets
from services.gateway.app import Gateway, create_app
from tests.conftest import TEST_CREDENTIAL, TEST_PEPPER, requires_db

pytestmark = [requires_db]

GATEWAY_DOMAIN = "maludb.local"


class _StubRealtime:
    """Records every handshake it is given, and echoes what it is sent."""

    def __init__(self) -> None:
        self.connections: list[dict] = []
        self.frames: list[str] = []
        self.port: int = 0

    async def handler(self, connection) -> None:
        self.connections.append(
            {
                "path": connection.request.path,
                "headers": {k.lower(): v for k, v in connection.request.headers.items()},
            }
        )
        async for message in connection:
            self.frames.append(message)
            await connection.send(f"echo:{message}")


@pytest.fixture
def realtime_upstream():
    """A WebSocket server on its own thread and event loop.

    Its own loop because the gateway under `TestClient` runs in one of its own,
    and a stub sharing it would deadlock the moment the proxy waited on a frame
    the stub had not sent yet.
    """
    stub = _StubRealtime()
    loop = asyncio.new_event_loop()
    started = threading.Event()
    # Shut down by resolving a future the server waits on, rather than by
    # stopping the loop underneath it. Stopping the loop leaves
    # `run_until_complete` raising into the thread, which pytest reports as an
    # unhandled thread exception -- noise that would eventually hide a real one.
    shutdown: asyncio.Future = loop.create_future()

    async def main() -> None:
        async with serve(stub.handler, "127.0.0.1", 0, ping_interval=None) as server:
            stub.port = server.sockets[0].getsockname()[1]
            started.set()
            await shutdown

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5), "the stub Realtime server did not start"

    yield stub

    loop.call_soon_threadsafe(lambda: shutdown.done() or shutdown.set_result(None))
    thread.join(timeout=5)


@pytest.fixture
def rt_project(db_pool, key_ring, realtime_upstream):
    """A serving project with Realtime enabled and a generous connection limit.

    Its worker is recorded as already RUNNING on the stub's port. ADR-034 makes
    the port a property of the project rather than of the node, so a fixture
    that left it unset would exercise the wake path in every test rather than
    the one written for it.
    """

    def make(
        ref: str, *, realtime_enabled: bool = True, realtime_connections: int = 10,
        status: str = "ACTIVE", realtime_port: int | None = None,
        worker_state: str = "RUNNING",
    ) -> uuid.UUID:
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name,config_json) VALUES (%s,'Test',%s) "
                "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
                (f"plan-{ref}",
                 psycopg.types.json.Jsonb({"limits": {"realtime_connections": realtime_connections}})),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                " database_name, realtime_enabled, realtime_port, realtime_worker_state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (project_id, org, ref, ref, plan, status, f"mldb_{ref}", realtime_enabled,
                 realtime_upstream.port if realtime_port is None else realtime_port,
                 worker_state),
            )
            workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
            conn.commit()
        return project_id

    return make


@pytest.fixture
def client(app_config, key_ring, realtime_upstream):
    gateway = Gateway(config=app_config, key_ring=key_ring, wake_sleeping=False)
    with TestClient(create_app(gateway)) as test_client:
        yield test_client, gateway


def _issue(project_id: uuid.UUID, key_type: str, key_ring) -> str:
    with db.connection() as conn:
        issued = api_keys.create(
            conn, project_id=project_id, key_type=key_type, pepper=TEST_PEPPER,
            key_ring=key_ring if key_type == api_keys.PUBLISHABLE else None,
        )
        conn.commit()
    return issued.plaintext


def _socket(test_client, ref: str, key: str | None, *, path: str = "/realtime/v1/websocket",
            in_query: bool = True, headers: dict | None = None):
    """Open a socket the way the official client does: key in the query string."""
    url = f"{path}?vsn=1.0.0"
    request_headers = {"host": f"{ref}.{GATEWAY_DOMAIN}"}
    if key is not None:
        if in_query:
            url = f"{url}&apikey={key}"
        else:
            request_headers["apikey"] = key
    request_headers.update(headers or {})
    return test_client.websocket_connect(url, headers=request_headers)


# --------------------------------------------------------------------------
# The controls this slice exists for.
# --------------------------------------------------------------------------


def test_a_key_for_another_project_cannot_open_a_socket(client, rt_project, key_ring):
    """The cross-tenant control, on the new surface.

    The same test Phase 03 slice 3 wrote for the request path, because the
    property is the same and the code enforcing it is entirely new: the project
    comes from the hostname and the key is checked against *that* project. Remove
    the check and this test fails, which is the only reason it is worth having.
    """
    rt_project("gwrt0001")
    other = rt_project("gwrt0002")
    other_key = _issue(other, api_keys.PUBLISHABLE, key_ring)

    test_client, _ = client
    with pytest.raises(WebSocketDisconnect) as refused:
        with _socket(test_client, "gwrt0001", other_key):
            pass
    assert refused.value.code == sockets.CLOSE_POLICY


def test_a_forged_host_cannot_open_a_socket(client, rt_project, key_ring):
    project_id = rt_project("gwrt0003")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    for host in ("gwrt0003.evil.com", "evil.com", "a.gwrt0003.maludb.local"):
        with pytest.raises(WebSocketDisconnect) as refused:
            with test_client.websocket_connect(
                f"/realtime/v1/websocket?apikey={key}", headers={"host": host}
            ):
                pass
        assert refused.value.code == sockets.CLOSE_POLICY


def test_no_key_is_refused(client, rt_project):
    rt_project("gwrt0004")
    test_client, _ = client
    with pytest.raises(WebSocketDisconnect) as refused:
        with _socket(test_client, "gwrt0004", None):
            pass
    assert refused.value.code == sockets.CLOSE_POLICY


def test_a_revoked_key_stops_opening_sockets(client, rt_project, key_ring):
    project_id = rt_project("gwrt0005")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, gateway = client

    with _socket(test_client, "gwrt0005", key) as socket:
        socket.send_text("hello")
        assert socket.receive_text() == "echo:hello"

    with db.connection() as conn:
        db.execute(conn, "UPDATE api_keys SET revoked_at = now() WHERE project_id = %s", (project_id,))
        conn.commit()
    gateway.cache.invalidate_project(project_id)

    with pytest.raises(WebSocketDisconnect) as refused:
        with _socket(test_client, "gwrt0005", key):
            pass
    assert refused.value.code == sockets.CLOSE_POLICY


def test_a_suspended_project_stops_serving_sockets(client, rt_project, key_ring):
    project_id = rt_project("gwrt0006", status="SUSPENDED")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client
    with pytest.raises(WebSocketDisconnect) as refused:
        with _socket(test_client, "gwrt0006", key):
            pass
    assert refused.value.code == sockets.CLOSE_POLICY


def test_a_path_outside_the_realtime_prefix_is_refused_uniformly(client, rt_project, key_ring):
    """Routing happens after authentication here too, so the path is not a probe."""
    project_id = rt_project("gwrt0007")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client
    for path in ("/rest/v1/things", "/nonsense", "/"):
        with pytest.raises(WebSocketDisconnect) as refused:
            with _socket(test_client, "gwrt0007", key, path=path):
                pass
        assert refused.value.code == sockets.CLOSE_POLICY


# --------------------------------------------------------------------------
# Enablement and limits, which are only reachable once a key has been proved.
# --------------------------------------------------------------------------


def test_a_project_without_realtime_enabled_is_told_so(client, rt_project, key_ring):
    """A named reason, because the caller has already proved it holds a key.

    Distinguishing this from an auth failure would be an oracle if it happened
    before authentication. After it, it is the difference between a customer
    running `cp-manage project realtime --enable` and one filing a bug.
    """
    project_id = rt_project("gwrt0008", realtime_enabled=False)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client
    with pytest.raises(WebSocketDisconnect) as refused:
        with _socket(test_client, "gwrt0008", key):
            pass
    assert refused.value.code == sockets.CLOSE_NOT_ENABLED


def test_a_free_project_cannot_open_a_socket_even_if_the_flag_is_set(client, rt_project, key_ring):
    """Defence in depth, and the reason the socket limiter refuses a zero limit.

    `realtime_connections` is 0 on free. The enablement path in slice 2 already
    refuses to turn Realtime on for such a project, so reaching here means the
    flag and the plan disagree -- exactly the state a downgrade produces between
    the plan changing and the next provisioning run applying it.
    """
    project_id = rt_project("gwrt0009", realtime_enabled=True, realtime_connections=0)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client
    with pytest.raises(WebSocketDisconnect) as refused:
        with _socket(test_client, "gwrt0009", key):
            pass
    assert refused.value.code == sockets.CLOSE_LIMIT


def test_the_connection_limit_is_enforced_and_released(client, rt_project, key_ring):
    project_id = rt_project("gwrt0010", realtime_connections=2)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, gateway = client

    with _socket(test_client, "gwrt0010", key), _socket(test_client, "gwrt0010", key):
        assert gateway.socket_limiter.open_sockets(project_id) == 2
        with pytest.raises(WebSocketDisconnect) as refused:
            with _socket(test_client, "gwrt0010", key):
                pass
        assert refused.value.code == sockets.CLOSE_LIMIT

    # Released on close. A slot leaked while the socket is gone is invisible
    # until the project cannot open another one.
    assert gateway.socket_limiter.open_sockets(project_id) == 0
    with _socket(test_client, "gwrt0010", key) as socket:
        socket.send_text("still works")
        assert socket.receive_text() == "echo:still works"


def test_a_refused_socket_never_reaches_the_upstream(client, rt_project, key_ring, realtime_upstream):
    other = rt_project("gwrt0011")
    rt_project("gwrt0012")
    key = _issue(other, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    before = len(realtime_upstream.connections)
    with pytest.raises(WebSocketDisconnect):
        with _socket(test_client, "gwrt0012", key):
            pass
    assert len(realtime_upstream.connections) == before


# --------------------------------------------------------------------------
# What the upstream is told.
# --------------------------------------------------------------------------


def test_the_upstream_sees_the_project_hostname_and_a_minted_token(
    client, rt_project, key_ring, realtime_upstream
):
    """Host identifies the tenant upstream, and it is *reconstructed*, not passed.

    Upstream Realtime resolves a tenant from the subdomain, exactly as it does
    on Supabase. Building it from the validated project ref rather than
    forwarding the client's header means a forged Host cannot name a different
    tenant than the one the key was checked against.
    """
    project_id = rt_project("gwrt0013")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0013", key):
        pass

    seen = realtime_upstream.connections[-1]
    assert seen["headers"]["host"] == f"gwrt0013.{GATEWAY_DOMAIN}"
    # The platform key is ours, not upstream's, and it is opaque to it anyway.
    assert "apikey" not in seen["headers"]

    with db.connection() as conn:
        secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    token = seen["headers"]["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, secret, algorithms=["HS256"])
    # A publishable key is minted an explicit anon token rather than being
    # dropped: unlike PostgREST, Realtime has no db-anon-role to fall back to.
    assert claims["role"] == "anon"
    assert claims["iss"] == "maludb-gateway"


def test_a_secret_key_becomes_a_service_role_token(client, rt_project, key_ring, realtime_upstream):
    project_id = rt_project("gwrt0014")
    key = _issue(project_id, api_keys.SECRET, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0014", key):
        pass

    with db.connection() as conn:
        secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    token = realtime_upstream.connections[-1]["headers"]["authorization"].removeprefix("Bearer ")
    assert jwt.decode(token, secret, algorithms=["HS256"])["role"] == "service_role"


def test_the_gateway_prefix_maps_to_the_upstream_socket_mount(
    client, rt_project, key_ring, realtime_upstream
):
    """`/realtime/v1/*` becomes `/socket/*`, which is not a simple strip.

    Realtime is the one surface that does not serve at its own root: Phoenix
    mounts the socket at `/socket`, and Supabase's own edge makes the same
    substitution -- which is why a client written against Supabase works
    unchanged. Slice 3 stripped the prefix and forwarded `/websocket`, and this
    test asserted that, because the stub accepted any path and could not
    disagree. A real Realtime answers 404 to `/websocket` and 403 to
    `/socket/websocket`, which is how the two were told apart.
    """
    project_id = rt_project("gwrt0015")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0015", key):
        pass

    path = realtime_upstream.connections[-1]["path"]
    assert path.startswith("/socket/websocket?")
    # `vsn` selects the Phoenix serialiser version. Dropping it while rebuilding
    # the query string would change the wire format.
    assert "vsn=1.0.0" in path


def test_the_platform_key_is_not_forwarded_upstream(
    client, rt_project, key_ring, realtime_upstream
):
    """The opaque MaluDB key must not reach the Realtime server.

    It is meaningless there -- ADR-028 keys are opaque, and upstream expects a
    JWT in `?apikey=` -- and forwarding it writes the platform's own credential
    into upstream's logs. The request path already strips the same value from
    the headers; this is the socket's version of that, and the query string is
    an easy place to forget.
    """
    project_id = rt_project("gwrt0021")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0021", key):
        pass

    path = realtime_upstream.connections[-1]["path"]
    assert key not in path

    # Replaced by the minted token, because `?apikey=` is where upstream looks
    # for one and the official client puts it there.
    forwarded = dict(parse_qsl(path.split("?", 1)[1]))
    with db.connection() as conn:
        secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    assert jwt.decode(forwarded["apikey"], secret, algorithms=["HS256"])["role"] == "anon"
    # And it is the same token the Authorization header carries, so the
    # connection does not depend on which upstream reads.
    header_token = realtime_upstream.connections[-1]["headers"]["authorization"].removeprefix(
        "Bearer "
    )
    assert forwarded["apikey"] == header_token


def test_a_forwarding_header_from_the_client_is_not_passed_on(
    client, rt_project, key_ring, realtime_upstream
):
    """The handshake headers are an allowlist, so this is true by construction --
    which is exactly why it is worth a test that would catch someone replacing
    it with a copy-and-filter."""
    project_id = rt_project("gwrt0016")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0016", key,
                 headers={"x-forwarded-for": "10.0.0.1", "x-maludb-internal": "spoofed"}):
        pass

    seen = realtime_upstream.connections[-1]["headers"]
    assert "x-forwarded-for" not in seen
    assert "x-maludb-internal" not in seen


# --------------------------------------------------------------------------
# Protocol shape.
# --------------------------------------------------------------------------


def test_frames_flow_in_both_directions(client, rt_project, key_ring):
    project_id = rt_project("gwrt0017")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0017", key) as socket:
        for message in ("first", "second", "third"):
            socket.send_text(message)
            assert socket.receive_text() == f"echo:{message}"


def test_the_key_may_also_be_a_header_for_a_server_side_client(client, rt_project, key_ring):
    """The query string is required for browsers; headers still work for Node."""
    project_id = rt_project("gwrt0018")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0018", key, in_query=False) as socket:
        socket.send_text("hi")
        assert socket.receive_text() == "echo:hi"


def test_an_unreachable_upstream_closes_cleanly_and_releases_the_slot(
    app_config, rt_project, key_ring
):
    # A port with nothing behind it. Port 1 is reserved and never a Realtime.
    project_id = rt_project("gwrt0019", realtime_port=1)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    gateway = Gateway(config=app_config, key_ring=key_ring, wake_sleeping=False)
    with TestClient(create_app(gateway)) as test_client:
        with pytest.raises(WebSocketDisconnect) as refused:
            with _socket(test_client, "gwrt0019", key):
                pass
        assert refused.value.code == sockets.CLOSE_UPSTREAM_UNAVAILABLE
    assert gateway.socket_limiter.open_sockets(project_id) == 0


def test_plain_http_to_the_realtime_prefix_still_says_not_available(client, rt_project, key_ring):
    """Slice 3 serves this surface over a socket only.

    A client that did not upgrade has not asked for anything the platform can
    answer, so the HTTP path's existing 404 is still the right response rather
    than something that hints at a socket it did not open.
    """
    project_id = rt_project("gwrt0020")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client
    response = test_client.get(
        "/realtime/v1/websocket",
        headers={"host": f"gwrt0020.{GATEWAY_DOMAIN}", "apikey": key},
    )
    assert response.status_code == 404
    assert "not available yet" in response.json()["message"]


# --------------------------------------------------------------------------
# The limiter itself.
# --------------------------------------------------------------------------


def test_the_socket_limiter_refuses_a_zero_limit():
    """The opposite of how the request limiter treats a missing rate, on purpose.

    Zero here is not a misconfiguration to fail open on. It is the free tier.
    """
    limiter = limits.SocketLimiter()
    assert not limiter.acquire(uuid.uuid4(), limit=0).allowed


def test_the_socket_limiter_does_not_go_negative():
    limiter = limits.SocketLimiter()
    project = uuid.uuid4()
    limiter.release(project)
    limiter.release(project)
    assert limiter.open_sockets(project) == 0
    assert limiter.acquire(project, limit=1).allowed
    assert not limiter.acquire(project, limit=1).allowed


# --------------------------------------------------------------------------
# The key inside the frame, and the wake.
# --------------------------------------------------------------------------


def test_the_key_in_a_channel_join_becomes_a_jwt(client, rt_project, key_ring, realtime_upstream):
    """The compatibility defect that only the official client could find.

    supabase-js sends its key twice: in the query string, which the gateway
    already replaces, and again as `access_token` in the payload of every
    `phx_join`. On Supabase the anon key *is* a JWT, so the copy inside the
    frame validates; ADR-028 made MaluDB's keys opaque, so it does not, and
    upstream answers `MalformedJWT`. The socket connects and every channel
    fails.
    """
    project_id = rt_project("gwrt0021")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    join = json.dumps({
        "topic": "realtime:notes",
        "event": "phx_join",
        "payload": {"config": {"private": False}, "access_token": key},
        "ref": "1",
    })
    with _socket(test_client, "gwrt0021", key) as socket:
        socket.send_text(join)
        socket.receive_text()

    forwarded = json.loads(realtime_upstream.frames[-1])
    sent = forwarded["payload"]["access_token"]
    assert sent != key, "the customer's opaque key reached upstream unchanged"
    claims = jwt.decode(sent, _jwt_secret(project_id, key_ring), algorithms=["HS256"])
    assert claims["role"] == "anon"


def test_the_second_phoenix_serialiser_is_handled_too(client, rt_project, key_ring, realtime_upstream):
    """vsn=2.0.0 sends an array, and it is what the official client uses.

    An implementation that only understood the object form would pass every
    test written by hand and fail every real client.
    """
    project_id = rt_project("gwrt0022")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    join = json.dumps(["1", "1", "realtime:notes", "phx_join", {"access_token": key}])
    with _socket(test_client, "gwrt0022", key) as socket:
        socket.send_text(join)
        socket.receive_text()

    forwarded = json.loads(realtime_upstream.frames[-1])
    assert forwarded[4]["access_token"] != key
    assert jwt.decode(
        forwarded[4]["access_token"], _jwt_secret(project_id, key_ring), algorithms=["HS256"]
    )["role"] == "anon"


def test_a_frame_that_is_not_a_join_passes_through_untouched(
    client, rt_project, key_ring, realtime_upstream
):
    # The gateway is not a Phoenix client and should not become one: a
    # heartbeat, an ack and anything it cannot parse are forwarded byte for
    # byte.
    project_id = rt_project("gwrt0023")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    with _socket(test_client, "gwrt0023", key) as socket:
        for frame in ("not json at all", json.dumps({"event": "heartbeat", "payload": {}})):
            socket.send_text(frame)
            socket.receive_text()

    assert realtime_upstream.frames[-2:] == [
        "not json at all", json.dumps({"event": "heartbeat", "payload": {}})
    ]


def test_an_end_user_token_is_not_rewritten(client, rt_project, key_ring, realtime_upstream):
    """Only the exact string the caller authenticated with is replaced.

    A signed-in application sends its user's JWT as `access_token`, and
    rewriting that would replace a token carrying a user identity with one
    carrying only a role -- which would quietly turn every RLS policy that reads
    `auth.uid()` into one that matches nothing.
    """
    project_id = rt_project("gwrt0024")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    test_client, _ = client

    end_user = jwt.encode(
        {"role": "authenticated", "sub": "user-1"}, "someone-elses-secret", algorithm="HS256"
    )
    join = json.dumps({"event": "phx_join", "payload": {"access_token": end_user}})
    with _socket(test_client, "gwrt0024", key) as socket:
        socket.send_text(join)
        socket.receive_text()

    assert json.loads(realtime_upstream.frames[-1])["payload"]["access_token"] == end_user


def test_a_sleeping_project_is_asked_to_come_back_rather_than_held(
    app_config, rt_project, key_ring, monkeypatch
):
    """A wake takes about nine seconds; the official client waits ten.

    So the socket is closed with 1013 and the wake runs in the background,
    which phoenix.js answers by reconnecting -- the customer's second attempt
    lands on a ready instance. Holding the connection instead fails the same
    connection, ten seconds later, with nothing to show for the wait.
    """
    project_id = rt_project("gwrt0025", worker_state="STOPPED")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    woken: list[uuid.UUID] = []

    def fake_start(conn, *, project_id, **kwargs):  # noqa: ARG001 - signature match
        woken.append(project_id)
        return 0.0

    monkeypatch.setattr(realtime_workers, "start_worker", fake_start)

    class NullSupervisor:
        def start(self, project_ref: str) -> None: ...
        def stop(self, project_ref: str) -> None: ...
        def is_active(self, project_ref: str) -> bool:
            return False

    gateway = Gateway(
        config=app_config, key_ring=key_ring,
        realtime_supervisor=NullSupervisor(), wake_sleeping=True,
    )
    with TestClient(create_app(gateway)) as test_client:
        with pytest.raises(WebSocketDisconnect) as refused:
            with _socket(test_client, "gwrt0025", key):
                pass
    assert refused.value.code == sockets.CLOSE_TRY_AGAIN
    # The wake is a background thread, so it is observed rather than awaited.
    for _ in range(100):
        if woken:
            break
        time.sleep(0.05)
    assert woken == [project_id], "the connection was refused without starting the instance"


def _jwt_secret(project_id, key_ring) -> str:
    with db.connection() as conn:
        return provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
