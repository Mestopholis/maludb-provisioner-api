"""The control plane's nightly dump (ADR-070; free slice 7d, ADR-087).

- **named by the time it was taken**, so the directory is the history and nothing is guessed from mtimes;
- **pruned only after a good dump, never the last one**, so failing nights keep the last good file;
- **preflight fails in production without a recent dump**, and says the KEK is the second artefact;
- **the unit holds the KEK as a credential and writes only its own directory**.
"""

from __future__ import annotations

import argparse
import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from services.control_plane import manage, preflight, recovery
from tests.test_deploy_preflight import _cfg

NOW = datetime(2026, 9, 17, 1, 30, tzinfo=UTC)
DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"


def _touch(directory, when):
    path = recovery.dump_path(str(directory), now=when)
    pathlib.Path(path).write_text("-- dump\n")
    return path


def test_dumps_are_named_by_time_and_other_files_are_ignored(tmp_path):
    path = _touch(tmp_path, NOW)
    assert os.path.basename(path) == "cp-20260917T013000Z.sql"
    (tmp_path / "cp-notatime.sql").write_text("")
    (tmp_path / "notes.txt").write_text("")
    assert recovery.dumps_in(str(tmp_path)) == [(NOW, path)]


def test_pruning_keeps_the_window_and_never_the_last_dump(tmp_path):
    old = [_touch(tmp_path, NOW - timedelta(days=d)) for d in (20, 15)]
    recent = _touch(tmp_path, NOW - timedelta(days=3))
    assert sorted(recovery.prune_dumps(str(tmp_path), keep_days=14, now=NOW)) == sorted(old)
    assert [p for _, p in recovery.dumps_in(str(tmp_path))] == [recent]

    only = tmp_path / "only"
    only.mkdir()
    ancient = _touch(only, NOW - timedelta(days=400))
    assert recovery.prune_dumps(str(only), keep_days=14, now=NOW) == [], "the last dump is never pruned"
    assert os.path.exists(ancient)
    with pytest.raises(ValueError):
        recovery.prune_dumps(str(only), keep_days=0, now=NOW)


def test_a_failed_dump_prunes_nothing(tmp_path, monkeypatch, capsys):
    _touch(tmp_path, datetime.now(UTC) - timedelta(days=30))
    _touch(tmp_path, datetime.now(UTC) - timedelta(days=20))

    class _Failed:
        error = "pg_dump exited 1"

    monkeypatch.setattr(manage.config, "load", lambda: type("S", (), {"database_url": "postgresql://x/y"})())
    monkeypatch.setattr(manage.recovery, "dump", lambda *a, **k: _Failed())
    args = argparse.Namespace(path=None, dir=str(tmp_path), keep_days=14, pg_dump="pg_dump")
    assert manage._cmd_control_plane_backup(args) == 1
    assert len(recovery.dumps_in(str(tmp_path))) == 2, "failing nights keep what is there"
    assert manage._cmd_control_plane_backup(argparse.Namespace(path="/x", dir=str(tmp_path), keep_days=14,
                                                                pg_dump="pg_dump")) == 2


def _check(cfg, monkeypatch, directory):
    monkeypatch.setenv("MALUDB_CONTROL_PLANE_BACKUP_DIR", str(directory))
    report = preflight.Report()
    preflight._check_control_plane_backup(cfg, report)
    return {c.name: c for c in report.checks}["control-plane backup"]


def test_preflight_wants_a_recent_dump_in_production(tmp_path, monkeypatch):
    empty = _check(_cfg(), monkeypatch, tmp_path)
    assert not empty.ok and not empty.advisory and "no dump" in empty.detail
    assert _check(_cfg(environment="development"), monkeypatch, tmp_path).advisory
    _touch(tmp_path, datetime.now(UTC) - timedelta(hours=30))
    stale = _check(_cfg(), monkeypatch, tmp_path)
    assert not stale.ok and "30 hours old" in stale.detail
    _touch(tmp_path, datetime.now(UTC) - timedelta(hours=2))
    fresh = _check(_cfg(), monkeypatch, tmp_path)
    assert fresh.ok and "KEK" in fresh.detail
    missing = _check(_cfg(), monkeypatch, tmp_path / "nope")
    assert not missing.ok and "could not read" in missing.detail


def test_the_unit_holds_the_kek_as_a_credential_and_writes_only_its_directory():
    text = (DEPLOY / "maludb-control-plane-backup.service").read_text()
    for directive in ("User=maludb-provisioner", "LoadCredential=kek:/etc/maludb/keys/kek",
                      "EnvironmentFile=/etc/maludb/provisioner.env", "UMask=0077", "ProtectSystem=strict",
                      "ReadWritePaths=/var/backups/maludb-control-plane"):
        assert directive in text, directive
    assert "--dir /var/backups/maludb-control-plane --keep-days 14" in text.replace("\\\n", " ").replace("  ", " ")
    timer = (DEPLOY / "maludb-control-plane-backup.timer").read_text()
    assert "Unit=maludb-control-plane-backup.service" in timer and "Persistent=true" in timer
