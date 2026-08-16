"""Turning Realtime on for a project, and everything that must refuse to.

Phase 06 slice 2. These provision a real tenant on a real node and then enable
Realtime on it, because the interesting assertions are all about state that only
exists on the node: whether the replicator role really holds `REPLICATION` and
really cannot reach another tenant, whether the slot is really created and
really released again, and whether the publication is really owned by the role
the customer connects as.

They need a node with `wal_level = logical`, so they run against
`MALUDB_REALTIME_NODE_DSN` -- the cluster `scripts/realtime-test-cluster.sh`
builds -- rather than the ordinary test node, which is `replica` and where
`pg_create_logical_replication_slot` cannot work at all.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from services.control_plane import (
    db,
    identity,
    jobs,
    provisioning,
    realtime,
    tenant_bootstrap,
)
from tests.conftest import TEST_CREDENTIAL, requires_db

REALTIME_DSN = os.environ.get("MALUDB_REALTIME_NODE_DSN", "").strip()
PLATFORM_OWNER = os.environ.get("MALUDB_REALTIME_PLATFORM_OWNER", "postgres")

pytestmark = [
    requires_db,
    pytest.mark.skipif(
        not REALTIME_DSN,
        reason="MALUDB_REALTIME_NODE_DSN is unset; build one with scripts/realtime-test-cluster.sh",
    ),
]

# Plans, by what they entitle. Free is the interesting one: `realtime_connections`
# has been 0 there since Phase 05 slice 1, and slice 2's whole enablement path
# hangs off that number rather than off a new flag.
PAID_LIMITS = {"realtime_connections": 200, "database_connections": 10}
FREE_LIMITS = {"realtime_connections": 0}


def _admin_conn():
    return psycopg.connect(REALTIME_DSN, row_factory=psycopg.rows.dict_row)


def _tenant_connect(database: str):
    parsed = psycopg.conninfo.conninfo_to_dict(REALTIME_DSN)
    parsed["dbname"] = database
    return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)


@pytest.fixture
def node(db_pool, key_ring) -> int:
    """The Realtime cluster, registered as a node and marked prepared.

    Marked prepared rather than checked, because `record_readiness` is slice 1's
    and is tested there. What slice 2 needs from it is the recorded flag that
    placement and enablement read.
    """
    with db.connection() as conn:
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, "
            " capacity_json, last_health_at) "
            "VALUES ('rt-enable','rt.example','127.0.0.1','shared','active', %s::jsonb, now()) "
            "ON CONFLICT (name) DO UPDATE SET capacity_json = EXCLUDED.capacity_json, "
            "  status = 'active', last_health_at = now() RETURNING id",
            (psycopg.types.json.Jsonb({"realtime_ready": True, "max_replication_slots": 4}),),
        )["id"]
        nodes_set_admin_dsn(conn, node_id, key_ring)
        conn.commit()
    return node_id


def nodes_set_admin_dsn(conn, node_id: int, key_ring) -> None:
    from services.control_plane import nodes

    row = db.one(conn, "SELECT name FROM nodes WHERE id = %s", (node_id,))
    nodes.set_admin_dsn(conn, name=row["name"], dsn=REALTIME_DSN, key_ring=key_ring)


@pytest.fixture
def tenant(db_pool, node, key_ring):
    """A fully provisioned tenant on the Realtime cluster.

    Provisioned through `jobs.provision` rather than by hand: enablement leans on
    the roles, the lockdown and the bootstrap being exactly what provisioning
    produces, and a fixture that built them itself would be testing the fixture.
    """
    created: list[str] = []

    def make(ref: str, *, limits: dict | None = None) -> uuid.UUID:
        created.append(ref)
        _drop_tenant(ref)
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code, name, config_json) VALUES (%s,'RT',%s) "
                "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
                (f"rt-{ref}", psycopg.types.json.Jsonb({"limits": limits or PAID_LIMITS})),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, node_id) "
                "VALUES (%s,%s,%s,%s,%s,'PLACEMENT_RESERVED',%s)",
                (project_id, org, ref, ref, plan, node),
            )
            conn.commit()

            admin_conn = _admin_conn()
            try:
                provisioning.ensure_shared_roles(admin_conn)
                jobs.provision(
                    conn, admin_conn, project_id=project_id, key_ring=key_ring,
                    platform_owner=PLATFORM_OWNER, tenant_connect=_tenant_connect,
                )
            finally:
                admin_conn.close()
        return project_id

    yield make

    for ref in created:
        _drop_tenant(ref)


def _drop_tenant(ref: str) -> None:
    names = provisioning.TenantNames.for_ref(ref)
    with psycopg.connect(REALTIME_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                " WHERE database = %s", (names.database,)
            )
        conn.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
        # The parameter grant outlives the database and blocks DROP ROLE with
        # `privileges for parameter log_min_messages`. Dropping the database
        # takes the schema and its ownership with it, but not this: parameter
        # ACLs are a cluster-wide catalogue.
        conn.execute(
            f'REVOKE SET ON PARAMETER log_min_messages FROM "{names.replicator}"'
        ) if _role_exists(conn, names.replicator) else None
        # Memberships the replicator *granted*, which also outlive the database:
        # role membership is a cluster-wide catalogue, and PostgreSQL refuses to
        # drop a grantor while its grants stand. Upstream's tenant migrations
        # end with `GRANT supabase_realtime_admin TO postgres` run as the
        # replicator, so this only bites once a real server has migrated the
        # tenant -- which is what tests/test_realtime_server.py does.
        #
        # `provisioning.drop_replicator_role` gets there by a shorter road: it
        # runs DROP OWNED BY inside the tenant database, which is still present
        # when Realtime is disabled properly. Here the database is already gone.
        for member, granted in _granted_by(conn, names.replicator):
            conn.execute(f'REVOKE "{granted}" FROM "{member}" GRANTED BY "{names.replicator}"')
        for role in (names.replicator, names.authenticator, names.auth, names.admin):
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def _granted_by(conn, grantor: str) -> list[tuple[str, str]]:
    """(member, granted role) pairs this role handed out, if it still exists."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT member.rolname, granted.rolname "
            "  FROM pg_auth_members m "
            "  JOIN pg_roles member ON member.oid = m.member "
            "  JOIN pg_roles granted ON granted.oid = m.roleid "
            "  JOIN pg_roles grantor ON grantor.oid = m.grantor "
            " WHERE grantor.rolname = %s",
            (grantor,),
        )
        return list(cur.fetchall())


def _role_exists(conn, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        return cur.fetchone() is not None


def _enable(project_id: uuid.UUID, key_ring) -> realtime.Enablement:
    with db.connection() as conn:
        admin_conn = _admin_conn()
        try:
            return realtime.enable(
                conn, admin_conn, project_id=project_id, key_ring=key_ring,
                tenant_connect=_tenant_connect,
            )
        finally:
            admin_conn.close()


def _disable(project_id: uuid.UUID) -> realtime.Enablement:
    with db.connection() as conn:
        admin_conn = _admin_conn()
        try:
            return realtime.disable(
                conn, admin_conn, project_id=project_id, tenant_connect=_tenant_connect,
            )
        finally:
            admin_conn.close()


def _slots(database: str) -> list[dict]:
    with psycopg.connect(REALTIME_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slot_name, plugin, slot_type FROM pg_replication_slots WHERE database = %s",
                (database,),
            )
            return cur.fetchall()


def _project(project_id: uuid.UUID) -> dict:
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT realtime_enabled, realtime_slot_name, realtime_slot_state, "
            "       realtime_slot_lost_at FROM projects WHERE id = %s",
            (project_id,),
        )


def _events(project_id: uuid.UUID) -> list[dict]:
    with db.connection() as conn:
        return db.query(
            conn,
            "SELECT event_type, detail_json FROM audit_events WHERE project_id = %s ORDER BY id",
            (project_id,),
        )


# --------------------------------------------------------------------------
# The publication, which every tenant gets whether or not it uses Realtime.
# --------------------------------------------------------------------------


def test_every_tenant_gets_the_publication_and_the_customer_owns_it(tenant):
    """Upstream's name, upstream's semantics, and reachable by direct SQL.

    Owned by the tenant admin so `ALTER PUBLICATION supabase_realtime ADD TABLE`
    works exactly as it does against Supabase. Not database ownership: ADR-004
    still holds.
    """
    tenant("rte00001")
    names = provisioning.TenantNames.for_ref("rte00001")
    with _tenant_connect(names.database) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT pubname, pubowner::regrole::text AS owner, puballtables "
                "  FROM pg_publication WHERE pubname = %s", (realtime.PUBLICATION,)
            )
            row = cur.fetchone()

    assert row is not None, "bootstrap 009 did not create the publication"
    assert row["owner"] == names.admin
    # FOR ALL TABLES would need superuser to own and would put every table a
    # tenant ever creates into the WAL stream whether or not anyone subscribes.
    assert row["puballtables"] is False


def test_the_publication_survives_a_second_bootstrap(tenant):
    """Bootstrap files are re-runnable, and CREATE PUBLICATION has no IF NOT EXISTS."""
    tenant("rte00002")
    names = provisioning.TenantNames.for_ref("rte00002")
    with db.connection() as conn, _tenant_connect(names.database) as tenant_conn:
        tenant_bootstrap.bootstrap_project(
            conn, tenant_conn, project_id=_ref_to_id(conn, "rte00002")
        )
        assert realtime.publication_present(tenant_conn)


def _ref_to_id(conn, ref: str) -> uuid.UUID:
    return db.one(conn, "SELECT id FROM projects WHERE project_ref = %s", (ref,))["id"]


# --------------------------------------------------------------------------
# Enablement.
# --------------------------------------------------------------------------


def test_enabling_reserves_capacity_without_creating_a_slot(tenant, key_ring):
    """ADR-034: the Realtime *server* owns its slots, and creates them lazily.

    Slice 2 created one here. It was on the wrong output plugin, nothing ever
    read it, and it consumed one of the node's ten -- which was observed filling
    a cluster and breaking the next tenant's server. So enablement reserves
    capacity and builds the privileges, and the slots appear when a client first
    subscribes.
    """
    project_id = tenant("rte00003")
    names = provisioning.TenantNames.for_ref("rte00003")

    result = _enable(project_id, key_ring)
    assert result.changed
    assert result.slot_names == realtime.slot_names_for("rte00003")
    assert len(result.slot_names) == 2

    assert _slots(names.database) == [], "the platform must not create the server's slots"

    project = _project(project_id)
    assert project["realtime_enabled"]
    # Pending, not active: nothing has subscribed, so no slot exists yet, and
    # calling that an incident would fire on every enablement.
    assert project["realtime_slot_state"] == realtime.PENDING
    assert [e["event_type"] for e in _events(project_id)] == ["realtime.enabled"]


def test_the_replicator_role_holds_replication_and_nothing_else(tenant, key_ring):
    """The role the whole phase is careful about.

    `REPLICATION` is unavoidable -- logical decoding cannot be had without it --
    so what matters is that it comes with nothing else and reaches nothing else.
    """
    project_id = tenant("rte00004")
    names = provisioning.TenantNames.for_ref("rte00004")
    _enable(project_id, key_ring)

    with _admin_conn() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT rolreplication, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                "       rolcanlogin, rolinherit, rolconnlimit "
                "  FROM pg_roles WHERE rolname = %s", (names.replicator,)
            )
            role = cur.fetchone()

    assert role["rolreplication"] is True
    assert role["rolcanlogin"] is True
    assert role["rolsuper"] is False
    assert role["rolbypassrls"] is False
    assert role["rolcreatedb"] is False
    assert role["rolcreaterole"] is False
    assert role["rolconnlimit"] > 0
    # INHERIT, which slice 2 deliberately set the other way. Upstream's tenant
    # migrations move object ownership to `supabase_realtime_admin` and then
    # alter those objects, so the replicator must hold that role's privileges
    # implicitly -- NOINHERIT fails with `must be owner of table channels`.
    # Contained by the same argument as ADR-016: the role it inherits is
    # NOLOGIN and every privilege it carries attaches to per-database objects.
    assert role["rolinherit"] is True


def test_the_replicator_is_not_the_admin_or_the_authenticator(tenant, key_ring):
    """`REPLICATION` on a customer-reachable role hands them every tenant on the node.

    `specs/tenant-role-model.md` lists this as a prohibited escalation, and it is
    the reason the replicator is a fourth role rather than an attribute added to
    an existing one.
    """
    project_id = tenant("rte00005")
    names = provisioning.TenantNames.for_ref("rte00005")
    _enable(project_id, key_ring)

    with _admin_conn() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT rolname, rolreplication FROM pg_roles WHERE rolname = ANY(%s)",
                ([names.admin, names.authenticator, names.auth],),
            )
            for row in cur.fetchall():
                assert row["rolreplication"] is False, f"{row['rolname']} holds REPLICATION"


def test_the_replicator_cannot_reach_another_tenant(tenant, key_ring):
    """ADR-014's lockdown, on the path that it does reach.

    Logical replication names a real database, so `CONNECT` bounds it. This is
    the property shared Realtime depends on: one server holding many tenants'
    credentials is only safe while each credential reaches one database.
    """
    first = tenant("rte00006")
    tenant("rte00007")
    _enable(first, key_ring)

    with db.connection() as conn:
        password = _replicator_password(conn, first, key_ring)

    other = provisioning.TenantNames.for_ref("rte00007")
    parsed = psycopg.conninfo.conninfo_to_dict(REALTIME_DSN)
    parsed.update(
        dbname=other.database,
        user=provisioning.TenantNames.for_ref("rte00006").replicator,
        password=password,
    )
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        psycopg.connect(psycopg.conninfo.make_conninfo(**parsed)).close()


def _replicator_password(conn, project_id: uuid.UUID, key_ring) -> str:
    """The live replicator credential, for a test that has to authenticate as it.

    Through `load_credential` rather than a fixture-held copy, because storing it
    correctly is half of what enablement has to get right: a credential the
    platform cannot reproduce is a Realtime server that can never connect.
    """
    return provisioning.load_credential(
        conn, project_id=project_id, credential_type=realtime.CREDENTIAL_TYPE, key_ring=key_ring
    )


def test_a_free_project_cannot_enable_realtime(tenant, key_ring):
    """Entitlement-driven, off the number Phase 05 already set to zero."""
    project_id = tenant("rte00008", limits=FREE_LIMITS)
    with pytest.raises(realtime.RealtimeError, match="plan does not include Realtime"):
        _enable(project_id, key_ring)

    assert not _project(project_id)["realtime_enabled"]
    names = provisioning.TenantNames.for_ref("rte00008")
    assert _slots(names.database) == []


def test_enabling_twice_is_a_no_op(tenant, key_ring):
    """Provisioning operations must be idempotent or safely retryable."""
    project_id = tenant("rte00009")
    first = _enable(project_id, key_ring)
    second = _enable(project_id, key_ring)

    assert first.changed and not second.changed
    assert [e["event_type"] for e in _events(project_id)] == ["realtime.enabled"]


def test_a_failed_enablement_does_not_leak_the_slot_claim(tenant, key_ring, monkeypatch):
    """A claim without a slot would hold one of ten against the node forever.

    It would also be reported as `missing` by every maintenance pass after it --
    an incident raised about a capability the customer never received.
    """
    project_id = tenant("rte00010")

    def boom(*_args, **_kwargs):
        raise psycopg.OperationalError("the node went away")

    monkeypatch.setattr(provisioning, "grant_realtime_migration_rights", boom)
    with pytest.raises(psycopg.OperationalError):
        _enable(project_id, key_ring)

    project = _project(project_id)
    assert not project["realtime_enabled"]
    assert project["realtime_slot_state"] == realtime.NONE


# --------------------------------------------------------------------------
# Disablement, which has to actually release the slot.
# --------------------------------------------------------------------------


def test_disabling_drops_the_slot_and_the_role(tenant, key_ring):
    """Turning the capability off must reduce what exists, not just what is reachable.

    A slot left behind pins WAL with no project to attribute it to, which is
    ADR-032's failure arrived at by tidying up. And a dormant role holding
    `REPLICATION` is one pg_hba regression away from reading the cluster.
    """
    project_id = tenant("rte00011")
    names = provisioning.TenantNames.for_ref("rte00011")
    _enable(project_id, key_ring)

    result = _disable(project_id)
    assert result.changed
    assert _slots(names.database) == []

    with _admin_conn() as conn:
        assert not provisioning.has_replicator_role(conn, names)

    project = _project(project_id)
    assert not project["realtime_enabled"]
    assert project["realtime_slot_state"] == realtime.NONE
    assert [e["event_type"] for e in _events(project_id)][-1] == "realtime.disabled"


def test_disabling_works_after_the_server_has_run_its_migrations(tenant, key_ring):
    """Disable a project whose replicator has granted a role onward.

    Upstream's CreateRealtimeAdminAndMoveOwnership migration ends with
    `GRANT supabase_realtime_admin TO postgres`, executed by the replicator --
    which makes the replicator the *grantor*, and PostgreSQL refuses to drop a
    grantor while its grants stand. Dropping the role by hand at that point
    fails with `privileges for membership of role postgres in role
    supabase_realtime_admin`, which is how this scenario was noticed.

    `drop_replicator_role` already survives it, because `DROP OWNED BY` removes
    memberships the role granted as well as objects it owns. That is not
    obvious from the statement's name, it is the only thing standing between
    disablement and a role holding REPLICATION that cannot be removed, and
    nothing covered it -- no test in this suite runs a Realtime server, so no
    test had ever issued that grant.

    Reproduced by making the grant the way the migration does -- `SET ROLE` to
    the replicator, so the grantor is right -- rather than by requiring a live
    server.
    """
    project_id = tenant("rte00018")
    names = provisioning.TenantNames.for_ref("rte00018")
    _enable(project_id, key_ring)

    with _admin_conn() as conn:
        conn.execute(f'SET ROLE "{names.replicator}"')
        conn.execute(f'GRANT {provisioning.REALTIME_ADMIN_ROLE} TO "{PLATFORM_OWNER}"')
        conn.execute("RESET ROLE")

    _disable(project_id)

    with _admin_conn() as conn:
        assert not provisioning.has_replicator_role(conn, names), (
            "the replicator survived disablement, which leaves a role holding "
            "REPLICATION on the node with nothing using it"
        )


def test_disabling_revokes_the_stored_credential(tenant, key_ring):
    project_id = tenant("rte00012")
    _enable(project_id, key_ring)
    _disable(project_id)

    with db.connection() as conn:
        live = db.one(
            conn,
            "SELECT count(*) AS n FROM project_credentials "
            " WHERE project_id = %s AND credential_type = %s AND revoked_at IS NULL",
            (project_id, realtime.CREDENTIAL_TYPE),
        )
    assert live["n"] == 0


def test_disabling_a_project_that_never_had_it_is_harmless(tenant, key_ring):
    project_id = tenant("rte00013")
    result = _disable(project_id)
    assert not result.changed


def test_a_downgrade_takes_realtime_away_on_the_next_provisioning_run(tenant, key_ring):
    """The same shape as direct SQL: the plan is what decides, and provisioning applies it.

    Only the removing half. An upgrade does not silently start replicating a
    customer's tables, because enabling creates a role holding `REPLICATION` and
    that should be a decision rather than a side effect of a billing change.
    """
    project_id = tenant("rte00014")
    names = provisioning.TenantNames.for_ref("rte00014")
    _enable(project_id, key_ring)

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = "
            "  (SELECT plan_id FROM projects WHERE id = %s)",
            (psycopg.types.json.Jsonb({"limits": FREE_LIMITS}), project_id),
        )
        conn.commit()
        admin_conn = _admin_conn()
        try:
            jobs.provision(
                conn, admin_conn, project_id=project_id, key_ring=key_ring,
                platform_owner=PLATFORM_OWNER, tenant_connect=_tenant_connect,
            )
        finally:
            admin_conn.close()

    assert not _project(project_id)["realtime_enabled"]
    assert _slots(names.database) == []


# --------------------------------------------------------------------------
# Recovery, which slice 1 deliberately left to a person.
# --------------------------------------------------------------------------


def test_recovering_an_invalidated_slot_records_that_the_gap_is_gone(tenant, key_ring):
    project_id = tenant("rte00015")
    names = provisioning.TenantNames.for_ref("rte00015")
    _enable(project_id, key_ring)

    # Stand in for the invalidation rather than generating 64 MB of WAL: that
    # path is measured end to end in tests/test_realtime_node.py, and what is
    # under test here is what the platform does about it afterwards.
    with _tenant_connect(names.database) as conn:
        for name in realtime.slot_names_for("rte00015"):
            conn.execute("SELECT pg_drop_replication_slot(%s) FROM pg_replication_slots "
                         " WHERE slot_name = %s", (name, name))
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET realtime_slot_state = %s, realtime_slot_lost_at = now() "
            " WHERE id = %s",
            (realtime.LOST, project_id),
        )
        conn.commit()

    with db.connection() as conn:
        result = realtime.recover_slot(
            conn, project_id=project_id, tenant_connect=_tenant_connect
        )

    assert result.changed
    # Nothing is re-created here: the invalidated slots are cleared away and the
    # project's Realtime server rebuilds them on its next subscription
    # (ADR-034). Creating one would put it on the wrong output plugin.
    assert _slots(names.database) == []
    project = _project(project_id)
    assert project["realtime_slot_state"] == realtime.PENDING
    assert project["realtime_slot_lost_at"] is None

    event = _events(project_id)[-1]
    assert event["event_type"] == "realtime.slot_recreated"
    assert event["detail_json"]["replayed_on_recovery"] is False
    assert event["detail_json"]["gap_began_at"]


def test_a_working_slot_is_not_recovered(tenant, key_ring):
    """Re-creating a healthy slot would lose changes for no reason."""
    project_id = tenant("rte00016")
    _enable(project_id, key_ring)
    with db.connection() as conn, pytest.raises(realtime.RealtimeError, match="nothing to"):
        realtime.recover_slot(conn, project_id=project_id, tenant_connect=_tenant_connect)


# --------------------------------------------------------------------------
# Capacity, enforced at enablement.
# --------------------------------------------------------------------------


def test_enablement_is_refused_when_the_node_is_out_of_slots(tenant, key_ring):
    """R2's shape, moved forward: the refusal lands before anything is created.

    PostgreSQL fails loudly at the ceiling, which is what makes this possible --
    but a customer should be refused by the platform's own accounting rather
    than by a partly-built tenant discovering it.
    """
    project_id = tenant("rte00017")
    names = provisioning.TenantNames.for_ref("rte00017")

    # Fill the node's usable slots with claims from other projects.
    with db.connection() as conn:
        node_id = db.one(
            conn, "SELECT node_id FROM projects WHERE id = %s", (project_id,)
        )["node_id"]
        for i in range(realtime.PLATFORM_SLOT_ALLOWANCE):
            filler = uuid.uuid4()
            _, org = identity.create_user_with_personal_org(
                conn, email=f"fill{i}-rte@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(conn, "SELECT plan_id FROM projects WHERE id = %s", (project_id,))
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                " node_id, realtime_enabled, realtime_slot_name, realtime_slot_state) "
                "VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,TRUE,%s,'active')",
                (filler, org, f"rtfill{i:03d}", "filler", plan["plan_id"], node_id,
                 f"mldb_rtfill{i:03d}_rt"),
            )
        conn.commit()

    with pytest.raises(realtime.RealtimeError, match="cannot take another Realtime project"):
        _enable(project_id, key_ring)

    # Nothing half-built: no slot, no role, and the project is not marked.
    assert _slots(names.database) == []
    with _admin_conn() as conn:
        assert not provisioning.has_replicator_role(conn, names)
    assert not _project(project_id)["realtime_enabled"]
