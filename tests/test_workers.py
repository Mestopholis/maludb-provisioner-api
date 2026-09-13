"""Per-project PostgREST workers.

The interesting assertions here are the ones Phase 00 and ADR-022 paid for:
that the worker connects as the tenant's constrained role rather than a
superuser, that readiness means "answers" rather than "port is open", and that
a table created after the worker started is queryable without restarting it.

The last one runs against a real PostgREST rather than a stub, because the
thing under test is PostgREST's own behaviour.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
import time

import psycopg
import pytest

from services.control_plane import db, tenant_bootstrap, workers
from tests.conftest import requires_db
from tests.test_provisioning import (
    ADMIN_DSN,
    PLATFORM_OWNER,
    _provision_core,
    _tenant_admin_dsn,
    requires_maludb_core,
)

pytestmark = [requires_db]

POSTGREST_BIN = os.environ.get("MALUDB_POSTGREST_BIN", "postgrest")
requires_postgrest = pytest.mark.skipif(
    shutil.which(POSTGREST_BIN) is None and not os.path.exists(POSTGREST_BIN),
    reason="PostgREST binary not available",
)
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

# Long enough for HS256, and obviously a fixture rather than anything live.
TEST_JWT_SECRET = "test-jwt-secret-not-for-production-" + "0" * 24  # noqa: S105


# -- configuration ---------------------------------------------------------


def _settings(**overrides) -> workers.WorkerSettings:
    base = {
        "project_ref": "wk000001",
        "database": "mldb_wk000001",
        "authenticator_role": "mldb_wk000001_authenticator",
        "authenticator_password": "s3cr3t-authenticator-password",
        "jwt_secret": TEST_JWT_SECRET,
        "port": 20001,
    }
    return workers.WorkerSettings(**{**base, **overrides})


def test_the_worker_connects_as_the_tenant_authenticator():
    """Never a superuser. PostgREST composes SQL from HTTP requests, so its
    database role is the last line of defence."""
    import re
    from urllib.parse import urlsplit

    rendered = workers.render_config(_settings())
    dsn = re.search(r'db-uri = "([^"]+)"', rendered).group(1)
    parsed = urlsplit(dsn)

    # Parsed rather than substring-matched: "postgres://" is the scheme, so a
    # naive search for "postgres" matches every valid config and would make
    # this assertion look strict while testing nothing.
    assert parsed.username == "mldb_wk000001_authenticator"
    assert parsed.username not in ("postgres", "maludb", PLATFORM_OWNER)
    assert parsed.path == "/mldb_wk000001", "the worker must be pinned to its own database"


def test_the_worker_listens_only_on_loopback():
    """docs/API-GATEWAY.md: internal worker endpoints must not be reachable
    from the internet. Binding the socket is stronger than a firewall rule."""
    rendered = workers.render_config(_settings())
    assert 'server-host = "127.0.0.1"' in rendered
    # S104 flags the literal; asserting its *absence* is the point.
    assert "0.0.0.0" not in rendered  # noqa: S104


def test_anon_is_the_unauthenticated_role_and_the_channel_is_enabled():
    rendered = workers.render_config(_settings())
    assert 'db-anon-role = "anon"' in rendered
    # Without the listener, bootstrap 006's NOTIFY goes nowhere and Phase 00
    # finding 3 comes straight back.
    assert "db-channel-enabled = true" in rendered
    # The MaluDB data-model graph reaches PostgREST as in-database config
    # (ADR-074); with this off, `maludb` would silently stop being served.
    assert "db-config = true" in rendered
    assert 'db-channel = "pgrst"' in rendered


def test_the_pre_request_check_is_named_only_when_asked_for():
    """ADR-076. Absent by default, so a caller that has not established the
    tenant has the function cannot render a config that fails every request."""
    assert "db-pre-request" not in workers.render_config(_settings())
    rendered = workers.render_config(_settings(pre_request=tenant_bootstrap.RPC_CHECK_FUNCTION))
    assert 'db-pre-request = "maludb_guard.refuse_extension_rpc"' in rendered


def test_a_worker_names_the_check_once_bootstrap_013_is_recorded():
    """Before 013 the function does not exist; from 013 it does, including the
    fleet run's intermediate state of 013 without 014's grants."""
    assert workers.pre_request_for(None) is None
    assert workers.pre_request_for(12) is None
    assert workers.pre_request_for(13) == tenant_bootstrap.RPC_CHECK_FUNCTION
    assert workers.pre_request_for(tenant_bootstrap.latest_version()) == tenant_bootstrap.RPC_CHECK_FUNCTION


def test_the_pool_size_matches_what_capacity_planning_assumes():
    """ADR-022 sized node density on this number; changing it silently would
    invalidate the measured warm-project ceiling."""
    assert workers.DEFAULT_POOL_SIZE == 3
    assert "db-pool = 3" in workers.render_config(_settings())


def test_the_config_file_is_never_world_readable(tmp_path):
    """Written 0600 before it has content. Creating it and then chmod-ing
    leaves a window where the password and JWT secret are readable, and on a
    shared node that window is enough."""
    path = workers.write_config(_settings(), config_dir=tmp_path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert "s3cr3t-authenticator-password" in path.read_text()


def test_rewriting_the_config_keeps_it_private(tmp_path):
    workers.write_config(_settings(), config_dir=tmp_path)
    path = workers.write_config(_settings(port=20002), config_dir=tmp_path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert "server-port = 20002" in path.read_text()


# -- supervision -----------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "abcd1234 --now", "abcd1234;reboot", "", "ABCD1234", "x" * 64],
)
def test_a_hostile_project_ref_never_reaches_systemctl(hostile):
    """AGENTS.md requires identifiers built from project metadata to be
    validated. Arguments are passed as a list so this is not shell injection --
    it is the weaker but real risk of acting on the wrong unit."""
    with pytest.raises(workers.WorkerError, match="invalid project ref"):
        workers.SystemdSupervisor().unit_for(hostile)


def test_the_unit_name_matches_the_shipped_template():
    """If these drift, the control plane starts units that do not exist."""
    unit = workers.SystemdSupervisor().unit_for("abcd1234")
    assert unit == "maludb-postgrest@abcd1234.service"
    template = open("deploy/maludb-postgrest@.service").read()
    assert "/etc/maludb/postgrest/%i.conf" in template


class RecordingSupervisor:
    """Substituted for systemd so lifecycle logic is testable without root."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start(self, project_ref: str) -> None:
        self.started.append(project_ref)

    def stop(self, project_ref: str) -> None:
        self.stopped.append(project_ref)

    def is_active(self, project_ref: str) -> bool:
        return project_ref in self.started and project_ref not in self.stopped


# -- readiness (ADR-022) ---------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 - http.server's interface
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # keep the test output readable
        pass


def _serve(status: int):
    handler = type("H", (_Handler,), {"status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _SlowHandler(_Handler):
    delay = 1.5

    def do_GET(self):  # noqa: N802 - http.server's interface
        time.sleep(self.delay)
        super().do_GET()


def test_a_service_slower_than_one_probe_still_becomes_ready():
    """A healthy worker whose first answer takes longer than a probe's timeout.

    The server answers one request at a time, as PostgREST does once its small
    pool is busy. A probe that gives up after a second and asks again leaves the
    abandoned request queued in front of the new one, so every later probe waits
    longer than the last and a healthy worker is never ready. That is how
    `test_extension_functions_are_refused_as_rpc_and_nothing_else_is` failed on
    CI: PostgREST loaded its schema cache in under a second and was then killed
    thirty seconds later having logged nothing.
    """
    server = http.server.HTTPServer(("127.0.0.1", 0), _SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        elapsed = workers.wait_until_ready(server.server_port, timeout=8)
        assert elapsed < 4, f"ready only after {elapsed:.1f}s for a 1.5s answer"
    finally:
        server.shutdown()


def test_a_service_answering_503_is_not_ready():
    """The ADR-022 property. PostgREST answers 503 PGRST002 while its schema
    cache loads, so a port-open check routes traffic into a worker that fails."""
    server = _serve(503)
    try:
        assert workers.is_ready(server.server_port) is False
    finally:
        server.shutdown()


def test_a_service_that_answers_is_ready():
    server = _serve(200)
    try:
        assert workers.is_ready(server.server_port) is True
    finally:
        server.shutdown()


def test_a_closed_port_is_not_ready():
    server = _serve(200)
    port = server.server_port
    server.shutdown()
    server.server_close()
    assert workers.is_ready(port) is False


def test_waiting_gives_up_rather_than_hanging():
    server = _serve(503)
    try:
        started = time.monotonic()
        with pytest.raises(workers.WorkerError, match="did not become ready"):
            workers.wait_until_ready(server.server_port, timeout=0.5)
        assert time.monotonic() - started < 5
    finally:
        server.shutdown()


# -- port allocation -------------------------------------------------------




def test_ports_are_unique_per_node(placed_project):
    a, b = placed_project("wk00000a"), placed_project("wk00000b")
    with db.connection() as conn:
        first = workers.allocate_port(conn, project_id=a)
        second = workers.allocate_port(conn, project_id=b)
        conn.commit()
    assert first != second


def test_allocating_twice_returns_the_same_port(placed_project):
    """Otherwise a restart moves the worker and the gateway routes to nothing."""
    project_id = placed_project("wk00000c")
    with db.connection() as conn:
        first = workers.allocate_port(conn, project_id=project_id)
        conn.commit()
        assert workers.allocate_port(conn, project_id=project_id) == first


def test_a_project_with_no_node_cannot_allocate_a_port(placed_project):
    project_id = placed_project("wk00000d")
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET node_id = NULL WHERE id = %s", (project_id,))
        conn.commit()
        with pytest.raises(workers.WorkerError, match="no node placement"):
            workers.allocate_port(conn, project_id=project_id)


# -- warm accounting (ADR-022) --------------------------------------------


def test_a_sleeping_project_does_not_count_as_warm(placed_project):
    """Free-tier density rests on this. Counting by project status instead
    would charge every sleeping project against the connection ceiling it is
    demonstrably not consuming."""
    from services.control_plane import nodes

    project_id = placed_project("wk00000e")
    with db.connection() as conn:
        node_id = db.one(conn, "SELECT node_id FROM projects WHERE id = %s", (project_id,))["node_id"]

        db.execute(conn, "UPDATE projects SET status='ACTIVE', worker_state='RUNNING' WHERE id = %s",
                   (project_id,))
        conn.commit()
        assert nodes.capacity_of(conn, node_id).current_warm_projects == 1

        db.execute(conn, "UPDATE projects SET worker_state='STOPPED' WHERE id = %s", (project_id,))
        conn.commit()
        warm = nodes.capacity_of(conn, node_id)
        assert warm.current_warm_projects == 0, "a slept project was still counted as warm"
        assert warm.current_projects == 1, "sleeping must not stop it being a project on the node"


def test_idle_workers_are_offered_for_sleep(placed_project):
    project_id = placed_project("wk00000f")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET worker_state='RUNNING', "
            "worker_last_active_at = now() - interval '2 hours' WHERE id = %s",
            (project_id,),
        )
        conn.commit()
        assert [row["id"] for row in workers.idle_workers(conn, idle_minutes=30)] == [project_id]
        assert workers.idle_workers(conn, idle_minutes=180) == []


# -- lifecycle -------------------------------------------------------------


def test_stopping_a_worker_touches_no_data(placed_project):
    """ADR-005: a slept free project must remain a project. Sleep is service
    state, not deletion."""
    project_id = placed_project("wk00000g")
    supervisor = RecordingSupervisor()
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET worker_state='RUNNING' WHERE id = %s", (project_id,))
        conn.commit()
        workers.stop_worker(conn, project_id=project_id, supervisor=supervisor)
        row = db.one(
            conn,
            "SELECT worker_state, database_name, status FROM projects WHERE id = %s",
            (project_id,),
        )
    assert supervisor.stopped == ["wk00000g"]
    assert row["worker_state"] == "STOPPED"
    assert row["database_name"] == "mldb_wk00000g", "sleeping dropped the database reference"
    assert row["status"] == "PROVISIONED"


def test_a_worker_is_not_started_for_an_unprovisioned_project(placed_project, key_ring):
    project_id = placed_project("wk00000h")
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status='FAILED' WHERE id = %s", (project_id,))
        conn.commit()
        with pytest.raises(workers.WorkerError, match="refusing to start"):
            workers.start_worker(
                conn, project_id=project_id, key_ring=key_ring,
                supervisor=RecordingSupervisor(),
            )


# -- the JWT secret --------------------------------------------------------


def test_the_jwt_secret_is_generated_once_and_reused(placed_project, key_ring):
    """PostgREST and GoTrue must share it: a token GoTrue signs has to verify in
    PostgREST, so a second secret in Phase 04 would give a project whose own
    Auth tokens its own Data API rejects."""
    project_id = placed_project("wk00000i")
    with db.connection() as conn:
        first = workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
        conn.commit()
        second = workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
    assert first == second
    assert len(first) >= 32


def test_the_jwt_secret_is_stored_encrypted_not_in_the_clear(placed_project, key_ring):
    project_id = placed_project("wk00000j")
    with db.connection() as conn:
        secret = workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
        conn.commit()
        row = db.one(
            conn,
            "SELECT ciphertext FROM project_credentials WHERE project_id = %s "
            "AND credential_type = 'jwt_signing'",
            (project_id,),
        )
    assert secret.encode() not in bytes(row["ciphertext"])


# -- Phase 00 finding 3, against a real PostgREST -------------------------


@requires_node
@requires_maludb_core
@requires_postgrest
def test_a_table_created_after_startup_is_queryable_without_a_restart(
    admin_conn, key_ring, project_factory, tmp_path
):
    """The finding this slice exists for.

    During the spike a table created after the worker started returned
    PGRST205 until `NOTIFY pgrst, 'reload schema'` was issued. Bootstrap 006
    makes the database send it; this proves PostgREST acts on it, which is the
    half no amount of SQL testing can establish.
    """
    import json
    import urllib.error
    import urllib.request

    ref = "wk0000pr"
    project_id = project_factory(ref)
    names, passwords = _provision_core(project_id, admin_conn, key_ring, ref)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
        tenant_conn.commit()
        with db.connection() as conn:
            tenant_bootstrap.bootstrap_project(conn, tenant_conn, project_id=project_id)

    settings = workers.WorkerSettings(
        project_ref=ref,
        database=names.database,
        authenticator_role=names.authenticator,
        authenticator_password=passwords["authenticator"],
        jwt_secret=TEST_JWT_SECRET,
        port=27431,
    )
    config = workers.write_config(settings, config_dir=tmp_path)

    process = subprocess.Popen(  # noqa: S603 - fixed binary, generated config
        [POSTGREST_BIN, str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        workers.wait_until_ready(settings.port, timeout=30)

        def get(path: str) -> tuple[int, str]:
            try:
                with urllib.request.urlopen(  # noqa: S310 - loopback, fixed scheme
                    f"http://127.0.0.1:{settings.port}{path}", timeout=5
                ) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        # Created after the worker started, exactly as a customer migration would.
        with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
            tenant_conn.execute(
                "CREATE TABLE public.notes (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,"
                " body text)"
            )
            tenant_conn.execute("ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY")
            tenant_conn.execute("INSERT INTO public.notes (body) VALUES ('hello')")
            tenant_conn.commit()

        # The reload is asynchronous; allow it a moment rather than racing it.
        deadline = time.monotonic() + 10
        status, body = 0, ""
        while time.monotonic() < deadline:
            status, body = get("/notes")
            if status != 404 and "PGRST205" not in body:
                break
            time.sleep(0.2)

        assert "PGRST205" not in body, f"schema cache never reloaded: {body}"
        assert status == 200, f"unexpected status {status}: {body}"
        # RLS is on with no policy, so anon sees an empty set rather than 42501 --
        # Phase 00 finding 7, end to end through the real API.
        assert json.loads(body) == []
    finally:
        process.terminate()
        process.wait(timeout=10)


@requires_node
@requires_maludb_core
def test_the_reload_notification_is_actually_sent(admin_conn, key_ring, project_factory):
    """The database half of the same finding, without PostgREST in the way.

    Proves the event trigger fires and the notification is delivered, so a
    failure in the test above can be attributed to one side or the other.
    """
    ref = "wk0000nt"
    project_id = project_factory(ref)
    names, _ = _provision_core(project_id, admin_conn, key_ring, ref)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
        tenant_conn.commit()
        with db.connection() as conn:
            tenant_bootstrap.bootstrap_project(conn, tenant_conn, project_id=project_id)

    listener = psycopg.connect(_tenant_admin_dsn(names.database), autocommit=True)
    try:
        listener.execute("LISTEN pgrst")
        with psycopg.connect(_tenant_admin_dsn(names.database)) as ddl:
            ddl.execute("CREATE TABLE public.late_arrival (id int)")
            ddl.commit()

        notifications = []
        generator = listener.notifies(timeout=10)
        for notification in generator:
            notifications.append(notification)
            break
    finally:
        listener.close()

    assert notifications, "no reload notification was delivered after DDL"
    assert notifications[0].channel == "pgrst"
    assert notifications[0].payload == "reload schema"


@requires_node
@requires_maludb_core
@requires_postgrest
def test_extension_functions_are_refused_as_rpc_and_nothing_else_is(
    admin_conn, key_ring, project_factory, tmp_path
):
    """ADR-076 decision 2, through a real PostgREST.

    Every customer role executes extension functions now, so what keeps
    `/rpc/gen_salt` -- the Phase 00 finding -- off the Data API is the
    pre-request check alone. The same tenant served by a worker without the check
    is the negative control: it answers with a salt, which is what proves the
    check is what refuses.
    """
    import json
    import urllib.error
    import urllib.request

    ref = "wk0000rq"
    project_id = project_factory(ref)
    names, passwords = _provision_core(project_id, admin_conn, key_ring, ref)
    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
        tenant_conn.commit()
        with db.connection() as conn:
            tenant_bootstrap.bootstrap_project(
                conn, tenant_conn, project_id=project_id, rpc_check_live=True
            )
        tenant_conn.execute(
            "CREATE FUNCTION public.own_rpc() RETURNS int LANGUAGE sql STABLE AS $$ SELECT 42 $$"
        )
        # Shares pgcrypto's name. Refused under "any" -- and it bumps a sequence,
        # which is not transactional, so a body that ran and was rolled back
        # would still show.
        tenant_conn.execute("CREATE SEQUENCE public.digest_calls")
        tenant_conn.execute(
            "CREATE FUNCTION public.digest(t text) RETURNS bigint LANGUAGE sql VOLATILE "
            "AS $$ SELECT nextval('public.digest_calls') $$"
        )
        tenant_conn.execute("SELECT nextval('public.digest_calls')")
        tenant_conn.execute(
            "GRANT EXECUTE ON FUNCTION public.own_rpc(), public.digest(text) TO anon"
        )
        tenant_conn.execute("GRANT USAGE ON SEQUENCE public.digest_calls TO anon")
        tenant_conn.commit()

    def digest_calls() -> int:
        with psycopg.connect(_tenant_admin_dsn(names.database)) as c:
            return c.execute("SELECT last_value FROM public.digest_calls").fetchone()[0]

    def serve(port: int, pre_request: str | None):
        settings = workers.WorkerSettings(
            project_ref=ref, database=names.database, authenticator_role=names.authenticator,
            authenticator_password=passwords["authenticator"], jwt_secret=TEST_JWT_SECRET,
            port=port, pre_request=pre_request,
        )
        config = workers.write_config(settings, config_dir=tmp_path / str(port))
        # To a file, not a pipe nobody reads: a pipe fills and blocks PostgREST,
        # and a worker that never becomes ready then fails this test with no
        # word about why -- which is how this first failed on CI.
        log_path = tmp_path / f"postgrest-{port}.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(  # noqa: S603 - fixed binary, generated config
                [POSTGREST_BIN, str(config)], stdout=log, stderr=subprocess.STDOUT
            )
        try:
            workers.wait_until_ready(port, timeout=30)
        except workers.WorkerError as exc:
            process.terminate()
            process.wait(timeout=10)
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-25:])
            raise AssertionError(f"{exc}; PostgREST said:\n{tail}") from None
        return process

    def call(port: int, method: str, path: str, *, body: bytes | None = None,
             content_type: str = "application/json", profile: str | None = None):
        headers = {"Content-Type": content_type}
        if profile:
            headers["Content-Profile" if method == "POST" else "Accept-Profile"] = profile
        request = urllib.request.Request(  # noqa: S310 - loopback, fixed scheme
            f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    guarded = serve(27432, workers.pre_request_for(tenant_bootstrap.latest_version()))
    try:
        status, body = call(27432, "POST", "/rpc/gen_salt", body=b"bf", content_type="text/plain")
        assert status == 403, f"gen_salt was not refused: {status} {body}"
        assert json.loads(body)["code"] == "PT403"

        # A GET can only resolve a function it can pass arguments to, so the probe
        # is pgcrypto's zero-argument gen_random_uuid rather than gen_salt.
        status, body = call(27432, "GET", "/rpc/gen_random_uuid")
        assert status == 403, f"a GET reached an extension function: {status} {body}"
        status, _ = call(27432, "POST", "/rpc/gen_salt", body=b"bf", content_type="text/plain",
                         profile="public")
        assert status == 403, "a profile header steered the check away"

        # Spellings of the same call. `request.path` is what the check reads, so
        # any spelling PostgREST resolves to the function while the path says
        # something else would walk past it.
        # Found in the grants slice 1 security review: PostgREST decodes the path
        # before resolving the function, and /rpc/gen%5Fsalt answered a salt past a
        # check that compared the raw `request.path`. Encoded spellings must be
        # refused by the check itself; the others never resolve in PostgREST.
        for spelling in ("/rpc/gen%5Fsalt", "/rpc/%67%65%6E%5F%73%61%6C%74", "/rpc/%67en_salt"):
            status, body = call(27432, "POST", spelling, body=b"bf", content_type="text/plain")
            assert (status, json.loads(body)["code"]) == (403, "PT403"), f"{spelling}: {status} {body}"
        for spelling in ("/rpc/gen_salt/", "/rpc//gen_salt", "/rpc/GEN_SALT", "/rpc/gen_salt%00"):
            status, body = call(27432, "POST", spelling, body=b"bf", content_type="text/plain")
            assert status == 404, f"{spelling} was not rejected by PostgREST: {status} {body}"
        status, body = call(27432, "POST", "/rpc/own%5Frpc", body=b"{}")
        assert (status, body) == (200, "42"), "decoding refused a customer's own encoded RPC name"

        status, body = call(27432, "POST", "/rpc/own_rpc", body=b"{}")
        assert (status, body) == (200, "42"), f"a customer's own RPC was refused: {status} {body}"

        before = digest_calls()
        status, _ = call(27432, "POST", "/rpc/digest", body=b'{"t": "x"}')
        assert status == 403, "a customer function sharing an extension's name was not refused"
        assert digest_calls() == before, "the refused function's body ran"
    finally:
        guarded.terminate()
        guarded.wait(timeout=10)

    unguarded = serve(27433, None)
    try:
        status, _ = call(27433, "GET", "/rpc/gen_random_uuid")
        assert status == 200, "negative control: without the check the GET should answer"
        status, body = call(27433, "POST", "/rpc/gen_salt", body=b"bf", content_type="text/plain")
        assert status == 200 and body.strip('"').startswith("$2a$"), (
            f"negative control: without the check gen_salt should answer, got {status} {body}"
        )
        before = digest_calls()
        status, _ = call(27433, "POST", "/rpc/digest", body=b'{"t": "x"}')
        assert status == 200 and digest_calls() == before + 1
    finally:
        unguarded.terminate()
        unguarded.wait(timeout=10)
