"""Readiness across more than one repository, judged by type (ADR-086, free slice 7c).

- **every `repoN` is read and checked**: retention unset, retention short of the promise, and
  co-location each name the repository;
- **a remote repository is never judged against this host's disks**: an SFTP repository's path is a
  path on the other site, and before 7c it would have been `stat`ed here;
- **credentials stay out**: `repoN-s3-key`, `repoN-sftp-private-key-file` and `repoN-cipher-pass` are
  not read;
- **production wants two repositories off the host** (a warning: the failure is co-location);
- **the node's report carries every repository**, parsed as untrusted input.
"""

from __future__ import annotations

from datetime import UTC, datetime

from services.control_plane import backup

CONFIG = """\
[global]
repo1-type=sftp
repo1-path=/var/lib/postgresql/17/main/backups
repo1-sftp-host=backup.site-b.example
repo1-sftp-host-user=pgbackrest
repo1-sftp-private-key-file=/var/lib/postgresql/.ssh/id_ed25519
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=do-not-read-this
repo1-retention-full-type=time
repo1-retention-full=30
repo1-retention-archive=30
repo2-type=s3
repo2-path=/node-01
repo2-s3-bucket=maludb-backups
repo2-s3-key=AKIA-NOT-TO-BE-READ
repo2-s3-key-secret=secret-not-to-be-read
repo2-retention-full-type=time
repo2-retention-full=7

[maludb-node-01]
pg1-path=/var/lib/postgresql/17/main
"""

PG = "/var/lib/postgresql/17/main"


def _options(tmp_path):
    conf = tmp_path / "pgbackrest.conf"
    conf.write_text(CONFIG)
    return backup._read_stanza_options("maludb-node-01", str(conf))  # noqa: SLF001


def _readiness(repositories, *, production=True, promised=30, reported=False):
    return backup.BackupReadiness(
        wal_level="replica", archive_mode="on", archive_command="pgbackrest archive-push %p",
        archive_timeout_s=300, archive_failed_count=0, archive_last_failed_wal=None,
        archive_last_archived_wal="00000001000000000000000A",
        repository=backup.RepositoryState(
            reachable=True, detail="1 backup(s)", check_ok=True, check_detail="ok", pg_path=PG,
            repo_path=repositories[0].path, retention_full=repositories[0].retention_full,
            retention_archive=repositories[0].retention_archive,
            retention_full_type=repositories[0].retention_full_type, repositories=tuple(repositories),
            reported_by_node=reported),
        production=production, stanza="maludb-node-01", promised_retention_days=promised)


def test_every_repository_is_read_and_no_credential_is(tmp_path):
    options = _options(tmp_path)
    assert all(backup._wanted(key) for key in options)  # noqa: SLF001
    assert not any(word in value for value in options.values()
                   for word in ("do-not-read", "AKIA", "secret-not", "id_ed25519", "site-b"))
    repo1, repo2 = backup.repositories_from_options(options)
    assert (repo1.index, repo1.type, repo1.retention_full, repo1.retention_full_type) == (1, "sftp", 30, "time")
    assert (repo2.index, repo2.type, repo2.retention_full, repo2.retention_archive) == (2, "s3", 7, None)


def test_a_remote_repository_is_never_judged_against_this_hosts_disks(tmp_path, monkeypatch):
    """repo1's path sits *inside* this host's data directory by name; it is on the other site."""
    repos = backup.repositories_from_options(_options(tmp_path))
    monkeypatch.setattr(backup, "repository_co_located",
                        lambda repo, pg: (_ for _ in ()).throw(AssertionError(f"judged {repo} locally")))
    readiness = _readiness(repos)
    assert readiness.repository_is_co_located is False
    assert not any("same filesystem" in text for text in readiness.failures + readiness.warnings)


def test_each_repository_is_held_to_retention_and_the_promise(tmp_path):
    readiness = _readiness(backup.repositories_from_options(_options(tmp_path)))
    failures = " | ".join(readiness.failures)
    assert "repo2-retention-archive is unset" in failures
    assert "repo2-retention-full is 7 days and the longest plan promises 30" in failures
    assert "repo1-retention" not in failures, "repo1 keeps 30 days of both halves"


def test_an_interim_local_repository_beside_remote_ones_is_still_a_failure():
    local = backup.RepositoryOptions(index=1, type="posix", path=f"{PG}/../../pgbackrest", retention_full=30,
                                     retention_archive=30, retention_full_type="time", co_located=True)
    remote = backup.RepositoryOptions(index=2, type="s3", path="/node-01", retention_full=30,
                                      retention_archive=30, retention_full_type="time")
    readiness = _readiness([local, remote], reported=True)
    assert readiness.repository_is_co_located is True
    assert any(text.startswith("repo1-path") and "ADR-064" in text for text in readiness.failures)


def test_production_wants_two_repositories_off_the_host():
    def remote(index, kind):
        return backup.RepositoryOptions(index=index, type=kind, path="/x", retention_full=30,
                                        retention_archive=30, retention_full_type="time")

    one = _readiness([remote(1, "sftp")])
    assert one.ready and any("ADR-086 keeps two" in note for note in one.warnings)
    two = _readiness([remote(1, "sftp"), remote(2, "s3")])
    assert two.ready and not any("ADR-086 keeps two" in note for note in two.warnings)
    assert not any("ADR-086" in note for note in _readiness([remote(1, "sftp")], production=False).warnings)


def test_an_unknown_repository_type_is_undetermined_not_off_host():
    odd = backup.RepositoryOptions(index=1, type="unknown", path="/x", retention_full=30,
                                   retention_archive=30, retention_full_type="time")
    assert _readiness([odd]).repository_is_co_located is None


def test_the_nodes_report_carries_every_repository_untrusted(tmp_path):
    repos = backup.repositories_from_options(_options(tmp_path))
    state = backup.RepositoryState(reachable=True, detail="ok", check_ok=True, pg_path=PG,
                                   repo_path=repos[0].path, repositories=repos)
    report = backup.repository_report(state, stanza="maludb-node-01")
    assert [(r["index"], r["type"], r["co_located"]) for r in report["repositories"]] == [(1, "sftp", False),
                                                                                          (2, "s3", False)]
    now = datetime(2026, 9, 17, 12, tzinfo=UTC)
    report["repositories"] += [{"index": 2, "type": "posix"}, {"index": 9, "type": "s3"}, "junk",
                               {"index": True, "type": "s3"}]
    report["repositories"][0]["type"] = "ftp"
    parsed = backup.repository_from_report(report, stanza="maludb-node-01", checked_at=now.isoformat(), now=now)
    assert [(r.index, r.type) for r in parsed.repositories] == [(1, "unknown"), (2, "s3")], \
        "duplicates, out-of-range indices, non-objects and unknown types are not believed"


def test_the_shipped_example_is_ready_for_production():
    """deploy/pgbackrest.conf.example is what DEPLOYMENT §2.8 installs; it must pass as written."""
    import pathlib

    example = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "pgbackrest.conf.example"
    options = backup._read_stanza_options("maludb-node-01", str(example))  # noqa: SLF001
    repos = backup.repositories_from_options(options)
    assert [(r.index, r.type) for r in repos] == [(1, "sftp"), (2, "s3")]
    readiness = _readiness(list(repos))
    assert readiness.ready, readiness.failures
    assert not any("ADR-086" in note for note in readiness.warnings)
    assert options.get("pg1-path") == PG
