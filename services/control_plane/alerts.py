"""Telling an operator that something is wrong (free slice 14).

Until this existed, nothing did. A maintenance pass that failed wrote its count to
`maintenance_runs` and its reasons to journald; a node that stopped reporting left a stale
`nodes.last_health_at`; `deploy preflight` said both, and preflight is something a person runs.
On a deployment nobody had signed up to, that was a reasonable place to stop. On one taking
public signups it means the customer notices first, which is the wrong order.

## What it watches, and what it refuses to watch

Three questions, deliberately: **is the pass running and succeeding**, **are the nodes
reporting**, and **can this host still write**. The first two are answered from the control plane's
own tables and the third from its own filesystem, so this opens no connection to a node and cannot
be made slow by one being down -- the same rule `maintenance.check_backups` follows for the same
reason.

It does not watch anything it would have to guess about. Latency and error rates are worth
alerting on and neither is recorded yet; a condition invented here out of a number nobody measures
would be a false alarm with a schedule.

**Disk was on that list and came off it on 2026-09-29**, when a 19.6 GB PostgreSQL log filled a
control plane's root filesystem and stopped it. What this slice originally declined was a disk
*trajectory*, which needs history nothing keeps -- and that is still declined. Free space and the
size of one file are neither guessed nor derived: they are two `stat` calls on this host, which is
the same standard the other two conditions meet. `host_disk` does the measuring, shared with
`deploy preflight` so the alert and the check cannot disagree about what "too full" means.

## One row per condition, not per notification

`operator_alerts` is keyed on a fingerprint -- `maintenance-stalled`, `node-health:node-01` --
because a five-minute timer that mailed every firing would send 288 messages a day for one
broken thing, and an alert nobody can silence is an alert everybody filters. A condition is
mailed when it appears, re-mailed every `renotify` hours while it persists, and mailed once more
when it clears, because "this fixed itself at 04:12 after eleven hours" is what an operator
wants at breakfast.

## The limit, stated rather than discovered

**Nothing here notices its own silence.** If this timer stops, or the control-plane host dies,
no alert is sent and the absence looks exactly like health. Closing that needs something
outside this deployment watching a heartbeat, which the platform does not have and which this
slice does not pretend to be. It is in `docs/OPEN-QUESTIONS.md` as the next step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from services.control_plane import db, host_disk, mail, maintenance

log = logging.getLogger("maludb.alerts")

#: A pass is late rather than absent for this long. Fifteen minutes is preflight's own bound
#: (`deploy preflight` fails a deployment whose pass has not finished in fifteen), so the alert and
#: the check cannot disagree about when the pass has stopped.
PASS_STALE_MINUTES = 15

#: A node reports every minute; the console calls five minutes stale. Alerting at five would mail
#: an operator for one missed reporter run, so this waits three windows -- long enough to be a
#: problem, short enough to matter.
HEALTH_STALE_MINUTES = 15

#: How long a condition goes un-repeated. Short enough that a morning's alert is not yesterday's,
#: long enough that an overnight outage is a handful of messages rather than a mailbox.
RENOTIFY_HOURS = 6


class AlertsUnavailable(RuntimeError):
    """No operator address, or no sender: an operator's problem, reported as one."""


@dataclass(frozen=True)
class Condition:
    """Something wrong, named so the same thing twice is the same row."""

    fingerprint: str
    kind: str
    subject: str
    detail: str = ""


@dataclass
class Report:
    """What a run found and what it did about it."""

    open: list[str]
    sent: list[str]
    resolved: list[str]
    suppressed: list[str]

    def __str__(self) -> str:
        return (f"{len(self.open)} open, {len(self.sent)} sent, {len(self.resolved)} resolved, "
                f"{len(self.suppressed)} already reported")


def _now() -> datetime:
    return datetime.now(UTC)


def maintenance_conditions(conn: psycopg.Connection, *, now: datetime | None = None,
                           stale_minutes: int = PASS_STALE_MINUTES) -> list[Condition]:
    """The pass: has it finished recently, and did the last one succeed?

    Both, not either. A pass that has stopped running and a pass that runs and fails look
    identical from outside -- nothing changes -- and they need different fixes: the first is a
    timer or a host, the second is whatever the pass is complaining about.
    """
    now = now or _now()
    out: list[Condition] = []

    last = db.one(
        conn,
        "SELECT started_at, finished_at, passes, failed FROM maintenance_runs "
        " WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
    )
    if last is None:
        out.append(Condition(
            fingerprint="maintenance-never-run",
            kind="maintenance",
            subject="The maintenance pass has never finished on this deployment",
            detail=("Nothing applies a purchase, measures storage or ends failed-payment grace "
                    "until it runs (ADR-053). Check maludb-maintenance.timer."),
        ))
        return out

    age = (now - last["finished_at"]).total_seconds() / 60
    if age > stale_minutes:
        out.append(Condition(
            fingerprint="maintenance-stalled",
            kind="maintenance",
            subject=f"The maintenance pass has not finished for {int(age)} minutes",
            detail=(f"Last finished {last['finished_at'].isoformat(timespec='seconds')}. The pass "
                    "applies purchases, measures storage and ends grace periods; while it is not "
                    "running none of that happens and every other route still answers. "
                    "Check maludb-maintenance.timer and its journal."),
        ))
    elif last["failed"]:
        # The pass records counts, not its notes. What it complained about is re-derived here from
        # the checks that need no node connection, so the mail says something actionable rather
        # than a number and an instruction to go and read a journal.
        detail = [f"{last['failed']} of {last['passes']} passes failed at "
                  f"{last['finished_at'].isoformat(timespec='seconds')}."]
        backups = maintenance.check_backups(conn)
        detail.extend(f"backups: {line}" for line in backups.detail)
        for node in maintenance.unenforced_capacity(conn):
            detail.append(f"capacity: node {node['name']} is over a ceiling: {node['reason']}")
        detail.append("journalctl -u maludb-maintenance -n 200 has the rest.")
        out.append(Condition(
            fingerprint="maintenance-failing",
            kind="maintenance",
            subject=f"The maintenance pass reported {last['failed']} failing pass(es)",
            detail="\n".join(detail),
        ))
    return out


def node_conditions(conn: psycopg.Connection, *, now: datetime | None = None,
                    stale_minutes: int = HEALTH_STALE_MINUTES) -> list[Condition]:
    """Nodes: reporting, and active.

    A node's own reporter writes `last_health_at` every minute. Silence there is the platform's
    only sign that a node is gone, because nothing else on the control plane talks to a node
    between provisioning operations -- so this is the check that turns a dead node into a message
    rather than into a customer's support ticket.
    """
    now = now or _now()
    out: list[Condition] = []
    rows = db.query(
        conn,
        "SELECT name, status, last_health_at FROM nodes WHERE status <> 'retired' ORDER BY name",
    )
    for row in rows:
        name = row["name"]
        if row["last_health_at"] is None:
            out.append(Condition(
                fingerprint=f"node-health:{name}",
                kind="node_health",
                subject=f"Node {name} has never reported health",
                detail="maludb-node-reporter has not written once. Check it on the node.",
            ))
        else:
            age = (now - row["last_health_at"]).total_seconds() / 60
            if age > stale_minutes:
                out.append(Condition(
                    fingerprint=f"node-health:{name}",
                    kind="node_health",
                    subject=f"Node {name} has not reported health for {int(age)} minutes",
                    detail=(f"Last report {row['last_health_at'].isoformat(timespec='seconds')}. "
                            "Every tenant database and file on that node is on one host; placement "
                            "will keep using it until its status changes. Check the node, then "
                            "maludb-node-reporter on it."),
                ))
        if row["status"] != "active":
            out.append(Condition(
                fingerprint=f"node-status:{name}",
                kind="node_status",
                subject=f"Node {name} is {row['status']}, not active",
                detail=("Placement will not put new projects here. If that was not deliberate, "
                        "`cp-manage node list` and the node's journal say why."),
            ))
    return out


def disk_conditions() -> list[Condition]:
    """This host's disk: is there room, and is one log file eating it?

    The only condition here that reads no table. The others ask the control-plane database what it
    recorded; this one asks the filesystem, because a disk that is filling leaves no row anywhere --
    which is precisely how 2026-09-29 reached 100% with every dashboard green.

    Two conditions, matching `deploy preflight`'s two findings, because they clear at different
    times: freeing space does not fix a rotation that cannot run, and fixing rotation does not by
    itself give the space back.

    A directory that cannot be read is **not** a condition. The unit runs as `maludb-cp` and the log
    files are `postgres:adm 640`, so sizes are readable through the world-executable directory but a
    stricter host could refuse -- and an alert an operator cannot act on, arriving every six hours,
    is how a mailbox learns to filter this sender. `deploy preflight` says so instead, where somebody
    is already reading.
    """
    out: list[Condition] = []
    floor = host_disk.free_min_percent()
    for fs in host_disk.filesystems():
        if fs.percent_free >= floor:
            continue
        out.append(Condition(
            # By path, not by what it holds: two full filesystems are two problems, and the same one
            # found twice is one.
            fingerprint=f"disk-headroom:{fs.path}",
            kind="disk_headroom",
            subject=f"{fs.short()} on the control plane",
            detail=(f"{fs.describe()}, below the {floor:.0f}% floor.\n\n"
                    "A control plane that cannot write stops answering rather than degrading: "
                    "PostgreSQL rejects connections mid-recovery and every unit above it fails on a "
                    "pool timeout that names no disk. Check `df -h /` and the size of "
                    f"{host_disk.log_dir()} first -- on 2026-09-29 a single log file was the whole of "
                    "it (docs/DEPLOYMENT.md 1.1a)."),
        ))

    survey = host_disk.survey_logs()
    bound = host_disk.log_file_max_bytes()
    if survey.problem is None and survey.largest_bytes > bound:
        out.append(Condition(
            fingerprint=f"log-unrotated:{survey.directory}",
            kind="log_rotation",
            subject=(f"{survey.largest_name} is {survey.largest_bytes / 1024**3:.1f} GiB; "
                     "PostgreSQL log rotation is not running"),
            detail=(f"{survey.describe()}, above the {bound / 1024**2:.0f} MiB bound.\n\n"
                    "This is the cause rather than the symptom, and it is worth fixing before the "
                    "disk is the problem: a `copytruncate` rotation needs as much free space as the "
                    "file, so an unrotated log gets harder to rotate the longer it is left. Check "
                    "that /etc/logrotate.d/postgresql-common is daily with `maxsize` and "
                    "`su postgres postgres`, and that `logrotate -f` on it succeeds "
                    "(docs/DEPLOYMENT.md 1.1a)."),
        ))
    return out


def evaluate(conn: psycopg.Connection, *, now: datetime | None = None,
             pass_stale_minutes: int = PASS_STALE_MINUTES,
             health_stale_minutes: int = HEALTH_STALE_MINUTES) -> list[Condition]:
    """Every condition, from the control plane alone -- its own tables and its own disk."""
    now = now or _now()
    return [
        *maintenance_conditions(conn, now=now, stale_minutes=pass_stale_minutes),
        *node_conditions(conn, now=now, stale_minutes=health_stale_minutes),
        # Last, and from the filesystem rather than the database. A disk that is filling is the one
        # condition here that would also stop the other two being recorded at all.
        *disk_conditions(),
    ]


def _compose(condition: Condition, *, repeat_of: datetime | None) -> mail.Message:
    since = ""
    if repeat_of is not None:
        since = f"\nOpen since {repeat_of.isoformat(timespec='seconds')}.\n"
    text = (f"{condition.subject}\n\n{condition.detail}\n{since}\n"
            "-- MaluDB platform alerts. This message is sent when a condition appears, every "
            f"{RENOTIFY_HOURS} hours while it persists, and once when it clears.\n")
    html = (f"<p><strong>{_escape(condition.subject)}</strong></p>"
            f"<pre>{_escape(condition.detail)}</pre>"
            + (f"<p>Open since {_escape(repeat_of.isoformat(timespec='seconds'))}.</p>"
               if repeat_of is not None else ""))
    prefix = "[MaluDB]" if repeat_of is None else "[MaluDB, still]"
    return mail.Message(subject=f"{prefix} {condition.subject}", text=text, html=html)


def _compose_resolved(row: dict) -> mail.Message:
    opened = row["first_seen_at"].isoformat(timespec="seconds")
    lasted = row["resolved_at"] - row["first_seen_at"]
    text = (f"Resolved: {row['subject']}\n\nOpen from {opened} for {_duration(lasted)}, "
            f"after {row['sends']} message(s).\n")
    html = (f"<p><strong>Resolved:</strong> {_escape(row['subject'])}</p>"
            f"<p>Open from {_escape(opened)} for {_escape(_duration(lasted))}.</p>")
    return mail.Message(subject=f"[MaluDB, resolved] {row['subject']}", text=text, html=html)


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def run(conn: psycopg.Connection, *, config, client: mail.MaluMail | None = None,
        now: datetime | None = None, send: bool = True) -> Report:
    """Evaluate, record, and mail what has not been mailed recently.

    The recording happens whether or not the mail does: a send that fails must not lose the fact
    that the condition was seen, or the next run treats an hours-old problem as new.
    """
    now = now or _now()
    recipients = operator_recipients(config)
    if send and not recipients:
        raise AlertsUnavailable("no operator alert address is configured (MALUDB_OPERATOR_ALERT_EMAIL)")
    if send and (not config.platform_email_from or not config.malumail_api_key):
        raise AlertsUnavailable("no platform sender is configured, so no alert can be delivered")

    conditions = evaluate(
        conn, now=now,
        pass_stale_minutes=getattr(config, "alert_pass_stale_minutes", PASS_STALE_MINUTES),
        health_stale_minutes=getattr(config, "alert_health_stale_minutes", HEALTH_STALE_MINUTES),
    )
    by_fingerprint = {condition.fingerprint: condition for condition in conditions}
    renotify = timedelta(hours=getattr(config, "alert_renotify_hours", RENOTIFY_HOURS))
    report = Report(open=[], sent=[], resolved=[], suppressed=[])
    sender = client or (mail.MaluMail(config.malumail_api_key) if send else None)

    open_rows = {
        row["fingerprint"]: row
        for row in db.query(
            conn,
            "SELECT id, fingerprint, kind, subject, detail, first_seen_at, last_sent_at, sends "
            "  FROM operator_alerts WHERE resolved_at IS NULL",
        )
    }

    for fingerprint, condition in by_fingerprint.items():
        report.open.append(fingerprint)
        row = open_rows.get(fingerprint)
        if row is None:
            # `now` rather than the column defaults, so "open for 40 minutes" is measured against
            # the same clock that decided it was open -- and so a test can pin both ends of it.
            row = db.one(
                conn,
                "INSERT INTO operator_alerts (fingerprint, kind, subject, detail, first_seen_at, "
                "                             last_seen_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, first_seen_at, last_sent_at, sends",
                (condition.fingerprint, condition.kind, condition.subject, condition.detail, now, now),
            )
            due, repeat_of = True, None
        else:
            db.execute(
                conn,
                "UPDATE operator_alerts SET last_seen_at = %s, subject = %s, detail = %s WHERE id = %s",
                (now, condition.subject, condition.detail, row["id"]),
            )
            last_sent = row["last_sent_at"]
            due = last_sent is None or (now - last_sent) >= renotify
            repeat_of = row["first_seen_at"]
        conn.commit()

        if not due:
            report.suppressed.append(fingerprint)
            continue
        if not send:
            report.sent.append(fingerprint)
            continue
        if _deliver(sender, config, recipients, _compose(condition, repeat_of=repeat_of)):
            db.execute(
                conn,
                "UPDATE operator_alerts SET last_sent_at = %s, sends = sends + 1 WHERE id = %s",
                (now, row["id"]),
            )
            conn.commit()
            report.sent.append(fingerprint)

    for fingerprint, row in open_rows.items():
        if fingerprint in by_fingerprint:
            continue
        db.execute(conn, "UPDATE operator_alerts SET resolved_at = %s WHERE id = %s", (now, row["id"]))
        conn.commit()
        report.resolved.append(fingerprint)
        if not send or not row["sends"]:
            # Never mailed, so its clearing is not news. Recorded, not sent.
            db.execute(conn, "UPDATE operator_alerts SET resolved_sent = TRUE WHERE id = %s", (row["id"],))
            conn.commit()
            continue
        resolved = db.one(
            conn,
            "SELECT subject, first_seen_at, resolved_at, sends FROM operator_alerts WHERE id = %s",
            (row["id"],),
        )
        if _deliver(sender, config, recipients, _compose_resolved(resolved)):
            db.execute(conn, "UPDATE operator_alerts SET resolved_sent = TRUE WHERE id = %s", (row["id"],))
            conn.commit()

    return report


def _deliver(sender: mail.MaluMail, config, recipients: list[str], message: mail.Message) -> bool:
    """Send to each operator address. A failure is logged and reported, never raised.

    One unreachable address must not stop the others, and a send failure must not lose the
    recording: an alert that raised here would be re-evaluated as new on the next run and mail
    the same thing again the moment the provider recovered.
    """
    delivered = False
    for address in recipients:
        try:
            sender.send(
                sender=config.platform_email_from,
                sender_name=config.platform_email_from_name,
                to=address,
                message=message,
            )
            delivered = True
        except Exception:  # noqa: BLE001 - every provider failure, reported not raised
            log.exception("an operator alert could not be delivered")
    return delivered


def operator_recipients(config) -> list[str]:
    """The operator addresses, from `MALUDB_OPERATOR_ALERT_EMAIL` (comma-separated)."""
    raw = getattr(config, "operator_alert_email", "") or ""
    return [address.strip() for address in raw.split(",") if address.strip()]


def open_alerts(conn: psycopg.Connection) -> list[dict]:
    return db.query(
        conn,
        "SELECT fingerprint, kind, subject, first_seen_at, last_sent_at, sends FROM operator_alerts "
        " WHERE resolved_at IS NULL ORDER BY first_seen_at",
    )


def recent_alerts(conn: psycopg.Connection, *, limit: int = 20) -> list[dict]:
    return db.query(
        conn,
        "SELECT fingerprint, kind, subject, first_seen_at, resolved_at, sends FROM operator_alerts "
        " ORDER BY first_seen_at DESC LIMIT %s",
        (limit,),
    )
