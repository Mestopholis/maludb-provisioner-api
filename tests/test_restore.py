"""Restore one tenant, and leave every other tenant serving.

Phase 11 slice 2. Closes the phase's first acceptance criterion, and the
assertion that closes it is not "the restore returned 0" — it is that the
recovered database holds the data as it was at the target time, that the
*other* tenant databases on the node answered throughout, and that the live
database was not touched.

Three kinds of test, on `tests/test_backup.py`'s split.

**Guards**, which need nothing. The two that matter both protect against a
command that destroys a node: `pg_dropcluster` on a cluster this module did not
create, and a restore that writes over the database a project is serving from.
A guard nothing ever exercises is not a guard, which is why `ScratchCluster`
takes path roots.

**Bookkeeping and activation**, which need the control-plane database. The
question is whether activation refuses what it must refuse — in particular a
restore whose schema ownership did not verify, which is the ADR-059 gate.

**A real restore**, which needs the throwaway cluster. A tenant is provisioned
through the real provisioning path, a marker is written before a PITR target and
another after it, a backup is taken, and the tenant is recovered. Only the first
marker may come back: a restore that merely *completed* would show both, and
that is the difference between recovering data and copying it.
"""

from __future__ import annotations

import ast
import subprocess  # noqa: S404 - asserting file modes on the node
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from services.control_plane import (
    backup,
    db,
    identity,
    provisioning,
    restore,
    tenant_bootstrap,
)
from tests.conftest import (
    BACKUP_NODE_DSN,
    BACKUP_STANZA,
    TEST_CREDENTIAL,
    requires_backup_node,
    requires_db,
)


def _names(ref: str) -> provisioning.TenantNames:
    return provisioning.TenantNames.for_ref(ref)


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", "Main", "main-cluster", "9lives", "../../etc", "a b", "x" * 33]
)
def test_a_cluster_name_that_is_not_one_is_refused(name):
    """This value reaches `pg_dropcluster`, which destroys a cluster and everything on it."""
    with pytest.raises(restore.RestoreError):
        restore.checked_cluster(name)


def test_ordinary_cluster_names_are_accepted():
    assert restore.checked_cluster("mldbrestore") == "mldbrestore"
    assert restore.checked_pg_version("17") == "17"


def test_an_unmarked_cluster_is_never_dropped(tmp_path):
    """The guard that stands between this module and a live node.

    A name cannot check itself, so creation writes a marker into the *config*
    directory — pgBackRest rewrites the data directory during a restore, so a
    marker there would be gone at exactly the moment it is needed — and nothing
    is dropped without it.
    """
    cluster = restore.ScratchCluster(
        version="17", name="pretendlive", port=5999, run_as="postgres",
        data_root=str(tmp_path / "data"), config_root=str(tmp_path / "etc"),
    )
    (tmp_path / "etc" / "17" / "pretendlive").mkdir(parents=True)

    assert cluster.is_ours is False
    with pytest.raises(restore.RestoreError, match="no MaluDB scratch marker"):
        restore.drop_scratch_cluster(cluster)


def test_a_marked_cluster_is_recognised_as_ours(tmp_path):
    cluster = restore.ScratchCluster(
        version="17", name="scratch", port=5999, run_as="postgres",
        data_root=str(tmp_path / "data"), config_root=str(tmp_path / "etc"),
    )
    config_dir = tmp_path / "etc" / "17" / "scratch"
    config_dir.mkdir(parents=True)
    (config_dir / restore.SCRATCH_MARKER).write_text("")
    assert cluster.is_ours is True


def test_dropping_a_cluster_that_does_not_exist_is_not_an_error_when_asked(tmp_path):
    cluster = restore.ScratchCluster(
        version="17", name="absent", port=5999, run_as="postgres",
        data_root=str(tmp_path / "data"), config_root=str(tmp_path / "etc"),
    )
    restore.drop_scratch_cluster(cluster, missing_ok=True)
    with pytest.raises(restore.RestoreError):
        restore.drop_scratch_cluster(cluster)


def test_a_restored_database_is_named_apart_from_the_live_one():
    names = _names("rst00001")
    at = datetime(2026, 8, 27, 9, 15, 0, tzinfo=UTC)
    target = restore.restored_database_name(names, at)
    assert target == f"{names.database}_restore_20260827091500"
    assert target != names.database


def test_a_restored_name_that_would_not_fit_is_refused():
    """PostgreSQL truncates identifiers at 63 bytes, silently.

    A truncated name could collide with another restore's, so this refuses
    rather than producing two databases that are the same one.
    """
    names = _names("rst00001")
    long_names = provisioning.TenantNames(
        project_ref="x", database="m" * 55, authenticator="a", auth="b", admin="c",
        executor="d", client="e", replicator="f", storage="g",
    )
    assert restore.restored_database_name(names, datetime.now(UTC))
    with pytest.raises(restore.RestoreError, match="63 bytes"):
        restore.restored_database_name(long_names, datetime.now(UTC))


def test_a_restore_target_is_not_iso_8601():
    """pgBackRest rejects the `T` separator, and its error names neither it nor the field.

    Slice 0's harness never hit this because it passed PostgreSQL's `now()::text`
    through, which is already in the accepted shape.
    """
    at = datetime(2026, 8, 27, 9, 15, 30, 123456, tzinfo=UTC)
    formatted = restore.pgbackrest_time(at)
    assert formatted == "2026-08-27 09:15:30.123456+0000"
    assert "T" not in formatted


def test_a_naive_restore_target_is_refused():
    """A naive timestamp is read as the *node's* local time.

    On a node in another zone that silently selects a different moment in a
    customer's history -- an hour of their data, restored or not, on the
    strength of `/etc/localtime`.
    """
    with pytest.raises(restore.RestoreError, match="timezone"):
        restore.pgbackrest_time(datetime(2026, 8, 27, 9, 15, 30))


def test_only_the_two_tenant_owned_schemas_are_checked():
    """`maludb_platform` and `public` keep their owners across a restore.

    Including them would report a difference that is not one; excluding the two
    that *do* change hands would report nothing at all.
    """
    names = _names("rst00001")
    expected = restore.expected_schema_owners(names)
    assert expected == {"auth": names.auth, "storage": names.storage}


def test_a_database_name_the_platform_did_not_generate_is_refused():
    assert restore.tenant_ref_of("mldb_rst00001") == "rst00001"
    with pytest.raises(restore.RestoreError):
        restore.tenant_ref_of("postgres")


# --------------------------------------------------------------------------
# The ownership report — slice 0's finding, made checkable
# --------------------------------------------------------------------------


def test_ownership_verifies_when_the_per_tenant_roles_own_their_schemas():
    names = _names("rst00001")
    report = restore.OwnershipReport(
        database="mldb_rst00001_restore_1",
        expected=restore.expected_schema_owners(names),
        observed={"auth": names.auth, "storage": names.storage},
    )
    assert report.verified
    assert report.wrong == {}


def test_ownership_fails_when_a_schema_fell_back_to_the_superuser():
    """The exact shape slice 0 measured: data intact, owners silently changed.

    All 164 RLS policies and every row arrive. `pg_restore` exits 1 with
    "errors ignored". ADR-059 puts the `storage` schema under a per-tenant role
    so it is *not* owned by something with superuser reach — a tenant restored
    this way has a different security posture from the one backed up, and
    nothing about the database says so.
    """
    names = _names("rst00001")
    report = restore.OwnershipReport(
        database="mldb_rst00001_restore_1",
        expected=restore.expected_schema_owners(names),
        observed={"auth": "postgres", "storage": "postgres"},
    )
    assert not report.verified
    assert report.wrong["auth"] == (names.auth, "postgres")
    assert report.wrong["storage"] == (names.storage, "postgres")
    assert "owned by postgres" in report.detail


def test_absent_roles_are_reported_even_when_owners_look_right():
    names = _names("rst00001")
    report = restore.OwnershipReport(
        database="mldb_rst00001_restore_1",
        expected=restore.expected_schema_owners(names),
        observed={"auth": names.auth, "storage": names.storage},
        missing_roles=(names.admin,),
    )
    assert not report.verified
    assert "roles absent" in report.detail


# --------------------------------------------------------------------------
# Bookkeeping and activation
# --------------------------------------------------------------------------


@pytest.fixture
def restorable(db_pool):
    """A project on a node with a stanza, and nothing provisioned anywhere."""
    ref = "rst00001"
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
        )
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, backup_stanza) "
            "VALUES ('rst-node','rst.example','rst.internal','shared','active','maludb-bk') "
            "ON CONFLICT (name) DO UPDATE SET backup_stanza = 'maludb-bk' RETURNING id",
        )["id"]
        plan = db.one(
            conn,
            "INSERT INTO plans (code,name) VALUES ('rst-plan','Restore') "
            "ON CONFLICT (code) DO UPDATE SET name='Restore' RETURNING id",
        )["id"]
        project_id = uuid.uuid4()
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
            "node_id, database_name) VALUES (%s,%s,%s,%s,%s,'PROVISIONED',%s,%s)",
            (project_id, org, ref, ref, plan, node_id, _names(ref).database),
        )
        conn.commit()
    return {"project_id": project_id, "node_id": node_id, "ref": ref}


def _record(restorable, *, status="complete", verified=True, database="mldb_rst00001_restore_1"):
    with db.connection() as conn:
        row = db.one(
            conn,
            "INSERT INTO tenant_restores (project_id, node_id, stanza, restored_database, "
            "                             status, ownership_verified, ownership_detail, "
            "                             finished_at) "
            "VALUES (%s,%s,'maludb-bk',%s,%s,%s,%s, "
            "        CASE WHEN %s = 'running' THEN NULL ELSE now() END) RETURNING id",
            (
                restorable["project_id"], restorable["node_id"], database, status, verified,
                "ok" if verified else "storage is owned by postgres, expected mldb_rst00001_storage",
                status,
            ),
        )
        conn.commit()
        return row["id"]


@requires_db
def test_a_complete_restore_must_name_a_database(restorable):
    """Schema-level. A restore that succeeded produced a database; one that did not, did not."""
    with db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            conn,
            "INSERT INTO tenant_restores (project_id, node_id, stanza, status, finished_at) "
            "VALUES (%s,%s,'maludb-bk','complete', now())",
            (restorable["project_id"], restorable["node_id"]),
        )


@requires_db
def test_a_running_restore_cannot_have_finished(restorable):
    with db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            conn,
            "INSERT INTO tenant_restores (project_id, node_id, stanza, status, finished_at) "
            "VALUES (%s,%s,'maludb-bk','running', now())",
            (restorable["project_id"], restorable["node_id"]),
        )


@requires_db
def test_activation_refuses_a_restore_whose_ownership_did_not_verify(restorable):
    """The ADR-059 gate, and the reason this slice records ownership at all.

    A restored tenant whose `auth` and `storage` schemas came back owned by the
    platform superuser is not the tenant that was backed up. Activating it would
    put that in front of customers with nothing having said so.
    """
    _record(restorable, verified=False)
    with db.connection() as conn, pytest.raises(restore.RestoreError, match="ADR-059"):
        restore.activate(
            conn, conn, project_id=restorable["project_id"], project_ref=restorable["ref"]
        )


@requires_db
def test_activation_refuses_a_restore_that_never_finished(restorable):
    _record(restorable, status="running", database=None)
    with db.connection() as conn, pytest.raises(restore.RestoreError, match="not complete"):
        restore.activate(
            conn, conn, project_id=restorable["project_id"], project_ref=restorable["ref"]
        )


@requires_db
def test_activation_refuses_when_there_is_nothing_to_activate(restorable):
    with db.connection() as conn, pytest.raises(restore.RestoreError, match="no restore on record"):
        restore.activate(
            conn, conn, project_id=restorable["project_id"], project_ref=restorable["ref"]
        )


@requires_db
def test_history_reports_what_happened(restorable):
    _record(restorable, verified=False)
    with db.connection() as conn:
        rows = restore.history(conn, project_id=restorable["project_id"])
    assert len(rows) == 1
    assert rows[0]["project_ref"] == restorable["ref"]
    assert rows[0]["ownership_verified"] is False


# --------------------------------------------------------------------------
# What the security review changed
# --------------------------------------------------------------------------



def _read_as_postgres(path: str) -> str:
    return subprocess.run(  # noqa: S603
        ["sudo", "-n", "-u", "postgres", "cat", path],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout


def test_nothing_in_this_module_reaches_a_shell():
    """Every command here runs under sudo and several of them destroy things.

    `ScratchCluster` carries path roots so the guards can be tested, and a field
    a test can set is a field. An argv that never reaches a shell cannot be made
    to mean something else by one.

    Checked against the parsed module rather than by grepping the text, because
    the first version of this test failed on the docstring that explains the
    rule -- which is the shape of assertion that gets deleted rather than fixed.
    """
    tree = ast.parse(Path(restore.__file__).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            assert kw.arg != "shell", f"shell= passed at line {node.lineno}"
        for arg in node.args:
            if isinstance(arg, ast.List):
                literals = [
                    e.value for e in arg.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                assert not ({"bash", "sh", "/bin/sh"} & set(literals)), (
                    f"a shell is invoked at line {node.lineno}"
                )


@requires_backup_node
def test_a_dump_is_not_readable_by_other_accounts_on_the_node(bk_admin):
    """A dump is the customer's whole database in the clear on a shared filesystem.

    `/var/lib/postgresql` is world-executable on a Debian layout and `pg_dump`
    honours the process umask, so a dump written straight into it is readable by
    any local account. Short-lived and deleted afterwards is not a permission.
    """
    path = restore.prepare_dump_dir(run_as="postgres")
    mode = subprocess.run(  # noqa: S603
        ["stat", "-c", "%a %U", path], capture_output=True, text=True, check=True,  # noqa: S607
    ).stdout.strip()
    assert mode == "700 postgres", f"{path} is {mode}"


@requires_backup_node
def test_the_scratch_cluster_carries_the_adr_031_reject(bk_admin):
    """A restored copy must not have a weaker posture than the original.

    `pg_hba.conf` lives in /etc, so pgBackRest's restore of the *data* directory
    does not bring it across: a fresh scratch cluster carries pg_createcluster's
    defaults and not the platform's reject. For the minutes it is up it holds a
    byte-level copy of every tenant on the node — which is precisely the kind of
    silent posture change this whole slice exists to catch.
    """
    cluster = restore.create_scratch_cluster(
        bk_admin, name="mldbhbatest", port=5442, run_as="postgres"
    )
    try:
        # Read as the owner. /etc/postgresql/<v>/<cluster> is postgres's, and a
        # root without CAP_DAC_OVERRIDE is not a way round that either.
        hba = _read_as_postgres(f"{cluster.config_dir}/pg_hba.conf")
        assert "host    replication     all     127.0.0.1/32    reject" in hba
        conf = _read_as_postgres(f"{cluster.config_dir}/postgresql.conf")
        # And the other half: a promoted copy must not push a new timeline into
        # the repository it was restored from.
        assert "archive_mode = off" in conf
    finally:
        restore.drop_scratch_cluster(cluster)



# --------------------------------------------------------------------------
# A real tenant, a real backup, a real restore
# --------------------------------------------------------------------------


REAL_REF = "rst00009"
NEIGHBOUR_REF = "rst00010"


@pytest.fixture
def bk_admin():
    conn = psycopg.connect(BACKUP_NODE_DSN, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _tenant_conn(admin_conn, database: str) -> psycopg.Connection:
    info = psycopg.conninfo.conninfo_to_dict(BACKUP_NODE_DSN)
    info["dbname"] = database
    return psycopg.connect(**info)



def _drop_tenant(admin_conn, ref: str) -> None:
    """Remove a tenant and everything a previous run left of it.

    Drop first, then create -- the rule `scripts/backup-test-cluster.sh` follows
    and for its reason. These databases live on the backup cluster, which the
    control-plane fixture's TRUNCATE does not reach, so without this the marker
    table accumulates rows across runs and the central assertion of this file
    starts passing or failing for reasons that have nothing to do with the
    change under test. It first showed as five markers where there should have
    been one.
    """
    names = _names(ref)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database WHERE datname = %s OR datname LIKE %s",
            (names.database, f"{names.database}\_%"),
        )
        for (datname,) in cur.fetchall():
            cur.execute(
                psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(datname)
                )
            )
        for role in (names.authenticator, names.auth, names.admin, names.executor,
                     names.client, names.replicator, names.storage):
            cur.execute(
                psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role))
            )


def _provision(admin_conn, ref: str) -> provisioning.TenantNames:
    """A tenant on the backup cluster, through the real provisioning path."""
    _drop_tenant(admin_conn, ref)
    names = _names(ref)
    passwords = {
        k: provisioning.generate_password()
        for k in ("authenticator", "auth", "admin", "executor", "client", "storage")
    }
    with psycopg.connect(BACKUP_NODE_DSN) as conn:
        provisioning.ensure_shared_roles(conn)
        provisioning.create_roles(
            conn, names, passwords=passwords,
            connection_limits={"authenticator": 20, "auth": 10},
        )
        provisioning.create_executor_role(conn, names, password=passwords["executor"])
        provisioning.create_client_role(conn, names, password=passwords["client"])
        provisioning.create_storage_role(conn, names, password=passwords["storage"])
        conn.commit()
        provisioning.create_database(conn, names, owner="postgres")
        provisioning.lock_down_database(conn, names)
        provisioning.grant_executor_connect(conn, names)
        provisioning.grant_client_connect(conn, names)
        provisioning.grant_storage_connect(conn, names)
        conn.commit()
    with _tenant_conn(admin_conn, names.database) as tconn:
        provisioning.install_extension(tconn)
        tenant_bootstrap.apply(tconn)
        tconn.commit()
    return names


@requires_backup_node
@requires_db
def test_a_tenant_is_recovered_to_a_point_in_time_while_its_neighbours_keep_serving(
    db_pool, bk_admin
):
    """The phase's first acceptance criterion, end to end.

    Two tenants are provisioned on the node. One writes a marker, a PITR target
    is taken, and it writes a second marker. A backup is taken, then the first
    tenant is restored to the target.

    Three assertions, and the first is the one that separates a restore from a
    copy: **only the marker written before the target may come back.** A restore
    that merely completed would show both.
    """
    names = _provision(bk_admin, REAL_REF)
    neighbour = _provision(bk_admin, NEIGHBOUR_REF)

    with _tenant_conn(bk_admin, names.database) as tconn:
        tconn.execute(
            "CREATE TABLE IF NOT EXISTS public.restore_marker (id serial primary key, note text)"
        )
        tconn.execute("INSERT INTO public.restore_marker (note) VALUES ('before-target')")
        tconn.commit()

    # A backup that contains the first marker. Taken before the target so the
    # restore has a base to start from and WAL to replay onto it.
    with bk_admin.cursor() as cur:
        cur.execute("SELECT now()")
        pre_backup = cur.fetchone()[0]
    assert pre_backup is not None

    with db.connection() as conn:
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, backup_stanza) "
            "VALUES ('rst-real','rr.example','rr.internal','shared','active',%s) "
            "ON CONFLICT (name) DO UPDATE SET backup_stanza = EXCLUDED.backup_stanza RETURNING id",
            (BACKUP_STANZA,),
        )["id"]
        conn.commit()
        run = backup.run_backup(
            conn, node_id=node_id, node_name="rst-real", stanza=BACKUP_STANZA,
            backup_type="full", process_max=2,
        )
    assert run.ok, run.error

    # The target, and then a write after it that must NOT come back.
    time.sleep(1)
    with bk_admin.cursor() as cur:
        cur.execute("SELECT now()")
        target_time = cur.fetchone()[0]
    time.sleep(2)
    with _tenant_conn(bk_admin, names.database) as tconn:
        tconn.execute("INSERT INTO public.restore_marker (note) VALUES ('after-target')")
        tconn.commit()
    with bk_admin.cursor() as cur:
        cur.execute("SELECT pg_switch_wal()")
    time.sleep(2)

    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{REAL_REF}@example.com", password=TEST_CREDENTIAL
        )
        plan = db.one(
            conn,
            "INSERT INTO plans (code,name) VALUES ('rst-real-plan','R') "
            "ON CONFLICT (code) DO UPDATE SET name='R' RETURNING id",
        )["id"]
        project_id = uuid.uuid4()
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
            "node_id, database_name) VALUES (%s,%s,%s,%s,%s,'PROVISIONED',%s,%s)",
            (project_id, org, REAL_REF, REAL_REF, plan, node_id, names.database),
        )
        conn.commit()

        outcome = restore.restore_tenant(
            conn,
            bk_admin,
            project_id=project_id,
            project_ref=REAL_REF,
            node_id=node_id,
            stanza=BACKUP_STANZA,
            target_time=target_time,
            scratch_name="mldbrsttest",
            scratch_port=5441,
        )

    assert outcome.ok, f"{outcome.error} {outcome.notes}"
    assert outcome.restored_database and outcome.restored_database != names.database

    # 1. The restore went back in time rather than merely completing.
    with _tenant_conn(bk_admin, outcome.restored_database) as rconn, rconn.cursor() as cur:
        cur.execute("SELECT note FROM public.restore_marker ORDER BY id")
        recovered = [row[0] for row in cur.fetchall()]
    assert recovered == ["before-target"], (
        f"expected only the pre-target marker, got {recovered}. Both present means the copy "
        "completed without going back to the target"
    )

    # 2. The schemas came back owned by their per-tenant roles (ADR-059).
    assert outcome.ownership is not None
    assert outcome.ownership.verified, outcome.ownership.detail

    # 3. The live database was not touched, and the neighbour kept serving.
    with _tenant_conn(bk_admin, names.database) as lconn, lconn.cursor() as cur:
        cur.execute("SELECT note FROM public.restore_marker ORDER BY id")
        live = [row[0] for row in cur.fetchall()]
    assert live == ["before-target", "after-target"], (
        "the live database lost data to a restore that was supposed to leave it alone"
    )
    with _tenant_conn(bk_admin, neighbour.database) as nconn, nconn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
    assert outcome.neighbours_available >= 1


@requires_backup_node
@requires_db
def test_a_load_is_refused_when_the_target_cluster_lacks_the_tenants_roles(db_pool, bk_admin):
    """Fail before the load, not diagnose after it.

    Slice 0 measured what happens otherwise: `pg_restore` carries on past eleven
    "role does not exist" errors, leaves 268,000 rows and 164 policies in place,
    exits 1, and hands back a database whose `auth` and `storage` schemas are
    owned by the superuser.
    """
    absent = _names("rst99999")
    with pytest.raises(restore.RestoreError, match="missing this tenant's roles"):
        restore.load_into_target(
            bk_admin, absent, dump_path="/nonexistent.dump",
            target_database="mldb_rst99999_restore_1", owner="postgres",
        )


@requires_backup_node
@requires_db
def test_a_restore_refuses_to_write_over_the_live_database(db_pool, bk_admin):
    """There is no code path here that overwrites a tenant's database."""
    names = _names(REAL_REF)
    with pytest.raises(restore.RestoreError, match="refusing to restore over the live database"):
        restore.load_into_target(
            bk_admin, names, dump_path="/nonexistent.dump",
            target_database=names.database, owner="postgres",
        )


@requires_backup_node
def test_a_scratch_cluster_cannot_be_built_over_the_live_data_directory(bk_admin):
    """The other half of the drop guard, from the creation end.

    Read from the node rather than assumed, because the value that matters is
    where the postmaster is actually running, not where configuration says.
    """
    with bk_admin.cursor() as cur:
        cur.execute("SHOW data_directory")
        live = cur.fetchone()[0]
    # Name the scratch cluster so that its derived data directory *is* the live
    # one, which is the mistake the check exists for.
    parts = live.rstrip("/").split("/")
    with pytest.raises(restore.RestoreError, match="refusing"):
        restore.create_scratch_cluster(
            bk_admin, version=parts[-2], name=parts[-1],
            port=5999, run_as="postgres",
        )


@requires_backup_node
def test_the_disk_precondition_reports_rather_than_guesses(bk_admin):
    """Slice 0 named disk as the real constraint, not time.

    A scratch restore holds a second copy of the cluster, so free disk is a
    restore prerequisite and not only a placement one.
    """
    verdict = restore.check_disk_headroom(bk_admin)
    # On a development box with room this is None; the point is that it answers
    # from measured sizes rather than from a fixed floor.
    assert verdict is None or "needs roughly" in verdict
