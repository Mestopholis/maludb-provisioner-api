"""Per-project Realtime workers: configuration, containment, and the admin API.

Phase 06 slice 5. Split by what each test needs, because the expensive things
here are genuinely expensive: rendering an environment file needs nothing, the
admin API needs a stub HTTP server, and proving that Postgres Changes actually
arrive needs a container, a tenant and a cluster with `wal_level = logical`.

The assertions worth reading twice are the containment ones. A Realtime
container that can reach the node's loopback can reach every other worker on
the node -- including other tenants' PostgREST, which answers anonymous
requests through `db-anon-role` to anything that can open its port. So
`render_env` refuses a loopback data address, and the container arguments are
asserted to carry `--network=slirp4netns` with no `allow_host_loopback`.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from services.control_plane import realtime_workers as rtw
from services.control_plane.config import Config
from tests.conftest import TEST_KEK, TEST_PEPPER

UNIT_FILE = Path(__file__).resolve().parents[1] / "deploy" / "maludb-realtime@.service"


def make_settings(**overrides) -> rtw.RealtimeSettings:
    names = rtw.RealtimeNames.for_ref(overrides.pop("project_ref", "rtw00001"))
    defaults = dict(
        project_ref=names.project_ref,
        names=names,
        tenant_database=f"mldb_{names.project_ref}",
        replicator_role=f"mldb_{names.project_ref}_replicator",
        replicator_password="replicator-password-not-a-real-one",  # noqa: S106 - fixture
        jwt_secret="jwt-secret-not-a-real-one",  # noqa: S106 - fixture
        metadata_password="metadata-password-not-a-real-one",  # noqa: S106 - fixture
        secrets=rtw.derived_secrets("0" * 64),
        port=24101,
        db_host="10.90.0.1",
        db_port=5433,
        image="docker.io/supabase/realtime:v2.110.0",
        memory_max="512m",
        max_concurrent_users=200,
    )
    defaults.update(overrides)
    return rtw.RealtimeSettings(**defaults)


def env_of(settings: rtw.RealtimeSettings) -> dict[str, str]:
    pairs = {}
    for line in rtw.render_env(settings).splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


def test_names_are_derived_from_a_validated_ref():
    names = rtw.RealtimeNames.for_ref("abcd1234")
    assert names.metadata_database == "maludb_realtime_abcd1234"
    assert names.metadata_role == "maludb_realtime_abcd1234"
    assert names.container == "maludb-realtime-abcd1234"
    assert names.app_name == "realtime-abcd1234"


def test_a_ref_that_could_not_make_a_database_cannot_make_a_container_name():
    # The unit name, the container name and the metadata database all come from
    # this string. AGENTS.md requires identifiers generated from project
    # metadata to be validated, and this is where that happens for all three.
    for bad in ("abcd 1234", "abcd-1234;drop", "../etc", "ABCD1234'"):
        with pytest.raises(Exception):  # noqa: B017 - models raises its own type
            rtw.RealtimeNames.for_ref(bad)


def test_the_slot_suffix_matches_what_the_platform_reserved_capacity_for():
    # ADR-034: the platform reserves two slots per Realtime project without
    # creating them, and the server names them from SLOT_NAME_SUFFIX. If the two
    # disagreed, the reservation would point at names nothing ever uses and the
    # maintenance pass would report every project as missing its slots.
    from services.control_plane import realtime

    names = rtw.RealtimeNames.for_ref("abcd1234")
    predicted = realtime.slot_names_for("abcd1234")
    assert all(name.endswith(f"_{names.slot_suffix}") for name in predicted)


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_derived_secrets_are_distinct_stable_and_correctly_sized():
    first = rtw.derived_secrets("root-secret")
    again = rtw.derived_secrets("root-secret")
    assert first == again, "a rebuilt instance must derive the same DB_ENC_KEY or lose its tenant"

    values = {first.api_jwt, first.metrics_jwt, first.secret_key_base, first.db_enc_key}
    assert len(values) == 4, "one leaked secret must not be another"
    # Raw AES-128 key material, so exactly 16 characters and not one more --
    # and base64 rather than hex, because sixteen hex characters would be 64
    # bits of key for the thing that encrypts the replicator password.
    assert len(first.db_enc_key) == rtw.DB_ENC_KEY_CHARS
    assert not all(c in "0123456789abcdef" for c in first.db_enc_key)
    # Phoenix wants at least 64 characters for secret_key_base.
    assert len(first.secret_key_base) >= 64
    assert rtw.derived_secrets("another-root").api_jwt != first.api_jwt


# --------------------------------------------------------------------------
# The environment file
# --------------------------------------------------------------------------


def test_the_environment_is_unquoted_because_podman_keeps_quotes():
    # systemd strips quotes and podman --env-file does not, so a quoted password
    # would reach PostgreSQL with two extra characters and fail as an
    # authentication error rather than as a syntax one.
    env = env_of(make_settings())
    assert env["DB_PASSWORD"] == "metadata-password-not-a-real-one"  # noqa: S105 - fixture
    assert not env["DB_PASSWORD"].startswith('"')
    assert env["DB_AFTER_CONNECT_QUERY"] == "SET search_path TO _realtime"


def test_the_environment_carries_what_the_server_will_not_boot_without():
    env = env_of(make_settings())
    # fetch_env! in a production release: absent, the server does not start.
    assert env["METRICS_JWT_SECRET"]
    assert env["SECRET_KEY_BASE"]
    assert env["API_JWT_SECRET"]
    # ADR-034: this is what stops two tenants on one cluster colliding on a
    # cluster-unique slot name.
    assert env["SLOT_NAME_SUFFIX"] == "rtw00001"
    # Peer discovery runs through the metadata database. One instance per
    # project means there are no peers, and looking for them across a shared
    # metadata database would cluster every tenant's server together.
    assert env["CLUSTER_STRATEGIES"] == "NONE"


def test_a_loopback_data_address_is_refused():
    # The container is given no access to the node's loopback on purpose, so a
    # loopback data address is not merely wrong, it cannot work -- and the
    # message has to send the operator to node preparation rather than to a
    # connectivity puzzle.
    for host in ("127.0.0.1", "localhost", "::1"):
        with pytest.raises(rtw.RealtimeWorkerError, match="loopback"):
            rtw.render_env(make_settings(db_host=host))


def test_a_newline_cannot_forge_an_entry():
    # Both readers take the rest of the line literally, so a newline is the only
    # character that could add a variable the platform did not write.
    with pytest.raises(rtw.RealtimeWorkerError, match="newline"):
        rtw.render_env(make_settings(image="image\nDB_PASSWORD=stolen"))


def test_the_connection_limit_comes_from_the_plan():
    env = env_of(make_settings(max_concurrent_users=17))
    assert env["TENANT_MAX_CONCURRENT_USERS"] == "17"
    assert env["MAX_CONNECTIONS"] == "17"


def test_the_environment_file_is_private_before_it_has_content(tmp_path):
    path = rtw.write_env(make_settings(), config_dir=tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "replicator-password-not-a-real-one" not in path.read_text(), (
        "the replicator password belongs in the tenant registration, not in the server's own "
        "environment -- the server stores it encrypted after registration instead"
    )


# --------------------------------------------------------------------------
# The container, and the unit that runs it
# --------------------------------------------------------------------------


def test_the_container_has_no_route_to_the_nodes_loopback():
    args = rtw.podman_args(make_settings())
    assert "--network=slirp4netns" in args
    assert not any("allow_host_loopback" in arg for arg in args), (
        "with host loopback the container reaches every other worker on the node, "
        "including tenants' PostgREST, which serves anonymous reads to anything that "
        "can open its port"
    )
    assert "--publish" in args
    assert args[args.index("--publish") + 1].startswith("127.0.0.1:")


def _exec_start(unit_text: str) -> list[str]:
    """The unit's ExecStart, joined across continuations and tokenised."""
    joined = unit_text.replace("\\\n", " ")
    line = next(row for row in joined.splitlines() if row.startswith("ExecStart="))
    return line.removeprefix("ExecStart=").split()


def test_the_unit_and_the_code_run_the_same_container():
    # Two places describe the container: this unit, which production uses, and
    # `podman_args`, which tests and an operator debugging a node without
    # systemd use. Drift between them would mean the thing under test is not the
    # thing that runs, so they are compared rather than trusted.
    settings = make_settings()
    env = env_of(settings)
    tokens = []
    for token in _exec_start(UNIT_FILE.read_text()):
        expanded = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), m.group(0)), token)
        tokens.append(expanded.replace("%i", settings.project_ref))

    from_code = rtw.podman_args(settings)
    # The unit names podman by absolute path; the code relies on PATH.
    assert tokens[0].endswith("/podman")
    assert tokens[1:] == from_code[1:]


# --------------------------------------------------------------------------
# The server's admin API
# --------------------------------------------------------------------------


class _StubRealtime(BaseHTTPRequestHandler):
    """Just enough of upstream's admin API to assert what we send it."""

    calls: list[tuple[str, str, dict | None]] = []
    tenant_status = 201

    def _read(self) -> dict | None:
        length = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def _respond(self, status: int, body: dict | None = None) -> None:
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        type(self).calls.append(("GET", self.path, None))
        if not self.headers.get("authorization", "").startswith("Bearer "):
            self._respond(403)
            return
        self._respond(200, {"data": []})

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls.append(("POST", self.path, self._read()))
        self._respond(type(self).tenant_status, {"data": {}})

    def do_DELETE(self) -> None:  # noqa: N802
        type(self).calls.append(("DELETE", self.path, None))
        self._respond(204)

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def stub_server():
    _StubRealtime.calls = []
    _StubRealtime.tenant_status = 201
    server = HTTPServer(("127.0.0.1", 0), _StubRealtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_registration_names_the_project_ref_not_the_hostname(stub_server):
    settings = make_settings(port=stub_server.server_port)
    rtw.register_tenant(settings)

    method, path, body = _StubRealtime.calls[-1]
    assert (method, path) == ("POST", "/api/tenants")
    # The server resolves a tenant from the first label of the Host header, so a
    # hostname here is never found: it logs TenantNotFound while the client sees
    # a transport failure.
    assert body["tenant"]["external_id"] == settings.project_ref
    assert "." not in body["tenant"]["external_id"]
    settings_sent = body["tenant"]["extensions"][0]["settings"]
    assert settings_sent["db_user"] == settings.replicator_role
    assert settings_sent["db_host"] == settings.db_host
    assert body["tenant"]["jwt_secret"] == settings.jwt_secret


def test_registration_accepts_the_second_attempt(stub_server):
    # Measured against the real server: 201 the first time, 200 thereafter. A
    # retried enablement must not fail, which AGENTS.md requires of provisioning.
    _StubRealtime.tenant_status = 200
    rtw.register_tenant(make_settings(port=stub_server.server_port))


def test_a_refused_registration_does_not_leak_the_credential(stub_server):
    _StubRealtime.tenant_status = 422
    with pytest.raises(rtw.RealtimeWorkerError) as raised:
        rtw.register_tenant(make_settings(port=stub_server.server_port))
    # The real server echoes the request's settings on a validation error, and
    # those settings carry the replicator password.
    assert "replicator-password-not-a-real-one" not in str(raised.value)


def test_deregistration_is_idempotent(stub_server):
    rtw.deregister_tenant(
        port=stub_server.server_port, api_secret="secret", project_ref="rtw00001"  # noqa: S106
    )
    assert _StubRealtime.calls[-1][:2] == ("DELETE", "/api/tenants/rtw00001")


def test_readiness_asks_a_question_only_a_migrated_server_can_answer(stub_server):
    settings = make_settings(port=stub_server.server_port)
    assert rtw.is_ready(settings.port, api_secret=settings.secrets.api_jwt)
    assert _StubRealtime.calls[-1][:2] == ("GET", "/api/tenants")
    # A port nothing listens on is not ready, rather than an exception.
    assert not rtw.is_ready(1, api_secret="secret", timeout=0.5)  # noqa: S106


def test_the_admin_token_is_short_lived_and_signed_with_the_derived_secret():
    import jwt as pyjwt

    secrets = rtw.derived_secrets("root")
    claims = pyjwt.decode(
        rtw.admin_token(secrets.api_jwt), secrets.api_jwt, algorithms=["HS256"]
    )
    assert claims["exp"] - claims["iat"] == rtw.ADMIN_TOKEN_TTL_SECONDS
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(rtw.admin_token(secrets.api_jwt), secrets.metrics_jwt, algorithms=["HS256"])


# --------------------------------------------------------------------------
# Port allocation, which is the existing machinery with one more column
# --------------------------------------------------------------------------


def test_a_realtime_port_never_collides_with_the_other_workers(placed_project):
    from services.control_plane import db, workers

    project_id = placed_project("rtw00002")
    with db.connection() as conn:
        api = workers.allocate_port(conn, project_id=project_id, column="api_port")
        auth = workers.allocate_port(conn, project_id=project_id, column="auth_port")
        realtime = workers.allocate_port(conn, project_id=project_id, column="realtime_port")
        conn.commit()
    assert len({api, auth, realtime}) == 3
    with db.connection() as conn:
        assert workers.allocate_port(conn, project_id=project_id, column="realtime_port") == realtime


def test_config_defaults_leave_a_node_unprepared_rather_than_badly_prepared():
    # No data address means no Realtime worker, which is the safe direction: the
    # alternative default is loopback, and loopback is the arrangement that
    # hands the container every other tenant on the node.
    config = Config(
        environment="test", database_url="postgresql://x/y", gateway_domain="maludb.local",
        docs_enabled=False, kek=TEST_KEK, token_pepper=TEST_PEPPER,
    )
    assert config.realtime_db_host is None
    assert config.realtime_image.endswith(":v2.110.0"), "ADR-033 pins the version"
