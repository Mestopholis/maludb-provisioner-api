"""The public gateway.

This is the component where a defect is a cross-tenant read by anyone on the
internet, so the tests are written from the attacker's side: present a valid
key against the wrong project, forge a Host, forge forwarding headers, keep
using a revoked key. Each of those is a test rather than an assumption.

The upstream is a recording HTTP server rather than a real PostgREST, because
what is under test here is *what the gateway forwards*, and a stub can be
asked precisely that. Slice 4 puts the official client against a real one.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from services.control_plane import api_keys, db, identity, provisioning, workers
from services.gateway import keys as gateway_keys
from services.gateway import routing
from services.gateway.app import Gateway, create_app
from tests.conftest import TEST_CREDENTIAL, TEST_PEPPER, requires_db

pytestmark = [requires_db]

GATEWAY_DOMAIN = "maludb.local"


# -- hostname routing ------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("abcd1234.maludb.local", "abcd1234"),
        ("ABCD1234.maludb.local", "abcd1234"),   # DNS is case-insensitive
        ("abcd1234.maludb.local:443", "abcd1234"),
        ("abcd1234.maludb.local.", "abcd1234"),  # trailing root dot
    ],
)
def test_a_project_hostname_resolves(host, expected):
    assert routing.project_ref_from_host(host, gateway_domain=GATEWAY_DOMAIN) == expected


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "evil.com",
        "maludb.local",
        # The suffix-confusion attack: our domain appears, but as a prefix of
        # theirs. A naive `in` or `startswith` check accepts this.
        "abcd1234.maludb.local.evil.com",
        "abcd1234.maludb.localevil.com",
        # A wildcard certificate covers one label; accepting more means a name
        # that resolves differently for the proxy and for us.
        "a.abcd1234.maludb.local",
        "AB.maludb.local",                 # too short to be a ref
        "abcd 1234.maludb.local",
        "abcd1234'.maludb.local",
        "[::1]",
        ".maludb.local",
    ],
)
def test_a_hostile_host_header_never_resolves(host):
    with pytest.raises(routing.RoutingError):
        routing.project_ref_from_host(host, gateway_domain=GATEWAY_DOMAIN)


# -- fixtures --------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    """Stands in for PostgREST and remembers exactly what it was sent."""

    received: list[dict] = []

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        type(self).received.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": self.rfile.read(length).decode() if length else "",
            }
        )
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        # A hop-by-hop header the gateway must not pass back to the client.
        self.send_header("connection", "keep-alive")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_DELETE = _handle  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def gateway_project(db_pool, key_ring, upstream):
    """A serving project whose worker is the recording upstream."""

    def make(ref: str, *, auth_enabled: bool = True) -> uuid.UUID:
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES (%s,'Test') "
                "ON CONFLICT (code) DO UPDATE SET name='Test' RETURNING id",
                (f"plan-{ref}",),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                "database_name, api_port, worker_state, auth_port, auth_worker_state, auth_enabled) "
                "VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,'RUNNING',%s,'RUNNING',%s)",
                (project_id, org, ref, ref, plan, f"mldb_{ref}", upstream.server_port,
                 upstream.server_port, auth_enabled),
            )
            workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
            conn.commit()
        return project_id

    return make


@pytest.fixture
def client(app_config, key_ring):
    gateway = Gateway(
        config=app_config,
        key_ring=key_ring,
        # Waking is exercised separately; here the worker is already "running".
        wake_sleeping=False,
        client=httpx.AsyncClient(timeout=10),
    )
    with TestClient(create_app(gateway)) as test_client:
        yield test_client, gateway


def _issue(project_id: uuid.UUID, key_type: str, key_ring) -> str:
    with db.connection() as conn:
        issued = api_keys.create(
            conn,
            project_id=project_id,
            key_type=key_type,
            pepper=TEST_PEPPER,
            key_ring=key_ring if key_type == api_keys.PUBLISHABLE else None,
        )
        conn.commit()
    return issued.plaintext


def _get(client, ref: str, key: str | None, path: str = "/rest/v1/things", **kwargs):
    headers = {"host": f"{ref}.{GATEWAY_DOMAIN}"}
    if key is not None:
        headers["apikey"] = key
    headers.update(kwargs.pop("headers", {}))
    return client.get(path, headers=headers, **kwargs)


# -- ADR-008: the control this slice exists for ---------------------------


def test_a_key_for_another_project_is_refused(client, gateway_project, key_ring):
    """The cross-tenant control, end to end through the gateway. A valid key
    for project A presented against project B's hostname must be refused."""
    test_client, _ = client
    a = gateway_project("gw000001")
    gateway_project("gw000002")
    key_a = _issue(a, api_keys.SECRET, key_ring)

    assert _get(test_client, "gw000001", key_a).status_code == 200
    hostile = _get(test_client, "gw000002", key_a)
    assert hostile.status_code == 401, "a key for one project reached another"


def test_the_refused_request_never_reaches_the_upstream(client, gateway_project, key_ring):
    """Refusing after proxying would still have served the data."""
    test_client, _ = client
    a = gateway_project("gw000003")
    gateway_project("gw000004")
    key_a = _issue(a, api_keys.SECRET, key_ring)
    _Recorder.received = []
    _get(test_client, "gw000004", key_a)
    assert _Recorder.received == []


@pytest.mark.parametrize(
    "case",
    ["unknown-key", "no-key", "wrong-domain", "unknown-project", "revoked"],
)
def test_every_refusal_looks_the_same(client, gateway_project, key_ring, case):
    """A distinguishable failure is an oracle for which refs and keys exist."""
    test_client, _ = client
    project_id = gateway_project("gw000005")
    good = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    if case == "unknown-key":
        response = _get(test_client, "gw000005", "mldb_secret_0badc0debadc0debadc0de")
    elif case == "no-key":
        response = _get(test_client, "gw000005", None)
    elif case == "wrong-domain":
        response = test_client.get("/rest/v1/things", headers={"host": "gw000005.evil.com", "apikey": good})
    elif case == "unknown-project":
        response = _get(test_client, "gw999999", good)
    else:
        with db.connection() as conn:
            row = db.one(conn, "SELECT id FROM api_keys WHERE project_id = %s", (project_id,))
            api_keys.revoke(conn, key_id=row["id"], project_id=project_id)
            conn.commit()
        response = _get(test_client, "gw000005", good)

    assert response.status_code == 401
    assert response.json() == {"message": "invalid project or API key"}


def test_a_valid_key_reaches_the_upstream(client, gateway_project, key_ring):
    test_client, _ = client
    project_id = gateway_project("gw000006")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    response = _get(test_client, "gw000006", key, path="/rest/v1/things")
    assert response.status_code == 200
    # The surface prefix belongs to the gateway. PostgREST serves at its own
    # root and answers PGRST125 for anything else, which is what the
    # compatibility suite hit before this was stripped -- so asserting the
    # stripping is asserting that every client call works at all.
    assert _Recorder.received[0]["path"] == "/things"


def test_a_query_string_survives_the_prefix_strip(client, gateway_project, key_ring):
    """PostgREST expresses filters, ordering and ranges entirely in the query
    string, so losing it would break every call that is not a bare select."""
    test_client, _ = client
    project_id = gateway_project("gw00006q")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    _get(test_client, "gw00006q", key, path="/rest/v1/things?tier=eq.free&order=name.asc")
    assert _Recorder.received[0]["path"] == "/things?tier=eq.free&order=name.asc"


@pytest.mark.parametrize("path", ["/", "/things", "/rest/v2/things", "/healthz", "/rest"])
def test_an_unrouted_path_is_not_proxied(client, gateway_project, key_ring, path):
    """Only declared surfaces reach a worker. Forwarding anything else makes
    the gateway an open proxy to an internal service."""
    test_client, _ = client
    project_id = gateway_project(f"gw00006{abs(hash(path)) % 10}")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    response = _get(test_client, f"gw00006{abs(hash(path)) % 10}", key, path=path)
    assert response.status_code == 404
    assert _Recorder.received == []


def test_the_auth_surface_is_routed_to_its_own_worker(client, gateway_project, key_ring):
    """`/auth/v1` used to answer a deliberate 404. It now proxies, and the
    prefix is stripped for the same reason `/rest/v1` is: GoTrue serves at its
    own root, so forwarding the path verbatim would 404 every call."""
    test_client, _ = client
    project_id = gateway_project("gw00000j")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    response = _get(test_client, "gw00000j", key, path="/auth/v1/settings")
    assert response.status_code == 200
    assert _Recorder.received[-1]["path"] == "/settings"


def test_the_auth_surface_is_404_when_the_project_has_not_enabled_it(
    client, gateway_project, key_ring
):
    """ADR-022: Auth is opt-in, because the worker is 17.6 MB of the 31.8 MB a
    warm project costs. A project without it has nothing to route to."""
    test_client, _ = client
    project_id = gateway_project("gw00000k", auth_enabled=False)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    before = len(_Recorder.received)
    response = _get(test_client, "gw00000k", key, path="/auth/v1/settings")
    assert response.status_code == 404
    assert len(_Recorder.received) == before, "a disabled surface still reached an upstream"


def test_enabling_auth_is_not_visible_to_an_unauthenticated_caller(
    client, gateway_project, key_ring
):
    """The enabled check happens after authentication, so it cannot be used to
    survey which projects run Auth."""
    test_client, _ = client
    gateway_project("gw00000l", auth_enabled=False)
    gateway_project("gw00000m", auth_enabled=True)

    off = _get(test_client, "gw00000l", None, path="/auth/v1/settings")
    on = _get(test_client, "gw00000m", None, path="/auth/v1/settings")
    assert off.status_code == on.status_code == 401
    assert off.json() == on.json()


def test_the_auth_surface_uses_the_auth_worker_port_not_the_api_port(
    client, gateway_project, key_ring
):
    """Both come from one range and are stored separately. Routing Auth to the
    API port would hand signup traffic to PostgREST."""
    test_client, _ = client
    project_id = gateway_project("gw00000n")
    with db.connection() as conn:
        db.execute(
            conn, "UPDATE projects SET auth_port = 1 WHERE id = %s", (project_id,)
        )
        conn.commit()
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    # Port 1 has nothing listening, so a correct implementation fails to reach
    # an upstream rather than quietly succeeding against the API worker.
    response = _get(test_client, "gw00000n", key, path="/auth/v1/settings")
    assert response.status_code in (502, 503)


def test_a_not_yet_implemented_surface_says_so(client, gateway_project, key_ring):
    """A client calling Auth against a project that has none should get a
    comprehensible answer, not a confusing one from PostgREST."""
    test_client, _ = client
    project_id = gateway_project("gw00000z")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    # Auth is served as of Phase 04 slice 2; Realtime and Storage are not.
    response = _get(test_client, "gw00000z", key, path="/realtime/v1/websocket")
    assert response.status_code == 404
    assert "not available yet" in response.json()["message"]
    assert _Recorder.received == []


def test_an_unauthenticated_request_cannot_probe_the_routing_table(client, gateway_project, key_ring):
    """Routing happens after authentication, so an unauthenticated caller gets
    the same 401 whatever path it asks for."""
    test_client, _ = client
    gateway_project("gw00000y")
    for path in ("/rest/v1/things", "/auth/v1/token", "/nonsense"):
        response = _get(test_client, "gw00000y", "mldb_secret_0badc0debadc0debadc0de", path=path)
        assert response.status_code == 401
        assert response.json() == {"message": "invalid project or API key"}


def test_a_suspended_project_stops_serving(client, gateway_project, key_ring):
    test_client, _ = client
    project_id = gateway_project("gw000007")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status='SUSPENDED' WHERE id = %s", (project_id,))
        conn.commit()
    assert _get(test_client, "gw000007", key).status_code == 401


# -- what the upstream is told --------------------------------------------


def test_the_platform_key_is_not_forwarded(client, gateway_project, key_ring):
    """PostgREST has no use for it, and a credential should not travel further
    than the component that checks it."""
    test_client, _ = client
    project_id = gateway_project("gw000008")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    _get(test_client, "gw000008", key)
    forwarded = _Recorder.received[0]["headers"]
    assert "apikey" not in forwarded
    assert key not in json.dumps(forwarded)


def test_a_publishable_key_in_authorization_is_dropped_not_passed_as_a_jwt(
    client, gateway_project, key_ring
):
    """supabase-js sends the key in both headers. Forwarded as a bearer token
    it is not a JWT, so PostgREST would answer 401 for a request the gateway
    just authorised -- and the absence of a token is what selects anon."""
    test_client, _ = client
    project_id = gateway_project("gw000009")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    _get(test_client, "gw000009", key, headers={"authorization": f"Bearer {key}"})
    assert "authorization" not in _Recorder.received[0]["headers"]


def test_a_secret_key_becomes_a_service_role_token(client, gateway_project, key_ring):
    """ADR-028's keys are opaque; PostgREST decides the role from a JWT. The
    translation happens in the gateway so the token stays short-lived and the
    key stays revocable."""
    test_client, _ = client
    project_id = gateway_project("gw00000a")
    key = _issue(project_id, api_keys.SECRET, key_ring)
    _Recorder.received = []
    _get(test_client, "gw00000a", key)

    header = _Recorder.received[0]["headers"]["authorization"]
    with db.connection() as conn:
        secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    claims = jwt.decode(header.removeprefix("Bearer "), secret, algorithms=["HS256"])
    assert claims["role"] == "service_role"
    assert claims["exp"] - claims["iat"] <= 300, "a service_role token should be short-lived"


def test_an_end_user_jwt_is_forwarded_untouched(client, gateway_project, key_ring):
    """Verifying it is PostgREST's job; re-signing here would erase its claims."""
    test_client, _ = client
    project_id = gateway_project("gw00000b")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    # Shaped like a JWT so the gateway must decide by shape, not by luck.
    user_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"  # noqa: S105
    _Recorder.received = []
    _get(test_client, "gw00000b", key, headers={"authorization": f"Bearer {user_token}"})
    assert _Recorder.received[0]["headers"]["authorization"] == f"Bearer {user_token}"


def test_client_supplied_forwarding_headers_are_dropped(client, gateway_project, key_ring):
    """Appending to them lets a caller forge the origin recorded downstream."""
    test_client, _ = client
    project_id = gateway_project("gw00000c")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    _get(
        test_client,
        "gw00000c",
        key,
        headers={"x-forwarded-for": "1.2.3.4", "x-forwarded-host": "evil.com", "forwarded": "for=1.2.3.4"},
    )
    forwarded = _Recorder.received[0]["headers"]
    for header in ("x-forwarded-for", "x-forwarded-host", "forwarded"):
        assert header not in forwarded, f"{header} was passed through"


def test_hop_by_hop_headers_do_not_come_back_to_the_client(client, gateway_project, key_ring):
    test_client, _ = client
    project_id = gateway_project("gw00000d")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    response = _get(test_client, "gw00000d", key)
    assert "keep-alive" not in {k.lower(): v for k, v in response.headers.items()}


def test_an_oversized_body_is_refused_before_the_upstream(client, gateway_project, key_ring):
    """docs/API-GATEWAY.md requires a body limit. PostgREST would accept a very
    large insert; the ceiling belongs in front of it."""
    from services.gateway import app as gateway_app

    test_client, _ = client
    project_id = gateway_project("gw00000e")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []
    response = test_client.post(
        "/rest/v1/things",
        headers={"host": f"gw00000e.{GATEWAY_DOMAIN}", "apikey": key},
        content=b"x" * (gateway_app.MAX_BODY_BYTES + 1),
    )
    assert response.status_code == 413
    assert _Recorder.received == []


def test_an_upstream_failure_does_not_leak_the_internal_address(
    client, gateway_project, key_ring, upstream
):
    """docs/API-GATEWAY.md forbids exposing internal node and database names."""
    test_client, _ = client
    project_id = gateway_project("gw00000f")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    port = upstream.server_port
    upstream.shutdown()
    upstream.server_close()

    response = _get(test_client, "gw00000f", key)
    assert response.status_code in (502, 503)
    body = response.text
    assert str(port) not in body
    assert "127.0.0.1" not in body
    assert "mldb_" not in body


# -- caching and revocation -----------------------------------------------


def test_a_revoked_key_stops_working_promptly(client, gateway_project, key_ring):
    """A cache that outlives a revocation is a revocation that did not happen."""
    test_client, gateway = client
    project_id = gateway_project("gw00000g")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    assert _get(test_client, "gw00000g", key).status_code == 200
    assert gateway.cache.size == 1, "the key was not cached, so this proves nothing"

    with db.connection() as conn:
        row = db.one(conn, "SELECT id, key_identifier FROM api_keys WHERE project_id = %s", (project_id,))
        api_keys.revoke(conn, key_id=row["id"], project_id=project_id)
        conn.commit()

    # What the LISTEN/NOTIFY consumer does when the announcement arrives.
    gateway_keys.apply_revocation(gateway.cache, f"{project_id}:{row['key_identifier']}")
    assert _get(test_client, "gw00000g", key).status_code == 401


def test_revocation_announces_itself_on_the_channel(db_pool, key_ring):
    """The gateway cannot invalidate what it is never told about."""
    import psycopg

    from tests.conftest import DATABASE_URL

    project_id = uuid.uuid4()
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email="gwnotify@example.com", password=TEST_CREDENTIAL
        )
        plan = db.one(
            conn,
            "INSERT INTO plans (code,name) VALUES ('gwn','N') "
            "ON CONFLICT (code) DO UPDATE SET name='N' RETURNING id",
        )["id"]
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
            "VALUES (%s,%s,'gw0000nt','n',%s,'ACTIVE')",
            (project_id, org, plan),
        )
        issued = api_keys.create(
            conn, project_id=project_id, key_type=api_keys.SECRET, pepper=TEST_PEPPER
        )
        conn.commit()

    listener = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        listener.execute(f"LISTEN {api_keys.REVOCATION_CHANNEL}")
        with db.connection() as conn:
            api_keys.revoke(conn, key_id=issued.id, project_id=project_id)
            conn.commit()
        received = next(iter(listener.notifies(timeout=10)), None)
    finally:
        listener.close()

    assert received is not None, "revoking a key announced nothing"
    assert received.payload == f"{project_id}:{issued.key_identifier}"


def test_a_rolled_back_revocation_announces_nothing(db_pool, key_ring):
    """Otherwise a gateway forgets a key that is still live, and the customer
    sees an outage with no cause recorded anywhere."""
    import psycopg

    from tests.conftest import DATABASE_URL

    project_id = uuid.uuid4()
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email="gwrollback@example.com", password=TEST_CREDENTIAL
        )
        plan = db.one(
            conn,
            "INSERT INTO plans (code,name) VALUES ('gwr','R') "
            "ON CONFLICT (code) DO UPDATE SET name='R' RETURNING id",
        )["id"]
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
            "VALUES (%s,%s,'gw0000rb','r',%s,'ACTIVE')",
            (project_id, org, plan),
        )
        issued = api_keys.create(
            conn, project_id=project_id, key_type=api_keys.SECRET, pepper=TEST_PEPPER
        )
        conn.commit()

    listener = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        listener.execute(f"LISTEN {api_keys.REVOCATION_CHANNEL}")
        with db.connection() as conn:
            api_keys.revoke(conn, key_id=issued.id, project_id=project_id)
            conn.rollback()
        received = next(iter(listener.notifies(timeout=2)), None)
    finally:
        listener.close()

    assert received is None, "a rolled back revocation still told gateways to forget the key"


def test_the_cache_never_answers_across_projects(db_pool, key_ring):
    """Keyed by (project, identifier). Keying by identifier alone would let a
    hit for one project answer a request for another."""
    cache = gateway_keys.KeyCache()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()

    with db.connection() as conn:
        for project_id, ref in ((project_a, "gw0000ca"), (project_b, "gw0000cb")):
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES (%s,'C') "
                "ON CONFLICT (code) DO UPDATE SET name='C' RETURNING id",
                (f"plan-{ref}",),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
                "VALUES (%s,%s,%s,%s,%s,'ACTIVE')",
                (project_id, org, ref, ref, plan),
            )
        issued = api_keys.create(
            conn, project_id=project_a, key_type=api_keys.SECRET, pepper=TEST_PEPPER
        )
        conn.commit()

        warm = cache.resolve(
            conn, presented=issued.plaintext, project_id=project_a, pepper=TEST_PEPPER
        )
        assert warm is not None
        cross = cache.resolve(
            conn, presented=issued.plaintext, project_id=project_b, pepper=TEST_PEPPER
        )
    assert cross is None, "a cached hit for one project answered another"


def test_unknown_keys_are_cached_so_junk_cannot_drive_database_load(db_pool):
    cache = gateway_keys.KeyCache()
    project_id = uuid.uuid4()
    with db.connection() as conn:
        for _ in range(3):
            assert cache.resolve(
                conn,
                presented="mldb_secret_0badc0debadc0debadc0de",
                project_id=project_id,
                pepper=TEST_PEPPER,
            ) is None
    assert cache.size == 1


def test_a_malformed_announcement_is_ignored_rather_than_fatal():
    cache = gateway_keys.KeyCache()
    for payload in ("", "nonsense", "not-a-uuid:abc", str(uuid.uuid4())):
        assert gateway_keys.apply_revocation(cache, payload) is False
    assert gateway_keys.apply_revocation(cache, f"{uuid.uuid4()}:abcdef12") is True


# -- negative test J (specs/tenant-role-model.md) --------------------------
#
# "A free-tier project has no login role reachable from outside the gateway."
# Carried from Phase 02, which had no gateway to test it against.
#
# The deployment half -- that the PostgreSQL port is not published -- is a
# node-configuration property and not something this repository can assert. The
# half that *is* ours is that the platform never hands a tenant database
# credential, host, or port to a caller: ADR-005 makes direct SQL a paid
# capability, and a free project that could read its own authenticator password
# out of an API response would have direct SQL whatever the plan said.


def test_J_no_api_response_carries_a_tenant_database_credential():
    """The control-plane contract is CI-enforced, so this is a durable check
    rather than a snapshot of today's handlers."""
    import yaml

    spec = yaml.safe_load(open("specs/control-plane-api.yaml"))
    forbidden = (
        "database_name", "db_uri", "dsn", "connection_string", "api_port",
        "node_id", "internal_host", "authenticator", "verification_data",
        "ciphertext", "jwt_secret", "admin_ciphertext",
    )
    schemas = spec.get("components", {}).get("schemas", {})
    offenders = [
        f"{name}.{field}"
        for name, schema in schemas.items()
        for field in (schema.get("properties") or {})
        if field in forbidden
    ]
    assert offenders == [], f"the public contract exposes tenant infrastructure: {offenders}"


def test_J_the_gateway_never_returns_tenant_infrastructure(client, gateway_project, key_ring):
    """Not even on the error paths, which is where internal detail usually
    escapes -- docs/API-GATEWAY.md forbids exposing node and database names."""
    test_client, _ = client
    project_id = gateway_project("gw00000j")
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)

    responses = [
        _get(test_client, "gw00000j", key),
        _get(test_client, "gw00000j", "mldb_secret_0badc0debadc0debadc0de"),
        _get(test_client, "gw00000j", key, path="/auth/v1/token"),
        _get(test_client, "gw00000j", None),
    ]
    for response in responses:
        body = response.text
        assert "mldb_gw00000j" not in body, "a tenant database or role name was returned"
        assert "127.0.0.1" not in body
        assert "_authenticator" not in body
