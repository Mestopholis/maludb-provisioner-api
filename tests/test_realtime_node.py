"""The node-level properties Realtime depends on, asserted against a real node.

These are the "required negative tests" table from
`specs/realtime-replication-model.md`. Every one of them was measured during the
Phase 06 slice 0 spike and then thrown away with the cluster it ran on; this file
is what keeps them true.

They need a cluster that plain PostgreSQL defaults cannot provide: `wal_level`
must be `logical`, which needs a restart, and `pg_hba.conf` must carry the
ADR-031 reject, which is a file. So they are gated on `MALUDB_REALTIME_NODE_DSN`
and skip loudly without it -- see the banner in `conftest.py`. Build one with:

    scripts/realtime-test-cluster.sh

R6b is the test the spec singles out as most likely to be dropped for being
awkward, because it needs a node whose `pg_hba.conf` is under test rather than
assumed. It is the difference between the lockdown holding and only appearing
to, so it is here twice: once through libpq's own physical-replication path, and
once through `pg_basebackup`, which is what an attacker would actually run.

Never point these at a node carrying customer data. A cluster that fails R6b
answers a base backup with a readable copy of every database on it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import psycopg
import pytest

from services.control_plane import provisioning, realtime

REALTIME_DSN = os.environ.get("MALUDB_REALTIME_NODE_DSN", "").strip()

# A cluster built *without* the ADR-031 reject, if one is available:
# `scripts/realtime-test-cluster.sh --permissive`. Optional, and worth having,
# because a check that has never returned "unsafe" has not been shown to be
# capable of it.
PERMISSIVE_DSN = os.environ.get("MALUDB_REALTIME_PERMISSIVE_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not REALTIME_DSN,
    reason="MALUDB_REALTIME_NODE_DSN is unset; build one with scripts/realtime-test-cluster.sh",
)

# Two tenant-shaped databases and the roles that reach them. Prefixed so a
# failed run leaves debris that is obviously ours.
TENANT_A = "mldb_rtt0001"
TENANT_B = "mldb_rtt0002"
REPLICATOR = "rtt_replicator"
PLAIN = "rtt_plain"
CREDENTIAL = "rtt-node-test-only"  # noqa: S105 - throwaway cluster, throwaway role


def _connect(dbname: str = "postgres", *, user: str | None = None, password: str | None = None,
             **extra) -> psycopg.Connection:
    parsed = psycopg.conninfo.conninfo_to_dict(REALTIME_DSN)
    parsed["dbname"] = dbname
    if user:
        parsed["user"] = user
        parsed["password"] = password
    parsed.update(extra)
    return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)


@pytest.fixture(scope="module")
def node():
    """Two locked-down tenant databases and a replicator that owns one of them.

    The lockdown is ADR-014's, applied exactly as `specs/tenant-role-model.md`
    specifies, because the finding under test is that it does not reach far
    enough -- and a test that applied a weaker lockdown would be proving
    something easier than the real thing.
    """
    admin = _connect()
    _teardown(admin)

    for database in (TENANT_A, TENANT_B):
        admin.execute(f'CREATE DATABASE "{database}"')
        admin.execute(f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC')

    # CREATE ROLE takes no parameters, so the password is a literal. Safe here
    # only because it is a constant in this file on a throwaway cluster; the
    # production path quotes through psycopg.sql in `provisioning.create_roles`.
    admin.execute(
        f"CREATE ROLE {REPLICATOR} LOGIN REPLICATION PASSWORD '{CREDENTIAL}' CONNECTION LIMIT 5"
    )
    admin.execute(f"CREATE ROLE {PLAIN} LOGIN PASSWORD '{CREDENTIAL}'")
    # CONNECT on its own database only, and explicitly denied the other.
    admin.execute(f'GRANT CONNECT ON DATABASE "{TENANT_A}" TO {REPLICATOR}, {PLAIN}')
    admin.execute(f'REVOKE CONNECT ON DATABASE "{TENANT_B}" FROM {REPLICATOR}')

    yield admin

    _teardown(admin)
    admin.close()


def _teardown(admin: psycopg.Connection) -> None:
    for database in (TENANT_A, TENANT_B):
        with contextlib_suppress():
            conn = _connect(database)
            conn.execute(
                "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                " WHERE database = current_database()"
            )
            conn.close()
    with contextlib_suppress():
        admin.execute(
            "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
            " WHERE database IS NULL"
        )
    for database in (TENANT_A, TENANT_B):
        admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    for role in (REPLICATOR, PLAIN):
        admin.execute(f"DROP ROLE IF EXISTS {role}")


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(psycopg.Error)


def _drop_slots(dbname: str) -> None:
    conn = _connect(dbname)
    conn.execute(
        "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
        " WHERE database = current_database() AND NOT active"
    )
    conn.close()


# --------------------------------------------------------------------------
# Node preparation. If this fails, nothing below it means anything.
# --------------------------------------------------------------------------


def test_the_test_node_is_prepared_for_realtime(node):
    readiness = realtime.inspect_node(node, dsn=REALTIME_DSN)
    assert readiness.ready, readiness.failures
    assert readiness.wal_level == "logical"
    assert readiness.max_slot_wal_keep_mb >= realtime.MIN_SLOT_WAL_KEEP_MB
    assert readiness.physical_replication_rejected is True


def test_the_default_local_peer_rule_is_not_reported_as_permissive(node):
    """`local replication all peer` is left alone on purpose.

    Peer authentication requires the connecting OS user to *be* the database
    role, which a Realtime process running as its own user cannot satisfy for
    `mldb_<ref>_replicator`. Flagging it would train an operator to ignore the
    check.
    """
    permissive, detail = realtime.inspect_hba(node)
    assert permissive == [], f"{permissive} ({detail})"


@pytest.mark.skipif(not PERMISSIVE_DSN, reason="MALUDB_REALTIME_PERMISSIVE_DSN is unset")
def test_the_probe_reports_a_node_without_the_reject_as_unsafe():
    """The negative control. A check that cannot fail has not been tested."""
    rejected, detail = realtime.probe_physical_replication(PERMISSIVE_DSN)
    assert rejected is False
    assert "accepted" in detail


# --------------------------------------------------------------------------
# R1, R2 -- slot arithmetic.
# --------------------------------------------------------------------------


def test_r1_a_slot_belongs_to_exactly_one_database(node):
    """One slot per tenant database. There is no multiplexing to be clever about."""
    conn_a = _connect(TENANT_A)
    conn_b = _connect(TENANT_B)
    try:
        conn_a.execute("SELECT pg_create_logical_replication_slot('rtt_a', 'pgoutput')")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState,
                           match="not created in this database"):
            conn_b.execute("SELECT pg_logical_slot_get_changes('rtt_a', NULL, NULL)")
    finally:
        conn_a.close()
        conn_b.close()
        _drop_slots(TENANT_A)


def test_r2_the_ceiling_fails_at_enablement_rather_than_at_runtime(node):
    """The good failure: loud, on the operation that asked for it, and retryable.

    Capacity accounting depends on this shape. A ceiling that failed later --
    on a project that believed it had Realtime -- could not be turned into a
    refusal at enablement time.
    """
    conn = _connect(TENANT_A)
    limit = int(node.execute("SHOW max_replication_slots").fetchone()[0])
    created = 0
    try:
        with pytest.raises(psycopg.errors.ConfigurationLimitExceeded,
                           match="all replication slots are in use"):
            for i in range(limit + 1):
                conn.execute(f"SELECT pg_create_logical_replication_slot('rtt_c{i}', 'pgoutput')")
                created += 1
        assert created <= limit
    finally:
        conn.close()
        _drop_slots(TENANT_A)


# --------------------------------------------------------------------------
# R5, R6, R8 -- what the replicator credential can and cannot reach.
# --------------------------------------------------------------------------


def test_r5_a_role_without_replication_cannot_decode(node):
    """There is no lesser grant that buys logical decoding.

    This is why the platform must issue `REPLICATION` to a customer-serving role
    at all, and therefore why ADR-031 exists.
    """
    conn = _connect(TENANT_A, user=PLAIN, password=CREDENTIAL)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege,
                           match="permission denied to use replication slots"):
            conn.execute("SELECT pg_create_logical_replication_slot('rtt_denied', 'pgoutput')")
    finally:
        conn.close()


def test_r6a_logical_replication_is_bound_by_connect(node):
    """The good half of R6, and the half shared Realtime depends on.

    Logical replication names a real database, so the ADR-014 lockdown reaches
    it. If this ever stops holding, one Realtime server holding many tenants'
    credentials becomes a cross-tenant read.
    """
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        _connect(TENANT_B, user=REPLICATOR, password=CREDENTIAL).close()


def test_r6b_the_replicator_cannot_open_a_physical_replication_connection(node):
    """ADR-031, through libpq's own path.

    `replication=true` is a physical replication connection and nothing else, so
    this reaches exactly the `pg_hba.conf` rule that decides whether the
    attribute is contained.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(REALTIME_DSN)
    parsed.update(user=REPLICATOR, password=CREDENTIAL, dbname="postgres")
    rejected, detail = realtime.probe_physical_replication(
        psycopg.conninfo.make_conninfo(**parsed)
    )
    assert rejected is True, detail


@pytest.mark.skipif(shutil.which("pg_basebackup") is None, reason="pg_basebackup is not installed")
def test_r6b_the_replicator_cannot_take_a_base_backup(node, tmp_path):
    """The same property, through the tool an attacker would actually reach for.

    During the spike this produced 484 MB containing every database on the
    cluster, including one the role was explicitly denied CONNECT on: physical
    replication names no database, so nothing scopes it. The assertion is on the
    error, and the assertion on the empty directory is the one that would catch
    a partial success.
    """
    target = tmp_path / "basebackup"
    # Resolved rather than looked up by the shell: the test asserts on what this
    # exact binary says, so it should not depend on PATH order at run time.
    binary = shutil.which("pg_basebackup")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary, "-h", _host(), "-p", _port(), "-U", REPLICATOR, "-D", str(target)],
        env={**os.environ, "PGPASSWORD": CREDENTIAL, "PGCONNECT_TIMEOUT": "10"},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    assert "pg_hba.conf rejects replication connection" in result.stderr
    assert not target.exists() or not any(target.iterdir())


def test_r8_the_replicator_reads_past_grants_and_row_level_security(node):
    """Decoding reads WAL, which is written before any policy is consulted.

    So the replicator is an unrestricted reader inside its own database --
    equivalent to BYPASSRLS plus SELECT on everything, present and future. Two
    things follow: the credential is a Class B secret of the highest value in
    the system, and **all** RLS enforcement for Postgres Changes happens in the
    Realtime server rather than in PostgreSQL. Slice 4's compatibility suite has
    to prove it does.
    """
    owner = _connect(TENANT_A)
    owner.execute("CREATE TABLE IF NOT EXISTS secret_t (id int primary key, note text)")
    owner.execute("REVOKE ALL ON secret_t FROM PUBLIC")
    owner.execute("ALTER TABLE secret_t ENABLE ROW LEVEL SECURITY")

    replicator = _connect(TENANT_A, user=REPLICATOR, password=CREDENTIAL)
    try:
        try:
            replicator.execute(
                "SELECT pg_create_logical_replication_slot('rtt_r8', 'test_decoding')"
            )
        except psycopg.errors.UndefinedFile:
            pytest.skip("the test_decoding output plugin is not installed on this node")

        # No grant, so the ordinary path is closed.
        with pytest.raises(psycopg.errors.InsufficientPrivilege,
                           match="permission denied for table secret_t"):
            replicator.execute("SELECT note FROM secret_t")

        owner.execute("INSERT INTO secret_t VALUES (1, 'rls-canary-visible-in-wal')")

        rows = replicator.execute(
            "SELECT data FROM pg_logical_slot_get_changes('rtt_r8', NULL, NULL)"
        ).fetchall()
        assert any("rls-canary-visible-in-wal" in row[0] for row in rows), (
            "the replicator could not read a row it is not granted -- if this is a real "
            "improvement rather than a broken test, the RLS story in ADR-031 changes"
        )
    finally:
        replicator.close()
        _drop_slots(TENANT_A)
        owner.execute("DROP TABLE IF EXISTS secret_t")
        owner.close()


# --------------------------------------------------------------------------
# R4 -- the cross-tenant availability failure, and the bound that contains it.
# --------------------------------------------------------------------------


def test_r4_a_stalled_consumer_loses_its_slot_instead_of_the_node_losing_its_disk(node):
    """ADR-032, end to end.

    Unbounded, one idle slot pinned 206 MB during a single insert and a
    CHECKPOINT did not release it; a full disk stops writes for every tenant on
    the node, so one project's *inactivity* becomes a cross-tenant outage. This
    is the inversion: the slot dies, the node lives.

    WAL is generated by switching segments rather than by inserting a few
    hundred thousand rows, because the property under test is how much WAL has
    passed since `restart_lsn`, not how it got there.
    """
    conn = _connect(TENANT_A)
    bound_mb = int(node.execute(
        "SELECT setting::int FROM pg_settings WHERE name = 'max_slot_wal_keep_size'"
    ).fetchone()[0])
    assert bound_mb > 0, "this node has no bound, which is the thing ADR-032 forbids"

    segment_mb = int(node.execute(
        "SELECT setting::int / (1024*1024) FROM pg_settings WHERE name = 'wal_segment_size'"
    ).fetchone()[0]) or 16
    switches = (bound_mb // segment_mb) + 3

    try:
        conn.execute("SELECT pg_create_logical_replication_slot('rtt_stall', 'pgoutput')")
        conn.execute("CREATE TABLE IF NOT EXISTS churn (id serial primary key, pad text)")
        for _ in range(switches):
            conn.execute("INSERT INTO churn (pad) VALUES (repeat('x', 1000))")
            conn.execute("SELECT pg_switch_wal()")
        # Invalidation happens at a checkpoint, not at the moment the bound is
        # crossed. Two, because the first only establishes the horizon the
        # second removes segments against.
        node.execute("CHECKPOINT")
        node.execute("CHECKPOINT")

        status = node.execute(
            "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rtt_stall'"
        ).fetchone()[0]
        assert status == "lost", (
            f"the slot is {status!r}, so WAL is still being retained for a consumer that "
            "is not reading it -- the disk-filling path ADR-032 exists to close"
        )

        # And the failure is reportable rather than silent, which is the half
        # that makes it acceptable.
        slots = {s.slot_name: s for s in realtime.slots_on_node(node)}
        assert slots["rtt_stall"].invalidated
    finally:
        conn.execute("DROP TABLE IF EXISTS churn")
        conn.close()
        _drop_slots(TENANT_A)


# --------------------------------------------------------------------------
# The attribute must never land on a customer-reachable role.
# --------------------------------------------------------------------------


def test_the_tenant_roles_provisioning_creates_never_hold_replication(node):
    """`REPLICATION` on the admin or authenticator role would hand a customer R6.

    Both are customer-reachable on paid plans, so this is the escalation
    `specs/tenant-role-model.md` lists as prohibited. Asserted against roles the
    real provisioning code creates rather than against a reading of it.
    """
    names = provisioning.TenantNames.for_ref("rtt00007")
    provisioning.ensure_shared_roles(node)
    provisioning.create_roles(
        node, names,
        passwords={"authenticator": CREDENTIAL, "auth": CREDENTIAL, "admin": CREDENTIAL},
        connection_limits={"authenticator": 5, "auth": 5},
    )
    try:
        rows = node.execute(
            "SELECT rolname, rolreplication, rolsuper, rolbypassrls FROM pg_roles "
            " WHERE rolname = ANY(%s)",
            ([names.authenticator, names.auth, names.admin],),
        ).fetchall()
        assert len(rows) == 3
        for name, replication, superuser, bypassrls in rows:
            assert not replication, f"{name} holds REPLICATION"
            assert not superuser, f"{name} is a superuser"
            assert not bypassrls, f"{name} holds BYPASSRLS"
    finally:
        for role in (names.authenticator, names.auth, names.admin):
            node.execute(f'DROP ROLE IF EXISTS "{role}"')


def _host() -> str:
    return psycopg.conninfo.conninfo_to_dict(REALTIME_DSN).get("host", "127.0.0.1")


def _port() -> str:
    return str(psycopg.conninfo.conninfo_to_dict(REALTIME_DSN).get("port", "5432"))
