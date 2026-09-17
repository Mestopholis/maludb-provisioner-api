"""A repository on the node, accepted by name and for a time (ADR-087, free slice 7c).

- **only the co-location failure is affected**: retention, archiving and the ADR-068 window still
  fail, and the warning that replaces the failure still says the host's loss loses the backups;
- **it lapses by itself**: after its date the failure is back, and preflight re-checks the date
  rather than trusting a readiness recorded while it was in force;
- **it is bounded and attributable**: at most 90 days, a reason required, who recorded it kept, and
  the database refuses a partial or over-long acceptance even from a direct write.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import psycopg
import pytest

from services.control_plane import backup, db, manage, preflight
from tests.test_deploy_preflight import _cfg, _named, _node, _run

TODAY = date(2026, 9, 17)
PG = "/var/lib/postgresql/17/main"


def _readiness(*, until=None, retention=30, today=TODAY):
    local = backup.RepositoryOptions(index=1, type="posix", path="/var/lib/pgbackrest", retention_full=retention,
                                     retention_archive=30, retention_full_type="time", co_located=True)
    return backup.BackupReadiness(
        wal_level="replica", archive_mode="on", archive_command="pgbackrest archive-push %p",
        archive_timeout_s=300, archive_failed_count=0, archive_last_failed_wal=None,
        archive_last_archived_wal="00000001000000000000000A",
        repository=backup.RepositoryState(reachable=True, detail="1 backup(s)", check_ok=True, check_detail="ok",
                                          pg_path=PG, repo_path=local.path, repositories=(local,),
                                          reported_by_node=True),
        production=True, stanza="maludb-node-01", promised_retention_days=30,
        local_accepted_until=until, local_accepted_reason="free-tier beta; off-host targets deferred",
        local_accepted_by="owner", today=today)


def test_without_an_acceptance_a_local_repository_fails_in_production():
    readiness = _readiness()
    assert any("ADR-064" in failure for failure in readiness.failures)


def test_an_acceptance_in_force_turns_only_co_location_into_a_named_warning():
    readiness = _readiness(until=TODAY + timedelta(days=30))
    assert readiness.ready, readiness.failures
    [note] = [n for n in readiness.warnings if "same filesystem" in n]
    assert "takes the backups with it" in note and "accepted by owner until 2026-10-17" in note and "ADR-087" in note
    short = _readiness(until=TODAY + timedelta(days=30), retention=7)
    assert any("ADR-068" in failure for failure in short.failures), "retention still fails"
    assert not any("ADR-086 keeps two" in n for n in readiness.warnings)


def test_it_lapses_by_itself():
    assert _readiness(until=TODAY).ready, "the last day is still accepted"
    lapsed = _readiness(until=TODAY - timedelta(days=1))
    assert any("ADR-064" in failure for failure in lapsed.failures)


def test_the_command_is_bounded_and_needs_a_reason(db_pool):  # noqa: ARG001
    _node()
    with db.connection() as conn:
        for until, reason, message in ((TODAY + timedelta(days=91), "beta", "at most 90 days"),
                                       (TODAY - timedelta(days=1), "beta", "in the past"),
                                       (TODAY + timedelta(days=30), "   ", "needs a reason")):
            with pytest.raises(backup.BackupError, match=message):
                backup.accept_local_repository(conn, name="node-01", until=until, reason=reason, by="op",
                                               today=TODAY)
        with pytest.raises(backup.BackupError, match="no node"):
            backup.accept_local_repository(conn, name="nope", until=TODAY, reason="x", by="op", today=TODAY)


def test_the_database_refuses_a_partial_or_overlong_acceptance(db_pool):  # noqa: ARG001
    _node()
    with db.connection() as conn:
        for statement in (
            "UPDATE nodes SET backup_local_accepted_until = current_date WHERE name = 'node-01'",
            "UPDATE nodes SET backup_local_accepted_until = current_date + 91, backup_local_accepted_reason = 'x', "
            "backup_local_accepted_by = 'op', backup_local_accepted_at = now() WHERE name = 'node-01'",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement)
            conn.rollback()


def test_the_cli_records_and_withdraws(db_pool, capsys):  # noqa: ARG001
    _node()
    until = (date.today() + timedelta(days=30)).isoformat()
    ns = argparse.Namespace(name="node-01", until=until, reason="free-tier beta", revoke=False)
    assert manage._cmd_node_backup_accept_local(ns) == 0
    assert f"accepted until {until}" in capsys.readouterr().out
    with db.connection() as conn:
        row = db.one(conn, "SELECT backup_local_accepted_reason, backup_local_accepted_by FROM nodes "
                           "WHERE name = 'node-01'")
    assert row["backup_local_accepted_reason"] == "free-tier beta" and row["backup_local_accepted_by"]
    assert manage._cmd_node_backup_accept_local(argparse.Namespace(name="node-01", until=None, reason=None,
                                                                   revoke=False)) == 2
    assert manage._cmd_node_backup_accept_local(argparse.Namespace(name="node-01", until=None, reason=None,
                                                                   revoke=True)) == 0
    assert "acceptance withdrawn" in capsys.readouterr().out


def _checked_local(*, accepted_in_days: int | None):
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET capacity_json = coalesce(capacity_json, '{}'::jsonb) "
                         "|| '{\"backup_ready\": true, \"backup_repo_co_located\": true}'::jsonb, "
                         "metrics_json = coalesce(metrics_json, '{}'::jsonb) "
                         "|| jsonb_build_object('backup_checked_at', now()) WHERE name = 'node-01'")
        if accepted_in_days is not None:
            db.execute(conn, "UPDATE nodes SET backup_local_accepted_at = now() - interval '10 days', "
                             "backup_local_accepted_until = current_date + %s, "
                             "backup_local_accepted_reason = 'free-tier beta', backup_local_accepted_by = 'owner' "
                             "WHERE name = 'node-01'", (accepted_in_days,))
        conn.commit()


def test_preflight_names_an_acceptance_and_fails_the_day_it_lapses(db_pool):  # noqa: ARG001
    _node()
    _checked_local(accepted_in_days=20)
    check = _named(_run(), "node backups")
    assert check.ok and "keeps its backups on the node, accepted by owner" in check.detail
    _checked_local(accepted_in_days=-1)
    lapsed = _named(_run(), "node backups")
    assert not lapsed.ok and not lapsed.advisory and "acceptance lapsed" in lapsed.detail
    with db.connection() as conn:
        db.execute(conn, "UPDATE nodes SET backup_local_accepted_until = NULL, backup_local_accepted_reason = NULL, "
                         "backup_local_accepted_by = NULL, backup_local_accepted_at = NULL")
        conn.commit()
    unaccepted = _named(_run(), "node backups")
    assert not unaccepted.ok and "on the node; ADR-064" in unaccepted.detail
    assert _named(_run(_cfg(environment="development")), "node backups").ok, "development does not require it"
    assert preflight.NODE_BACKUP_CHECK_STALE
