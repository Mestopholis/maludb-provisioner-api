"""The node's storage reconciler (free slice 10c).

`jobs.delete_project` cannot reach the worker's admin API -- ADR-085 keeps it on the node's
loopback -- so a deleted project's registration stayed behind on the node, holding that project's
database URL and its JWT signing secret. This pass removes it from the node, and what these tests
assert is mostly about what it refuses to do:

- it deregisters exactly the refs the worker holds that this node no longer serves, **judged by a
  real gateway role under ADR-072's row policies**, so a project on another node is not "stale";
- a control plane it cannot reach deregisters nothing and exits non-zero;
- an answer that would empty the node is refused rather than acted on;
- the worker's listing yields ids and never the secrets the admin API sends with them;
- the module reaches no key ring, node credential or provisioning code -- `node_maintenance`'s rule,
  and the reason the admin API was split into a leaf module at all.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from services.control_plane import db, node_storage, storage_admin, storage_workers
from tests.conftest import DATABASE_URL, requires_db
from tests.test_control_plane_surfaces import FORBIDDEN_MODULES, _import_closure, _module_file
from tests.test_gateway_grants import gateway_role, two_nodes_two_projects  # noqa: F401 - fixtures
from tests.test_storage_workers import _settings as _storage_settings


class FakeAdmin:
    """The admin API, without a worker: what it holds, and what was removed from it."""

    def __init__(self, tenants: tuple[str, ...], fail: set[str] | None = None):
        self.tenants = tenants
        self.removed: list[str] = []
        self.fail = fail or set()
        self.StorageWorkerError = storage_admin.StorageWorkerError

    def known_tenants(self, *, admin_port, api_key):  # noqa: ARG002
        return self.tenants

    def deregister_tenant(self, *, admin_port, api_key, project_ref):  # noqa: ARG002
        if project_ref in self.fail:
            raise storage_admin.StorageWorkerError(f"worker answered 500 for {project_ref}")
        self.removed.append(project_ref)


def _reconcile(admin, connect):
    return node_storage.reconcile(
        database_url="postgresql://unused", admin_port=5001, api_key="k",
        connect=connect, admin=admin,
    )


def _serving(refs):
    """A `connect` that answers as a control plane serving exactly these refs."""

    @contextlib.contextmanager
    def connect(*args, **kwargs):  # noqa: ARG001
        class Cursor:
            def execute(self, _sql, params):
                self.rows = [(ref,) for ref in params[0] if ref in refs]

            def fetchall(self):
                return self.rows

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Conn:
            def cursor(self, row_factory=None):  # noqa: ARG002 - pinned by live_refs, not the caller
                return Cursor()

        yield Conn()

    return connect


# -- what it removes, and what it leaves ------------------------------------------------


def test_only_the_tenants_this_node_no_longer_serves_are_deregistered():
    admin = FakeAdmin(("aaaaaaaa", "bbbbbbbb", "cccccccc"))
    removed = _reconcile(admin, _serving({"aaaaaaaa", "cccccccc"}))
    assert removed == 1
    assert admin.removed == ["bbbbbbbb"]


def test_a_node_whose_registrations_all_belong_deregisters_nothing():
    admin = FakeAdmin(("aaaaaaaa", "bbbbbbbb"))
    assert _reconcile(admin, _serving({"aaaaaaaa", "bbbbbbbb"})) == 0
    assert admin.removed == []


def test_a_worker_holding_nothing_never_asks_the_control_plane():
    def refuse(*_args, **_kwargs):
        raise AssertionError("nothing to ask about")

    assert _reconcile(FakeAdmin(()), refuse) == 0


def test_one_tenants_failure_does_not_stop_the_next():
    admin = FakeAdmin(("aaaaaaaa", "bbbbbbbb", "cccccccc"), fail={"bbbbbbbb"})
    removed = _reconcile(admin, _serving(set()))
    assert removed == 2 and admin.removed == ["aaaaaaaa", "cccccccc"]


# -- what it refuses ---------------------------------------------------------------------


def test_a_control_plane_it_cannot_reach_deregisters_nothing():
    """The failure to avoid is not a stale registration: it is taking a live tenant off the air."""
    admin = FakeAdmin(("aaaaaaaa", "bbbbbbbb"))

    def unreachable(*_args, **_kwargs):
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.Error):
        _reconcile(admin, unreachable)
    assert admin.removed == []


def test_an_answer_that_would_empty_the_node_is_refused():
    """The shape of a gateway role that lost its node mapping, not of a busy day's deletions."""
    admin = FakeAdmin(tuple(f"x{n:07d}" for n in range(node_storage.MAX_DEREGISTRATIONS + 1)))
    with pytest.raises(node_storage.ReconcileRefused) as caught:
        _reconcile(admin, _serving(set()))
    assert "gateway role" in str(caught.value)
    assert admin.removed == []


def test_exactly_the_limit_is_still_acted_on():
    admin = FakeAdmin(tuple(f"x{n:07d}" for n in range(node_storage.MAX_DEREGISTRATIONS)))
    assert _reconcile(admin, _serving(set())) == node_storage.MAX_DEREGISTRATIONS


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"MALUDB_GATEWAY_DATABASE_URL": "postgresql://x"},
        {"MALUDB_GATEWAY_DATABASE_URL": "postgresql://x", "SERVER_ADMIN_API_KEYS": "k"},
        {"MALUDB_GATEWAY_DATABASE_URL": "postgresql://x", "SERVER_ADMIN_API_KEYS": "k",
         "MALUDB_STORAGE_ADMIN_HOST_PORT": "not-a-port"},
    ],
)
def test_a_half_configured_pass_refuses_to_start(monkeypatch, environment):
    for name in ("MALUDB_GATEWAY_DATABASE_URL", "SERVER_ADMIN_API_KEYS",
                 "MALUDB_STORAGE_ADMIN_HOST_PORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit):
        node_storage.main(["reconcile"])


def test_a_refusal_exits_non_zero_without_naming_the_dsn(monkeypatch, caplog):
    monkeypatch.setenv("MALUDB_GATEWAY_DATABASE_URL", "postgresql://gw:hunter2@cp:5432/db")
    monkeypatch.setenv("SERVER_ADMIN_API_KEYS", "k")
    monkeypatch.setenv("MALUDB_STORAGE_ADMIN_HOST_PORT", "5001")

    def refuse(**_kwargs):
        raise storage_admin.StorageWorkerError("the storage worker's admin API answered 401")

    monkeypatch.setattr(node_storage, "reconcile", refuse)
    assert node_storage.main(["reconcile"]) == 1
    assert "hunter2" not in caplog.text


# -- the listing the admin API answers with ----------------------------------------------


def _listing_transport(body, status=200):
    def handler(request):  # noqa: ARG001
        return httpx.Response(status, content=json.dumps(body),
                              headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def test_the_listing_yields_ids_and_never_the_secrets_beside_them(monkeypatch):
    """Upstream sends each tenant's whole configuration when it is configured to. Keep the id."""
    body = [
        {"id": "aaaaaaaa", "databaseUrl": "postgresql://mldb_a:hunter2@node/db",
         "jwtSecret": "s3cret", "serviceKey": "svc"},
        {"id": "bbbbbbbb", "jwtSecret": "another"},
    ]
    transport = _listing_transport(body)
    monkeypatch.setattr(
        httpx, "request",
        lambda method, url, **kwargs: httpx.Client(transport=transport).request(method, url, **kwargs),
    )
    assert storage_admin.known_tenants(admin_port=5001, api_key="k") == ("aaaaaaaa", "bbbbbbbb")


def test_a_listing_entry_that_is_not_a_project_ref_is_dropped(monkeypatch):
    """It came out of the worker's own database and is about to be a URL path (AGENTS.md)."""
    body = [
        {"id": "aaaaaaaa"},
        {"id": "../../tenants/bbbbbbbb"},
        {"id": "short"},
        {"id": None},
        {"nope": 1},
        "not-an-object",
    ]
    transport = _listing_transport(body)
    monkeypatch.setattr(
        httpx, "request",
        lambda method, url, **kwargs: httpx.Client(transport=transport).request(method, url, **kwargs),
    )
    assert storage_admin.known_tenants(admin_port=5001, api_key="k") == ("aaaaaaaa",)


def test_a_listing_that_is_not_a_list_is_an_error_rather_than_an_empty_node(monkeypatch):
    transport = _listing_transport({"error": "nope"})
    monkeypatch.setattr(
        httpx, "request",
        lambda method, url, **kwargs: httpx.Client(transport=transport).request(method, url, **kwargs),
    )
    with pytest.raises(storage_admin.StorageWorkerError):
        storage_admin.known_tenants(admin_port=5001, api_key="k")


# -- the real role, under the real policies ----------------------------------------------


@pytest.fixture
def mapped_gateway(gateway_role, two_nodes_two_projects):  # noqa: F811
    """The gateway role is alpha's; beta's project exists and is another node's business."""
    nodes = two_nodes_two_projects
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = %s WHERE id = %s",
                   (gateway_role, nodes["alpha"]["node_id"]))
        conn.commit()
    yield nodes
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET gateway_role = NULL WHERE gateway_role = %s", (gateway_role,))
        conn.commit()


@contextlib.contextmanager
def _as_role(role: str):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute(f'SET ROLE "{role}"')
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()


@requires_db
def test_the_gateway_role_sees_its_own_nodes_projects_and_no_others(gateway_role, mapped_gateway):  # noqa: F811
    candidates = ("rlsalpha", "rlsbeta", "nosuchpr")
    with _as_role(gateway_role) as conn:
        assert node_storage.live_refs(conn, candidates) == {"rlsalpha"}


@requires_db
def test_a_deleted_project_stops_being_this_nodes_business(gateway_role, mapped_gateway):  # noqa: F811
    """`delete_project` clears the placement, which is what makes the registration reconcilable."""
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET status='DELETED', deleted_at=now(), node_id=NULL "
                         "WHERE project_ref = 'rlsalpha'")
        conn.commit()
    with _as_role(gateway_role) as conn:
        assert node_storage.live_refs(conn, ("rlsalpha",)) == set()


@requires_db
def test_a_role_that_is_no_nodes_gateway_is_told_it_serves_nothing(gateway_role, two_nodes_two_projects):  # noqa: F811, ARG001
    """And so would deregister everything -- which is what MAX_DEREGISTRATIONS is there to stop."""
    with _as_role(gateway_role) as conn:
        assert node_storage.live_refs(conn, ("rlsalpha", "rlsbeta")) == set()


# -- the leaf property -------------------------------------------------------------------


def test_the_reconciler_reaches_no_credential_or_provisioning_code():
    modules, calls = _import_closure(["services.control_plane.node_storage"])
    assert "services.control_plane.storage_admin" in modules, "the walk found nothing"
    heavy = {"services.control_plane.storage_workers", "services.control_plane.workers",
             "services.control_plane.provisioning", "services.control_plane.crypto",
             "services.control_plane.maintenance", "services.control_plane.nodes",
             "services.control_plane.config"}
    assert not modules & (FORBIDDEN_MODULES | heavy), modules & (FORBIDDEN_MODULES | heavy)
    assert calls == {}, calls
    for module in modules:
        path = _module_file(module)
        if path is not None:
            assert "KeyRing(" not in path.read_text() and "MALUDB_KEK_REF" not in path.read_text(), module


def test_the_control_plane_still_reaches_the_admin_api_by_its_old_names():
    """The split moved code; it must not have moved a name any caller uses."""
    for name in ("StorageWorkerError", "ADMIN_TIMEOUT_SECONDS", "deregister_tenant",
                 "tenant_known", "known_tenants", "is_ready"):
        assert getattr(storage_workers, name) is getattr(storage_admin, name), name


# -- the file the pass is given ----------------------------------------------------------


def test_the_reconcile_file_carries_the_admin_address_and_nothing_else():
    """Deliberately not `storage.env`: that one decrypts every tenant's Storage credentials."""
    settings = _storage_settings()
    rendered = dict(
        line.split("=", 1) for line in
        storage_workers.render_reconcile_env(settings).splitlines() if line
    )
    assert set(rendered) == {"SERVER_ADMIN_API_KEYS", "MALUDB_STORAGE_ADMIN_HOST_PORT"}
    full = storage_workers.render_env(settings)
    for name, value in rendered.items():
        assert f"{name}={value}" in full, f"{name} disagrees with storage.env"
    for secret in ("AUTH_ENCRYPTION_KEY", "DATABASE_MULTITENANT_URL", "AWS_SECRET_ACCESS_KEY"):
        assert secret not in storage_workers.render_reconcile_env(settings), secret


def test_the_two_names_the_pass_reads_are_the_names_the_file_writes():
    """A rename on either side would leave the pass refusing to start, which is a poor way to find out."""
    source = Path(node_storage.__file__).read_text()
    for name in storage_workers.render_reconcile_env(_storage_settings()).splitlines():
        assert f'"{name.split("=")[0]}"' in source, name
