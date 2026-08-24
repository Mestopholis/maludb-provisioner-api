"""The state machine: idempotency, resume, retry, and cleanup that refuses.

Slice 3 left provisioning as one linear function that raised if the project
already had a database, so a run that died partway was terminal -- the tenant
kept its roles and its database and nothing could move it either way. These
test the properties that fixes, by breaking a run at a real boundary and
resuming it, rather than by asserting that a status column was written.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from services.control_plane import db, jobs, provisioning
from tests.conftest import requires_db
from tests.test_provisioning import (
    ADMIN_DSN,
    PLATFORM_OWNER,
    _tenant_admin_dsn,
    requires_maludb_core,
)

pytestmark = [
    requires_db,
    pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset"),
]


def _tenant_connect(database: str):
    return psycopg.connect(_tenant_admin_dsn(database), autocommit=True)


def _run(project_id: uuid.UUID, admin_conn, key_ring, **kwargs):
    with db.connection() as conn:
        return jobs.provision(
            conn,
            admin_conn,
            project_id=project_id,
            key_ring=key_ring,
            platform_owner=PLATFORM_OWNER,
            tenant_connect=_tenant_connect,
            **kwargs,
        )


def _status(project_id: uuid.UUID) -> str:
    with db.connection() as conn:
        return db.one(conn, "SELECT status FROM projects WHERE id = %s", (project_id,))["status"]


def _attempts(project_id: uuid.UUID) -> list[dict]:
    with db.connection() as conn:
        return db.query(
            conn,
            "SELECT attempt, state, error_code, error_detail, completed_at FROM provisioning_jobs "
            "WHERE project_id = %s ORDER BY attempt",
            (project_id,),
        )


# -- idempotency -----------------------------------------------------------


@requires_maludb_core
def test_provisioning_is_idempotent(admin_conn, key_ring, project_factory):
    """Re-running a completed project re-validates and returns, rather than
    erroring on the database it created itself last time."""
    project_id = project_factory("pj000001")
    first = _run(project_id, admin_conn, key_ring)
    second = _run(project_id, admin_conn, key_ring)

    assert first == second
    assert _status(project_id) == "PROVISIONED"

    with db.connection() as conn:
        credentials = db.query(
            conn,
            "SELECT count(*) AS n FROM project_credentials WHERE project_id = %s AND revoked_at IS NULL",
            (project_id,),
        )
    # Five types since ADR-047 added the client role, six since Phase 10 slice 1
    # added the storage role. The number is not the point; the point is that a
    # second run supersedes rather than accumulates, so an extra row here would
    # mean two live credentials for one role and `load_credential` returning
    # whichever the planner reached first.
    assert credentials[0]["n"] == 6, "a second run must not leave two live credentials per type"


@requires_maludb_core
def test_a_second_run_skips_the_steps_already_done(admin_conn, key_ring, project_factory):
    """The point of the `done` predicates: the second attempt does no node work."""
    project_id = project_factory("pj000002")
    _run(project_id, admin_conn, key_ring)

    with db.connection() as conn:
        before = db.one(
            conn, "SELECT ciphertext FROM project_credentials WHERE project_id = %s "
            "AND credential_type = 'db_admin' AND revoked_at IS NULL", (project_id,)
        )["ciphertext"]

    _run(project_id, admin_conn, key_ring)

    with db.connection() as conn:
        after = db.one(
            conn, "SELECT ciphertext FROM project_credentials WHERE project_id = %s "
            "AND credential_type = 'db_admin' AND revoked_at IS NULL", (project_id,)
        )["ciphertext"]

    assert bytes(before) == bytes(after), "roles were recreated and the password needlessly rotated"


# -- resume ----------------------------------------------------------------


@requires_maludb_core
def test_a_run_that_dies_after_the_database_resumes_and_completes(admin_conn, key_ring, project_factory):
    """The failure that used to be terminal: roles and a database exist, the
    project is mid-flight, and the old code refused to touch it again."""
    project_id = project_factory("pj000003")
    names = provisioning.TenantNames.for_ref("pj000003")

    def explode(database: str):
        raise psycopg.OperationalError("node went away mid-bootstrap")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    assert _status(project_id) == "RETRY_WAIT"
    assert provisioning.database_exists(admin_conn, names.database)

    # The retry has a real database and real roles waiting for it.
    with db.connection() as conn:
        db.execute(conn, "UPDATE projects SET retry_after = now() WHERE id = %s", (project_id,))
        conn.commit()
    resumed = _run(project_id, admin_conn, key_ring)

    assert resumed.database == names.database
    assert _status(project_id) == "PROVISIONED"
    assert [a["attempt"] for a in _attempts(project_id)] == [1, 2]


@requires_maludb_core
def test_a_resume_recovers_credentials_the_failed_attempt_lost(admin_conn, key_ring, project_factory):
    """Roles existing without a recoverable password is the state that strands
    a tenant: nothing can authenticate and no later step can fix it."""
    project_id = project_factory("pj000004")
    names = provisioning.TenantNames.for_ref("pj000004")

    # Simulate an attempt that created the roles and died before persisting.
    provisioning.ensure_shared_roles(admin_conn)
    provisioning.create_roles(
        admin_conn, names,
        passwords={k: provisioning.generate_password() for k in ("authenticator", "auth", "admin")},
        connection_limits={},
    )
    admin_conn.commit()

    _run(project_id, admin_conn, key_ring)

    with db.connection() as conn:
        password = provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_authenticator", key_ring=key_ring
        )
    # The recovered password must actually work, which is the whole claim.
    from tests.test_provisioning import _tenant_dsn

    with psycopg.connect(_tenant_dsn(names.database, names.authenticator, password)) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


# -- retry and failure -----------------------------------------------------


def test_a_failure_moves_to_retry_wait_with_a_time(admin_conn, key_ring, project_factory):
    project_id = project_factory("pj000005")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    with db.connection() as conn:
        row = db.one(
            conn, "SELECT status, retry_after, failed_at FROM projects WHERE id = %s", (project_id,)
        )
    assert row["status"] == "RETRY_WAIT"
    assert row["retry_after"] is not None, "RETRY_WAIT with no time is retried instantly and fails instantly"
    assert row["failed_at"] is not None


def test_retries_are_capped_and_then_the_project_is_failed(admin_conn, key_ring, project_factory):
    project_id = project_factory("pj000006")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    for _ in range(jobs.MAX_ATTEMPTS):
        with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
            jobs.provision(
                conn, admin_conn, project_id=project_id, key_ring=key_ring,
                platform_owner=PLATFORM_OWNER, tenant_connect=explode,
            )

    assert _status(project_id) == "FAILED"
    with db.connection() as conn, pytest.raises(jobs.RetriesExhausted):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )


def test_every_attempt_is_recorded_separately(admin_conn, key_ring, project_factory):
    """One row per attempt. Overwriting in place destroys exactly the history
    an operator needs to see that a tenant failed twice before succeeding."""
    project_id = project_factory("pj000007")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    for _ in range(2):
        with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
            jobs.provision(
                conn, admin_conn, project_id=project_id, key_ring=key_ring,
                platform_owner=PLATFORM_OWNER, tenant_connect=explode,
            )

    attempts = _attempts(project_id)
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert all(a["completed_at"] is not None for a in attempts)
    assert all(a["state"] == "FAILED" for a in attempts)


def test_due_for_retry_returns_only_projects_whose_wait_has_elapsed(admin_conn, key_ring, project_factory):
    project_id = project_factory("pj000008")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    with db.connection() as conn:
        assert jobs.due_for_retry(conn) == [], "a project still inside its backoff was handed out"
        db.execute(conn, "UPDATE projects SET retry_after = now() - interval '1 minute' WHERE id = %s",
                   (project_id,))
        conn.commit()
        assert [row["id"] for row in jobs.due_for_retry(conn)] == [project_id]


# -- credential leakage ----------------------------------------------------


def test_a_failed_run_records_no_credential_anywhere(admin_conn, key_ring, project_factory, caplog):
    """Acceptance criterion. psycopg puts the failing statement in its error
    text, and CREATE ROLE embeds the password literal -- so the driver message
    must never reach provisioning_jobs, which every operator dashboard reads."""
    project_id = project_factory("pj000009")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with caplog.at_level("DEBUG"), db.connection() as conn:
        with pytest.raises(provisioning.ProvisioningError):
            jobs.provision(
                conn, admin_conn, project_id=project_id, key_ring=key_ring,
                platform_owner=PLATFORM_OWNER, tenant_connect=explode,
            )

    with db.connection() as conn:
        password = provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_authenticator", key_ring=key_ring
        )
        stored = db.query(
            conn,
            "SELECT error_code, error_detail FROM provisioning_jobs WHERE project_id = %s",
            (project_id,),
        )

    recorded = " ".join(f"{r['error_code']} {r['error_detail']}" for r in stored)
    assert password not in recorded
    assert "PASSWORD" not in recorded.upper()
    assert password not in caplog.text
    # And it must still be useful to an operator.
    assert stored[0]["error_code"].startswith("postgres.")


# -- cleanup ---------------------------------------------------------------


def test_cleanup_refuses_a_project_that_is_not_failed(admin_conn, key_ring, project_factory):
    project_id = project_factory("pj00000a")
    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError, match="refusing to clean up"):
        jobs.cleanup(
            conn, admin_conn, project_id=project_id, tenant_connect=_tenant_connect,
        )


def test_cleanup_does_not_drop_a_database_by_default(admin_conn, key_ring, project_factory):
    """The data-safety invariant. Cleanup restoring desired state is never
    reason enough to drop a database that might hold customer data."""
    project_id = project_factory("pj00000b")
    names = provisioning.TenantNames.for_ref("pj00000b")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    with db.connection() as conn:
        report = jobs.cleanup(conn, admin_conn, project_id=project_id, tenant_connect=_tenant_connect)

    assert report.dropped_database is None
    assert report.retained_database == names.database
    assert report.refused_because is not None
    assert provisioning.database_exists(admin_conn, names.database), "the database was dropped anyway"


def test_cleanup_refuses_even_when_allowed_if_the_database_holds_tenant_objects(
    admin_conn, key_ring, project_factory
):
    project_id = project_factory("pj00000c")
    names = provisioning.TenantNames.for_ref("pj00000c")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        tenant_conn.execute("CREATE TABLE public.customer_data (id int)")
        tenant_conn.commit()

    with db.connection() as conn:
        report = jobs.cleanup(
            conn, admin_conn, project_id=project_id,
            tenant_connect=_tenant_connect, allow_database_drop=True,
        )

    assert report.dropped_database is None
    assert "tenant-created objects" in report.refused_because
    assert provisioning.database_exists(admin_conn, names.database)


def test_cleanup_reclaims_an_empty_database_when_explicitly_allowed(admin_conn, key_ring, project_factory):
    project_id = project_factory("pj00000d")
    names = provisioning.TenantNames.for_ref("pj00000d")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    with db.connection() as conn:
        report = jobs.cleanup(
            conn, admin_conn, project_id=project_id,
            tenant_connect=_tenant_connect, allow_database_drop=True,
        )

    assert report.dropped_database == names.database
    # All six, not three. This assertion pinned the three-role tuple that
    # `jobs._drop_roles` actually had, which is how the executor and client
    # leak survived from Phase 08 slice 2 and Phase 09 slice 2 -- every cleanup
    # left them on the cluster, and the test agreed. Phase 10 slice 1 fixed the
    # tuple while adding the storage role to it.
    expected = {
        names.authenticator, names.auth, names.admin,
        names.executor, names.client, names.storage,
    }
    assert set(report.dropped_roles) == expected
    assert not provisioning.database_exists(admin_conn, names.database)
    for role in expected:
        assert not provisioning.role_exists(admin_conn, role)


def test_a_full_reclaim_frees_the_placement_for_reuse(admin_conn, key_ring, project_factory):
    """`nodes.release_placement` refuses to forget a node while a database
    exists and points here instead. Once nothing is left on the node, the
    project must be placeable again -- otherwise its capacity is held forever."""
    project_id = project_factory("pj000010")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )

    with db.connection() as conn:
        jobs.cleanup(
            conn, admin_conn, project_id=project_id,
            tenant_connect=_tenant_connect, allow_database_drop=True,
        )
        row = db.one(
            conn,
            "SELECT status, node_id, database_name, retry_after FROM projects WHERE id = %s",
            (project_id,),
        )

    assert row["status"] == "REQUESTED"
    assert row["node_id"] is None, "the node still counts this project against its capacity"
    assert row["database_name"] is None
    assert row["retry_after"] is None


@requires_maludb_core
def test_a_cleaned_up_project_can_be_provisioned_again(admin_conn, key_ring, project_factory):
    """Otherwise cleanup is a trap: it reclaims everything, returns the project
    to REQUESTED, and leaves it permanently unprovisionable because the retry
    cap counted attempts that no longer correspond to anything on any node."""
    project_id = project_factory("pj000011")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    for _ in range(jobs.MAX_ATTEMPTS):
        with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
            jobs.provision(
                conn, admin_conn, project_id=project_id, key_ring=key_ring,
                platform_owner=PLATFORM_OWNER, tenant_connect=explode,
            )
    assert _status(project_id) == "FAILED"

    with db.connection() as conn:
        jobs.cleanup(
            conn, admin_conn, project_id=project_id,
            tenant_connect=_tenant_connect, allow_database_drop=True,
        )
        # Placement is normally re-reserved here; the test node is the only one.
        db.execute(
            conn,
            "UPDATE projects SET node_id = (SELECT node_id FROM projects WHERE id <> %s "
            "AND node_id IS NOT NULL LIMIT 1), status = 'PLACEMENT_RESERVED' WHERE id = %s",
            (project_id, project_id),
        )
        conn.commit()

    _run(project_id, admin_conn, key_ring)
    assert _status(project_id) == "PROVISIONED"


def test_cleanup_refuses_while_a_provisioning_run_is_open(admin_conn, key_ring, project_factory):
    """An operator cleaning up while a retry worker is mid-flight would drop the
    database out from under it."""
    project_id = project_factory("pj000012")
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO provisioning_jobs (id, project_id, state, attempt) "
            "VALUES (%s, %s, 'BOOTSTRAPPING', 1)",
            (uuid.uuid4(), project_id),
        )
        db.execute(conn, "UPDATE projects SET status = 'RETRY_WAIT' WHERE id = %s", (project_id,))
        conn.commit()
        with pytest.raises(provisioning.ProvisioningError, match="in progress"):
            jobs.cleanup(
                conn, admin_conn, project_id=project_id,
                tenant_connect=_tenant_connect, allow_database_drop=True,
            )


def test_cleanup_refuses_a_database_name_that_is_not_this_projects(admin_conn, key_ring, project_factory):
    """Defence in depth on the one operation that destroys data. The name is
    quoted, so this is not injection -- it is dropping the wrong tenant."""
    project_id = project_factory("pj000013")
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE projects SET status = 'FAILED', database_name = 'mldb_someone_else' WHERE id = %s",
            (project_id,),
        )
        conn.commit()
        with pytest.raises(provisioning.ProvisioningError, match="does not match"):
            jobs.cleanup(
                conn, admin_conn, project_id=project_id,
                tenant_connect=_tenant_connect, allow_database_drop=True,
            )


def test_cleanup_never_drops_the_shared_roles(admin_conn, key_ring, project_factory):
    """They are cluster-wide and belong to every other tenant on the node."""
    project_id = project_factory("pj00000e")

    def explode(database: str):
        raise psycopg.OperationalError("nope")

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError):
        jobs.provision(
            conn, admin_conn, project_id=project_id, key_ring=key_ring,
            platform_owner=PLATFORM_OWNER, tenant_connect=explode,
        )
    with db.connection() as conn:
        jobs.cleanup(
            conn, admin_conn, project_id=project_id,
            tenant_connect=_tenant_connect, allow_database_drop=True,
        )

    for role in provisioning.SHARED_ROLES:
        assert provisioning.role_exists(admin_conn, role), f"{role} was dropped"


@requires_maludb_core
def test_cleanup_refuses_a_provisioned_project_outright(admin_conn, key_ring, project_factory):
    project_id = project_factory("pj00000f")
    _run(project_id, admin_conn, key_ring)

    with db.connection() as conn, pytest.raises(provisioning.ProvisioningError, match="refusing to clean up"):
        jobs.cleanup(
            conn, admin_conn, project_id=project_id,
            tenant_connect=_tenant_connect, allow_database_drop=True,
        )
