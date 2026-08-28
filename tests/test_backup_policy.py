"""Retention and PITR as plan entitlements (Phase 11 slice 3, ADR-068).

Three things are under test here and they are different kinds of claim.

**The promise is configuration.** `AGENTS.md` forbids hard-coding production
plan limits in application logic, and a recovery window is a plan limit in
exactly the way a storage quota is. So the numbers resolve through
`entitlements` and a deployment overrides them with a row.

**The promise is checked against the node.** A plan may promise thirty days;
whether a repository keeps thirty days is a separate fact, and pgBackRest's
default expresses retention as a *count of full backups* rather than as a
window. A count cannot be compared with a window without knowing the backup
schedule, so the check has three outcomes -- kept, not kept, and not checkable
-- and the third must not read as the first.

**The promise is enforced on the request, and so is physics.** A target older
than the plan allows is refused by policy; a target older than the repository's
oldest backup is refused by the repository. The two have opposite fixes -- one
is answered by changing a plan, the other by accepting that the data is gone --
so the refusal says which refused.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from services.control_plane import backup, db, entitlements, restore
from tests.conftest import (
    BACKUP_STANZA,
    requires_backup_node,
    requires_db,
)

RUN_AS = os.environ.get("MALUDB_BACKUP_RUN_AS")


def _repo(**kwargs) -> backup.RepositoryState:
    """A repository state with the fields this file cares about and sane rest."""
    defaults = {
        "reachable": True,
        "detail": "1 backup(s) in the repository",
        "check_ok": True,
        "check_detail": "ok",
        "pg_path": "/var/lib/postgresql/17/bk",
        "repo_path": "/srv/backups",
        "retention_full": 30,
        "retention_archive": 30,
        "retention_full_type": "time",
        "backup_labels": ("20260827-011337F",),
        "oldest_backup_at": datetime.now(UTC) - timedelta(days=31),
        "newest_backup_at": datetime.now(UTC) - timedelta(hours=1),
    }
    return backup.RepositoryState(**{**defaults, **kwargs})


def _readiness(*, promised: int, repository: backup.RepositoryState) -> backup.BackupReadiness:
    return backup.BackupReadiness(
        wal_level="replica",
        archive_mode="on",
        archive_command="pgbackrest --stanza=s archive-push %p",
        archive_timeout_s=60,
        archive_failed_count=0,
        archive_last_failed_wal=None,
        archive_last_archived_wal="000000010000000000000044",
        repository=repository,
        stanza="s",
        promised_retention_days=promised,
    )


# --------------------------------------------------------------------------
# The promise is configuration
# --------------------------------------------------------------------------


def test_every_tier_has_a_real_retention_number():
    """No tier resolves to zero retention by accident.

    Zero is expressible and is a real value -- an operator may sell a tier with
    no restore promise -- but no tier ships that way, and a missing key must not
    produce one.
    """
    for code in ("free", "starter", "production"):
        allowed = entitlements.resolve(code, None)
        assert allowed.backup_retention_days > 0, code


def test_free_is_backed_up_and_gets_no_point_in_time():
    """The slice's product decision, asserted rather than only documented.

    Free's bytes are in the node backup whether or not anyone sells them --
    a node is backed up whole -- so a retention of zero would be a fiction. What
    free does not get is a second of its choosing.
    """
    free = entitlements.resolve("free", None)
    assert free.backup_retention_days == 7
    assert free.pitr_window_hours == 0
    assert free.pitr_hours_effective() == 0


def test_paid_tiers_grant_a_point_in_time_and_free_does_not():
    assert entitlements.resolve("starter", None).pitr_hours_effective() > 0
    assert entitlements.resolve("production", None).pitr_hours_effective() > 0
    assert entitlements.resolve("free", None).pitr_hours_effective() == 0


def test_an_unknown_plan_gets_the_free_tier_window():
    """The fallback direction matters: an unidentifiable plan is not sold a month."""
    unknown = entitlements.resolve("enterprise-gold", None)
    assert unknown.backup_retention_days == 7
    assert unknown.pitr_hours_effective() == 0


def test_a_deployment_overrides_both_numbers_with_a_row():
    config = {"limits": {"backup_retention_days": 90, "pitr_window_hours": 2160}}
    allowed = entitlements.resolve("production", config)
    assert allowed.backup_retention_days == 90
    assert allowed.pitr_hours_effective() == 2160


def test_zero_retention_is_expressible():
    """A tier sold with no restore promise is a legitimate thing to configure.

    `_int_from` rather than `_positive_int_from`, deliberately: neither of these
    fails open at zero, so zero can stay a real value.
    """
    allowed = entitlements.resolve("free", {"limits": {"backup_retention_days": 0}})
    assert allowed.backup_retention_days == 0
    assert allowed.pitr_hours_effective() == 0


def test_a_pitr_window_longer_than_retention_is_clamped_not_honoured():
    """A promise a plan cannot keep resolves downward, never upward."""
    allowed = entitlements.resolve(
        "production", {"limits": {"backup_retention_days": 7, "pitr_window_hours": 720}}
    )
    assert allowed.pitr_window_hours == 720, "the configured value is preserved for reporting"
    assert allowed.pitr_hours_effective() == 7 * 24, "what is honoured is the shorter one"


def test_a_malformed_window_falls_back_to_the_default_not_to_unlimited():
    for bad in ("thirty", None, -5, float("inf"), True):
        allowed = entitlements.resolve("free", {"limits": {"backup_retention_days": bad}})
        assert allowed.backup_retention_days == 7, bad


@requires_db
def test_the_longest_promise_comes_from_offered_plans_only(db_pool):
    """A retired tier's window is not a promise to anyone new.

    It is still owed to a project *on* that plan, which is why the restore path
    resolves the project's own entitlement rather than this maximum -- asserted
    separately below.
    """
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code,name,is_active,config_json) VALUES "
            "('pol-live','Live',true,%s), ('pol-dead','Dead',false,%s)",
            (
                Jsonb({"limits": {"backup_retention_days": 10}}),
                Jsonb({"limits": {"backup_retention_days": 400}}),
            ),
        )
        conn.commit()
        assert backup.longest_promised_retention_days(conn) == 10


@requires_db
def test_an_empty_catalogue_promises_nothing_rather_than_everything(db_pool):
    with db.connection() as conn:
        assert backup.longest_promised_retention_days(conn) == 0


# --------------------------------------------------------------------------
# The promise is checked against the node
# --------------------------------------------------------------------------


def test_a_node_keeping_less_than_the_longest_promise_fails():
    readiness = _readiness(
        promised=30, repository=_repo(retention_full=7, retention_full_type="time")
    )
    assert not readiness.ready
    assert any("ADR-068" in f and "7 days" in f for f in readiness.failures), readiness.failures


def test_a_node_keeping_the_promise_passes():
    readiness = _readiness(
        promised=30, repository=_repo(retention_full=30, retention_full_type="time")
    )
    assert readiness.ready, readiness.failures


def test_retention_as_a_count_is_unverifiable_rather_than_passing():
    """pgBackRest's default. The check must say it did not run.

    A count of full backups can be perfectly adequate -- thirty nightly fulls is
    thirty days -- and the platform has no way to know the schedule. What it
    must not do is report a promise as checked when it was not.
    """
    readiness = _readiness(
        promised=30, repository=_repo(retention_full=2, retention_full_type="count")
    )
    assert readiness.ready, "a count is not a failure"
    assert any("did NOT run" in w for w in readiness.warnings), readiness.warnings
    assert any("repo1-retention-full-type=time" in w for w in readiness.warnings)


def test_an_absent_retention_type_is_read_as_the_default_not_as_unset():
    """A node never configured and a node configured to the default are the same node."""
    assert backup._retention_type(None) == "count"


def test_a_retention_type_is_a_closed_set_not_a_string_from_a_config_file():
    """It reaches a terminal and `capacity_json`, so it may not be free-form.

    Slice 1's security review closed this shape once already -- a stanza name
    reaching a subprocess argv, and `pgbackrest.conf` credentials reaching a
    printed dataclass. There are two legal values, so anything else is
    `unknown`; a length cap and an escape filter would be the weaker answer.
    """
    assert backup._retention_type("  TIME  ") == "time"
    assert backup._retention_type("count") == "count"
    assert backup._retention_type("\x1b[31mred\x1b[0m" * 500) == "unknown"
    assert backup._retention_type("") == "unknown"


def test_an_unrecognised_retention_type_is_not_guessed_as_a_count():
    """`unknown` means the file said something this module did not understand.

    The ADR-068 check treats it the way it treats a count -- not checkable --
    rather than assuming a default that might be wrong in the direction that
    passes a node.
    """
    readiness = _readiness(
        promised=30, repository=_repo(retention_full=99, retention_full_type="unknown")
    )
    assert readiness.ready, "an unreadable value is not a failure"
    assert any("did NOT run" in w for w in readiness.warnings), readiness.warnings


def test_no_promise_supplied_disables_the_check_rather_than_passing_every_node():
    """A caller that forgot to pass the promise must not thereby clear every node."""
    readiness = _readiness(
        promised=0, repository=_repo(retention_full=1, retention_full_type="time")
    )
    assert readiness.ready
    assert not any("ADR-068" in w for w in readiness.warnings)


def test_a_young_repository_is_reported_and_is_not_a_failure():
    """A node backed up for the first time this morning is correct, not broken."""
    readiness = _readiness(
        promised=30,
        repository=_repo(oldest_backup_at=datetime.now(UTC) - timedelta(days=2)),
    )
    assert readiness.ready
    assert any("oldest backup finished" in w and "2.0 days ago" in w for w in readiness.warnings), (
        readiness.warnings
    )


def test_a_repository_with_no_backups_says_so():
    readiness = _readiness(
        promised=30, repository=_repo(backup_labels=(), oldest_backup_at=None, newest_backup_at=None)
    )
    assert any("holds no backups" in w for w in readiness.warnings), readiness.warnings


def test_an_unreachable_repository_is_not_judged_against_the_promise():
    """An unexamined repository must not be reported as one that failed a check."""
    readiness = _readiness(
        promised=30,
        repository=backup.RepositoryState(reachable=False, detail="pgbackrest is not on PATH"),
    )
    assert not any("ADR-068" in f for f in readiness.failures), readiness.failures


def test_the_earliest_recoverable_point_is_when_the_oldest_backup_finished():
    """Not when it started. Recovery replays forward from a consistent point."""
    stop = datetime.now(UTC) - timedelta(days=3)
    state = _repo(oldest_backup_at=stop)
    assert state.earliest_recoverable_at == stop


def test_an_empty_repository_has_no_earliest_point_rather_than_an_unbounded_one():
    state = _repo(backup_labels=(), oldest_backup_at=None)
    assert state.earliest_recoverable_at is None


# --------------------------------------------------------------------------
# The promise is enforced on the request
# --------------------------------------------------------------------------


def _window(**kwargs) -> restore.RestoreWindow:
    defaults = {
        "plan_code": "production",
        "retention_days": 30,
        "pitr_hours": 720,
        "earliest_in_repository": datetime.now(UTC) - timedelta(days=25),
        "newest_in_repository": datetime.now(UTC) - timedelta(hours=1),
        "has_backups": True,
    }
    return restore.RestoreWindow(**{**defaults, **kwargs})


def test_a_target_inside_the_window_is_allowed():
    assert _window().refusal(datetime.now(UTC) - timedelta(hours=6)) is None


def test_no_target_is_allowed_on_a_plan_with_no_pitr():
    """Free restores to the state of a backup. That is a restore, and it is allowed."""
    assert _window(plan_code="free", retention_days=7, pitr_hours=0).refusal(None) is None


def test_a_plan_without_pitr_refuses_a_target_and_says_which_plan():
    refusal = _window(plan_code="free", retention_days=7, pitr_hours=0).refusal(
        datetime.now(UTC) - timedelta(hours=1)
    )
    assert refusal is not None
    assert "free" in refusal and "ADR-068" in refusal
    assert "Omit the target" in refusal, "the refusal has to name the thing that does work"


def test_a_target_older_than_the_plan_window_is_refused_by_policy():
    refusal = _window(plan_code="starter", retention_days=14, pitr_hours=168).refusal(
        datetime.now(UTC) - timedelta(days=10)
    )
    assert refusal is not None
    assert "starter" in refusal and "168h" in refusal
    assert "repository" not in refusal, "this is the plan refusing, not the repository"


def test_a_target_before_the_oldest_backup_is_refused_by_the_repository():
    """The other bound, and the refusal must not be mistaken for a plan limit.

    A customer told to upgrade when the real answer is that the bytes are gone
    has been sold a recovery the platform cannot perform.
    """
    refusal = _window(
        earliest_in_repository=datetime.now(UTC) - timedelta(days=3)
    ).refusal(datetime.now(UTC) - timedelta(days=5))
    assert refusal is not None
    assert "oldest backup" in refusal
    assert "No plan change makes this target reachable" in refusal


def test_the_plan_bound_wins_when_both_would_refuse():
    """Ordered so the most actionable reason is the one shown."""
    refusal = _window(
        plan_code="starter",
        retention_days=14,
        pitr_hours=168,
        earliest_in_repository=datetime.now(UTC) - timedelta(days=3),
    ).refusal(datetime.now(UTC) - timedelta(days=10))
    assert refusal is not None and "starter" in refusal


def test_an_empty_repository_refuses_before_any_policy_question():
    assert "holds no backups" in (_window(has_backups=False).refusal(None) or "")


def test_a_future_target_is_refused():
    refusal = _window().refusal(datetime.now(UTC) + timedelta(hours=1))
    assert refusal is not None and "future" in refusal


def test_a_naive_target_is_refused():
    """A timestamp with no zone names a different moment on a node in another zone."""
    refusal = _window().refusal(datetime(2026, 8, 1, 12, 0, 0))  # noqa: DTZ001 - the point
    assert refusal is not None and "timezone" in refusal


def test_an_unreadable_repository_does_not_refuse_on_what_it_did_not_read():
    """A control plane that cannot see the repository must not veto a restore.

    `has_backups` is False here because nothing was read, which is not the same
    fact as an empty repository and must not be enforced as though it were.
    """
    window = _window(has_backups=False, repository_readable=False)
    assert window.refusal(datetime.now(UTC) - timedelta(hours=6)) is None


def test_the_window_describes_itself_for_an_operator():
    assert "no point-in-time recovery" in _window(plan_code="free", pitr_hours=0).describe()
    assert "within 720h" in _window().describe()


# --------------------------------------------------------------------------
# Against a real repository
# --------------------------------------------------------------------------


@requires_db
@requires_backup_node
def test_a_real_repository_reports_a_retention_type_and_a_recoverable_span():
    """Read from the measurement cluster rather than from a fixture.

    The retention *type* is what ADR-068's check turns on, and the span is the
    physical bound. Both come out of pgBackRest rather than out of a config
    file the platform hopes matches.
    """
    state = backup.inspect_repository(BACKUP_STANZA, run_as=RUN_AS)
    assert state.reachable, state.detail
    assert state.retention_full_type in ("count", "time")
    assert state.backup_labels, "the measurement cluster should hold at least one backup"
    assert state.oldest_backup_at is not None
    assert state.newest_backup_at is not None
    assert state.oldest_backup_at <= state.newest_backup_at
    assert state.earliest_recoverable_at == state.oldest_backup_at


@requires_db
@requires_backup_node
def test_a_free_project_on_a_real_repository_is_refused_a_point_in_time(db_pool):
    """The whole slice, end to end, without running a restore.

    A project on a plan with no PITR entitlement, a real repository holding real
    backups, and a target well inside what the repository could physically
    deliver. The refusal is the plan's, and the repository is not the reason.
    """
    with db.connection() as conn:
        plan = db.one(
            conn,
            "INSERT INTO plans (code,name,config_json) VALUES ('pol-free','F',%s) RETURNING id",
            (Jsonb({"limits": {"backup_retention_days": 7, "pitr_window_hours": 0}}),),
        )["id"]
        org = db.one(
            conn,
            "INSERT INTO organizations (id, slug, display_name) VALUES (%s,'pol-org','Pol') "
            "RETURNING id",
            (uuid.uuid4(),),
        )["id"]
        project_id = uuid.uuid4()
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
            "VALUES (%s,%s,'pol00001','pol',%s,'PROVISIONED')",
            (project_id, org, plan),
        )
        conn.commit()

        window = restore.restore_window(
            conn, project_id=project_id, stanza=BACKUP_STANZA, run_as=RUN_AS
        )
        assert window.plan_code == "free"
        assert window.pitr_hours == 0
        assert window.has_backups, "the measurement cluster should hold at least one backup"

        refusal = window.refusal(datetime.now(UTC) - timedelta(minutes=5))
        assert refusal is not None
        assert "point-in-time" in refusal
        assert "oldest backup" not in refusal, "the repository is not why this was refused"


# `restore_tenant` never touches the node connection on the paths these tests
# reach -- the policy gate is upstream of it, and cluster creation is patched
# out below -- so passing None asserts that rather than hiding it behind a
# connection that would make a real one plausible.
_FAKE_ADMIN_CONN = None


def _project_on_a_node(conn, ref: str, node_name: str):
    """A project with a node, which is all `restore_tenant`'s bookkeeping needs."""
    plan = db.one(
        conn,
        "INSERT INTO plans (code,name,config_json) VALUES (%s,'P',%s) RETURNING id",
        (f"plan-{ref}", Jsonb({"limits": {"pitr_window_hours": 0}})),
    )["id"]
    org = db.one(
        conn,
        "INSERT INTO organizations (id, slug, display_name) VALUES (%s,%s,%s) RETURNING id",
        (uuid.uuid4(), f"org-{ref}", ref),
    )["id"]
    node_id = db.one(
        conn,
        "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, backup_stanza) "
        "VALUES (%s,%s,%s,'shared','active','maludb-bk') RETURNING id",
        (node_name, f"{node_name}.example", f"{node_name}.internal"),
    )["id"]
    project_id = uuid.uuid4()
    db.execute(
        conn,
        "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, node_id) "
        "VALUES (%s,%s,%s,%s,%s,'PROVISIONED',%s)",
        (project_id, org, ref, ref, plan, node_id),
    )
    conn.commit()
    return project_id, node_id


@requires_db
def test_restore_tenant_refuses_before_it_builds_anything(db_pool, monkeypatch):
    """The refusal happens before a cluster is created, not after.

    A policy checked after a 150-second scratch restore has already spent the
    resource it exists to bound. Asserted by making cluster creation explode:
    if the gate were downstream of it, this test would see the explosion
    instead of the refusal.
    """
    def _never(*args, **kwargs):
        raise AssertionError("the policy gate let a scratch cluster be created")

    monkeypatch.setattr(restore, "create_scratch_cluster", _never)

    with db.connection() as conn:
        project_id, node_id = _project_on_a_node(conn, "pol00002", "pol-node")
        with pytest.raises(restore.RestoreError, match="point-in-time"):
            restore.restore_tenant(
                conn,
                _FAKE_ADMIN_CONN,
                project_id=project_id,
                project_ref="pol00002",
                node_id=node_id,
                stanza="maludb-bk",
                target_time=datetime.now(UTC) - timedelta(minutes=5),
                window=_window(plan_code="free", retention_days=7, pitr_hours=0),
            )

        assert restore.history(conn, project_id=project_id) == [], (
            "a refused restore must not leave a row saying one was attempted"
        )


@requires_db
def test_the_operator_override_proceeds_past_a_refusal(db_pool, monkeypatch):
    """An incident is a real reason to restore past what a plan sold.

    Asserted by getting *past* the gate rather than by completing a restore: the
    run then fails on the sentinel below, which is a different failure and is
    the proof that the policy was not the thing that stopped it.
    """
    def _boom(*args, **kwargs):
        raise restore.RestoreError("sentinel: the gate was passed")

    monkeypatch.setattr(restore, "create_scratch_cluster", _boom)

    with db.connection() as conn:
        project_id, node_id = _project_on_a_node(conn, "pol00003", "pol-node3")
        outcome = restore.restore_tenant(
            conn,
            _FAKE_ADMIN_CONN,
            project_id=project_id,
            project_ref="pol00003",
            node_id=node_id,
            stanza="maludb-bk",
            target_time=datetime.now(UTC) - timedelta(minutes=5),
            window=_window(plan_code="free", retention_days=7, pitr_hours=0),
            beyond_entitlement=True,
        )

    assert outcome.status == "failed", "the override does not make a restore succeed"
    assert "sentinel" in (outcome.error or ""), outcome.error
    assert "point-in-time" not in (outcome.error or ""), (
        "the override was meant to get past the policy, and it did"
    )
