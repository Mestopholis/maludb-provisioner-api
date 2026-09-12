"""Enabling the MaluDB data-model graph for a project (Phase 12 slice 2, ADR-074).

Against real tenants built by the provisioning module. The properties worth
asserting are the ones a stub cannot show: that the schema the platform builds
is owned by the platform, that a schema a customer created under the same name
is refused rather than built into, that no customer role can reach what was
built, and that a failed enablement leaves nothing behind for a re-run to trip
over.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import psycopg
import psycopg.sql
import pytest
from psycopg.types.json import Jsonb

from services.control_plane import db, identity, maludb, provisioning, tenant_bootstrap
from tests.conftest import NODE_ADMIN_DSN, PLATFORM_OWNER, TEST_CREDENTIAL, requires_db

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _control_plane(db_pool):  # noqa: ARG001 - every test here reads or writes the control plane
    yield


def _extension_versions() -> tuple[str | None, str | None]:
    """(default, previous-with-an-upgrade-path) for maludb_core, or Nones."""
    if not NODE_ADMIN_DSN:
        return None, None
    try:
        with psycopg.connect(NODE_ADMIN_DSN) as conn:
            row = conn.execute(
                "SELECT default_version FROM pg_available_extensions WHERE name = 'maludb_core'"
            ).fetchone()
            if row is None:
                return None, None
            sources = [r[0] for r in conn.execute(
                "SELECT source FROM pg_extension_update_paths('maludb_core') "
                "WHERE target = %s AND path IS NOT NULL AND source <> %s",
                (row[0], row[0]),
            ).fetchall()]
    except psycopg.Error:
        return None, None
    previous = max(sources, key=maludb.version_tuple) if sources else None
    return row[0], previous


DEFAULT_VERSION, PREVIOUS_VERSION = _extension_versions()
requires_node = pytest.mark.skipif(
    DEFAULT_VERSION is None
    or maludb.version_tuple(DEFAULT_VERSION or "0") < maludb.DATAMODEL_SINCE,
    reason="needs MALUDB_NODE_ADMIN_DSN and maludb_core 0.104.0 or later on the node",
)


@pytest.fixture
def admin_node_conn():
    conn = psycopg.connect(NODE_ADMIN_DSN, autocommit=True)
    yield conn
    conn.close()


def _drop_tenant(admin: psycopg.Connection, ref: str) -> None:
    names = provisioning.TenantNames.for_ref(ref)
    admin.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
    for (role,) in admin.execute(
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (f"mldb\\_{ref}\\_%",)
    ).fetchall():
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def _tenant_conn(database: str, **kw) -> psycopg.Connection:
    info = psycopg.conninfo.conninfo_to_dict(NODE_ADMIN_DSN)
    info["dbname"] = database
    return psycopg.connect(**info, **kw)


def _tenant_connect(database: str) -> psycopg.Connection:
    """What `cp-manage` hands `maludb.enable`: an autocommit superuser connection."""
    return _tenant_conn(database, autocommit=True)


@pytest.fixture
def tenants(admin_node_conn):
    made: list[str] = []

    def make(ref: str, *, version: str | None = None, node_name: str = "mdb-node",
             status: str = "ACTIVE", plan_config: dict | None = None):
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
        with _tenant_conn(names.database) as t:
            clause = f" VERSION '{version}'" if version else ""
            t.execute(f"CREATE EXTENSION maludb_core{clause} CASCADE")
            t.commit()
            tenant_bootstrap.apply(t)
            t.commit()

        with db.connection() as conn:
            node = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
                "VALUES (%s,%s,'10.0.0.8','shared','active',now()) "
                "ON CONFLICT (name) DO UPDATE SET status='active' RETURNING id",
                (node_name, f"{node_name}.example.com"),
            )["id"]
            code = f"mdb-{uuid.uuid4().hex[:6]}"
            plan = db.one(
                conn, "INSERT INTO plans (code, name, config_json) VALUES (%s,'mdb',%s) RETURNING id",
                (code, Jsonb(plan_config or {})),
            )["id"]
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}-{uuid.uuid4().hex[:6]}@example.com", password=TEST_CREDENTIAL
            )
            project_id = uuid.uuid4()
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                "node_id, database_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (project_id, org, ref, ref, plan, status, node, names.database),
            )
            conn.commit()
        return project_id, names, node

    yield make
    for ref in made:
        _drop_tenant(admin_node_conn, ref)


def _enable(project_id):
    with db.connection() as conn:
        return maludb.enable(conn, project_id=project_id, tenant_connect=_tenant_connect)


def _project_row(project_id):
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT maludb_datamodel_enabled, maludb_datamodel_enabled_at, "
            "maludb_memory_schema_version FROM projects WHERE id = %s",
            (project_id,),
        )


def _audit_count(project_id) -> int:
    with db.connection() as conn:
        return db.one(
            conn, "SELECT count(*) AS n FROM audit_events WHERE project_id = %s AND event_type = %s",
            (project_id, maludb.AUDIT_ENABLED),
        )["n"]


# -- enabling --------------------------------------------------------------


@requires_node
def test_enabling_builds_a_platform_owned_schema_and_records_it(tenants):
    project_id, names, _ = tenants("mdbena01")

    result = _enable(project_id)

    assert result.changed and result.detail == "enabled"
    assert result.memory_schema_version == DEFAULT_VERSION
    with _tenant_conn(names.database) as t:
        owned_by_superuser, _owner = maludb.memory_schema_owner(t)
        assert owned_by_superuser, "the platform's memory schema is not owned by the platform"
        assert maludb.datamodel_facades_present(t) == len(maludb.DATAMODEL_FACADES)
    row = _project_row(project_id)
    assert row["maludb_datamodel_enabled"]
    assert row["maludb_datamodel_enabled_at"] is not None
    assert row["maludb_memory_schema_version"] == DEFAULT_VERSION
    assert _audit_count(project_id) == 1


@requires_node
def test_no_customer_role_can_reach_what_enabling_built(tenants):
    """Slice 0's finding, asserted against a real enablement rather than a probe.

    The facades run as the node superuser. `enable` checks this itself on every
    run; the test holds the property independently, so a change to that check
    cannot quietly remove both.
    """
    project_id, names, _ = tenants("mdbrch01")
    _enable(project_id)
    with _tenant_conn(names.database) as t:
        for role in maludb.customer_roles(names):
            usage = t.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, maludb.MEMORY_SCHEMA)
            ).fetchone()[0]
            assert not usage, f"{role} can use {maludb.MEMORY_SCHEMA}"


@requires_node
def test_enabling_twice_changes_nothing(tenants):
    project_id, _, _ = tenants("mdbtwi01")
    _enable(project_id)
    first = _project_row(project_id)

    again = _enable(project_id)

    assert not again.changed and again.detail == "already enabled"
    assert _project_row(project_id)["maludb_datamodel_enabled_at"] == first["maludb_datamodel_enabled_at"]
    assert _audit_count(project_id) == 1, "a re-run recorded a second enablement"


@requires_node
def test_an_enablement_that_built_the_schema_but_never_recorded_it_is_finished(tenants):
    """The retry case AGENTS.md asks for.

    The tenant work commits before the control-plane record is written, so the
    failure a crash can leave is a built schema with no record. A re-run has to
    finish it, not trip over the schema that is already there.
    """
    project_id, names, _ = tenants("mdbret01")
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'CREATE SCHEMA "{maludb.MEMORY_SCHEMA}"')
        t.execute("SELECT maludb_core.enable_memory_schema(%s)", (maludb.MEMORY_SCHEMA,))
    assert not _project_row(project_id)["maludb_datamodel_enabled"]

    result = _enable(project_id)

    assert result.changed
    assert _project_row(project_id)["maludb_datamodel_enabled"]


# -- refusals --------------------------------------------------------------


@requires_node
def test_a_schema_the_customer_created_under_that_name_is_refused(tenants):
    """From the SQL console, which every tier has: executor -> admin -> CREATE SCHEMA.

    Building into it would put superuser-owned SECURITY DEFINER functions inside a
    schema the customer owns. The refusal has to say what to do, or the feature
    silently will not turn on.
    """
    project_id, names, _ = tenants("mdbsqt01")
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute(f'CREATE SCHEMA "{maludb.MEMORY_SCHEMA}"')

    with pytest.raises(maludb.MaludbError, match="Rename or drop"):
        _enable(project_id)

    with _tenant_conn(names.database) as t:
        planted = t.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = %s", (maludb.MEMORY_SCHEMA,)
        ).fetchone()[0]
    assert planted == 0, "platform functions were built inside the customer's schema"
    assert not _project_row(project_id)["maludb_datamodel_enabled"]


@requires_node
def test_a_plan_without_the_entitlement_is_refused_before_the_tenant_is_touched(tenants):
    project_id, names, _ = tenants("mdbent01", plan_config={"maludb_datamodel": False})

    with pytest.raises(maludb.MaludbError, match="plan does not include"):
        _enable(project_id)

    with _tenant_conn(names.database) as t:
        assert maludb.memory_schema_owner(t) is None


@requires_node
def test_a_project_mid_operation_is_refused(tenants):
    project_id, names, _ = tenants("mdbmov01", status="MOVING")
    with pytest.raises(maludb.MaludbError, match="MOVING"):
        _enable(project_id)
    with _tenant_conn(names.database) as t:
        assert maludb.memory_schema_owner(t) is None


@requires_node
def test_enabling_is_refused_while_an_extension_upgrade_holds_the_node(tenants):
    project_id, names, node = tenants("mdblck01")
    with db.connection() as holder:
        db.one(holder, "SELECT pg_advisory_lock(%s, %s) AS ok", (maludb.NODE_LOCK_NAMESPACE, node))
        try:
            with pytest.raises(maludb.MaludbError, match="extension upgrade is running"):
                _enable(project_id)
        finally:
            db.one(holder, "SELECT pg_advisory_unlock(%s, %s) AS ok",
                   (maludb.NODE_LOCK_NAMESPACE, node))
            holder.commit()
    with _tenant_conn(names.database) as t:
        assert maludb.memory_schema_owner(t) is None


@pytest.mark.skipif(
    PREVIOUS_VERSION is None
    or maludb.version_tuple(PREVIOUS_VERSION or "0") >= maludb.DATAMODEL_SINCE,
    reason="needs a maludb_core version older than the data-model graph on the node",
)
def test_a_tenant_on_an_extension_too_old_is_refused(tenants):
    """The refusal names the fix. It fires before anything is built, so this
    shows the order of the checks; the rollback is shown by the test below."""
    project_id, names, _ = tenants("mdbold01", version=PREVIOUS_VERSION)

    with pytest.raises(maludb.MaludbError, match="cp-manage extension"):
        _enable(project_id)

    with _tenant_conn(names.database) as t:
        assert maludb.memory_schema_owner(t) is None
    assert not _project_row(project_id)["maludb_datamodel_enabled"]


@requires_node
def test_a_failure_after_the_schema_is_built_leaves_nothing_behind(tenants):
    """The rollback, shown where it matters: after CREATE SCHEMA and the facades.

    Default privileges make any schema the platform creates grant `anon` USAGE --
    the shape an upstream release changing the extension's ACLs would take. So
    enablement builds the schema and all 165 objects, then its own check finds a
    customer role can reach them. Everything must be gone afterwards, or a re-run
    would find a half-built schema and the customer a reachable one.
    """
    project_id, names, _ = tenants("mdbrbk01")
    with _tenant_conn(names.database, autocommit=True) as t:
        admin_user = t.execute("SELECT current_user").fetchone()[0]
        t.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{admin_user}" GRANT USAGE ON SCHEMAS TO anon')

    with pytest.raises(maludb.MaludbError, match="anon can use"):
        _enable(project_id)

    with _tenant_conn(names.database) as t:
        assert maludb.memory_schema_owner(t) is None, (
            "the schema survived a failed enablement -- the tenant work is not one transaction"
        )
    assert not _project_row(project_id)["maludb_datamodel_enabled"]
    assert _audit_count(project_id) == 0


# -- entitlements ----------------------------------------------------------


def test_every_tier_is_entitled_to_the_data_model_graph():
    """ADR-074 decision 4: every plan, with refresh limited per plan."""
    from services.control_plane import entitlements

    for code, defaults in entitlements.DEFAULTS.items():
        assert defaults["maludb_datamodel"] is True, code
        assert defaults["datamodel_refreshes_per_hour"] > 0, code
    tiers = entitlements.DEFAULTS
    assert (tiers["free"]["datamodel_refreshes_per_hour"]
            < tiers["starter"]["datamodel_refreshes_per_hour"]
            < tiers["production"]["datamodel_refreshes_per_hour"])


# -- the copy, and exposing it (slice 3) -----------------------------------


def _customer_schema(names) -> None:
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("CREATE TABLE public.customers (id bigint PRIMARY KEY, email text NOT NULL)")
        t.execute("CREATE TABLE public.orders (id bigint PRIMARY KEY, "
                  "customer_id bigint REFERENCES public.customers(id), total numeric)")
        t.execute("CREATE VIEW public.big_orders AS SELECT * FROM public.orders WHERE total > 100")
        t.execute("CREATE FUNCTION public.order_total(p bigint) RETURNS numeric LANGUAGE sql "
                  "AS $$ SELECT total FROM public.orders WHERE id = p $$")
        # Shares its name with pgcrypto's armor(bytea), which ADR-018 leaves in public.
        t.execute("CREATE FUNCTION public.armor(x integer) RETURNS integer LANGUAGE sql AS $$ SELECT x $$")


def _rows(database: str, sql: str, params=()):
    with _tenant_conn(database) as t:
        return t.execute(sql, params).fetchall()


@requires_node
def test_enabling_takes_a_first_copy_of_the_customers_own_model(tenants):
    """What a customer reads is their model, not the extension's.

    The refresh introspects all of `public`, where ADR-018 leaves maludb_core's
    373 functions: in a tenant with three relations, 334 of 338 raw nodes were
    the extension's. The copy keeps the customer's own objects -- including a
    function that happens to share its name with one of the extension's.
    """
    project_id, names, _ = tenants("mdbcpy01")
    _customer_schema(names)

    result = _enable(project_id)

    assert result.copy is not None and result.copy.relations == 3
    relations = dict(_rows(names.database,
                           "SELECT relation_name, kind FROM maludb.datamodel_relations"))
    assert relations == {"customers": "table", "orders": "table", "big_orders": "view"}
    routines = {r[0] for r in _rows(names.database,
                                    "SELECT name FROM maludb.datamodel_nodes WHERE node_type = 'db_routine'")}
    assert routines == {"public.order_total", "public.armor"}, (
        f"expected only the customer's routines, got {len(routines)}: {sorted(routines)[:5]}"
    )
    fk = _rows(names.database, """
        SELECT 1 FROM maludb.datamodel_edges e
          JOIN maludb.datamodel_nodes s ON s.node_id = e.source_node_id
          JOIN maludb.datamodel_nodes d ON d.node_id = e.target_node_id
         WHERE s.name = 'public.orders' AND e.relationship = 'fk_references'
           AND d.name = 'public.customers'""")
    assert fk, "the copy lost the foreign key from orders to customers"
    columns = _rows(names.database, "SELECT description -> 'columns' FROM maludb.datamodel_relations "
                                    "WHERE relation_name = 'orders'")[0][0]
    assert {c["name"] for c in columns} == {"id", "customer_id", "total"}


@requires_node
def test_a_refresh_replaces_the_copy_with_the_schema_as_it_now_is(tenants):
    project_id, names, _ = tenants("mdbrfs01")
    _customer_schema(names)
    _enable(project_id)
    before = _rows(names.database, "SELECT max(refreshed_at) FROM maludb.datamodel_relations")[0][0]
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute("DROP VIEW public.big_orders")
        t.execute("CREATE TABLE public.invoices (id bigint PRIMARY KEY)")

    with db.connection() as conn:
        result = maludb.refresh(conn, project_id=project_id, tenant_connect=_tenant_connect)

    names_now = {r[0] for r in _rows(names.database, "SELECT relation_name FROM maludb.datamodel_relations")}
    assert names_now == {"customers", "orders", "invoices"}
    assert result.relations == 3
    after = _rows(names.database, "SELECT min(refreshed_at) FROM maludb.datamodel_relations")[0][0]
    assert after > before


class _FailingOn:
    """A tenant connection that raises on one statement, to fail a copy halfway."""

    def __init__(self, conn, fragment: str):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_fragment", fragment)

    def execute(self, query, *args, **kwargs):
        if self._fragment in str(query):
            raise RuntimeError("injected failure after the old copy was deleted")
        return self._conn.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


@requires_node
def test_a_copy_that_fails_halfway_leaves_the_previous_copy_intact(tenants):
    """Readers see one refresh or the next, never neither.

    The failure is injected after the old rows are deleted and the nodes
    reinserted -- the worst point to stop. A copy that was not one transaction
    would leave a customer reading an empty or partial graph.
    """
    project_id, names, _ = tenants("mdbatm01")
    _customer_schema(names)
    _enable(project_id)
    before = _rows(names.database, "SELECT count(*), max(refreshed_at) FROM maludb.datamodel_relations")[0]

    def failing(database):
        return _FailingOn(_tenant_connect(database), "INSERT INTO maludb.datamodel_relations")

    with db.connection() as conn, pytest.raises(RuntimeError, match="injected"):
        maludb.refresh(conn, project_id=project_id, tenant_connect=failing)

    assert _rows(names.database,
                 "SELECT count(*), max(refreshed_at) FROM maludb.datamodel_relations")[0] == before
    assert _rows(names.database, "SELECT count(*) FROM maludb.datamodel_nodes")[0][0] > 0


@requires_node
def test_only_service_role_can_read_the_copy_and_nobody_can_write_it(tenants):
    """Slice 0: `describe` discloses the structure of tables the caller cannot read.

    So the copy is `service_role`'s alone. Asserted here independently of the
    check `enable` makes on itself, so a change to that check cannot quietly
    remove both.
    """
    project_id, names, _ = tenants("mdbacl01")
    _enable(project_id)
    with _tenant_conn(names.database) as t:
        for role in maludb.customer_roles(names):
            usage = t.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')",
                              (role, maludb.COPY_SCHEMA)).fetchone()[0]
            assert usage == (role == "service_role"), f"{role} USAGE on {maludb.COPY_SCHEMA}: {usage}"
            for table in maludb.COPY_TABLES:
                qualified = f"{maludb.COPY_SCHEMA}.{table}"
                writes = t.execute(
                    "SELECT has_table_privilege(%s, %s, 'INSERT') OR has_table_privilege(%s, %s, 'UPDATE') "
                    "OR has_table_privilege(%s, %s, 'DELETE') OR has_table_privilege(%s, %s, 'TRUNCATE')",
                    (role, qualified) * 4,
                ).fetchone()[0]
                assert not writes, f"{role} can write {qualified}"


@requires_node
def test_exposure_is_set_in_the_database_and_no_customer_role_can_change_it(tenants):
    """PostgREST reads `pgrst.db_schemas` from the authenticator. So who can write it
    decides what the Data API publishes -- `auth` included. Only the platform."""
    project_id, names, _ = tenants("mdbexp01")
    _, other, _ = tenants("mdbexp02")  # never enabled
    _enable(project_id)

    setting = _rows(names.database,
                    "SELECT array_to_string(setconfig, ',') FROM pg_db_role_setting "
                    "WHERE setrole = %s::regrole", (names.authenticator,))
    assert setting == [(f"pgrst.db_schemas=public, {maludb.COPY_SCHEMA}",)]
    assert _rows(other.database,
                 "SELECT count(*) FROM pg_db_role_setting WHERE setrole = %s::regrole",
                 (other.authenticator,))[0][0] == 0, "a project that never enabled it was exposed"

    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        for statement in (
            f"ALTER ROLE \"{names.authenticator}\" IN DATABASE \"{names.database}\" "
            "SET pgrst.db_schemas = 'public, auth'",
            f"ALTER ROLE \"{names.authenticator}\" SET pgrst.db_schemas = 'public, auth'",
            f"ALTER DATABASE \"{names.database}\" SET pgrst.db_schemas = 'public, auth'",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                t.execute(statement)


@requires_node
def test_a_customer_owned_maludb_schema_is_refused_before_anything_is_built(tenants):
    project_id, names, _ = tenants("mdbsq201")
    with _tenant_conn(names.database, autocommit=True) as t:
        t.execute(f'SET ROLE "{names.admin}"')
        t.execute(f'CREATE SCHEMA "{maludb.COPY_SCHEMA}"')

    with pytest.raises(maludb.MaludbError, match=f"schema named {maludb.COPY_SCHEMA}"):
        _enable(project_id)

    with _tenant_conn(names.database) as t:
        assert maludb.memory_schema_owner(t) is None, "superuser code ran before the refusal"
    assert _rows(names.database, "SELECT count(*) FROM pg_db_role_setting WHERE setrole = %s::regrole",
                 (names.authenticator,))[0][0] == 0


@requires_node
def test_a_refresh_is_refused_for_a_project_that_is_not_enabled(tenants):
    project_id, _, _ = tenants("mdbnen01")
    with db.connection() as conn, pytest.raises(maludb.MaludbError, match="not enabled"):
        maludb.refresh(conn, project_id=project_id, tenant_connect=_tenant_connect)


POSTGREST_BIN = os.environ.get("MALUDB_POSTGREST_BIN", "postgrest")


@requires_node
@pytest.mark.skipif(shutil.which(POSTGREST_BIN) is None and not os.path.exists(POSTGREST_BIN),
                    reason="needs a PostgREST binary")
def test_postgrest_serves_the_copy_to_service_role_with_the_rendered_file_unchanged(tenants, tmp_path):
    """The claim this slice rests on, against a real PostgREST.

    The worker's config file is rendered on the node with `db-schemas = "public"`
    and nothing here rewrites it. The copy is served because enablement set
    `pgrst.db_schemas` in the database -- and to `service_role` only.
    """
    import jwt

    from services.control_plane import workers

    project_id, names, _ = tenants("mdbpgr01")
    _customer_schema(names)
    password = provisioning.generate_password()
    with psycopg.connect(NODE_ADMIN_DSN, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            psycopg.sql.Identifier(names.authenticator), psycopg.sql.Literal(password)))
    secret = "slice-3-test-jwt-secret-not-for-production-0000"  # noqa: S105 - test fixture
    settings = workers.WorkerSettings(
        project_ref="mdbpgr01", database=names.database, authenticator_role=names.authenticator,
        authenticator_password=password, jwt_secret=secret, port=27433,
    )
    config = workers.write_config(settings, config_dir=tmp_path)
    assert 'db-schemas = "public"' in config.read_text()

    process = subprocess.Popen([POSTGREST_BIN, str(config)],  # noqa: S603 - fixed binary, generated config
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        workers.wait_until_ready(settings.port, timeout=30)
        _enable(project_id)

        def get(token: str | None) -> tuple[int, str]:
            headers = {"Accept-Profile": maludb.COPY_SCHEMA}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                f"http://127.0.0.1:{settings.port}/datamodel_relations?select=relation_name",
                headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        service = jwt.encode({"role": "service_role"}, secret, algorithm="HS256")
        deadline = time.monotonic() + 15
        status, body = 0, ""
        while time.monotonic() < deadline:
            status, body = get(service)
            if status == 200:
                break
            time.sleep(0.2)
        assert status == 200, f"service_role could not read the copy: {status} {body}"
        assert "orders" in body

        anon_status, anon_body = get(None)
        assert anon_status != 200, f"anon read the copy: {anon_body}"
        authed = jwt.encode({"role": "authenticated"}, secret, algorithm="HS256")
        authed_status, authed_body = get(authed)
        assert authed_status != 200, f"authenticated read the copy: {authed_body}"
    finally:
        process.terminate()
        process.wait(timeout=10)
