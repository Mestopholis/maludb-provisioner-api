"""The fleet extension upgrade procedure (Phase 12 slice 1, ADR-074 decision 5).

Every assertion that matters here is about a **real** `ALTER EXTENSION` on a
**real** tenant built by the provisioning module at the node's previous
`maludb_core` version. A stubbed upgrade would prove the loop and nothing about
the one claim the design rests on: that a tenant which fails verification is
rolled back and is still on its old version afterwards, because PostgreSQL has
no general extension downgrade and a check after `COMMIT` could only report the
damage.

Needs `MALUDB_NODE_ADMIN_DSN`, `maludb_core` on the node, and an older
`maludb_core` version with an update path to the default. Skips without them.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, extension_upgrade, identity, provisioning, tenant_bootstrap
from tests.conftest import (
    NODE_ADMIN_DSN,
    PLATFORM_OWNER,
    TEST_CREDENTIAL,
    agree_with_pins,
    node_provided_versions,
    requires_db,
)

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _control_plane(db_pool):  # noqa: ARG001 - every test here reads or writes the control plane
    yield


# -- the node and its versions ---------------------------------------------


def _versions() -> tuple[str, str] | None:
    """(previous, default) for maludb_core on the node, if an upgrade path exists."""
    if not NODE_ADMIN_DSN:
        return None
    try:
        with psycopg.connect(NODE_ADMIN_DSN) as conn:
            default = conn.execute(
                "SELECT default_version FROM pg_available_extensions WHERE name = 'maludb_core'"
            ).fetchone()
            if default is None:
                return None
            rows = conn.execute(
                "SELECT source FROM pg_extension_update_paths('maludb_core') "
                "WHERE target = %s AND path IS NOT NULL",
                (default[0],),
            ).fetchall()
    except psycopg.Error:
        return None
    sources = [r[0] for r in rows if r[0] != default[0]]
    if not sources:
        return None
    previous = max(sources, key=extension_upgrade._version)  # noqa: SLF001
    return previous, default[0]


VERSIONS = _versions()
requires_upgrade_path = pytest.mark.skipif(
    VERSIONS is None,
    reason="needs MALUDB_NODE_ADMIN_DSN and maludb_core with an older version to upgrade from",
)


@pytest.fixture
def admin_node_conn():
    conn = psycopg.connect(NODE_ADMIN_DSN, autocommit=True)
    yield conn
    conn.close()


def _node(name: str, *, status: str = "active") -> int:
    with db.connection() as conn:
        row = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES (%s,%s,%s,'shared',%s,now()) "
            "ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status RETURNING id",
            (name, f"{name}.example.com", "10.0.0.9", status),
        )
        conn.commit()
        # Pinned to what the node under test provides: the run targets the pin
        # and refuses a node whose packages disagree with it (ADR-075).
        agree_with_pins(conn, row["id"])
        return row["id"]


def _project(ref: str, node_id: int, *, status: str = "ACTIVE") -> uuid.UUID:
    with db.connection() as conn:
        plan = db.one(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('xu','xu',%s) "
            "ON CONFLICT (code) DO UPDATE SET name='xu' RETURNING id",
            (Jsonb({}),),
        )
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}-{uuid.uuid4().hex[:6]}@example.com", password=TEST_CREDENTIAL
        )
        pid = uuid.uuid4()
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
            "node_id, database_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (pid, org, ref, ref, plan["id"], status, node_id, f"mldb_{ref}"),
        )
        conn.commit()
        return pid


def _drop_tenant(admin: psycopg.Connection, ref: str) -> None:
    names = provisioning.TenantNames.for_ref(ref)
    admin.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
    for (role,) in admin.execute(
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (f"mldb\\_{ref}\\_%",)
    ).fetchall():
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture
def old_tenants(admin_node_conn):
    """Build bootstrapped tenants at the node's previous maludb_core version."""
    made: list[str] = []

    def make(ref: str) -> provisioning.TenantNames:
        previous, _ = VERSIONS
        _drop_tenant(admin_node_conn, ref)
        made.append(ref)
        names = provisioning.TenantNames.for_ref(ref)
        passwords = {k: provisioning.generate_password()
                     for k in ("authenticator", "auth", "admin", "executor", "client", "storage")}
        with psycopg.connect(NODE_ADMIN_DSN) as conn:
            provisioning.ensure_shared_roles(conn)
            provisioning.create_roles(conn, names, passwords=passwords,
                                      connection_limits={"authenticator": 5, "auth": 5})
            provisioning.create_executor_role(conn, names, password=passwords["executor"])
            provisioning.create_client_role(conn, names, password=passwords["client"])
            provisioning.create_storage_role(conn, names, password=passwords["storage"])
            conn.commit()
            provisioning.create_database(conn, names, owner=PLATFORM_OWNER)
            provisioning.lock_down_database(conn, names)
            conn.commit()
        with _tenant(names.database) as t:
            t.execute(f"CREATE EXTENSION maludb_core VERSION '{previous}' CASCADE")
            t.commit()
            tenant_bootstrap.apply(t)
            tenant_bootstrap.verify(t)
            t.commit()
        return names

    yield make
    for ref in made:
        _drop_tenant(admin_node_conn, ref)


def _tenant(database: str, **kw) -> psycopg.Connection:
    info = psycopg.conninfo.conninfo_to_dict(NODE_ADMIN_DSN)
    info["dbname"] = database
    return psycopg.connect(**info, **kw)


def _installed(database: str) -> str:
    with _tenant(database) as t:
        return t.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'maludb_core'"
        ).fetchone()[0]


def _run(admin_node_conn, node: str, **kw) -> extension_upgrade.UpgradeOutcome:
    with db.connection() as conn:
        return extension_upgrade.upgrade_node(conn, admin_node_conn, node_name=node, **kw)


# -- refusals, which need no tenant at all ---------------------------------


def test_a_draining_node_is_refused():
    _node("xu-draining", status="draining")
    with db.connection() as conn:
        problems = extension_upgrade.preflight(conn, node_name="xu-draining")
    assert any("draining" in p for p in problems), problems


def test_a_node_with_a_move_in_progress_is_refused():
    node = _node("xu-moving")
    _project("xumove01", node, status="MOVING")
    with db.connection() as conn:
        problems = extension_upgrade.preflight(conn, node_name="xu-moving")
    assert any("move is in progress" in p and "xumove01" in p for p in problems), problems


def test_a_node_with_a_restore_running_is_refused():
    node = _node("xu-restoring")
    pid = _project("xurest01", node)
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO tenant_restores (project_id, node_id, status, target_time, stanza) "
            "VALUES (%s, %s, 'running', now(), 'st')",
            (pid, node),
        )
        conn.commit()
        problems = extension_upgrade.preflight(conn, node_name="xu-restoring")
    assert any("restore is running" in p for p in problems), problems


def test_an_unknown_version_is_refused_before_any_tenant_is_touched(admin_node_conn):
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    _node("xu-badver")
    outcome = _run(admin_node_conn, "xu-badver", to_version="999.0.0")
    assert outcome.status == "refused"
    assert "is not this node's pin" in outcome.error
    assert outcome.tenants == []


# -- the upgrade itself ----------------------------------------------------


@requires_upgrade_path
def test_the_first_run_upgrades_one_canary_and_stops(admin_node_conn, old_tenants):
    """Nobody else is touched until an operator has looked at a real upgraded tenant."""
    previous, target = VERSIONS
    node = _node("xu-canary")
    a, b = old_tenants("xucan001"), old_tenants("xucan002")
    _project("xucan001", node)
    _project("xucan002", node)

    outcome = _run(admin_node_conn, "xu-canary")

    assert outcome.ok, outcome.error
    assert outcome.canary_run
    assert outcome.count("upgraded") == 1
    assert outcome.left == ["xucan002"]
    assert any("canary verified" in n for n in outcome.notes), outcome.notes
    assert _installed(a.database) == target
    assert _installed(b.database) == previous, "a second tenant was upgraded before the canary was inspected"
    with db.connection() as conn:
        recorded = db.one(conn, "SELECT extension_versions FROM projects WHERE project_ref = 'xucan001'")
    assert recorded["extension_versions"].get("maludb_core") == target


@requires_upgrade_path
def test_a_later_run_takes_the_batch(admin_node_conn, old_tenants):
    _, target = VERSIONS
    node = _node("xu-batch")
    for ref in ("xubat001", "xubat002", "xubat003"):
        old_tenants(ref)
        _project(ref, node)

    _run(admin_node_conn, "xu-batch")  # the canary
    outcome = _run(admin_node_conn, "xu-batch", batch_size=10)

    assert outcome.ok, outcome.error
    assert not outcome.canary_run
    assert outcome.count("current") == 1, "the canary should be recorded as already current"
    assert outcome.count("upgraded") == 2
    for ref in ("xubat001", "xubat002", "xubat003"):
        assert _installed(f"mldb_{ref}") == target


@requires_upgrade_path
def test_a_tenant_that_fails_verification_is_rolled_back_and_stops_the_run(
    admin_node_conn, old_tenants
):
    """The claim the design rests on.

    The middle tenant loses ADR-018's event trigger -- the control that stops a
    new extension function becoming anon-callable. Its upgrade must be undone,
    not reported, and the tenant after it must not be attempted.
    """
    previous, target = VERSIONS
    node = _node("xu-fail")
    for ref in ("xufai001", "xufai002", "xufai003"):
        old_tenants(ref)
        _project(ref, node)
    _run(admin_node_conn, "xu-fail")  # canary: xufai001

    with _tenant("mldb_xufai002", autocommit=True) as t:
        t.execute("DROP EVENT TRIGGER maludb_harden_extensions")

    outcome = _run(admin_node_conn, "xu-fail", batch_size=10)

    assert outcome.status == "stopped"
    assert outcome.stopped_at == "xufai002"
    failed = next(t for t in outcome.tenants if t.project_ref == "xufai002")
    assert failed.status == "failed"
    assert "maludb_harden_extensions" in failed.detail
    assert _installed("mldb_xufai002") == previous, (
        "the failed tenant is on the new version -- verification ran after the commit"
    )
    assert _installed("mldb_xufai003") == previous, "a tenant after the failure was attempted"
    assert outcome.left == ["xufai003"]
    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT e.status, e.from_version FROM extension_upgrades e JOIN projects p "
            "ON p.id = e.project_id WHERE p.project_ref = 'xufai002' "
            "ORDER BY e.started_at DESC LIMIT 1",
        )
        versions = db.one(conn, "SELECT extension_versions FROM projects WHERE project_ref = 'xufai002'")
    assert row["status"] == "failed" and row["from_version"] == previous
    assert versions["extension_versions"].get("maludb_core") != target


@requires_upgrade_path
def test_a_platform_owned_memory_schema_is_re_enabled(admin_node_conn, old_tenants):
    """Phase 12 slice 0: ALTER EXTENSION leaves an enabled schema on its old facades."""
    _, target = VERSIONS
    node = _node("xu-mem")
    names = old_tenants("xumem001")
    _project("xumem001", node)
    with _tenant(names.database, autocommit=True) as t:
        t.execute(f'CREATE SCHEMA "{extension_upgrade.MEMORY_SCHEMA}"')
        t.execute("SELECT maludb_core.enable_memory_schema(%s)", (extension_upgrade.MEMORY_SCHEMA,))

    outcome = _run(admin_node_conn, "xu-mem")

    assert outcome.ok, outcome.error
    tenant = outcome.tenants[0]
    assert tenant.memory_schema_version == target
    with db.connection() as conn:
        recorded = db.one(
            conn, "SELECT maludb_memory_schema_version FROM projects WHERE project_ref = 'xumem001'"
        )
    assert recorded["maludb_memory_schema_version"] == target, (
        "the upgrade re-enabled the schema but left the project recording the old facades"
    )
    with _tenant(names.database) as t:
        facades = t.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s AND p.proname = ANY(%s)",
            (extension_upgrade.MEMORY_SCHEMA, list(extension_upgrade.DATAMODEL_FACADES)),
        ).fetchone()[0]
    if extension_upgrade._version(target) >= extension_upgrade.DATAMODEL_SINCE:  # noqa: SLF001
        assert facades == len(extension_upgrade.DATAMODEL_FACADES)


@requires_upgrade_path
def test_a_customer_owned_memory_schema_is_left_alone_and_does_not_stop_the_run(
    admin_node_conn, old_tenants
):
    """A customer can create `maludb_memory` from the SQL console.

    Re-enabling it would put superuser-owned SECURITY DEFINER functions in a
    schema the customer owns. Failing on it would let any customer block a node's
    security upgrade by naming a schema. Neither.
    """
    _, target = VERSIONS
    node = _node("xu-squat")
    names = old_tenants("xusqt001")
    _project("xusqt001", node)
    with _tenant(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute(f'CREATE SCHEMA "{extension_upgrade.MEMORY_SCHEMA}"')

    outcome = _run(admin_node_conn, "xu-squat")

    assert outcome.ok, outcome.error
    tenant = outcome.tenants[0]
    assert tenant.status == "upgraded"
    assert tenant.memory_schema_version is None
    assert "not the platform" in (tenant.detail or "")
    assert _installed(names.database) == target
    with _tenant(names.database) as t:
        planted = t.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s", (extension_upgrade.MEMORY_SCHEMA,)
        ).fetchone()[0]
    assert planted == 0, "platform functions were built inside a customer-owned schema"


@requires_upgrade_path
def test_a_tenant_mid_operation_is_skipped_not_raced(admin_node_conn, old_tenants):
    previous, _ = VERSIONS
    node = _node("xu-skip")
    old_tenants("xuskp001")
    _project("xuskp001", node, status="UPGRADING")

    outcome = _run(admin_node_conn, "xu-skip")

    assert outcome.ok, outcome.error
    assert outcome.tenants[0].status == "skipped"
    assert _installed("mldb_xuskp001") == previous


def test_a_concurrent_upgrade_on_the_same_node_is_refused(admin_node_conn):
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    node = _node("xu-lock")
    with db.connection() as holder:
        db.one(holder, "SELECT pg_advisory_lock(%s, %s) AS ok",
               (extension_upgrade._LOCK_NAMESPACE, node))  # noqa: SLF001
        try:
            outcome = _run(admin_node_conn, "xu-lock")
        finally:
            db.one(holder, "SELECT pg_advisory_unlock(%s, %s) AS ok",
                   (extension_upgrade._LOCK_NAMESPACE, node))  # noqa: SLF001
            holder.commit()
    assert outcome.status == "refused"
    assert "already running" in outcome.error


@requires_upgrade_path
def test_a_canary_run_does_not_report_current_tenants_as_left(admin_node_conn, old_tenants):
    """"Left" has to mean work remains.

    Found by running the command rather than the tests: after the canary, the run
    stopped looking, so a tenant already on the target was reported as outstanding.
    On a node where most tenants already took a version, the canary report claimed
    nearly the whole node was still to do.
    """
    _, target = VERSIONS
    node = _node("xu-left")
    for ref in ("xulft001", "xulft002", "xulft003"):
        old_tenants(ref)
        _project(ref, node)
    # The last tenant already took the target version some other way.
    with _tenant("mldb_xulft003", autocommit=True) as t:
        t.execute(f"ALTER EXTENSION maludb_core UPDATE TO '{target}'")

    outcome = _run(admin_node_conn, "xu-left")

    assert outcome.canary_run
    assert outcome.count("upgraded") == 1
    assert outcome.left == ["xulft002"], outcome.left
    assert [t.project_ref for t in outcome.tenants if t.status == "current"] == ["xulft003"]


# -- ADR-075 pinning slice 3: the run follows the pin ------------------------


def test_a_node_with_no_pin_is_refused(admin_node_conn):
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    node = _node("xu-nopin")
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM node_extension_pins WHERE node_id = %s", (node,))
        conn.commit()
    outcome = _run(admin_node_conn, "xu-nopin")
    assert outcome.status == "refused" and "no pin" in outcome.error


def test_a_pin_the_node_does_not_provide_is_refused_before_any_tenant(admin_node_conn):
    """Decision 4's corrective run, "once the pin and the package agree": a pin set
    ahead of its package must not start ALTERing tenants toward a version whose
    library is not there."""
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    from services.control_plane import extension_pins

    provided = node_provided_versions()["vector"]
    other = [v for v in extension_pins.tested_versions()["vector"] if v != provided]
    if not other:
        pytest.skip("needs a second listed vector version")
    node = _node("xu-ahead")
    with db.connection() as conn:
        agree_with_pins(conn, node, versions={"vector": other[0]})
    outcome = _run(admin_node_conn, "xu-ahead", extension="vector")
    assert outcome.status == "refused"
    assert f"pinned at {other[0]} but this node's packages provide {provided}" in outcome.error
    assert outcome.tenants == []


@requires_upgrade_path
def test_a_vector_run_records_tenants_at_the_pin_as_current(admin_node_conn, old_tenants):
    node = _node("xu-vec")
    for ref in ("xuvec001", "xuvec002"):
        old_tenants(ref)
        _project(ref, node)
    outcome = _run(admin_node_conn, "xu-vec", extension="vector", batch_size=10)
    assert outcome.ok, outcome.error
    assert outcome.target_version == node_provided_versions()["vector"]
    assert [t.status for t in outcome.tenants] == ["current", "current"]
    with db.connection() as conn:
        rows = db.query(conn, "SELECT DISTINCT extension FROM extension_upgrades e JOIN projects p "
                              "ON p.id = e.project_id WHERE p.project_ref LIKE 'xuvec%%'")
    assert [r["extension"] for r in rows] == ["vector"]


@requires_upgrade_path
def test_maludb_core_waits_for_a_tenant_whose_vector_lags_the_pin(admin_node_conn, old_tenants):
    """ADR-075: vector first. Here the node's vector pin is a listed version its
    tenants are not on; maludb_core's own pin is what the node provides, so the
    run starts -- and stops at the tenant, telling the operator which run is due."""
    from services.control_plane import extension_pins

    provided = node_provided_versions()
    other = [v for v in extension_pins.tested_versions()["vector"] if v != provided["vector"]]
    if not other:
        pytest.skip("needs a second listed vector version")
    node = _node("xu-order")
    with db.connection() as conn:
        agree_with_pins(conn, node, versions={"vector": other[0]})
    names = old_tenants("xuord001")
    _project("xuord001", node)

    outcome = _run(admin_node_conn, "xu-order")
    assert outcome.status == "stopped"
    assert "--extension vector" in outcome.tenants[0].detail
    assert _installed(names.database) == VERSIONS[0], "maludb_core moved ahead of vector"


def test_an_invalid_vector_index_is_named(admin_node_conn):
    if not NODE_ADMIN_DSN:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is unset")
    database = "mldb_xuvecidx"
    admin_node_conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    admin_node_conn.execute(f'CREATE DATABASE "{database}"')
    try:
        with _tenant(database) as t:
            t.execute("CREATE EXTENSION vector")
            t.execute("CREATE TABLE items (id int, embedding vector(3))")
            t.execute("CREATE INDEX items_hnsw ON items USING hnsw (embedding vector_l2_ops)")
            t.commit()
            assert extension_upgrade.invalid_vector_indexes(t) == []
            t.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'items_hnsw'::regclass")
            assert extension_upgrade.invalid_vector_indexes(t) == ["items_hnsw"]
            t.rollback()
    finally:
        admin_node_conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def test_the_drift_report_names_tenants_behind_a_pin_and_nodes_that_disagree():
    from services.control_plane import extension_pins

    agreeing = _node("xu-drift")
    with db.connection() as conn:
        pins = {ext: row["version"] for ext, row in extension_pins.pins(conn, agreeing).items()}
    ahead = _project("xudrf001", agreeing)
    behind = _project("xudrf002", agreeing)
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET extension_versions = %s WHERE id = %s",
                   (Jsonb(dict(pins)), ahead))
        db.execute(conn, "UPDATE projects SET extension_versions = %s WHERE id = %s",
                   (Jsonb({**pins, "vector": "0.0.1"}), behind))
        conn.commit()
    unpinned = _node("xu-drift-nopin")
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM node_extension_pins WHERE node_id = %s", (unpinned,))
        conn.commit()
        report = extension_pins.drift(conn)
    by_name = {n["name"]: n for n in report["nodes"]}
    assert by_name["xu-drift"]["refusal"] is None
    assert [t["project_ref"] for t in by_name["xu-drift"]["lagging"]] == ["xudrf002"]
    assert by_name["xu-drift"]["lagging"][0]["behind"] == {"vector": ("0.0.1", pins["vector"])}
    assert "no extension pin" in by_name["xu-drift-nopin"]["refusal"]


@requires_upgrade_path
def test_a_tenant_whose_vector_wrappers_fail_reverification_is_rolled_back(
    admin_node_conn, old_tenants, monkeypatch
):
    """ADR-077: an upgrade that breaks a tenant's vector wrappers must be undone.

    The node's previous maludb_core predates vector compartments, so no tenant can
    be enabled before this upgrade; `reverify` itself is exercised against a real
    enabled tenant in tests/test_maludb_vectors.py. This asserts the other half:
    the upgrade calls it inside the transaction, and its failure rolls back.
    """
    from services.control_plane import maludb_vectors

    previous, _target = VERSIONS
    node = _node("xu-vec")
    old_tenants("xuvec001")
    _project("xuvec001", node)
    called = []

    def broken(tenant_conn, names):
        called.append(names.project_ref)
        raise maludb_vectors.VectorsError("the vector wrappers could not run as service_role: simulated")

    monkeypatch.setattr(extension_upgrade.maludb_vectors, "reverify", broken)
    outcome = _run(admin_node_conn, "xu-vec")

    assert called == ["xuvec001"], "the upgrade did not re-verify vector wrappers"
    assert outcome.status == "stopped"
    assert "simulated" in outcome.tenants[0].detail
    assert _installed("mldb_xuvec001") == previous, "the upgrade committed past a broken wrapper"
