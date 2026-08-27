"""Node backup: preconditions, metadata, and the verification pass.

Phase 11 slice 1. Three kinds of test here, and the split is deliberate.

**Readiness logic**, which needs no cluster. `BackupReadiness.failures` is where
ADR-064's severity split lives -- co-location refuses in production and warns
everywhere else -- and where the two quiet failures from ADR-067 are turned into
sentences. Testing it as a pure function of what was observed is why
`production` is a field rather than something read from the environment inside
the property.

**Metadata and the pass**, which need the control-plane database and no node.
The thing under test is that silence reads as failure: a node with no stanza, a
node that has never been backed up, and a backup that has been `running` since
Tuesday all have to come out as problems. A pass that only reported *errors*
would report nothing at all in the exact case the slice exists for.

**A real backup**, which needs the throwaway cluster. This is the only place the
central claim is actually checked rather than asserted: that pgBackRest
completes a full backup of a cluster carrying ADR-031's `host replication all
<cidr> reject`, and does it with **zero walsenders**. That reject exists because
a non-superuser holding REPLICATION took a 484 MB copy of every database on a
cluster. If backup needed it narrowed, this slice would be a security
regression rather than a feature.
"""

from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest

from services.control_plane import backup, db, maintenance
from tests.conftest import (
    BACKUP_NODE_DSN,
    BACKUP_STANZA,
    requires_backup_node,
    requires_db,
)

# --------------------------------------------------------------------------
# Readiness, as a pure function of what was observed
# --------------------------------------------------------------------------


def _repo(**overrides) -> backup.RepositoryState:
    """A repository that is fine, so a test can break exactly one thing."""
    base = backup.RepositoryState(
        reachable=True,
        detail="3 backup(s) in the repository",
        check_ok=True,
        check_detail="ok",
        pg_path="/var/lib/postgresql/17/main",
        repo_path="/srv/backups/maludb",
        retention_full=2,
        retention_archive=2,
        backup_labels=("20260826-120000F",),
    )
    return replace(base, **overrides)


def _readiness(**overrides) -> backup.BackupReadiness:
    """A node that is ready, so a test can break exactly one thing."""
    base = backup.BackupReadiness(
        wal_level="replica",
        archive_mode="on",
        archive_command="pgbackrest --stanza=maludb-n1 archive-push %p",
        archive_timeout_s=60,
        archive_failed_count=0,
        archive_last_failed_wal=None,
        archive_last_archived_wal="000000010000000000000003",
        repository=_repo(),
        production=False,
        stanza="maludb-n1",
    )
    return replace(base, **overrides)


def test_a_healthy_node_is_ready():
    assert _readiness().ready
    assert _readiness().failures == []


def test_archive_mode_off_is_the_first_failure_named():
    """Ordered by fix cost, following realtime.NodeReadiness.

    `archive_mode` is postmaster context: fixing it restarts the cluster and
    takes every tenant on the node down. A node that is going to fail on that
    should fail on it before anybody edits a repository path.
    """
    failures = _readiness(archive_mode="off").failures
    assert failures, "archive_mode off must not be ready"
    assert "archive_mode" in failures[0]
    assert "restarts the cluster" in failures[0]


def test_wal_level_minimal_is_a_failure():
    assert any("wal_level" in f for f in _readiness(wal_level="minimal").failures)


def test_wal_level_logical_is_sufficient():
    """A node prepared for Realtime is already above the floor (ADR-031)."""
    assert _readiness(wal_level="logical").ready


def test_an_archiver_that_has_never_archived_is_a_failure():
    """The failure that is invisible from the cluster.

    WAL that cannot be shipped does not stop the postmaster. It accumulates in
    pg_wal while every tenant is served normally, and the node has no
    recoverable point in time after the break.
    """
    failures = _readiness(
        archive_failed_count=41,
        archive_last_failed_wal="000000010000000000000009",
        archive_last_archived_wal=None,
    ).failures
    assert any("archiver has failed" in f and "no backup taken here is restorable" in f
               for f in failures)


def test_an_archiver_that_failed_but_recovered_is_a_warning_not_a_failure():
    """Past failures on a working archiver are history, not a current fault."""
    readiness = _readiness(
        archive_failed_count=3,
        archive_last_failed_wal="000000010000000000000002",
        archive_last_archived_wal="000000010000000000000009",
    )
    assert readiness.ready
    assert any("3 failures on record" in w for w in readiness.warnings)


def test_unset_retention_fails_both_halves_separately():
    """ADR-067: both, or expiry is half-done.

    Without `repo1-retention-full` the repository grows without bound; without
    `repo1-retention-archive` WAL outlives every backup it belongs to. pgBackRest
    warns about each on every run and neither is fatal, which is the problem.
    """
    failures = _readiness(repository=_repo(retention_full=None, retention_archive=None)).failures
    assert any("repo1-retention-full" in f for f in failures)
    assert any("repo1-retention-archive" in f for f in failures)


def test_a_failing_pgbackrest_check_is_a_failure():
    failures = _readiness(
        repository=_repo(check_ok=False, check_detail="unable to find primary cluster")
    ).failures
    assert any("pgbackrest check` failed" in f for f in failures)


def test_archive_timeout_zero_is_a_warning():
    """Not disqualifying, and worth saying.

    ADR-022 makes "nobody is writing to this cluster" the free tier's normal
    state, and with no timeout no segment is closed, so the recoverable point in
    time stops advancing.
    """
    readiness = _readiness(archive_timeout_s=0)
    assert readiness.ready
    assert any("archive_timeout is 0" in w for w in readiness.warnings)


# --------------------------------------------------------------------------
# ADR-064: the failure-domain rule, and its ratified severity split
# --------------------------------------------------------------------------


def test_co_located_repository_refuses_in_production():
    """ADR-064. A repository in the same failure domain as the data is not a backup."""
    readiness = _readiness(
        production=True,
        repository=_repo(
            pg_path="/var/lib/postgresql/17/main", repo_path="/var/lib/postgresql/17/main/backups"
        ),
    )
    assert readiness.repository_is_co_located is True
    assert not readiness.ready
    assert any("ADR-064" in f and "takes the backups with it" in f for f in readiness.failures)


def test_co_located_repository_only_warns_outside_production():
    """The slice-0 measurement cluster puts the repository beside the data on purpose.

    That fixture has to keep working, and an operator on a development box
    should still be told. Same sentence, different severity.
    """
    readiness = _readiness(
        production=False,
        repository=_repo(
            pg_path="/var/lib/postgresql/17/main", repo_path="/var/lib/postgresql/17/main/backups"
        ),
    )
    assert readiness.repository_is_co_located is True
    assert readiness.ready
    assert any("ADR-064" in w for w in readiness.warnings)
    assert not any("ADR-064" in f for f in readiness.failures)


def test_a_repository_behind_a_uri_is_not_judged_co_located():
    """An S3 endpoint is not a path this process can stat.

    Returning True for it would refuse every remote repository, which is the one
    arrangement ADR-064 is asking for.
    """
    readiness = _readiness(
        production=True, repository=_repo(repo_path="s3://maludb-backups/node1")
    )
    assert readiness.repository_is_co_located is False
    assert readiness.ready


def test_an_unexamined_repository_is_reported_as_unexamined():
    """`reachable=False` is not `healthy`, and it is not `broken` either.

    pgBackRest runs on the node; a control plane elsewhere cannot see the
    repository. Reporting that as a pass is the failure this module exists to
    prevent, and reporting it as a fault would make every non-co-located control
    plane refuse its own nodes.
    """
    readiness = _readiness(
        repository=backup.RepositoryState(reachable=False, detail="pgbackrest is not on PATH")
    )
    assert readiness.repository_is_co_located is None
    # Not disqualifying...
    assert readiness.ready
    # ...but never silent about what was skipped.
    assert any("NOT evaluated" in w and "unexamined one" in w for w in readiness.warnings)


def test_as_capacity_records_unprepared_as_unprepared():
    unready = _readiness(archive_mode="off").as_capacity()
    assert unready["backup_ready"] is False
    assert _readiness().as_capacity()["backup_ready"] is True


# --------------------------------------------------------------------------
# Metadata and the verification pass
# --------------------------------------------------------------------------


@pytest.fixture
def bk_node(db_pool) -> int:
    with db.connection() as conn:
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status) "
            "VALUES ('bk-node','bk.example','bk.internal','shared','active') "
            "ON CONFLICT (name) DO UPDATE SET status='active' RETURNING id",
        )["id"]
        db.execute(conn, "DELETE FROM node_backups WHERE node_id = %s", (node_id,))
        db.execute(
            conn,
            "UPDATE nodes SET backup_stanza = NULL, backup_max_age_hours = NULL WHERE id = %s",
            (node_id,),
        )
        conn.commit()
    return node_id


def _set_stanza(node_id: int, stanza: str | None = "maludb-bk") -> None:
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET backup_stanza = %s WHERE id = %s", (stanza, node_id))
        conn.commit()


def _insert_backup(node_id: int, *, age_hours: float, status: str, backup_type: str = "full",
                   label: str | None = "20260826-120000F", error: str | None = None) -> int:
    with db.connection() as conn:
        row = db.one(
            conn,
            "INSERT INTO node_backups (node_id, stanza, backup_type, label, started_at, "
            "                          finished_at, status, error) "
            "VALUES (%s,'maludb-bk',%s,%s, now() - make_interval(mins => %s), "
            "        CASE WHEN %s = 'running' THEN NULL ELSE now() - make_interval(mins => %s) END, "
            "        %s, %s) RETURNING id",
            (
                node_id, backup_type,
                None if status == "running" else label,
                int(age_hours * 60), status, int(age_hours * 60) - 1, status, error,
            ),
        )
        conn.commit()
        return row["id"]


@requires_db
def test_a_node_with_no_stanza_is_a_problem(bk_node):
    """The most important row in the report, so it is never filtered out.

    A query that only returned prepared nodes would answer "all healthy" on a
    platform with no backups at all.
    """
    status = db_conn_status(bk_node)
    assert status.stanza is None
    assert any("not prepared for backup" in p for p in status.problems)


def db_conn_status(node_id: int) -> backup.NodeBackupStatus:
    with db.connection() as conn:
        found = backup.node_status(conn, node_id=node_id)
    assert len(found) == 1
    return found[0]


@requires_db
def test_a_prepared_node_that_has_never_been_backed_up_is_a_problem(bk_node):
    _set_stanza(bk_node)
    status = db_conn_status(bk_node)
    assert any("no backup has ever been recorded" in p for p in status.problems)


@requires_db
def test_a_recent_complete_full_backup_is_healthy(bk_node):
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=2, status="complete")
    assert db_conn_status(bk_node).healthy


@requires_db
def test_a_backup_older_than_the_node_allows_is_a_problem(bk_node):
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=40, status="complete")
    assert any("over the" in p and "old" in p for p in db_conn_status(bk_node).problems)


@requires_db
def test_the_max_age_is_configuration_not_a_constant(bk_node):
    """AGENTS.md forbids hard-coding this. A node backed up weekly is legitimate."""
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=40, status="complete")
    with db.connection() as conn:
        db.execute(
            conn, "UPDATE nodes SET backup_max_age_hours = 168 WHERE id = %s", (bk_node,)
        )
        conn.commit()
    assert db_conn_status(bk_node).healthy


@requires_db
def test_a_backup_running_since_tuesday_is_a_problem(bk_node):
    """ADR-067's quiet failure, stated as a test.

    An untuned pgBackRest backup of an idle cluster waits for a checkpoint that
    PostgreSQL never schedules. It does not error and it does not exit. The only
    way the platform can see it is by ageing out a row that is still `running`.
    """
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=backup.STALE_RUNNING_HOURS + 3, status="running")
    problems = db_conn_status(bk_node).problems
    assert any("has been running for" in p and "--start-fast" in p for p in problems)


@requires_db
def test_a_backup_running_for_ten_minutes_is_not_yet_a_stale_one(bk_node):
    """In progress is in progress. It is still not evidence the last one worked.

    So the age check below it still applies -- which is why this node is not
    healthy either, but for the *previous* backup's absence rather than for this
    one's duration.
    """
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=0.2, status="running")
    problems = db_conn_status(bk_node).problems
    assert not any("has been running for" in p for p in problems)
    assert any("no completed *full* backup" in p for p in problems)


@requires_db
def test_a_failed_backup_reports_its_reason(bk_node):
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=1, status="failed", label=None,
                   error="unable to open missing file")
    assert any("unable to open missing file" in p for p in db_conn_status(bk_node).problems)


@requires_db
def test_a_diff_chain_with_no_full_is_a_problem(bk_node):
    """diff and incr restore only through the full they are rooted on."""
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=1, status="complete", backup_type="diff",
                   label="20260826-120000F_20260826-130000D")
    assert any("no completed *full* backup" in p for p in db_conn_status(bk_node).problems)


@requires_db
def test_start_backup_records_the_row_before_the_backup_runs(bk_node):
    """The ordering is the design, not an implementation detail.

    A table populated on success cannot tell "hung since Tuesday" from "never
    ran", and the next morning both look like nothing at all.
    """
    _set_stanza(bk_node)
    with db.connection() as conn:
        backup_id = backup.start_backup(
            conn, node_id=bk_node, stanza="maludb-bk", backup_type="full"
        )
        row = db.one(conn, "SELECT status, label, finished_at FROM node_backups WHERE id = %s",
                     (backup_id,))
    assert row["status"] == "running"
    assert row["label"] is None
    assert row["finished_at"] is None


@requires_db
def test_start_backup_refuses_an_unknown_type(bk_node):
    with db.connection() as conn, pytest.raises(backup.BackupError):
        backup.start_backup(conn, node_id=bk_node, stanza="maludb-bk", backup_type="mirror")


@requires_db
def test_a_complete_backup_cannot_be_recorded_without_a_label(bk_node):
    """Schema-level, because a completed backup with no label cannot be restored from.

    `pgbackrest restore --set=` takes the label. A row without one is a claim of
    success that nothing can act on, and the constraint is what stops a future
    caller writing one on the strength of an exit code.
    """
    with db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            conn,
            "INSERT INTO node_backups (node_id, stanza, backup_type, status, finished_at) "
            "VALUES (%s,'maludb-bk','full','complete', now())",
            (bk_node,),
        )


@requires_db
def test_a_running_backup_cannot_have_finished(bk_node):
    with db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            conn,
            "INSERT INTO node_backups (node_id, stanza, backup_type, status, finished_at) "
            "VALUES (%s,'maludb-bk','full','running', now())",
            (bk_node,),
        )


@requires_db
def test_the_maintenance_pass_counts_an_unbacked_node_as_failed(bk_node):
    """Failed, not merely noted.

    This repository's recurring failure mode is a green run that verified
    nothing. A node with no usable backup is not an observation.
    """
    _set_stanza(bk_node)
    with db.connection() as conn:
        result = maintenance.check_backups(conn)
    assert result.failed >= 1
    assert any("bk-node" in note for note in result.detail)


@requires_db
def test_the_maintenance_pass_is_quiet_about_a_healthy_node(bk_node):
    _set_stanza(bk_node)
    _insert_backup(bk_node, age_hours=2, status="complete")
    with db.connection() as conn:
        result = maintenance.check_backups(conn)
    assert not any("bk-node" in note for note in result.detail)


# --------------------------------------------------------------------------
# What reaches a subprocess argv
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stanza",
    [
        "",
        "-leading-dash",
        "has space",
        "semi;colon",
        "slash/path",
        "--repo1-path=/etc",
        "a" * 65,
    ],
)
def test_a_stanza_that_is_not_a_stanza_is_refused(stanza):
    """Validated before it reaches `--stanza=`, and before it reaches the node row.

    Not customer input today -- an operator sets it. It is checked anyway,
    because "not reachable yet" is the assumption that stops being true without
    anybody editing the file, and because `record_readiness` persists it.
    """
    with pytest.raises(backup.BackupError):
        backup._checked_stanza(stanza)


@pytest.mark.parametrize("user", ["", "-x", "root; rm -rf /", "a b", "u" * 33])
def test_a_run_as_user_that_is_not_a_user_is_refused(user):
    """This value reaches `sudo -u`, which is the one that would actually matter."""
    with pytest.raises(backup.BackupError):
        backup._checked_run_as(user)


def test_ordinary_names_are_accepted():
    assert backup._checked_stanza("maludb-bk") == "maludb-bk"
    assert backup._checked_run_as("postgres") == "postgres"


@requires_db
def test_start_backup_refuses_an_invalid_stanza(bk_node):
    with db.connection() as conn, pytest.raises(backup.BackupError):
        backup.start_backup(conn, node_id=bk_node, stanza="../../etc", backup_type="full")


def test_repository_credentials_are_not_carried_out_of_the_config_file(tmp_path):
    """pgbackrest.conf can hold S3 keys. Nothing here should be holding one.

    Filtered at the point of parsing rather than remembered about later: the
    dict feeds a dataclass an operator prints.
    """
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(
        "[global]\n"
        "repo1-path=/srv/backups\n"
        "repo1-s3-key=AKIAEXAMPLE\n"
        "repo1-s3-key-secret=super-secret-value\n"
        "[maludb-bk]\n"
        "pg1-path=/var/lib/postgresql/17/bk\n"
        "repo1-retention-full=2\n"
    )
    options = backup._read_stanza_options("maludb-bk", str(conf))
    assert options["repo1-path"] == "/srv/backups"
    assert options["repo1-retention-full"] == "2"
    assert "super-secret-value" not in repr(options)
    assert not any("s3" in key for key in options)


def test_the_stanza_section_wins_over_global(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(
        "[global]\nrepo1-retention-full=1\n[maludb-bk]\nrepo1-retention-full=7\n"
    )
    assert backup._read_stanza_options("maludb-bk", str(conf))["repo1-retention-full"] == "7"


def test_another_stanzas_options_are_not_read(tmp_path):
    """One node's retention is not another's."""
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(
        "[maludb-other]\nrepo1-retention-full=99\n[maludb-bk]\nrepo1-retention-full=2\n"
    )
    assert backup._read_stanza_options("maludb-bk", str(conf))["repo1-retention-full"] == "2"


# --------------------------------------------------------------------------
# A real cluster, a real repository, a real backup
# --------------------------------------------------------------------------


@pytest.fixture
def node_conn():
    conn = psycopg.connect(BACKUP_NODE_DSN)
    try:
        yield conn
    finally:
        conn.close()


@requires_backup_node
def test_the_measurement_cluster_is_ready_for_backup(node_conn):
    readiness = backup.inspect_node(node_conn, stanza=BACKUP_STANZA)
    assert readiness.repository.reachable, readiness.repository.detail
    assert readiness.failures == [], readiness.failures
    assert readiness.repository.check_ok is True


@requires_backup_node
def test_the_cluster_rejects_physical_replication_and_is_backed_up_anyway(db_pool, node_conn):
    """ADR-031 and ADR-067 in one assertion, which is the point of the slice.

    The reject exists because a non-superuser holding REPLICATION took a 484 MB
    physical copy of every database on a cluster it held CONNECT on exactly one
    of. If taking a backup required narrowing it, this feature would be a
    security regression. It does not: pgBackRest copies the data directory
    between pg_backup_start() and pg_backup_stop() over an ordinary libpq
    connection.

    Asserted here rather than trusted from slice 0's spec, because a node
    rebuilt without the reject would still pass every other test in this file.
    """
    # The reject, verified against the running node rather than read from a file.
    refused = False
    try:
        psycopg.connect(BACKUP_NODE_DSN, replication="true").close()
    except psycopg.OperationalError as exc:
        refused = "pg_hba.conf rejects replication connection" in str(exc)
    assert refused, "the measurement cluster is not carrying ADR-031's reject"

    with db.connection() as conn:
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, backup_stanza) "
            "VALUES ('bk-real','bkr.example','bkr.internal','shared','active',%s) "
            "ON CONFLICT (name) DO UPDATE SET backup_stanza = EXCLUDED.backup_stanza RETURNING id",
            (BACKUP_STANZA,),
        )["id"]
        conn.commit()
        run = backup.run_backup(
            conn, node_id=node_id, node_name="bk-real", stanza=BACKUP_STANZA,
            backup_type="incr", process_max=2,
        )

    assert run.ok, run.error
    assert run.label, "a completed backup must carry the label a restore needs"
    assert run.repository_bytes and run.repository_bytes > 0

    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT status, label, finished_at FROM node_backups WHERE id = %s", (run.backup_id,)
        )
    assert row["status"] == "complete"
    assert row["label"] == run.label
    assert row["finished_at"] is not None


@requires_backup_node
def test_no_walsender_is_opened_during_a_backup(db_pool, node_conn):
    """The mechanism behind the finding, asserted directly.

    Slice 0 measured `0` walsenders during a full backup. That is *why* the
    ADR-031 reject costs nothing, and it is the part that would change silently
    if a future pgBackRest option (`--backup-standby`, say) were added to the
    invocation. A count taken during the backup is the only thing that catches
    that.
    """
    import threading

    peak = 0

    def watch() -> None:
        nonlocal peak
        with psycopg.connect(BACKUP_NODE_DSN) as watcher:
            for _ in range(400):
                with watcher.cursor() as cur:
                    cur.execute("SELECT count(*) FROM pg_stat_replication")
                    peak = max(peak, cur.fetchone()[0])
                if done.is_set():
                    return

    done = threading.Event()
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        with db.connection() as conn:
            node_id = db.one(
                conn,
                "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, "
                "backup_stanza) VALUES ('bk-sender','bks.example','bks.internal','shared',"
                "'active',%s) ON CONFLICT (name) DO UPDATE "
                "SET backup_stanza = EXCLUDED.backup_stanza RETURNING id",
                (BACKUP_STANZA,),
            )["id"]
            conn.commit()
            run = backup.run_backup(
                conn, node_id=node_id, node_name="bk-sender", stanza=BACKUP_STANZA,
                backup_type="incr",
            )
    finally:
        done.set()
        watcher.join(timeout=5)

    assert run.ok, run.error
    assert peak == 0, f"pgBackRest opened {peak} replication connection(s); ADR-067 says it opens none"
