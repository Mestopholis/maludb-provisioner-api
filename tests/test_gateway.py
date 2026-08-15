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

    def make(ref: str) -> uuid.UUID:
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
                "database_name, api_port, worker_state) "
                "VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,'RUNNING')",
                (project_id, org, ref, ref, plan, f"mldb_{ref}", upstream.server_port),
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
    assert _Recorder.received[0]["path"].startswith("/rest/v1/things")


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
