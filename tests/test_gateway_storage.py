"""`/storage/v1` at the gateway (Phase 10 slice 4).

Written from the attacker's side like the rest of the gateway suite, because
this surface adds two controls the others do not have and both fail quietly.

The **tenant name is a header the gateway sets**. `storage-api` resolves a
tenant from `X-Forwarded-Host` and serves whatever that names, so a client whose
own copy survived to the upstream would be a client choosing its tenant. Nothing
downstream would notice: the request succeeds, against somebody else's objects.

And **egress is counted rather than rated**. A limiter that undercounts is a
ceiling that never arrives, which ADR-056 makes the free tier's exposure to
anyone with a public URL. So the counting is asserted against the row it writes,
not against the fact that a function was called.

The upstream is the recording stub from `tests.test_gateway`, for the reason
that module gives: what is under test here is *what the gateway forwards*.
`tests/test_storage_workers.py` puts a real `storage-api` behind it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from services.control_plane import api_keys, db, object_storage, storage_workers
from services.gateway import limits
from services.gateway.app import Gateway, create_app
from tests.conftest import requires_db
from tests.test_gateway import GATEWAY_DOMAIN, _issue, _Recorder, gateway_project, upstream  # noqa: F401

pytestmark = [requires_db]

MB = 1024 * 1024

# What a node with an object store looks like to the gateway. The endpoint is
# never opened here -- the gateway only reads it to decide the surface exists --
# but it is a data address rather than loopback because ADR-035 means a real one
# always is, and a test fixture that models the wrong thing teaches the wrong
# thing.
STORE_ENDPOINT = "http://10.91.0.1:8333"


@pytest.fixture
def storage_config(app_config, upstream):  # noqa: F811 - fixture
    """A node prepared for Storage, whose worker is the recording upstream."""
    return dataclasses.replace(
        app_config,
        gateway_domain=GATEWAY_DOMAIN,
        storage_port=upstream.server_port,
        storage_admin_port=upstream.server_port + 1,
        storage_db_host="10.91.0.1",
        storage_s3_endpoint=STORE_ENDPOINT,
        storage_s3_access_key="maludb-platform",
        storage_s3_secret_key="not-a-real-secret",  # noqa: S106 - test fixture
    )


@pytest.fixture
def client(storage_config, key_ring):
    """The gateway, flushing egress on every request.

    `flush_seconds=0` on purpose: batching is what the meter is *for*, and it is
    tested directly further down. Here it would only make every assertion about
    a recorded byte count depend on a clock.
    """
    gateway = Gateway(
        config=storage_config,
        key_ring=key_ring,
        wake_sleeping=False,
        client=httpx.AsyncClient(timeout=10),
        egress=limits.EgressMeter(flush_seconds=0.0),
    )
    with TestClient(create_app(gateway)) as test_client:
        yield test_client, gateway


def _registered(project_id: uuid.UUID) -> None:
    """Mark the project as known to the node's worker."""
    with db.connection() as conn:
        storage_workers.mark_registered(conn, project_id)
        conn.commit()


def _on_a_node(project_id: uuid.UUID) -> int:
    """Place the project on a node, which registration needs.

    The node carries the storage worker's root secret (migration 0025), so a
    project with no node has nothing to register *with* -- which is its own case
    below rather than a setup detail.
    """
    with db.connection() as conn:
        node = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES ('storage-node','s.example','s.internal','shared','active', now()) "
            "ON CONFLICT (name) DO UPDATE SET status = 'active' RETURNING id",
        )["id"]
        db.execute(conn, "UPDATE projects SET node_id = %s WHERE id = %s", (node, project_id))
        conn.commit()
    return node


def _call(client, ref: str, key: str | None, path: str, method: str = "GET", **kwargs):
    headers = {"host": f"{ref}.{GATEWAY_DOMAIN}"}
    if key is not None:
        headers["apikey"] = key
    headers.update(kwargs.pop("headers", {}))
    return client.request(method, path, headers=headers, **kwargs)


def _egress_row(project_id: uuid.UUID) -> int:
    with db.connection() as conn:
        return object_storage.egress_used(conn, project_id=project_id)


# -- routing, and the header that names the tenant -------------------------


def test_the_prefix_is_stripped_and_the_tenant_is_named(client, gateway_project, key_ring):  # noqa: F811
    """Upstream serves at its own root, and resolves the tenant from a header.

    Both halves in one assertion because both are how a request reaches the
    right tenant's objects: the path `storage-api` sees, and the host it maps to
    a tenant.
    """
    test_client, _ = client
    project_id = gateway_project("st000001")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    _Recorder.received = []

    response = _call(test_client, "st000001", key, "/storage/v1/bucket")

    assert response.status_code == 200
    assert len(_Recorder.received) == 1
    forwarded = _Recorder.received[0]
    assert forwarded["path"] == "/bucket", "the gateway's prefix reached storage-api"
    assert forwarded["headers"]["x-forwarded-host"] == f"st000001.{GATEWAY_DOMAIN}"


def test_a_client_cannot_choose_its_tenant(client, gateway_project, key_ring):  # noqa: F811
    """The control this surface exists to get right.

    `X-Forwarded-Host` is what `storage-api` resolves a tenant from. A client's
    own copy is dropped by `UNTRUSTED_INBOUND` and the gateway sets its own from
    the hostname it authenticated -- so a caller holding a valid key for one
    project cannot name another project's tenant and read its objects.
    """
    test_client, _ = client
    victim = gateway_project("st000002")
    attacker = gateway_project("st000003")
    _registered(victim)
    _registered(attacker)
    key = _issue(attacker, api_keys.SECRET, key_ring)
    _Recorder.received = []

    response = _call(
        test_client, "st000003", key, "/storage/v1/object/files/secret.txt",
        headers={
            "x-forwarded-host": f"st000002.{GATEWAY_DOMAIN}",
            # The other spelling, which some proxies honour instead.
            "forwarded": f"host=st000002.{GATEWAY_DOMAIN}",
        },
    )

    assert response.status_code == 200
    forwarded = _Recorder.received[0]["headers"]
    assert forwarded["x-forwarded-host"] == f"st000003.{GATEWAY_DOMAIN}", (
        "a client-supplied forwarded host reached the storage worker"
    )
    assert "forwarded" not in forwarded


def test_a_key_for_another_project_is_refused(client, gateway_project, key_ring):  # noqa: F811
    """ADR-008 applies here exactly as it does to the Data API."""
    test_client, _ = client
    a = gateway_project("st000004")
    gateway_project("st000005")
    _registered(a)
    key_a = _issue(a, api_keys.SECRET, key_ring)
    _Recorder.received = []

    assert _call(test_client, "st000005", key_a, "/storage/v1/bucket").status_code == 401
    assert _Recorder.received == [], "a refused request reached the storage worker"


def test_a_node_without_an_object_store_says_so(app_config, key_ring, gateway_project):  # noqa: F811
    """A deployment with no Storage is not a broken one.

    Answered after authentication, like the Auth surface's own 404, so it does
    not tell an unauthenticated caller anything about the node.
    """
    project_id = gateway_project("st000006")
    key = _issue(project_id, api_keys.SECRET, key_ring)
    gateway = Gateway(
        config=dataclasses.replace(app_config, gateway_domain=GATEWAY_DOMAIN),
        key_ring=key_ring, wake_sleeping=False,
    )
    with TestClient(create_app(gateway)) as test_client:
        response = _call(test_client, "st000006", key, "/storage/v1/bucket")
    assert response.status_code == 404
    assert _call(test_client, "st000006", None, "/storage/v1/bucket").status_code == 401


# -- public buckets, which are the free tier's egress vector ---------------


def test_a_public_object_is_served_without_a_key(client, gateway_project):  # noqa: F811
    """`getPublicUrl` produces a URL with no key in it.

    A browser following one sends an `apikey` header for nobody, exactly as it
    does for a confirmation link -- so this path joins `PUBLIC_AUTH_PATHS` in
    being reachable unauthenticated.
    """
    test_client, _ = client
    _registered(gateway_project("st000007"))
    _Recorder.received = []

    response = _call(test_client, "st000007", None, "/storage/v1/object/public/files/logo.png")

    assert response.status_code == 200
    assert _Recorder.received[0]["path"] == "/object/public/files/logo.png"


def test_a_signed_url_is_redeemed_without_a_key(client, gateway_project):  # noqa: F811
    """`createSignedUrl` hands out a link for a **private** bucket (ADR-062).

    The link is the product: mailed, pasted, put behind an `<img>`. It carries a
    `token` and never an `apikey`, so a gateway that required one answered 401
    for every signed URL the platform issued -- which slice 5 found by asking
    the official client to make one and then following it.

    Not unauthorised: upstream registers this route with no JWT plugin and
    checks the token against the project's own signing secret, which is also why
    the token is left in the query string untouched here.
    """
    test_client, _ = client
    _registered(gateway_project("st000038"))
    _Recorder.received = []

    response = _call(
        test_client, "st000038", None,
        "/storage/v1/object/sign/private/report.pdf?token=signed.by.the.project",
    )

    assert response.status_code == 200
    forwarded = _Recorder.received[0]
    # The query string carried through as well as the path: the `token` in it is
    # the entire authorisation for this request, and a gateway that dropped it
    # would turn every signed link into a 400 from upstream.
    assert forwarded["path"] == "/object/sign/private/report.pdf?token=signed.by.the.project"
    assert "authorization" not in forwarded["headers"], (
        "the gateway minted a token for a caller that presented none"
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_signed_path_is_read_only(client, gateway_project, method):  # noqa: F811
    """Minting one still needs a key; redeeming one does not.

    `POST /object/sign/<bucket>/<path>` is `createSignedUrl` itself -- the call
    that asks the project to authorise a link. Reachable without a key it would
    let anyone who knows a project hostname issue themselves a signed URL for
    any object in it, which is every private bucket on the project.
    """
    test_client, _ = client
    _registered(gateway_project("st000039"))
    _Recorder.received = []

    response = _call(
        test_client, "st000039", None, "/storage/v1/object/sign/private/report.pdf", method=method
    )

    assert response.status_code == 401
    assert _Recorder.received == []


def test_a_signed_upload_url_cannot_be_redeemed_without_a_key(client, gateway_project):  # noqa: F811
    """The neighbour that stays closed, asserted so it stays closed by decision.

    `PUT /object/upload/sign/<bucket>/<path>?token=...` is upstream's signed
    *upload*, registered in the same unauthenticated group as the download. It
    is a write reachable with no API key, which is a decision of its own rather
    than a consequence of how the prefixes are spelled -- and it is deferred, so
    the assertion is that the spelling did not quietly grant it.
    """
    test_client, _ = client
    _registered(gateway_project("st00003a"))
    _Recorder.received = []

    response = _call(
        test_client, "st00003a", None,
        "/storage/v1/object/upload/sign/private/report.pdf?token=signed.by.the.project",
        method="PUT",
    )

    assert response.status_code == 401
    assert _Recorder.received == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_public_path_is_read_only(client, gateway_project, method):  # noqa: F811
    """A write to a public bucket still needs a key.

    The prefix alone would let anyone who knows a project hostname delete from a
    public bucket, on the assumption that upstream refuses it for want of a
    token. A gateway that relies on what is behind it to make up for what it let
    through is one upstream default away from a hole.
    """
    test_client, _ = client
    _registered(gateway_project("st000008"))
    _Recorder.received = []

    response = _call(
        test_client, "st000008", None, "/storage/v1/object/public/files/logo.png", method=method
    )

    assert response.status_code == 401
    assert _Recorder.received == []


def _raw_request(app, path: str, headers: dict, method: str = "GET") -> tuple[int, bytes]:
    """Drive the ASGI app with a path exactly as uvicorn would hand it over.

    Not `TestClient`, and the reason is the whole point of the tests below:
    **httpx resolves dot segments in the client too**, so a request written as
    `/storage/v1/object/public/../files/secret.txt` never leaves it with those
    characters in it. The gateway would be handed an ordinary path and the test
    would prove nothing -- which is exactly what the first version of this did,
    passing against code that had the hole.

    An attacker does not use httpx. `curl --path-as-is`, or anything writing to
    a socket, sends the literal target, and uvicorn's parser puts it in
    `scope["path"]` unchanged.
    """
    import asyncio

    async def drive() -> tuple[int, bytes]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, body

    return asyncio.run(drive())


TRAVERSALS = [
    # The finding this slice's security review turned up. Each passes a naive
    # `startswith` on the public prefix -- which is what decides that no API key
    # is required -- and each arrives at `storage-api` as the *authenticated*
    # object endpoint once httpx has resolved the dot segments on the way out.
    "/storage/v1/object/public/../files/secret.txt",
    "/storage/v1/object/public/a/../../files/secret.txt",
    # The same request written so a raw scan of the path does not see it.
    "/storage/v1/object/public/%2e%2e/files/secret.txt",
    "/storage/v1/object/public/%252e%252e/files/secret.txt",
    "/storage/v1/object/public/%2e%2e%2ffiles/secret.txt",
    # ADR-062 added a second prefix that needs no key, so it needs the same
    # answer. Written out rather than left to the shared `_has_dot_segment`
    # call: what makes the check hold is that *every* unauthenticated prefix
    # goes through it, and a list that only covers the older one would keep
    # passing after an edit that added a third.
    "/storage/v1/object/sign/../files/secret.txt",
    "/storage/v1/object/sign/%2e%2e/files/secret.txt",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_the_public_prefix_cannot_be_walked_out_of(storage_config, key_ring, gateway_project, path):  # noqa: F811
    """The gateway authorises one path and forwards another, unless it refuses.

    Refused at the key check rather than at routing, which is the stronger of
    the two answers available: with the dot segment present the request is not a
    public read, so it is a request with no API key and it gets the same 401 as
    every other one. Nothing about it distinguishes a project that exists.
    """
    _registered(gateway_project("st000025"))
    gateway = Gateway(
        config=storage_config, key_ring=key_ring, wake_sleeping=False,
        client=httpx.AsyncClient(timeout=10), egress=limits.EgressMeter(flush_seconds=0.0),
    )
    _Recorder.received = []

    status, _ = _raw_request(
        create_app(gateway), path, {"host": f"st000025.{GATEWAY_DOMAIN}"}
    )

    assert status == 401
    assert _Recorder.received == [], "a traversed path reached the storage worker"


def test_a_key_does_not_buy_a_traversed_path_either(storage_config, key_ring, gateway_project):  # noqa: F811
    """Refused for an authenticated caller too, rather than only where it bites.

    Within one project a traversal crosses nothing -- the tenant is fixed by a
    header the caller cannot set. It is refused anyway, because a rule that
    holds only on the path somebody remembered to think about is the rule that
    gets moved. Here the answer is 404: the caller proved it holds a key, so
    there is no oracle to protect and "no such path" is the honest reply.
    """
    project_id = gateway_project("st000026")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    gateway = Gateway(
        config=storage_config, key_ring=key_ring, wake_sleeping=False,
        client=httpx.AsyncClient(timeout=10), egress=limits.EgressMeter(flush_seconds=0.0),
    )
    _Recorder.received = []

    status, _ = _raw_request(
        create_app(gateway),
        "/storage/v1/object/public/../files/secret.txt",
        {"host": f"st000026.{GATEWAY_DOMAIN}", "apikey": key},
    )

    assert status == 404
    assert _Recorder.received == []


def test_the_traversal_would_otherwise_have_reached_the_private_endpoint(storage_config, key_ring, gateway_project):  # noqa: F811
    """What the refusal is worth, demonstrated rather than described.

    With the dot-segment check bypassed, the same request reaches the upstream
    as `/object/files/secret.txt` -- upstream's authenticated object endpoint --
    having presented no API key at all. This test exists so that a later change
    which quietly drops the check fails here with the reason attached.
    """
    _registered(gateway_project("st000032"))
    gateway = Gateway(
        config=storage_config, key_ring=key_ring, wake_sleeping=False,
        client=httpx.AsyncClient(timeout=10), egress=limits.EgressMeter(flush_seconds=0.0),
    )
    app_module = __import__("services.gateway.app", fromlist=["app"])
    _Recorder.received = []

    original = app_module._has_dot_segment
    try:
        app_module._has_dot_segment = lambda path: False
        status, _ = _raw_request(
            create_app(gateway),
            "/storage/v1/object/public/../files/secret.txt",
            {"host": f"st000032.{GATEWAY_DOMAIN}"},
        )
    finally:
        app_module._has_dot_segment = original

    assert status == 200, "the hole this check closes did not reproduce"
    assert _Recorder.received[0]["path"] == "/object/files/secret.txt", (
        "the request the gateway authorised as a public read is not the one it forwarded"
    )


def test_an_object_key_may_still_contain_dots(client, gateway_project, key_ring):  # noqa: F811
    """The refusal is a segment that *is* a dot segment, not a dot in a name."""
    test_client, _ = client
    project_id = gateway_project("st000027")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)

    response = _call(test_client, "st000027", key, "/storage/v1/object/files/a..b/v1.2.3.tar.gz")

    assert response.status_code == 200


def test_a_private_object_needs_a_key(client, gateway_project):  # noqa: F811
    """Only the public prefix is open, not the surface it sits in."""
    test_client, _ = client
    _registered(gateway_project("st000009"))
    _Recorder.received = []
    assert _call(test_client, "st000009", None, "/storage/v1/object/files/x.txt").status_code == 401
    assert _Recorder.received == []


def test_anonymous_bytes_are_still_the_projects(client, gateway_project):  # noqa: F811
    """ADR-056's reason for enforcing this here.

    A public bucket is served to whoever has the URL, with no key presented and
    therefore no project identified by the caller. The bytes are still the
    project's, and a meter that only counted authenticated responses would leave
    the free tier's largest exposure uncounted.
    """
    test_client, _ = client
    project_id = gateway_project("st000010")
    _registered(project_id)

    _call(test_client, "st000010", None, "/storage/v1/object/public/files/logo.png")

    assert _egress_row(project_id) > 0


# -- the role the worker sees (ADR-062) ------------------------------------
#
# Added by slice 5, because driving the official client at this surface is what
# found it. The Data API's rule -- drop a publishable key, let the absence of a
# token select `db-anon-role` -- has no analogue in `storage-api`: it reads the
# bearer, and an empty one is refused 403 before any policy is consulted. Every
# route but the public-object one answered 403 for a caller holding a valid
# publishable key, which is a whole tier of the product.


def _claims(project_id: uuid.UUID, header: str, key_ring) -> dict:
    from services.control_plane import provisioning

    with db.connection() as conn:
        secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    import jwt as pyjwt

    return pyjwt.decode(header.removeprefix("Bearer "), secret, algorithms=["HS256"])


def test_a_publishable_key_becomes_an_anon_token(client, gateway_project, key_ring):  # noqa: F811
    """Minted rather than dropped, which is the whole of ADR-062.

    `anon` is what makes bootstrap 012's model real: it grants the shared roles
    `USAGE` on `storage` and leaves RLS to decide, and a policy can only decide
    about a role that arrives.
    """
    test_client, _ = client
    project_id = gateway_project("st000033")
    _registered(project_id)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []

    response = _call(test_client, "st000033", key, "/storage/v1/object/files/a.txt")

    assert response.status_code == 200
    header = _Recorder.received[0]["headers"]["authorization"]
    claims = _claims(project_id, header, key_ring)
    assert claims["role"] == "anon"
    assert claims["exp"] - claims["iat"] <= 300, "an anon token should be short-lived"


def test_a_secret_key_still_becomes_a_service_role_token(client, gateway_project, key_ring):  # noqa: F811
    """The other half, asserted here rather than assumed from the Data API's.

    A refactor that reached for one role for both key types would pass every
    other test in this file: the stub answers 200 whatever the token says.
    """
    test_client, _ = client
    project_id = gateway_project("st000034")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    _Recorder.received = []

    _call(test_client, "st000034", key, "/storage/v1/bucket")

    claims = _claims(project_id, _Recorder.received[0]["headers"]["authorization"], key_ring)
    assert claims["role"] == "service_role"


def test_an_end_user_token_is_not_replaced_by_an_anon_one(client, gateway_project, key_ring):  # noqa: F811
    """A signed-in user's claims are what a storage policy is written against.

    Minting over them would make every request anonymous and `auth.uid()` null,
    which is a policy suite that silently denies everything -- or, if the policy
    were written the other way, silently allows it.
    """
    test_client, _ = client
    project_id = gateway_project("st000035")
    _registered(project_id)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    user_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"  # noqa: S105
    _Recorder.received = []

    _call(
        test_client, "st000035", key, "/storage/v1/object/files/a.txt",
        headers={"authorization": f"Bearer {user_token}"},
    )

    assert _Recorder.received[0]["headers"]["authorization"] == f"Bearer {user_token}"


def test_an_anonymous_public_read_carries_no_token(client, gateway_project):  # noqa: F811
    """Nothing was authenticated, so there is nothing to mint from.

    Upstream's public-object route sets `allowInvalidJwt` and treats an absent
    bearer as `anon` itself. Minting here would be the gateway issuing a token
    to a caller that proved nothing -- and would make the anonymous surface
    indistinguishable upstream from a keyed one.
    """
    test_client, _ = client
    _registered(gateway_project("st000036"))
    _Recorder.received = []

    _call(test_client, "st000036", None, "/storage/v1/object/public/files/logo.png")

    assert "authorization" not in _Recorder.received[0]["headers"]


def test_a_project_with_no_signing_secret_is_refused_rather_than_crashing(client, gateway_project, key_ring):  # noqa: F811
    """Found in this slice's security review.

    Before ADR-062 only a secret key reached `_jwt_secret` on this surface, and
    a project missing its `jwt_signing` credential is a state provisioning does
    not produce. Minting an `anon` token puts the publishable path -- much the
    more common one -- on the same call, and an uncaught `ProvisioningError`
    there is a 500 with a stack trace where a refusal belongs.
    """
    test_client, _ = client
    project_id = gateway_project("st00003b")
    _registered(project_id)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE project_credentials SET revoked_at = now() "
            " WHERE project_id = %s AND credential_type = 'jwt_signing'",
            (project_id,),
        )
        conn.commit()
    _Recorder.received = []

    response = _call(test_client, "st00003b", key, "/storage/v1/object/files/a.txt")

    assert response.status_code == 503
    assert _Recorder.received == [], "a request with no mintable token reached the worker"
    assert "credential" not in response.text.lower(), "the refusal described the platform's state"


def test_the_minted_token_is_not_taken_from_the_request(client, gateway_project, key_ring):  # noqa: F811
    """The role reaches `set_config('role', ...)` in the tenant database.

    A caller that could name it would be a caller choosing its own privileges,
    so the header a client might use to suggest one is asserted to do nothing.
    """
    test_client, _ = client
    project_id = gateway_project("st000037")
    _registered(project_id)
    key = _issue(project_id, api_keys.PUBLISHABLE, key_ring)
    _Recorder.received = []

    _call(
        test_client, "st000037", key, "/storage/v1/object/files/a.txt",
        headers={"x-client-role": "service_role", "role": "service_role"},
    )

    claims = _claims(project_id, _Recorder.received[0]["headers"]["authorization"], key_ring)
    assert claims["role"] == "anon", "a client-supplied role reached the minted token"


# -- egress accounting -----------------------------------------------------


def test_bytes_served_reach_the_ledger(client, gateway_project, key_ring):  # noqa: F811
    """What the stub returns is what the row records."""
    test_client, _ = client
    project_id = gateway_project("st000011")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)

    first = _call(test_client, "st000011", key, "/storage/v1/object/files/a.txt")
    after_one = _egress_row(project_id)
    _call(test_client, "st000011", key, "/storage/v1/object/files/b.txt")
    after_two = _egress_row(project_id)

    assert after_one == len(first.content)
    assert after_two == 2 * after_one, "egress overwrote rather than accumulated"


def test_the_data_api_is_not_counted_as_egress(client, gateway_project, key_ring):  # noqa: F811
    """ADR-056 is about object storage.

    The Data API's responses pass through the same process and are not object
    bytes; counting them would spend a project's storage allowance on its
    database queries.
    """
    test_client, _ = client
    project_id = gateway_project("st000012")
    key = _issue(project_id, api_keys.SECRET, key_ring)

    _call(test_client, "st000012", key, "/rest/v1/things")

    assert _egress_row(project_id) == 0


# -- the two refusals, which is what makes slice 2's states real -----------


def test_egress_over_the_ceiling_is_refused(client, gateway_project, key_ring):  # noqa: F811
    """429, with `Retry-After` to the month boundary and a date to plan around.

    ADR-050's product point in the message: a ceiling hit with no visible way
    forward is a churn event. The refusal says how much, of how much, and when
    it resets.
    """
    test_client, _ = client
    project_id = gateway_project("st000013", plan_limits={"egress_bytes_per_month": 8})
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)

    # The first response is larger than the whole ceiling, so the second request
    # is the one over it. Checked before serving rather than after, which is why
    # the first is allowed at all: a project under its ceiling gets its request.
    first = _call(test_client, "st000013", key, "/storage/v1/object/files/a.txt")
    assert first.status_code == 200
    assert len(first.content) > 8
    _Recorder.received = []
    refused = _call(test_client, "st000013", key, "/storage/v1/object/files/b.txt")

    assert refused.status_code == 429
    assert _Recorder.received == [], "a refused request still served bytes"
    assert int(refused.headers["retry-after"]) > 0
    resets = object_storage.next_period_start().isoformat()
    assert resets in refused.json()["message"]
    assert "8 bytes" in refused.json()["message"]


def test_the_egress_ceiling_reaches_a_public_reader_too(client, gateway_project):  # noqa: F811
    """The unauthenticated path is the one that most needs the ceiling."""
    test_client, _ = client
    project_id = gateway_project("st000014", plan_limits={"egress_bytes_per_month": 100})
    _registered(project_id)
    with db.connection() as conn:
        object_storage.record_egress(conn, project_id=project_id, bytes_served=100)
        conn.commit()
    _Recorder.received = []

    response = _call(test_client, "st000014", None, "/storage/v1/object/public/files/logo.png")

    assert response.status_code == 429
    assert _Recorder.received == []


def test_a_full_project_is_refused_uploads(client, gateway_project, key_ring):  # noqa: F811
    """413 on the way in, from the state slice 2's maintenance pass recorded."""
    test_client, _ = client
    project_id = gateway_project("st000015", plan_limits={"object_storage_bytes": 4 * MB})
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET object_exceeded_at = now(), object_bytes = %s WHERE id = %s",
            (5 * MB, project_id),
        )
        conn.commit()
    _Recorder.received = []

    refused = _call(
        test_client, "st000015", key, "/storage/v1/object/files/big.bin",
        method="POST", content=b"more",
    )

    assert refused.status_code == 413
    assert _Recorder.received == []
    message = refused.json()["message"]
    assert "5.0 MB" in message and "4.0 MB" in message
    assert "upgrade" in message.lower(), "a refusal with no way forward is a churn event"


def test_a_full_project_can_still_read_and_delete(client, gateway_project, key_ring):  # noqa: F811
    """The other half of ADR-050's point, and the one easier to get wrong.

    A customer over their storage ceiling has exactly two ways back under it:
    look at what they have, and delete some of it. A gateway that refused every
    Storage request for a full project would take both away.
    """
    test_client, _ = client
    project_id = gateway_project("st000016", plan_limits={"object_storage_bytes": 4 * MB})
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    with db.connection() as conn:
        db.execute(
            conn, "UPDATE projects SET object_exceeded_at = now() WHERE id = %s", (project_id,)
        )
        conn.commit()

    assert _call(test_client, "st000016", key, "/storage/v1/object/list/files").status_code == 200
    assert _call(
        test_client, "st000016", key, "/storage/v1/object/files/a.txt", method="DELETE"
    ).status_code == 200


def test_an_anonymous_reader_is_not_told_the_projects_usage(client, gateway_project):  # noqa: F811
    """A public URL must not be a usage oracle.

    The refusal still says what an anonymous caller can act on -- this project
    is not serving now, and when that changes -- and not how much of its
    allowance the project has spent or which plan it is on. Anyone who has ever
    been sent a link to a file would otherwise be able to read both.
    """
    test_client, _ = client
    project_id = gateway_project("st000028", plan_limits={"egress_bytes_per_month": 100})
    _registered(project_id)
    with db.connection() as conn:
        object_storage.record_egress(conn, project_id=project_id, bytes_served=100)
        conn.commit()

    refused = _call(test_client, "st000028", None, "/storage/v1/object/public/files/logo.png")
    message = refused.json()["message"]

    assert refused.status_code == 429
    assert "100 bytes" not in message, "an anonymous caller was told the project's usage"
    assert object_storage.next_period_start().isoformat() in message
    assert int(refused.headers["retry-after"]) > 0


def test_the_projects_own_client_is_told_the_figures(client, gateway_project, key_ring):  # noqa: F811
    """The other side of it: a caller holding a key gets what it needs to act."""
    test_client, _ = client
    project_id = gateway_project("st000029", plan_limits={"egress_bytes_per_month": 100})
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    with db.connection() as conn:
        object_storage.record_egress(conn, project_id=project_id, bytes_served=100)
        conn.commit()

    refused = _call(test_client, "st000029", key, "/storage/v1/object/files/a.txt")

    assert refused.status_code == 429
    assert "100 bytes" in refused.json()["message"]


# -- the upload ceiling ----------------------------------------------------


def test_an_upload_larger_than_the_data_api_body_limit_is_served(
    client, gateway_project, key_ring,  # noqa: F811
):
    """The gateway's 8 MiB body cap is sized for a PostgREST insert.

    Applied here it would have capped every upload on the platform at 8 MiB,
    against upstream's 50 MB default -- an incompatibility nobody decided and
    that the object store has no part in. The storage surface has its own
    ceiling, and it is configuration rather than a constant.
    """
    test_client, _ = client
    project_id = gateway_project("st000030")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)

    response = _call(
        test_client, "st000030", key, "/storage/v1/object/files/big.bin",
        method="POST", content=b"x" * (9 * MB),
    )

    assert response.status_code == 200


def test_an_upload_over_the_storage_ceiling_is_refused(storage_config, key_ring, gateway_project):  # noqa: F811
    """And the ceiling is real, not merely larger."""
    gateway = Gateway(
        config=dataclasses.replace(storage_config, storage_max_upload_bytes=1024),
        key_ring=key_ring, wake_sleeping=False, client=httpx.AsyncClient(timeout=10),
        egress=limits.EgressMeter(flush_seconds=0.0),
    )
    project_id = gateway_project("st000031")
    _registered(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    _Recorder.received = []

    with TestClient(create_app(gateway)) as test_client:
        response = _call(
            test_client, "st000031", key, "/storage/v1/object/files/big.bin",
            method="POST", content=b"x" * 2048,
        )

    assert response.status_code == 413
    assert _Recorder.received == []


# -- registration on demand ------------------------------------------------


def test_an_unregistered_project_is_registered_by_its_next_request(
    client, gateway_project, key_ring, monkeypatch,  # noqa: F811
):
    """Migration 0025 left this here in as many words.

    Provisioning registers once and treats a failure as a delay, because a
    container being down must not fail a project. This is what makes that safe:
    the request that needs the tenant registers it.
    """
    test_client, gateway = client
    project_id = gateway_project("st000017")
    _on_a_node(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)
    calls = []

    def fake_register(conn, **kwargs):
        calls.append(kwargs["project_ref"])
        storage_workers.mark_registered(conn, kwargs["project_id"])
        return True

    monkeypatch.setattr(storage_workers, "ensure_registered", fake_register)

    assert _call(test_client, "st000017", key, "/storage/v1/bucket").status_code == 200
    assert calls == ["st000017"]

    with db.connection() as conn:
        row = db.one(conn, "SELECT storage_registered_at FROM projects WHERE id = %s", (project_id,))
    assert row["storage_registered_at"] is not None

    # And the cached row was dropped, so the next request does not register
    # again on a copy that still says NULL.
    assert _call(test_client, "st000017", key, "/storage/v1/bucket").status_code == 200
    assert calls == ["st000017"], "registration repeated for an already-registered project"


def test_a_project_that_cannot_be_registered_is_not_proxied(
    client, gateway_project, key_ring, monkeypatch,  # noqa: F811
):
    """503 rather than a request the worker answers `TenantNotFound`.

    A tenant the worker has never heard of is not a 200 with an empty body, and
    the difference matters to a client deciding whether to retry.
    """
    test_client, _ = client
    project_id = gateway_project("st000018")
    _on_a_node(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)

    def fails(conn, **kwargs):
        raise storage_workers.StorageWorkerError("could not register st000018 (ConnectError)")

    monkeypatch.setattr(storage_workers, "ensure_registered", fails)
    _Recorder.received = []

    response = _call(test_client, "st000018", key, "/storage/v1/bucket")

    assert response.status_code == 503
    assert _Recorder.received == []


def test_the_registration_failure_never_names_the_dsn(
    client, gateway_project, key_ring, monkeypatch, caplog,  # noqa: F811
):
    """The DSN carries a live password and a driver error echoes its statement."""
    test_client, _ = client
    project_id = gateway_project("st000019")
    _on_a_node(project_id)
    key = _issue(project_id, api_keys.SECRET, key_ring)

    def fails(conn, **kwargs):
        raise storage_workers.StorageWorkerError(
            "could not register st000019 with the storage worker (OperationalError)"
        )

    monkeypatch.setattr(storage_workers, "ensure_registered", fails)
    with caplog.at_level("ERROR"):
        _call(test_client, "st000019", key, "/storage/v1/bucket")

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "postgresql://" not in logged
    assert "st000019" in logged


# -- the meter itself ------------------------------------------------------


def test_the_meter_batches_rather_than_writing_per_response(db_pool, gateway_project):  # noqa: F811
    """One row write for many responses, which is the point of the batching.

    `object_storage.record_egress` was given a batch-total signature in slice 2
    for this: the gateway is on the path ADR-026 published a latency figure for,
    and a write per response is not available there.
    """
    project_id = gateway_project("st000020")
    meter = limits.EgressMeter(flush_seconds=3600.0)
    for _ in range(10):
        meter.add(project_id, 100)

    assert _egress_row(project_id) == 0, "the meter wrote before it was flushed"
    with db.connection() as conn:
        written = meter.flush(conn)
        conn.commit()
    assert written == 1000
    assert _egress_row(project_id) == 1000


def test_a_failed_flush_keeps_the_bytes(db_pool, gateway_project, monkeypatch):  # noqa: F811
    """Bytes that were served are owed whether or not the ledger was writable.

    Dropped on a failed write, they would be free egress for anyone who could
    make the control-plane database briefly unavailable.
    """
    project_id = gateway_project("st000021")
    meter = limits.EgressMeter(flush_seconds=0.0)
    meter.add(project_id, 512)

    def explode(conn, **kwargs):
        raise psycopg.OperationalError("the ledger is unavailable")

    monkeypatch.setattr(object_storage, "record_egress", explode)
    with db.connection() as conn, pytest.raises(psycopg.OperationalError):
        meter.flush(conn)

    monkeypatch.undo()
    with db.connection() as conn:
        assert meter.flush(conn) == 512
        conn.commit()
    assert _egress_row(project_id) == 512


def test_a_new_month_is_not_judged_against_the_old_one(db_pool, gateway_project, monkeypatch):  # noqa: F811
    """A gateway process outlives a month boundary; its cached total must not.

    The period is compared rather than assumed, because the failure is silent
    and expensive in the customer's disfavour: a project would start a new month
    already at last month's total and be refused on its first request.
    """
    project_id = gateway_project("st000022")
    meter = limits.EgressMeter(flush_seconds=0.0, refresh_seconds=3600.0)
    with db.connection() as conn:
        object_storage.record_egress(conn, project_id=project_id, bytes_served=900)
        conn.commit()
        assert meter.used(conn, project_id=project_id) == 900

        last_month = dt.date.today().replace(day=1)
        next_month = object_storage.next_period_start()
        monkeypatch.setattr(object_storage, "period_start", lambda *a, **k: next_month)
        assert meter.used(conn, project_id=project_id) == 0, (
            "last month's bytes counted against this month's ceiling"
        )
        assert last_month != next_month


def test_forgetting_a_project_does_not_forget_what_it_owes(db_pool, gateway_project):  # noqa: F811
    """`forget` drops cached state when a project stops serving.

    Pending bytes are not cached state: they have been served and are not yet
    written down, and dropping them is the one thing this class must never do.
    """
    project_id = gateway_project("st000023")
    meter = limits.EgressMeter(flush_seconds=3600.0)
    meter.add(project_id, 256)
    meter.forget(project_id)

    with db.connection() as conn:
        assert meter.flush(conn) == 256
        conn.commit()
    assert _egress_row(project_id) == 256


def test_shutdown_writes_down_what_is_owed(storage_config, key_ring, gateway_project):  # noqa: F811
    """A graceful restart is the common case and must not be free egress."""
    project_id = gateway_project("st000024")
    _registered(project_id)
    gateway = Gateway(
        config=storage_config, key_ring=key_ring, wake_sleeping=False,
        client=httpx.AsyncClient(timeout=10),
        egress=limits.EgressMeter(flush_seconds=3600.0),
    )
    with TestClient(create_app(gateway)) as test_client:
        _call(test_client, "st000024", None, "/storage/v1/object/public/files/logo.png")
        assert _egress_row(project_id) == 0, "the meter flushed before shutdown"

    assert _egress_row(project_id) > 0, "bytes served were lost on a clean shutdown"
