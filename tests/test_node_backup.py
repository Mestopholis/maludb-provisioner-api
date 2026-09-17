"""The node's backup runner, and the control plane's reading of its report (ADR-086, free slice 7b).

What is held here:

- **a backup is recorded around pgBackRest**: a running row first, closed with pgBackRest's own
  label and sizes, and closed as failed -- with the reason -- when it fails; a timeout leaves the
  row running for the maintenance pass;
- **the runner holds no key**: it imports neither `crypto` nor `config`, and its unit loads no
  credential and runs as `postgres`;
- **a node's report is untrusted input**: missing, stale or for another stanza reads as an
  *unexamined* repository, types are checked, and co-location is the node's answer -- the
  control plane never `stat`s a node's paths on its own filesystem.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from services.control_plane import backup, node_backup

DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"
SETTINGS = node_backup.Settings(database_url="postgresql://backup_node01@cp/x", stanza="maludb-node-01")
LABEL = "20260917-021500F"


class _Recorder:
    """A stand-in for the recorder's connections: every call, in order, across connections."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.opened = 0

    def __call__(self, dsn, **kwargs):
        assert dsn == SETTINGS.database_url and kwargs.get("autocommit") is True
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=()):
        self.calls.append((statement.split("(")[0].replace("SELECT public.", ""), params))
        return self

    def fetchone(self):
        name = self.calls[-1][0]
        return (41,) if name == "start_node_backup" else ("node-01",)


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["pgbackrest"], returncode, stdout, stderr)


def test_a_backup_is_opened_first_and_closed_with_pgbackrests_own_figures():
    recorder, seen = _Recorder(), []

    def pgbackrest(stanza, *argv, timeout, run_as):
        seen.append((stanza, argv, run_as))
        assert [name for name, _ in recorder.calls] == ["start_node_backup"], "the row exists before pgBackRest runs"
        return _proc()

    detail = {"label": LABEL, "database_bytes": 64_000_000, "repository_bytes": 7_000_000,
              "wal_start": "000000010000000000000009", "wal_stop": "00000001000000000000000A"}
    assert node_backup.take_backup(SETTINGS, "full", connect=recorder, pgbackrest=pgbackrest,
                                   latest=lambda stanza, run_as: detail) == 0
    assert seen == [("maludb-node-01", ("--log-level-console=warn", "backup", "--type=full", "--start-fast"), "")], \
        "--start-fast always (ADR-067), and no sudo: the runner already is postgres"
    assert recorder.calls == [
        ("start_node_backup", ("full",)),
        ("finish_node_backup", (41, "complete", LABEL, 64_000_000, 7_000_000,
                                "000000010000000000000009", "00000001000000000000000A", None)),
    ]
    assert recorder.opened == 2, "two short connections, not one held across the backup"


def test_a_failed_backup_is_closed_as_failed_with_the_reason():
    recorder = _Recorder()
    code = node_backup.take_backup(
        SETTINGS, "diff", connect=recorder,
        pgbackrest=lambda *a, **k: _proc(56, stderr="ERROR: [056]: unable to find primary cluster"),
        latest=lambda *a, **k: pytest.fail("info is not read after a failure"))
    assert code == 1
    (_, finish), = [c for c in recorder.calls if c[0] == "finish_node_backup"]
    assert finish[1] == "failed" and finish[2] is None and "unable to find primary cluster" in finish[7]


def test_exit_zero_with_no_backup_in_the_repository_is_not_recorded_as_complete():
    recorder = _Recorder()
    assert node_backup.take_backup(SETTINGS, "full", connect=recorder, pgbackrest=lambda *a, **k: _proc(),
                                   latest=lambda *a, **k: None) == 1
    assert recorder.calls[-1][1][1] == "failed"


def test_a_backup_that_does_not_return_leaves_the_row_running():
    recorder = _Recorder()

    def hangs(*argv, **kwargs):
        raise subprocess.TimeoutExpired("pgbackrest", 1)

    assert node_backup.take_backup(SETTINGS, "full", connect=recorder, pgbackrest=hangs) == 1
    assert [name for name, _ in recorder.calls] == ["start_node_backup"], "the maintenance pass ages it out"


def test_the_check_records_the_repository_as_seen_on_the_node(tmp_path):
    recorder = _Recorder()
    state = backup.RepositoryState(
        reachable=True, detail="2 backup(s) in the repository", check_ok=True, check_detail="ok",
        pg_path="/etc/postgresql/17/main", repo_path=str(tmp_path / "repo"), retention_full=14,
        retention_archive=14, retention_full_type="time", backup_labels=(LABEL,),
        oldest_backup_at=datetime(2026, 9, 17, 2, 20, tzinfo=UTC),
        newest_backup_at=datetime(2026, 9, 17, 2, 20, tzinfo=UTC))
    assert node_backup.check(SETTINGS, connect=recorder, inspect=lambda stanza, **k: state,
                             data_directory=lambda: str(tmp_path / "data")) == 0
    (name, (payload,)), = recorder.calls
    report = json.loads(payload)
    assert name == "record_node_backup_check"
    assert report["stanza"] == "maludb-node-01" and report["check_ok"] is True
    assert report["pg_path"] == str(tmp_path / "data"), "the postmaster's data directory, not the config file's"
    assert report["co_located"] is None or isinstance(report["co_located"], bool)
    assert "repo1-s3-key" not in payload and "passphrase" not in payload


@pytest.mark.parametrize("env, message", [
    ({}, "MALUDB_BACKUP_RECORDER_DATABASE_URL"),
    ({"MALUDB_BACKUP_RECORDER_DATABASE_URL": "postgresql://r@cp/x"}, "MALUDB_BACKUP_STANZA"),
    ({"MALUDB_BACKUP_RECORDER_DATABASE_URL": "postgresql://r@cp/x", "MALUDB_BACKUP_STANZA": "s",
      "MALUDB_BACKUP_PROCESS_MAX": "64"}, "1..16"),
])
def test_the_runner_refuses_an_incomplete_environment(monkeypatch, env, message):
    for key in ("MALUDB_BACKUP_RECORDER_DATABASE_URL", "MALUDB_BACKUP_STANZA", "MALUDB_BACKUP_PROCESS_MAX"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match=message):
        node_backup.Settings.from_environment()


def test_the_runner_imports_no_key_material():
    code = ("import sys; import services.control_plane.node_backup; "
            "print(','.join(sorted(m for m in sys.modules if m.startswith('services.'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,  # noqa: S603
                         cwd=pathlib.Path(__file__).resolve().parent.parent).stdout
    loaded = set(out.strip().split(","))
    assert not loaded & {"services.control_plane.crypto", "services.control_plane.config"}, loaded


# -- the report, read on the control plane ---------------------------------------------


def _report(**overrides):
    base = backup.repository_report(
        backup.RepositoryState(reachable=True, detail="1 backup(s)", check_ok=True, check_detail="ok",
                               pg_path="/var/lib/postgresql/17/main", repo_path="/var/lib/pgbackrest",
                               retention_full=14, retention_archive=14, retention_full_type="time",
                               backup_labels=(LABEL,)),
        stanza="maludb-node-01")
    base.update(overrides)
    return base


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("report, checked_at, expected", [
    (None, NOW.isoformat(), "has not reported"),
    (_report(), None, "never"),
    (_report(), (NOW - timedelta(hours=27)).isoformat(), "over 26 hours ago"),
    (_report(stanza="someone-else"), NOW.isoformat(), "expects 'maludb-node-01'"),
])
def test_a_missing_stale_or_foreign_report_is_an_unexamined_repository(report, checked_at, expected):
    state = backup.repository_from_report(report, stanza="maludb-node-01", checked_at=checked_at, now=NOW)
    assert state.reachable is False and expected in state.detail


def test_a_report_is_untrusted_input():
    hostile = _report(check_ok="yes", retention_full=-3, retention_archive=True, retention_full_type="forever",
                      backup_labels=["x" * 500, 7], detail="d" * 5000, co_located="no")
    state = backup.repository_from_report(hostile, stanza="maludb-node-01", checked_at=NOW.isoformat(), now=NOW)
    assert state.check_ok is None and state.retention_full is None and state.retention_archive is None
    assert state.retention_full_type == "unknown"
    assert state.backup_labels == ("x" * 40,) and len(state.detail) <= 400
    assert state.co_located is None and state.reported_by_node


def test_co_location_is_the_nodes_answer_never_a_local_stat(monkeypatch):
    """The same paths exist on the control plane and would answer a different question."""
    reports = {answer: _report(co_located=answer) for answer in (True, False)}
    monkeypatch.setattr(backup, "repository_co_located",
                        lambda repo, pg: pytest.fail(f"judged {repo} against {pg} on the control plane"))
    for answer, report in reports.items():
        state = backup.repository_from_report(report, stanza="maludb-node-01",
                                              checked_at=NOW.isoformat(), now=NOW)
        readiness = backup.BackupReadiness(
            wal_level="replica", archive_mode="on", archive_command="pgbackrest archive-push %p",
            archive_timeout_s=60, archive_failed_count=0, archive_last_failed_wal=None,
            archive_last_archived_wal="000000010000000000000009", repository=state, production=True,
            stanza="maludb-node-01", promised_retention_days=7)
        assert readiness.repository_is_co_located is answer
        assert any("same filesystem" in f for f in readiness.failures) is answer


# -- the units -----------------------------------------------------------------------------


def test_the_runner_unit_runs_as_postgres_with_no_key_and_shared_locks():
    text = (DEPLOY / "maludb-node-backup@.service").read_text()
    directives = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert "User=postgres" in directives
    assert "EnvironmentFile=/etc/maludb/node-backup.env" in directives
    assert "ExecStart=/opt/maludb/.venv/bin/python -m services.control_plane.node_backup %i" in directives
    assert not any(line.startswith("LoadCredential") for line in directives), "the runner holds no KEK"
    assert not any(line.startswith("PrivateTmp") for line in directives), "pgBackRest's locks are in /tmp"
    assert "NoNewPrivileges=true" in directives and "TimeoutStartSec=6h" in directives
    env = (DEPLOY / "node-backup.env.example").read_text()
    assert "KEK" not in env and "MALUDB_BACKUP_RECORDER_DATABASE_URL=" in env


@pytest.mark.parametrize("instance", ["full", "diff", "check"])
def test_each_timer_starts_its_instance(instance):
    text = (DEPLOY / f"maludb-node-backup-{instance}.timer").read_text()
    assert f"Unit=maludb-node-backup@{instance}.service" in text
    assert "Persistent=true" in text and "OnCalendar=" in text
