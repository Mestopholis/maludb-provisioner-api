"""The shared storage worker: its configuration, and a real instance serving.

Two halves, and the split is deliberate.

The **pure** half — env rendering, the forwarded-host pattern, the podman
invocation — runs everywhere, because those are where a mistake is silent. A
loopback address that should have been refused, an unanchored regexp, a
capability that should have been dropped: none of them fail, they just quietly
mean the containment is not what the comments say.

The **live** half needs Podman, the pinned image, a prepared node and an object
store, and it is where the claims that matter are made: that two tenants using
the same bucket and key names read back their own bytes, that a token signed for
one tenant reaches nothing of another's, and that the container cannot reach the
node's loopback. `scripts/storage-test-cluster.sh` builds what it needs;
`MALUDB_REQUIRE_STORAGE_SERVER=1` turns an absent one into a failed run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

from services.control_plane import storage_workers as sw
from tests.conftest import (
    STORAGE_IMAGE,
    object_store_configured,
    requires_db,
    storage_env_config,
    storage_image_available,
)
from tests.test_provisioning import ADMIN_DSN, _tenant_admin_dsn

DATA_HOST = os.environ.get("MALUDB_STORAGE_DB_HOST", "").strip()

# Passed where a signature is never checked -- the calls below are refused for
# the ref before they reach the network.
NOT_A_SECRET = "unused-in-these-tests"  # noqa: S105

requires_storage_server = pytest.mark.skipif(
    not (storage_image_available() and object_store_configured() and DATA_HOST and ADMIN_DSN),
    reason=(
        f"needs {STORAGE_IMAGE}, an object store, MALUDB_STORAGE_DB_HOST and a node "
        "(scripts/storage-test-cluster.sh)"
    ),
)


def _secrets() -> sw.DerivedSecrets:
    return sw.derived_secrets("t" * 64)


def _settings(**overrides) -> sw.StorageSettings:
    base = {
        "secrets": _secrets(),
        "db_host": "10.91.0.1",
        "db_port": 5432,
        "port": 5000,
        "admin_port": 5001,
        "image": STORAGE_IMAGE,
        "memory_max": "1g",
        "s3_endpoint": "http://10.91.0.1:8333",
        "s3_bucket": "maludb",
        "s3_region": "us-east-1",
        "s3_access_key": "key",
        "s3_secret_key": "secret",
        "gateway_domain": "maludb.local",
    }
    base.update(overrides)
    return sw.StorageSettings(**base)


# -- configuration, which fails silently when it is wrong ------------------


def test_the_environment_refuses_a_loopback_data_address():
    """ADR-035. A container that can reach the node's loopback can reach every
    other worker on it, including tenants' PostgREST — which answers anonymous
    reads to anything that can open its port.

    Refused rather than warned about, because a badly contained worker that
    starts is worse than one that does not."""
    with pytest.raises(sw.StorageWorkerError, match="loopback"):
        sw.render_env(_settings(db_host="127.0.0.1"))
    with pytest.raises(sw.StorageWorkerError, match="loopback"):
        sw.render_env(_settings(db_host="localhost"))


def test_the_environment_refuses_a_loopback_object_store():
    """The second reason, on top of ADR-035's: an endpoint that only works from
    the node is one that has assumed the store is co-located, which is exactly
    what ADR-055's exit to dedicated hardware depends on nobody assuming."""
    with pytest.raises(sw.StorageWorkerError, match="loopback"):
        sw.render_env(_settings(s3_endpoint="http://127.0.0.1:8333"))


def test_a_newline_cannot_forge_a_second_environment_entry():
    """The env file is read line-wise by systemd and by podman, both of which
    take the rest of a line literally. Generated values cannot contain a
    newline; a configured image or domain could, and this is where they cross."""
    with pytest.raises(sw.StorageWorkerError, match="newline"):
        sw.render_env(_settings(image="img\nSERVER_ADMIN_API_KEYS=attacker"))


def test_the_roles_are_not_installed_and_the_deferred_features_are_off():
    env = sw.render_env(_settings())
    # ADR-004 and ADR-016: true here would have the container create `anon`,
    # `authenticated` and `service_role` — names shared with every tenant on the
    # cluster.
    assert "DB_INSTALL_ROLES=false" in env
    # Deferred by Phase 10 and named rather than left to a default, because a
    # deferral that arrives by omission is a decision nobody made. The S3
    # protocol endpoint is a credential plus a reachable endpoint, which is
    # ADR-039's paid line; image transformation needs imgproxy, which slice 0
    # explicitly did not clear on a CPU with no AVX2.
    assert "S3_PROTOCOL_ENABLED=false" in env
    assert "IMAGE_TRANSFORMATION_ENABLED=false" in env
    assert "MULTI_TENANT=true" in env


def test_the_forwarded_host_pattern_is_anchored_and_escaped():
    """Unanchored, `evil.com/?x=abcd0001.maludb.local` names a tenant.
    Unescaped, `maludb.local` also matches `maludbxlocal`."""
    import re

    from services.control_plane import models

    pattern = re.compile(sw.forwarded_host_regexp("maludb.local"))
    assert pattern.match("abcd0001.maludb.local").group(1) == "abcd0001"
    assert pattern.match("evil.com/?x=abcd0001.maludb.local") is None
    assert pattern.match("abcd0001.maludbxlocal") is None
    assert pattern.match("abcd0001.maludb.local.evil.com") is None

    # Exactly what `models.is_valid_project_ref` accepts, and nothing else. The
    # length is taken from that module rather than written here: the pattern was
    # first written as `{4,16}`, which would have let the worker resolve a
    # tenant name no project could ever have.
    assert models.PROJECT_REF_LENGTH == 8
    assert pattern.match("abc00001".ljust(9, "x") + ".maludb.local") is None
    assert pattern.match("abcd001.maludb.local") is None
    assert pattern.match("ABCD0001.maludb.local") is None
    assert pattern.match("abcd-001.maludb.local") is None


def test_the_container_drops_every_capability():
    """Stronger than the Realtime unit, deliberately. That image needs SETUID
    and SETGID back because its entrypoint runs a migration through sudo; this
    one is Node as a non-root user with no such step, so the stronger
    containment is available — and taken, rather than matched to the weaker
    neighbour for symmetry."""
    args = sw.podman_args(_settings())
    assert "--cap-drop" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "--cap-add" not in args
    assert "no-new-privileges" in args
    assert "--network=slirp4netns" in args
    # Without `allow_host_loopback`, which is the whole point.
    assert not any("allow_host_loopback" in a for a in args)


def test_both_ports_are_published_on_loopback_only():
    """The data port is the gateway's to reach. The admin port can reconfigure
    any tenant on the node — including its database URL — which is ADR-037's
    internal side, and it must never face the gateway's proxy path."""
    args = sw.podman_args(_settings())
    published = [args[i + 1] for i, a in enumerate(args) if a == "--publish"]
    assert published == ["127.0.0.1:5000:5000", "127.0.0.1:5001:5001"]


def test_the_unit_and_the_module_run_the_same_container():
    """Two places describe the invocation and they must not drift. This is the
    test that says so when they do — the same guarantee
    `tests/test_realtime_workers.py` holds for its unit."""
    unit = Path("deploy/maludb-storage.service").read_text()
    # Comments stripped before matching. The unit explains at length *why*
    # `allow_host_loopback` is absent, so a naive substring check over the whole
    # file finds the word in the paragraph warning against it -- which is how
    # the first version of this test failed.
    directives = "\n".join(
        line for line in unit.splitlines() if not line.lstrip().startswith("#")
    )
    args = sw.podman_args(_settings())
    for flag in ("--network=slirp4netns", "--cap-drop", "no-new-privileges", "--rm"):
        assert flag in directives, f"{flag} is in podman_args but not in the unit"
    assert "allow_host_loopback" not in directives
    # The published ports, as the unit spells them.
    assert "127.0.0.1:${MALUDB_STORAGE_HOST_PORT}:5000" in unit
    assert "127.0.0.1:${MALUDB_STORAGE_ADMIN_HOST_PORT}:5001" in unit
    assert f"{sw.CONTAINER_PORT}" in "".join(args)


def test_settings_refuse_an_unprepared_node():
    """A node with no data address or no object store cannot run a contained
    worker. Refused with the names of what is missing, because the alternative
    is a worker that starts and cannot serve."""

    class _Config:
        storage_db_host = None
        storage_s3_endpoint = None
        storage_s3_access_key = None
        storage_s3_secret_key = None

    with pytest.raises(sw.StorageWorkerError) as raised:
        sw.settings_for(_Config(), _secrets())
    assert "MALUDB_STORAGE_DB_HOST" in str(raised.value)
    assert "MALUDB_STORAGE_S3_ENDPOINT" in str(raised.value)


def test_the_derived_secrets_are_distinct_and_stable():
    """One stored root carries an instance, and separate HKDF info strings mean
    a leak of the admin key does not also hand over the key that decrypts every
    registered tenant's database URL."""
    first, second = sw.derived_secrets("root"), sw.derived_secrets("root")
    assert first == second, "a rebuilt instance must derive the same values"
    assert len({first.admin_api_key, first.auth_encryption_key, first.metadata_password}) == 3
    assert sw.derived_secrets("other").admin_api_key != first.admin_api_key


def test_a_tenant_is_never_registered_with_a_zero_file_size_limit():
    """`fileSizeLimit: 0` reads like "no limit" and means zero bytes: every
    upload answers 413. Found by booting the thing, which is why there is a
    constant and a test rather than a literal."""
    payload = sw.tenant_payload(
        project_ref="abcd0001", tenant_dsn="postgresql://x",
        jwt_secret=NOT_A_SECRET, database_pool_size=5,
    )
    assert payload["fileSizeLimit"] == sw.TENANT_FILE_SIZE_LIMIT > 0


def test_an_invalid_project_ref_never_reaches_the_admin_api():
    """`AGENTS.md` requires identifiers generated from project metadata to be
    validated. A ref reaches the admin API as a path segment and as a tenant
    id."""
    for bad in ("../tenants", "ab", "ABCD0001", "abcd 0001", ""):
        with pytest.raises(sw.StorageWorkerError, match="invalid project ref"):
            sw.register_tenant(
                admin_port=1, api_key="k", project_ref=bad,
                tenant_dsn="postgresql://x", jwt_secret=NOT_A_SECRET,
            )
        with pytest.raises(sw.StorageWorkerError, match="invalid project ref"):
            sw.deregister_tenant(admin_port=1, api_key="k", project_ref=bad)


# -- a real instance -------------------------------------------------------


@pytest.fixture(scope="module")
def worker(tmp_path_factory):
    """One shared instance, started once for this module. ADR-058's topology is
    the reason a module-scoped fixture is honest here: there is one per node."""
    if not (storage_image_available() and object_store_configured() and DATA_HOST and ADMIN_DSN):
        pytest.skip("no storage server available")

    config = storage_env_config()
    secrets_ = _secrets()
    settings = sw.settings_for(config, secrets_)
    config_dir = tmp_path_factory.mktemp("storage-config")

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
        sw.ensure_metadata_database(
            admin_conn,
            password=secrets_.metadata_password,
            metadata_connect=lambda db: psycopg.connect(_tenant_admin_dsn(db), autocommit=True),
        )

    sw.write_env(settings, config_dir=config_dir)
    subprocess.run(["podman", "rm", "-f", sw.CONTAINER_NAME], capture_output=True, check=False)  # noqa: S603, S607
    # Kept rather than discarded, and that is the difference between a
    # diagnosis and a guess. This fixture sent both streams to DEVNULL for one
    # release, so when CI's node turned out not to be listening on the storage
    # data address the only evidence was "never became ready" -- while the
    # container had been saying ECONNREFUSED and the address once a second for a
    # minute.
    log_path = config_dir / "worker.log"
    log = log_path.open("wb")
    process = subprocess.Popen(  # noqa: S603
        sw.podman_args(settings, config_dir=config_dir),
        stdout=log, stderr=subprocess.STDOUT,
    )

    deadline = time.time() + sw.READINESS_TIMEOUT_SECONDS
    while time.time() < deadline:
        if sw.is_ready(admin_port=settings.admin_port, api_key=secrets_.admin_api_key):
            break
        time.sleep(sw.READINESS_POLL_SECONDS)
    else:
        process.kill()
        log.flush()
        output = log_path.read_text(errors="replace").strip()
        pytest.fail(
            f"the storage worker never became ready in {sw.READINESS_TIMEOUT_SECONDS:.0f}s "
            f"(db {settings.db_host}:{settings.db_port}, s3 {settings.s3_endpoint}). "
            f"Container output:\n{output[-4000:] or '<nothing>'}"
        )

    yield settings, secrets_

    subprocess.run(["podman", "rm", "-f", sw.CONTAINER_NAME], capture_output=True, check=False)  # noqa: S603, S607
    process.wait(timeout=30)
    log.close()


def _provision_storage_tenant(ref: str) -> tuple[str, str]:
    """A tenant the worker can serve: roles, database, bootstrap 012.

    Built here rather than through `jobs.provision` because this module is about
    the worker, and a full provisioning run would make every assertion below
    depend on machinery two slices older.
    """
    database = f"mldb_{ref}"
    password = uuid.uuid4().hex
    jwt_secret = uuid.uuid4().hex * 2
    bootstrap = Path("services/control_plane/bootstrap/012_storage_schema.sql").read_text()

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        for role in ("authenticator", "auth", "admin", "storage"):
            conn.execute(f'DROP ROLE IF EXISTS "{database}_{role}"')
        conn.execute(
            f"CREATE ROLE \"{database}_authenticator\" LOGIN PASSWORD '{uuid.uuid4().hex}' NOINHERIT"
        )
        conn.execute(f"CREATE ROLE \"{database}_auth\" LOGIN PASSWORD '{uuid.uuid4().hex}' NOINHERIT")
        conn.execute(f'CREATE ROLE "{database}_admin" NOLOGIN NOINHERIT')
        conn.execute(
            f"CREATE ROLE \"{database}_storage\" LOGIN PASSWORD '{password}' NOINHERIT "
            "NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
        )
        conn.execute(f'GRANT anon, authenticated, service_role TO "{database}_storage"')
        conn.execute(f'CREATE DATABASE "{database}" OWNER postgres')
        conn.execute(f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC')
        conn.execute(f'GRANT CONNECT ON DATABASE "{database}" TO "{database}_storage"')

    with psycopg.connect(_tenant_admin_dsn(database), autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS maludb_platform")
        conn.execute(bootstrap)

    dsn = f"postgresql://{database}_storage:{password}@{DATA_HOST}:5432/{database}"
    return dsn, jwt_secret


def _token(jwt_secret: str, role: str = "service_role") -> str:
    import jwt as pyjwt

    return pyjwt.encode(
        {"role": role, "iss": "supabase", "sub": "test", "exp": int(time.time()) + 600},
        jwt_secret,
        algorithm="HS256",
    )


def _call(settings, method: str, path: str, ref: str, jwt_secret: str, **kwargs):
    headers = {
        "authorization": f"Bearer {_token(jwt_secret)}",
        "x-forwarded-host": f"{ref}.{settings.gateway_domain}",
    }
    headers.update(kwargs.pop("headers", {}))
    return httpx.request(
        method, f"http://127.0.0.1:{settings.port}{path}", headers=headers, timeout=30, **kwargs
    )


@requires_storage_server
@requires_db
def test_two_tenants_with_the_same_names_read_back_their_own_bytes(worker):
    """ADR-057's whole claim, on one shared instance.

    One platform bucket holds every tenant's objects, so isolation is a property
    of the key prefix and the metadata rather than of the object store. Two
    tenants create a bucket of the **same name** holding a key of the **same
    name**; if the prefix were ever taken from the request rather than from the
    resolved tenant, one of these reads would return the other's bytes.
    """
    settings, secrets_ = worker
    first, second = "swk00001", "swk00002"
    bodies = {first: b"first tenant bytes", second: b"second tenant bytes"}
    jwt_secrets = {}

    for ref in (first, second):
        dsn, jwt_secret = _provision_storage_tenant(ref)
        jwt_secrets[ref] = jwt_secret
        sw.register_tenant(
            admin_port=settings.admin_port, api_key=secrets_.admin_api_key,
            project_ref=ref, tenant_dsn=dsn, jwt_secret=jwt_secret,
        )
        created = _call(
            settings, "POST", "/bucket", ref, jwt_secret,
            json={"name": "shared-name", "id": "shared-name", "public": False},
        )
        assert created.status_code == 200, created.text
        uploaded = _call(
            settings, "POST", "/object/shared-name/secret.txt", ref, jwt_secret,
            content=bodies[ref], headers={"content-type": "text/plain"},
        )
        assert uploaded.status_code == 200, uploaded.text

    for ref in (first, second):
        got = _call(settings, "GET", "/object/shared-name/secret.txt", ref, jwt_secrets[ref])
        assert got.status_code == 200, got.text
        assert got.content == bodies[ref], "a tenant read another tenant's bytes"


@requires_storage_server
@requires_db
def test_the_object_keys_are_prefixed_by_tenant_in_one_platform_bucket(worker):
    """The layout ADR-057 depends on, read out of the store itself rather than
    inferred from the API's answers. Object bytes are also outside PostgreSQL,
    which is Phase 10's first acceptance criterion."""
    settings, secrets_ = worker
    ref = "swk00003"
    dsn, jwt_secret = _provision_storage_tenant(ref)
    sw.register_tenant(
        admin_port=settings.admin_port, api_key=secrets_.admin_api_key,
        project_ref=ref, tenant_dsn=dsn, jwt_secret=jwt_secret,
    )
    _call(
        settings, "POST", "/bucket", ref, jwt_secret,
        json={"name": "files", "id": "files", "public": False},
    )
    assert _call(
        settings, "POST", "/object/files/note.txt", ref, jwt_secret,
        content=b"in the object store", headers={"content-type": "text/plain"},
    ).status_code == 200

    import boto3
    from botocore.config import Config

    config = storage_env_config()
    s3 = boto3.client(
        "s3",
        endpoint_url=config.storage_s3_endpoint,
        aws_access_key_id=config.storage_s3_access_key,
        aws_secret_access_key=config.storage_s3_secret_key,
        region_name=config.storage_s3_region,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )
    listed = s3.list_objects_v2(Bucket=config.storage_s3_bucket, Prefix=f"{ref}/")
    keys = [item["Key"] for item in listed.get("Contents", [])]
    assert keys, "the object never reached the object store"
    assert all(key.startswith(f"{ref}/files/note.txt/") for key in keys), keys


@requires_storage_server
@requires_db
def test_a_token_signed_for_one_tenant_reaches_nothing_of_anothers(worker):
    """The second of the two boundaries slice 0 measured, and the one the
    platform is allowed to rely on: the host selects the tenant, and that
    tenant's own JWT secret must verify.

    Also pins slice 0's observation that the two routes report it differently.
    `/bucket` says `403 signature verification failed`; the object path masks
    the same denial as a `404`. Both deny, and the masking matters for
    monitoring — a counter of authentication failures will not see cross-tenant
    attempts on the busiest route in the service.
    """
    settings, secrets_ = worker
    victim, attacker = "swk00004", "swk00005"
    secrets_by_ref = {}
    for ref in (victim, attacker):
        dsn, jwt_secret = _provision_storage_tenant(ref)
        secrets_by_ref[ref] = jwt_secret
        sw.register_tenant(
            admin_port=settings.admin_port, api_key=secrets_.admin_api_key,
            project_ref=ref, tenant_dsn=dsn, jwt_secret=jwt_secret,
        )

    _call(
        settings, "POST", "/bucket", victim, secrets_by_ref[victim],
        json={"name": "private", "id": "private", "public": False},
    )
    assert _call(
        settings, "POST", "/object/private/secret.txt", victim, secrets_by_ref[victim],
        content=b"the victim's bytes", headers={"content-type": "text/plain"},
    ).status_code == 200

    # The attacker's own token, aimed at the victim's host.
    forged = httpx.get(
        f"http://127.0.0.1:{settings.port}/object/private/secret.txt",
        headers={
            "authorization": f"Bearer {_token(secrets_by_ref[attacker])}",
            "x-forwarded-host": f"{victim}.{settings.gateway_domain}",
        },
        timeout=30,
    )
    assert forged.status_code >= 400
    assert b"the victim's bytes" not in forged.content

    listed = httpx.get(
        f"http://127.0.0.1:{settings.port}/bucket",
        headers={
            "authorization": f"Bearer {_token(secrets_by_ref[attacker])}",
            "x-forwarded-host": f"{victim}.{settings.gateway_domain}",
        },
        timeout=30,
    )
    # Denied for the right reason, which is the assertion that matters: the
    # tenant's own JWT secret did not verify the attacker's token.
    assert "signature verification failed" in listed.text.lower(), listed.text

    # And a correction to slice 0's record, pinned so it stays true. That spike
    # wrote "403 signature verification failed"; measured here, the **HTTP
    # status is 400** and only the JSON body carries 403. Both slice 0's note
    # about the object path masking a denial as a 404 and this one point the
    # same way: nothing useful about Storage authentication failures can be
    # counted from HTTP status codes alone, which is a monitoring fact worth
    # knowing before an incident rather than during one.
    assert listed.status_code == 400, listed.status_code
    assert json.loads(listed.text)["statusCode"] == "403"


@requires_storage_server
def test_an_unregistered_host_names_no_tenant(worker):
    """`400 TenantNotFound`, and it is worth asserting because the alternative
    would be a request evaluated against whatever tenant the worker reached
    for."""
    settings, _ = worker
    response = httpx.get(
        f"http://127.0.0.1:{settings.port}/bucket",
        headers={"x-forwarded-host": f"zzzz9999.{settings.gateway_domain}"},
        timeout=30,
    )
    assert response.status_code >= 400
    assert "zzzz9999" not in response.text or "not" in response.text.lower()


@requires_storage_server
@requires_db
def test_registering_a_tenant_twice_rewrites_rather_than_failing(worker):
    """PUT, not POST. Upstream offers both: `POST` inserts and answers 500 with
    a primary key violation the second time, `PUT` upserts. Written as POST
    first, and this is the test that would have caught it — a provisioning retry
    is the ordinary case, not the exotic one."""
    settings, secrets_ = worker
    ref = "swk00006"
    dsn, jwt_secret = _provision_storage_tenant(ref)
    for _ in range(2):
        sw.register_tenant(
            admin_port=settings.admin_port, api_key=secrets_.admin_api_key,
            project_ref=ref, tenant_dsn=dsn, jwt_secret=jwt_secret,
        )
    assert _call(settings, "GET", "/bucket", ref, jwt_secret).status_code == 200


@requires_storage_server
@requires_db
def test_deregistering_a_tenant_is_idempotent_and_stops_service(worker):
    """A 404 from the admin API is success: the goal is that the worker does not
    serve this tenant, and one that has never heard of it already meets that."""
    settings, secrets_ = worker
    ref = "swk00007"
    dsn, jwt_secret = _provision_storage_tenant(ref)
    sw.register_tenant(
        admin_port=settings.admin_port, api_key=secrets_.admin_api_key,
        project_ref=ref, tenant_dsn=dsn, jwt_secret=jwt_secret,
    )
    assert _call(settings, "GET", "/bucket", ref, jwt_secret).status_code == 200

    sw.deregister_tenant(
        admin_port=settings.admin_port, api_key=secrets_.admin_api_key, project_ref=ref
    )
    sw.deregister_tenant(  # again: must not raise
        admin_port=settings.admin_port, api_key=secrets_.admin_api_key, project_ref=ref
    )
    assert _call(settings, "GET", "/bucket", ref, jwt_secret).status_code >= 400


@requires_storage_server
def test_the_admin_api_refuses_a_wrong_key(worker):
    """It can reconfigure any tenant on the node, including its database URL.
    ADR-037 puts it on the internal side; this is the check that it is not also
    unauthenticated."""
    settings, _ = worker
    response = httpx.get(
        f"http://127.0.0.1:{settings.admin_port}/tenants",
        headers={"apikey": "not-the-key"},
        timeout=10,
    )
    assert response.status_code in (401, 403)
    assert not sw.is_ready(admin_port=settings.admin_port, api_key="not-the-key")


@requires_storage_server
def test_the_container_reaches_the_object_store_and_not_the_nodes_loopback(worker):
    """ADR-035, measured for a second component.

    The container reaches the object store on its data address and cannot reach
    the node's loopback — where every tenant's PostgREST answers anonymous reads
    to anything that can open its port. This is also what makes ADR-055's exit
    to dedicated storage hardware an endpoint change rather than a migration:
    the store is addressed as though remote because it cannot be addressed any
    other way.
    """
    settings, _ = worker
    config = storage_env_config()
    script = (
        "fetch(process.argv[1])"
        ".then(r => console.log('reached', r.status))"
        ".catch(e => console.log('refused', e.cause?.code || e.message))"
    )

    def probe(url: str) -> str:
        return subprocess.run(  # noqa: S603
            ["podman", "exec", sw.CONTAINER_NAME, "node", "-e", script, url],  # noqa: S607
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout.strip()

    # The object store, on the data address: reached. 403 is S3 refusing an
    # unsigned request, which is a reply and therefore proof of reach.
    assert probe(config.storage_s3_endpoint).startswith("reached")

    # The same store, addressed on the node's loopback: refused.
    loopback = config.storage_s3_endpoint.replace(
        httpx.URL(config.storage_s3_endpoint).host, "127.0.0.1"
    )
    assert probe(loopback).startswith("refused"), (
        "the container reached the node's loopback; ADR-035's containment is gone"
    )


@requires_storage_server
def test_the_environment_file_is_private_before_it_has_content(tmp_path):
    """It carries the object-store credential, the admin API key, and the key
    that decrypts every registered tenant's database URL — the node's whole
    Storage trust anchor in one file. Written 0600 at creation rather than
    chmod'ed afterwards, which would leave a window."""
    path = sw.write_env(_settings(), config_dir=tmp_path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    content = path.read_text()
    assert "AWS_SECRET_ACCESS_KEY=" in content
    assert json.dumps  # noqa: B018 - keeps the import honest for the linter
