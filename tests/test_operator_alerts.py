"""Telling an operator something is wrong, once (free slice 14).

Nothing alerted anybody. A failing maintenance pass wrote its count to `maintenance_runs` and its
reasons to journald; a node that stopped reporting left a stale `nodes.last_health_at`; `deploy
preflight` said both and preflight is something a person runs. On a deployment nobody had signed up
to that was a reasonable stopping point. Signups opened on 2026-09-29, which made "the customer
finds out first" the actual on-call policy.

What is held here:

- **the three questions it answers**, from the control plane alone: is the pass running and
  succeeding, are the nodes reporting, and can this host still write. It opens no node connection,
  so the outage it reports cannot make it slow;
- **a full disk is a condition and an unreadable log directory is not**: one is actionable from the
  message, the other is an alert every six hours that nobody can do anything about, which is how a
  mailbox learns to filter this sender;
- **one message per condition, not per firing**: a five-minute timer that mailed every run would
  send 288 messages a day for one broken thing, and an alert nobody can silence is one everybody
  filters;
- **a condition that clears says so**, once, and only if it was ever announced;
- **a send failure does not lose the record**, or an hours-old problem is treated as new the moment
  the provider recovers;
- **nothing is delivered without an operator address**, and that refusal is loud rather than a
  silent no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from services.control_plane import alerts, db, mail
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)


@dataclass
class FakeConfig:
    """Only what `alerts.run` reads."""

    operator_alert_email: str = "ops@example.com"
    platform_email_from: str = "noreply@example.com"
    platform_email_from_name: str | None = "MaluDB"
    malumail_api_key: str | None = "k"
    alert_renotify_hours: int = 6
    alert_pass_stale_minutes: int = 15
    alert_health_stale_minutes: int = 15


@dataclass
class FakeSender:
    sent: list[tuple[str, mail.Message]] = field(default_factory=list)
    fail: bool = False

    def send(self, *, sender, sender_name, to, message):  # noqa: ARG002 - the client's shape
        if self.fail:
            raise mail.MailError("the provider is unhappy")
        self.sent.append((to, message))
        return {"id": "x"}


def _pass_finished(conn, *, minutes_ago: int, failed: int = 0, now: datetime = NOW) -> None:
    finished = now - timedelta(minutes=minutes_ago)
    db.execute(
        conn,
        "INSERT INTO maintenance_runs (started_at, finished_at, passes, failed) VALUES (%s, %s, 9, %s)",
        (finished - timedelta(seconds=20), finished, failed),
    )
    conn.commit()


def _node(conn, *, name: str, health_minutes_ago: int | None, status: str = "active",
          now: datetime = NOW) -> None:
    health = None if health_minutes_ago is None else now - timedelta(minutes=health_minutes_ago)
    db.execute(
        conn,
        "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
        "VALUES (%s, %s, %s, 'shared', %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status, "
        "  last_health_at = EXCLUDED.last_health_at",
        (name, f"{name}.example", f"{name}.internal", status, health),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _host_disk_out_of_the_way(tmp_path, monkeypatch):
    """Point the disk conditions at an empty directory with an unreachable floor.

    `disk_conditions` reads a real filesystem, so without this a developer's full disk -- or a CI
    runner's -- would add a condition to tests that count messages, and they would fail for a reason
    that is not about them. The cases below that exercise it override this.
    """
    empty = tmp_path / "pglog"
    empty.mkdir()
    monkeypatch.setenv("MALUDB_PREFLIGHT_LOG_DIR", str(empty))
    monkeypatch.setenv("MALUDB_CONTROL_PLANE_BACKUP_DIR", str(empty))
    monkeypatch.setenv("MALUDB_DISK_FREE_MIN_PERCENT", "0")


@pytest.fixture
def clean(db_pool):  # noqa: ARG001 - db_pool prepares the database
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM operator_alerts")
        db.execute(conn, "DELETE FROM maintenance_runs")
        db.execute(conn, "DELETE FROM nodes")
        conn.commit()
    return None


def test_a_pass_that_has_stopped_and_a_pass_that_fails_are_different_conditions(clean):  # noqa: ARG001
    """They look identical from outside -- nothing changes -- and they need different fixes."""
    with db.connection() as conn:
        assert [c.fingerprint for c in alerts.evaluate(conn, now=NOW)] == ["maintenance-never-run"]

        _pass_finished(conn, minutes_ago=40)
        assert [c.fingerprint for c in alerts.evaluate(conn, now=NOW)] == ["maintenance-stalled"]

        _pass_finished(conn, minutes_ago=1, failed=2)
        conditions = alerts.evaluate(conn, now=NOW)
        assert [c.fingerprint for c in conditions] == ["maintenance-failing"]
        assert "2 of 9" in conditions[0].detail, "the count, so the mail says something"

        _pass_finished(conn, minutes_ago=0)
        assert alerts.evaluate(conn, now=NOW) == []


def test_a_node_that_stops_reporting_and_one_that_is_not_active_both_surface(clean):  # noqa: ARG001
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=1)
        _node(conn, name="node-a", health_minutes_ago=1)
        assert alerts.evaluate(conn, now=NOW) == [], "a reporting, active node is not news"

        _node(conn, name="node-a", health_minutes_ago=44)
        _node(conn, name="node-b", health_minutes_ago=None)
        _node(conn, name="node-c", health_minutes_ago=1, status="draining")
        fingerprints = [c.fingerprint for c in alerts.evaluate(conn, now=NOW)]
    assert fingerprints == ["node-health:node-a", "node-health:node-b", "node-status:node-c"]


def test_one_message_per_condition_then_silence_until_it_is_due_again(clean):  # noqa: ARG001
    sender, cfg = FakeSender(), FakeConfig()
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=40)

        first = alerts.run(conn, config=cfg, client=sender, now=NOW)
        assert first.sent == ["maintenance-stalled"] and len(sender.sent) == 1
        assert sender.sent[0][0] == "ops@example.com"
        assert sender.sent[0][1].subject.startswith("[MaluDB] ")

        # Five minutes later, the same condition: recorded again, mailed not at all.
        second = alerts.run(conn, config=cfg, client=sender, now=NOW + timedelta(minutes=5))
        assert second.suppressed == ["maintenance-stalled"] and second.sent == []
        assert len(sender.sent) == 1, "a five-minute timer must not mail every firing"

        # Past the renotify window it repeats, and says it is a repeat.
        third = alerts.run(conn, config=cfg, client=sender, now=NOW + timedelta(hours=7))
        assert third.sent == ["maintenance-stalled"] and len(sender.sent) == 2
        assert "still" in sender.sent[1][1].subject
        assert "Open since" in sender.sent[1][1].text


def test_a_condition_that_clears_is_announced_once(clean):  # noqa: ARG001
    sender, cfg = FakeSender(), FakeConfig()
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=40)
        alerts.run(conn, config=cfg, client=sender, now=NOW)

        _pass_finished(conn, minutes_ago=0, now=NOW + timedelta(minutes=5))
        later = NOW + timedelta(minutes=5)
        report = alerts.run(conn, config=cfg, client=sender, now=later)
        assert report.resolved == ["maintenance-stalled"] and report.open == []
        assert "resolved" in sender.sent[-1][1].subject.lower()
        assert "for 5 minute(s)" in sender.sent[-1][1].text, "how long it was open is the useful part"

        again = alerts.run(conn, config=cfg, client=sender, now=later + timedelta(minutes=5))
        assert again.resolved == [] and len(sender.sent) == 2, "a resolution is announced once"


def test_a_condition_never_announced_is_not_announced_when_it_clears(clean):  # noqa: ARG001
    """Otherwise a blip inside one firing produces a resolution notice for something nobody heard
    about -- which is how an operator learns to ignore the channel."""
    sender, cfg = FakeSender(), FakeConfig()
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=40)
        alerts.run(conn, config=cfg, client=sender, now=NOW, send=False)  # recorded, never mailed
        _pass_finished(conn, minutes_ago=0, now=NOW + timedelta(minutes=5))
        report = alerts.run(conn, config=cfg, client=sender, now=NOW + timedelta(minutes=5))
    assert report.resolved == ["maintenance-stalled"]
    assert sender.sent == [], "nothing was ever sent, so its clearing is not news"


def test_a_send_failure_keeps_the_record_and_retries_next_run(clean):  # noqa: ARG001
    """A provider outage must not turn an hours-old condition into a new one the moment it lifts."""
    failing, cfg = FakeSender(fail=True), FakeConfig()
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=40)
        report = alerts.run(conn, config=cfg, client=failing, now=NOW)
        assert report.open == ["maintenance-stalled"] and report.sent == []
        row = db.one(conn, "SELECT sends, last_sent_at, first_seen_at FROM operator_alerts")
        assert row["sends"] == 0 and row["last_sent_at"] is None
        first_seen = row["first_seen_at"]

        working = FakeSender()
        alerts.run(conn, config=cfg, client=working, now=NOW + timedelta(minutes=5))
        row = db.one(conn, "SELECT sends, first_seen_at FROM operator_alerts")
    assert len(working.sent) == 1 and row["sends"] == 1
    assert row["first_seen_at"] == first_seen, "the same condition, not a new one"


def test_nothing_is_sent_without_an_operator_address_and_the_refusal_is_loud(clean):  # noqa: ARG001
    with db.connection() as conn, pytest.raises(alerts.AlertsUnavailable, match="MALUDB_OPERATOR_ALERT_EMAIL"):
        alerts.run(conn, config=FakeConfig(operator_alert_email=""), client=FakeSender(), now=NOW)

    with db.connection() as conn, pytest.raises(alerts.AlertsUnavailable, match="no platform sender"):
        alerts.run(conn, config=FakeConfig(platform_email_from=""), client=FakeSender(), now=NOW)


def test_every_recipient_is_tried_and_one_failure_does_not_stop_the_others(clean):  # noqa: ARG001
    sender = FakeSender()
    cfg = FakeConfig(operator_alert_email="one@example.com, two@example.com")
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=40)
        alerts.run(conn, config=cfg, client=sender, now=NOW)
    assert [to for to, _ in sender.sent] == ["one@example.com", "two@example.com"]


def test_no_alert_carries_anything_a_customer_owns(clean):  # noqa: ARG001
    """These messages leave the deployment. A node name and a count are operational; a project ref,
    an address or a credential would be a customer's, and none belongs in an operator's inbox."""
    sender, cfg = FakeSender(), FakeConfig()
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=1, failed=1)
        _node(conn, name="node-a", health_minutes_ago=99)
        alerts.run(conn, config=cfg, client=sender, now=NOW)
    body = "\n".join(message.text + message.html for _, message in sender.sent)
    for forbidden in ("postgresql://", "mldb_", "@maludb.org", "password", "secret"):
        assert forbidden not in body, f"an alert carried {forbidden}"


# -- the disk ---------------------------------------------------------------
#
# Slice 14 deliberately left disk out, on the grounds that a "disk trajectory" needs history nothing
# keeps. On 2026-09-29 a 19.6 GB PostgreSQL log filled a control plane's root filesystem and stopped
# it, with every table this module reads looking exactly as it should. Free space and one file's size
# are not a trajectory and not a guess -- they are two `stat` calls -- so they are here. The
# trajectory is still absent.


def _disk(tmp_path, monkeypatch, *, floor: str = "0", max_mb: str | None = None) -> None:
    monkeypatch.setenv("MALUDB_PREFLIGHT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MALUDB_CONTROL_PLANE_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("MALUDB_DISK_FREE_MIN_PERCENT", floor)
    if max_mb is not None:
        monkeypatch.setenv("MALUDB_LOG_FILE_MAX_MB", max_mb)


def test_a_full_filesystem_is_one_condition_per_filesystem(tmp_path, monkeypatch):
    """A floor of 100% is a full disk without needing one. Both watched paths are the same
    filesystem here, which is the default install -- and it must read as one problem, not two."""
    _disk(tmp_path, monkeypatch, floor="100")
    found = alerts.disk_conditions()
    assert len(found) == 1, [c.fingerprint for c in found]
    condition = found[0]
    assert condition.kind == "disk_headroom"
    assert condition.fingerprint == f"disk-headroom:{tmp_path}"
    assert "% full" in condition.subject and "GiB free" in condition.subject
    assert "names no disk" in condition.detail, "the detail should say how a full disk presents"


def test_an_unrotated_log_is_a_condition_while_there_is_still_room(tmp_path, monkeypatch):
    """The cause, separately from the symptom: fixing rotation and freeing space are different
    jobs, and this one is cheap only while the disk is still fine."""
    (tmp_path / "postgresql-17-main.log").write_bytes(b"x" * 4096)
    _disk(tmp_path, monkeypatch, floor="0", max_mb="0.001")
    found = alerts.disk_conditions()
    assert [c.kind for c in found] == ["log_rotation"], "the disk has room; only rotation is wrong"
    condition = found[0]
    assert condition.fingerprint == f"log-unrotated:{tmp_path}"
    assert "postgresql-17-main.log" in condition.subject
    assert "su postgres postgres" in condition.detail and "maxsize" in condition.detail, (
        "an alert an operator cannot act on is an alert they filter"
    )


def test_a_full_disk_and_an_unrotated_log_are_separate_conditions(tmp_path, monkeypatch):
    """They clear at different times, so one row each."""
    (tmp_path / "postgresql-17-main.log").write_bytes(b"x" * 4096)
    _disk(tmp_path, monkeypatch, floor="100", max_mb="0.001")
    kinds = sorted(c.kind for c in alerts.disk_conditions())
    assert kinds == ["disk_headroom", "log_rotation"]


def test_a_compressed_archive_is_rotation_working(tmp_path, monkeypatch):
    (tmp_path / "postgresql-17-main.log").write_bytes(b"x" * 16)
    (tmp_path / "postgresql-17-main.log.2.gz").write_bytes(b"x" * 8192)
    _disk(tmp_path, monkeypatch, floor="0", max_mb="0.001")
    assert alerts.disk_conditions() == []


def test_a_log_directory_that_cannot_be_read_is_not_an_alert(tmp_path, monkeypatch):
    """Deliberate. The unit runs as maludb-cp against postgres-owned files, and a stricter host could
    refuse; an unactionable message every six hours teaches a mailbox to filter this sender.
    `deploy preflight` reports it instead, where somebody is already reading."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "postgresql-17-main.log").write_bytes(b"x" * 4096)
    locked.chmod(0o000)
    _disk(tmp_path, monkeypatch, floor="0", max_mb="0.001")
    monkeypatch.setenv("MALUDB_PREFLIGHT_LOG_DIR", str(locked))
    try:
        assert alerts.disk_conditions() == []
    finally:
        locked.chmod(0o700)


def test_an_absent_log_directory_is_not_an_alert(tmp_path, monkeypatch):
    """PostgreSQL may be on another host. Nothing to say, so nothing is said."""
    _disk(tmp_path, monkeypatch, floor="0", max_mb="0.001")
    monkeypatch.setenv("MALUDB_PREFLIGHT_LOG_DIR", str(tmp_path / "absent"))
    assert alerts.disk_conditions() == []


def test_a_disk_condition_is_mailed_once_and_announces_its_own_resolution(clean, tmp_path, monkeypatch):  # noqa: ARG001
    """It goes through the same machinery as the other two: one message, then silence, then a
    resolution when the operator has fixed it."""
    log = tmp_path / "postgresql-17-main.log"
    log.write_bytes(b"x" * 4096)
    _disk(tmp_path, monkeypatch, floor="0", max_mb="0.001")
    sender, cfg = FakeSender(), FakeConfig()

    with db.connection() as conn:
        # A healthy pass, so the disk is the only thing wrong and the counts below are about it.
        _pass_finished(conn, minutes_ago=1)
        first = alerts.run(conn, config=cfg, client=sender, now=NOW)
        second = alerts.run(conn, config=cfg, client=sender, now=NOW + timedelta(minutes=5))
    assert len(first.sent) == 1 and second.sent == [], "a five-minute timer must not mail every run"
    assert "log rotation is not running" in sender.sent[0][1].subject

    log.write_bytes(b"x" * 16)          # rotation now works
    with db.connection() as conn:
        third = alerts.run(conn, config=cfg, client=sender, now=NOW + timedelta(hours=1))
    assert len(third.resolved) == 1
    assert "resolved" in sender.sent[-1][1].subject.lower()


def test_a_disk_alert_carries_no_customer_data(clean, tmp_path, monkeypatch):  # noqa: ARG001
    """Same rule as the other conditions. A path and a size are operational; this reads sizes and
    never opens a log, so audited SQL cannot reach an operator's inbox through it."""
    (tmp_path / "postgresql-17-main.log").write_bytes(b"SELECT * FROM customers -- secret")
    _disk(tmp_path, monkeypatch, floor="100", max_mb="0.00001")
    sender, cfg = FakeSender(), FakeConfig()
    with db.connection() as conn:
        _pass_finished(conn, minutes_ago=1)
        alerts.run(conn, config=cfg, client=sender, now=NOW)
    body = "\n".join(message.text + message.html for _, message in sender.sent)
    assert body, "the conditions should have fired"
    assert len(sender.sent) == 2, [m.subject for _, m in sender.sent]
    for forbidden in ("postgresql://", "mldb_", "SELECT", "secret", "password"):
        assert forbidden not in body, f"a disk alert carried {forbidden}"
