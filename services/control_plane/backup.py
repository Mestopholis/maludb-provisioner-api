"""Node backup preconditions, backup execution, and what the platform believes.

Phase 11 slice 1. Two ADRs live in this file.

**ADR-067** names pgBackRest, and names it because of a question that had two
bad answers available. Every node carries ADR-031's `host replication all <cidr>
reject`, after a non-superuser holding `REPLICATION` took a 484 MB physical copy
of every database on a cluster it held `CONNECT` on exactly one of. That line is
also what `pg_basebackup` needs. So either the platform could not take a
physical backup, or the control that keeps one tenant out of every other
tenant's bytes had to be narrowed to let a backup through.

Neither. pgBackRest copies the data directory between `pg_backup_start()` and
`pg_backup_stop()` over an ordinary libpq connection and opens no replication
connection at all -- `0` walsenders during a backup, measured, on a cluster
where `pg_basebackup` is refused for the superuser. **No node configuration is
relaxed for backup.**

**ADR-064.** A repository in the same failure domain as the data is not a
backup. In production that is a hard failure here; in development it is recorded
and reported, because the measurement cluster slice 0 built deliberately puts
the repository on the same filesystem and that is a fixture rather than a
deployment.

Evidence and reproduction for all of it: `specs/backup-restore-model.md`.

## The two ways this fails without saying so

Both were found by running pgBackRest rather than reading about it, and neither
produces an error.

**A backup of an idle cluster waits forever.** pgBackRest's default is
`start-fast=n` -- begin after the next *regular* checkpoint -- and PostgreSQL
skips a timed checkpoint when no WAL has been written since the last one.
Measured: 15+ minutes at 0% CPU, `num_timed = 0` after forty minutes of uptime.
ADR-022 rests free-tier economics on projects that sleep, so a node full of
sleeping projects is a node writing no WAL, and its nightly backup hangs rather
than fails. Every backup this module starts passes `--start-fast`, and there is
no option to turn that off.

**A failing `archive_command` is invisible from the cluster.** WAL that cannot
be archived does not stop the postmaster; it accumulates in `pg_wal` and the
only symptom is a counter. A node in that state runs normally, serves every
tenant, and has no recoverable point in time after the archiver broke. So
`pg_stat_archiver` is part of readiness rather than part of monitoring.

## What this module deliberately does not claim

Nothing here is evidence that a restorable backup exists. These rows say what
the platform did and what pgBackRest reported. Only a restore proves a backup,
and that is slice 2. There is no `verified` column and no `verify()` that
returns true, because this slice has no honest way to set one -- and a green
field named `verified` is exactly how a backup system lies to its operator.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - pgBackRest is a command; there is no library
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import db

log = logging.getLogger("maludb.backup")


class BackupError(RuntimeError):
    """A backup could not be run, or a node has no repository to run it against."""


# How old the most recent completed backup may be before the verification pass
# calls it a failure. A default rather than a constant: `nodes.backup_max_age_hours`
# overrides it per node, because a node backed up hourly and a node backed up
# weekly are both legitimate deployments and AGENTS.md forbids hard-coding the
# answer in application logic.
#
# 26 rather than 24, so a nightly schedule that drifts by an hour does not
# report a failure every morning. The margin is deliberate and small: two hours
# is a late backup, and 24 hours exactly is a pass that depends on cron jitter.
DEFAULT_MAX_AGE_HOURS = 26

# And the same question for a *full* backup, which is the one a `diff` or `incr`
# chain is rooted on. A node whose last full is older than its retention window
# has a chain to nowhere. Weekly full with daily diffs is the shape this assumes.
DEFAULT_MAX_FULL_AGE_HOURS = 24 * 8

# A backup still `running` after this long is reported as failed rather than
# waited for. This is the idle-cluster hang above, and the number is not a
# timeout on the backup -- nothing here kills a running pgBackRest -- it is the
# point at which the platform stops calling an unfinished backup "in progress".
#
# Slice 0 measured a full backup of a 219.7 MB cluster at 105.9 s single-process
# and extrapolated ~40 minutes for a full node at `DEFAULT_MAX_PROJECTS = 200`.
# Six hours is well clear of that and well inside a nightly window.
STALE_RUNNING_HOURS = 6

# `wal_level = minimal` cannot produce a restorable base backup at all: it omits
# the WAL records a replay needs. `replica` is the floor; a node prepared for
# Realtime is already at `logical`, which is above it (ADR-031).
SUFFICIENT_WAL_LEVELS = ("replica", "logical")

# pgBackRest's backup types, in the order a restore consumes them.
BACKUP_TYPES = ("full", "diff", "incr")

# Stanza names and OS user names both reach a subprocess argv, so both are
# validated before they get there.
#
# Neither is customer input today: a stanza comes from an operator's `--stanza`
# or the node row, and the run-as user from an environment variable or a flag.
# They are validated anyway, and the reason is the rule in AGENTS.md about
# identifiers built from metadata -- it is a rule about habits. A stanza that
# reaches `--stanza=` as one argv element cannot inject a second option today,
# because `--stanza=x --repo1-path=y` is a single argument pgBackRest rejects as
# a bad name. The value that reaches `sudo -u` is the one that would actually
# matter if it ever became reachable, and "it is not reachable yet" is the
# assumption that stops being true without anyone editing this file.
#
# pgBackRest's own rule for a stanza name is alphanumeric plus `-`; POSIX
# portable usernames are alphanumeric plus `.`, `_` and `-`, not leading `-`.
_STANZA_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_RUN_AS_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._-]{0,31}\Z")


def checked_stanza(stanza: str) -> str:
    if not _STANZA_RE.match(stanza or ""):
        raise BackupError(
            f"invalid stanza name {stanza!r}; pgBackRest allows letters, digits and '-'"
        )
    return stanza


def checked_run_as(user: str) -> str:
    if not _RUN_AS_RE.match(user):
        raise BackupError(
            f"invalid run-as user {user!r}; this value reaches `sudo -u` and must be a "
            "plain user name"
        )
    return user


# The OS user pgBackRest runs as, or empty to run as whoever invoked this.
#
# **pgBackRest has to be the cluster's owner.** It reads the data directory
# directly and `/etc/pgbackrest.conf` is mode 0600 owned by `postgres` on a
# Debian/Ubuntu install, so any other user gets `unable to open file
# '/etc/pgbackrest.conf' for read: [13] Permission denied` -- an error that
# names a config file and not the actual cause, which is the wrong user.
#
# Root is not a way out of this: on this platform a root without
# CAP_DAC_OVERRIDE cannot read a file it does not own, which is the same lesson
# `scripts/backup-test-cluster.sh` records for writing them. Become the owner.
#
# Empty by default rather than `postgres`, because a control plane that is not
# on the node has no business shelling out to sudo, and a silent `sudo -u` in
# the default path is worse than an error that says what to do.
DEFAULT_RUN_AS = os.environ.get("MALUDB_BACKUP_RUN_AS", "").strip()


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryState:
    """What pgBackRest says about the repository, read on the node itself.

    Separate from the settings half because the two are read from different
    places and fail independently. Everything in `BackupReadiness` that comes
    from PostgreSQL can be read over libpq from anywhere; everything here needs
    pgBackRest and its configuration file, which live on the node.

    `reachable` is False when the command could not be run at all. That is not
    the same as a node with a broken repository and must not be reported as
    though it were -- the same distinction `realtime.NodeReadiness` draws for
    the physical-replication probe, and for the same reason.
    """

    reachable: bool
    detail: str
    # From `pgbackrest check`, which validates the configuration end to end and
    # forces a WAL segment through the archive_command. This is the one call
    # that proves archiving works rather than that it is configured.
    check_ok: bool | None = None
    check_detail: str = ""
    pg_path: str | None = None
    repo_path: str | None = None
    # Both halves of retention. Unset, pgBackRest warns on every run that the
    # repository may run out of space, and separately that archive logs will not
    # be expired -- both true, neither fatal, which is the problem.
    retention_full: int | None = None
    retention_archive: int | None = None
    # Labels of the backups the repository actually holds, newest last.
    backup_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackupReadiness:
    """Whether a node can be backed up, and whether its backups would be worth having.

    Read from the node. Nothing is defaulted from what a node is assumed to be,
    on `realtime.NodeReadiness`'s reasoning: the interesting failures are in the
    settings an operator is most likely to have left alone, and `archive_mode`
    is off by default on every PostgreSQL cluster ever installed.
    """

    wal_level: str
    archive_mode: str
    archive_command: str
    # Seconds, or 0 for disabled. Not a durability setting -- it bounds how
    # stale a PITR target can be on a cluster nobody is writing to, by forcing a
    # segment switch. Zero means an idle tenant's recoverable point in time
    # stops advancing entirely.
    archive_timeout_s: int
    # From pg_stat_archiver. A failing archiver is invisible from the cluster:
    # WAL accumulates in pg_wal, everything serves normally, and there is no
    # recoverable point in time after the break.
    archive_failed_count: int
    archive_last_failed_wal: str | None
    archive_last_archived_wal: str | None
    repository: RepositoryState
    # Whether this deployment is production, which is the only thing ADR-064's
    # severity depends on. Carried on the readiness rather than read from config
    # inside `failures`, so the dataclass stays a pure function of what was
    # observed and a test can assert both severities without touching the
    # environment.
    production: bool = False
    stanza: str = ""

    @property
    def repository_is_co_located(self) -> bool | None:
        """Whether the repository shares a filesystem with the data directory.

        None when it could not be determined, which is not the same as False.

        The check is deliberately shallow -- a path prefix comparison, plus the
        `st_dev` of both paths when they can be stat'd -- and it is shallow in
        the conservative direction. It catches the default and the common
        mistake: a repository under `/var/lib/pgbackrest` on the node that holds
        `/var/lib/postgresql`. It does not catch an NFS mount backed by the same
        SAN, or an S3 endpoint served by the same Proxmox host. Those are real
        and this cannot see them, which is why ADR-064 is a decision with a
        runbook and not merely this function.
        """
        repo = self.repository.repo_path
        pg = self.repository.pg_path
        if not repo or not pg:
            return None
        # A repository addressed through a URI is not a local path and is not
        # judged here: `repo1-s3-bucket` and friends put the repository
        # somewhere this process cannot stat.
        if "://" in repo:
            return False
        if repo.startswith(pg.rstrip("/") + "/") or pg.startswith(repo.rstrip("/") + "/"):
            return True
        try:
            return os.stat(repo).st_dev == os.stat(pg).st_dev
        except OSError:
            return None

    @property
    def failures(self) -> list[str]:
        """Why this node must not be relied on for backup. Empty means it may.

        Ordered by how expensive the fix is, following `realtime.NodeReadiness`:
        `archive_mode` is postmaster context, so changing it restarts the
        cluster and takes every tenant on the node down with it. A node that is
        going to fail on that should fail on it before anybody edits a
        repository path.
        """
        problems: list[str] = []

        if self.archive_mode not in ("on", "always"):
            problems.append(
                f"archive_mode is {self.archive_mode!r}; WAL is not archived, so there is no "
                "point in time to recover to and no backup is restorable past the moment it "
                "was taken. This is postmaster context: fixing it restarts the cluster"
            )

        if self.wal_level not in SUFFICIENT_WAL_LEVELS:
            problems.append(
                f"wal_level is {self.wal_level!r}, below 'replica'; the WAL a replay needs is "
                "not written at all. Also postmaster context"
            )

        if not self.archive_command.strip() or self.archive_command.strip() == "(disabled)":
            problems.append(
                "archive_command is empty; archive_mode is on and nothing is archiving"
            )

        # The archiver's own report, which is the difference between "configured"
        # and "working". A node can pass every setting above and be archiving
        # nothing.
        if self.archive_failed_count > 0 and self.archive_last_archived_wal is None:
            problems.append(
                f"the WAL archiver has failed {self.archive_failed_count} times and has never "
                f"archived a segment (last failure: {self.archive_last_failed_wal}); "
                "WAL is accumulating in pg_wal and no backup taken here is restorable"
            )

        if self.repository.reachable and self.repository.check_ok is False:
            problems.append(
                f"`pgbackrest check` failed: {self.repository.check_detail}. The repository is "
                "configured and does not work, which is the state a nightly backup reports "
                "nothing about"
            )

        if self.repository.reachable and self.repository.retention_full is None:
            problems.append(
                "repo1-retention-full is unset; pgBackRest keeps every backup forever and warns "
                "about it on every run, so the repository fills and the only symptom is a "
                "warning nobody reads"
            )

        if self.repository.reachable and self.repository.retention_archive is None:
            problems.append(
                "repo1-retention-archive is unset; WAL outlives every backup it belongs to. "
                "Both halves have to be set or expiry is half-done"
            )

        # ADR-064. Production refuses; everywhere else this is a warning, because
        # the slice-0 measurement cluster puts the repository beside the data
        # directory on purpose and that fixture must keep working.
        if self.production and self.repository_is_co_located:
            problems.append(
                f"repo1-path ({self.repository.repo_path}) is on the same filesystem as "
                f"pg1-path ({self.repository.pg_path}); ADR-064: the loss that takes this host "
                "takes the backups with it, so this is not a backup"
            )

        return problems

    @property
    def warnings(self) -> list[str]:
        """True and not disqualifying. Reported, never enforced.

        The ADR-064 co-location warning is here in development and in `failures`
        in production, and it is the same sentence either way -- an operator
        reading it on a development box and on a production node should not have
        to work out whether they are being told two different things.
        """
        notes: list[str] = []

        if not self.production and self.repository_is_co_located:
            notes.append(
                f"repo1-path ({self.repository.repo_path}) is on the same filesystem as "
                f"pg1-path ({self.repository.pg_path}); ADR-064: the loss that takes this host "
                "takes the backups with it. Not enforced outside production"
            )

        if self.repository_is_co_located is None and self.repository.reachable:
            notes.append(
                "could not determine whether the repository shares a filesystem with the data "
                "directory; ADR-064 cannot be checked here and has to be confirmed by hand"
            )

        if not self.repository.reachable:
            notes.append(
                f"the repository could not be inspected from here ({self.repository.detail}). "
                "pgBackRest runs on the node, so retention, `pgbackrest check` and the ADR-064 "
                "failure-domain check were NOT evaluated -- this is not a passing repository, "
                "it is an unexamined one"
            )

        if self.archive_timeout_s == 0:
            notes.append(
                "archive_timeout is 0; on a cluster nobody is writing to, no segment is closed "
                "and the recoverable point in time stops advancing. ADR-022 makes that the free "
                "tier's normal state rather than an edge case"
            )

        if self.archive_failed_count > 0 and self.archive_last_archived_wal is not None:
            notes.append(
                f"the WAL archiver has {self.archive_failed_count} failures on record "
                f"(last: {self.archive_last_failed_wal}) but is archiving now "
                f"(last archived: {self.archive_last_archived_wal})"
            )

        return notes

    @property
    def ready(self) -> bool:
        return not self.failures

    def as_capacity(self) -> dict[str, Any]:
        """The subset recorded on the node row.

        Recorded rather than re-derived so that `cp-manage node list` and the
        maintenance pass can say whether a node is backed up without opening a
        connection to it -- and so that a node whose `archive_mode` changed
        since it was checked shows as stale rather than silently passing.
        """
        return {
            "backup_ready": self.ready,
            "backup_stanza": self.stanza,
            "wal_level": self.wal_level,
            "archive_mode": self.archive_mode,
            "archive_timeout_s": self.archive_timeout_s,
            "backup_repo_reachable": self.repository.reachable,
            "backup_repo_co_located": self.repository_is_co_located,
            "backup_retention_full": self.repository.retention_full,
            "backup_retention_archive": self.repository.retention_archive,
        }


# --------------------------------------------------------------------------
# Reading the node
# --------------------------------------------------------------------------


_SETTINGS = ("wal_level", "archive_mode", "archive_command", "archive_timeout", "data_directory")


def _settings(admin_conn: psycopg.Connection) -> dict[str, str]:
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)", (list(_SETTINGS),)
        )
        return {row["name"]: row["setting"] for row in cur.fetchall()}


def _archiver(admin_conn: psycopg.Connection) -> dict[str, Any]:
    """What the archiver has actually done, as opposed to what it is told to do."""
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT archived_count, failed_count, last_archived_wal, last_failed_wal "
            "  FROM pg_stat_archiver"
        )
        return cur.fetchone() or {}


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, no interpolated user input
        argv, capture_output=True, text=True, check=False, timeout=timeout
    )


def run_pgbackrest(
    stanza: str, *args: str, timeout: int = 60, run_as: str | None = None
) -> subprocess.CompletedProcess:
    """Invoke pgBackRest for one stanza, as the user that owns the cluster.

    A list argv and never a shell string. The stanza reaches this from node
    configuration rather than from a customer, but the rule in AGENTS.md about
    identifiers built from project metadata is a rule about habits: the safe
    form costs nothing here and is the one that stays correct when a later slice
    derives a stanza name from something less trusted.
    """
    user = DEFAULT_RUN_AS if run_as is None else run_as
    prefix = ["sudo", "-n", "-u", checked_run_as(user)] if user else []
    return _run(
        [*prefix, "pgbackrest", "--stanza=" + checked_stanza(stanza), *args], timeout=timeout
    )


def _permission_hint(output: str) -> str:
    """Turn pgBackRest's misleading permission error into the actual cause.

    It names `/etc/pgbackrest.conf`, so the obvious reading is that the file is
    missing or malformed. It is neither: it is mode 0600 owned by the cluster
    owner, and the caller is somebody else.
    """
    if "Permission denied" in output or "unable to open file" in output:
        return (
            " -- pgBackRest must run as the cluster's owner; set MALUDB_BACKUP_RUN_AS "
            "(or run this command as that user). Root is not sufficient without "
            "CAP_DAC_OVERRIDE"
        )
    return ""


def inspect_repository(
    stanza: str,
    *,
    config_path: str = "/etc/pgbackrest.conf",
    run_as: str | None = None,
) -> RepositoryState:
    """Ask pgBackRest about the repository, on the node it lives on.

    Returns `reachable=False` rather than raising when pgBackRest is not here.
    The control plane may run somewhere the repository does not, and an
    unexamined repository has to be reported as unexamined -- reporting it as
    healthy is the failure this module exists to prevent, and reporting it as
    broken would make every control plane that is not co-located refuse its own
    nodes.
    """
    if shutil.which("pgbackrest") is None:
        return RepositoryState(reachable=False, detail="pgbackrest is not on PATH")

    try:
        info = run_pgbackrest(stanza, "--output=json", "info", run_as=run_as)
    except (OSError, subprocess.SubprocessError) as exc:
        return RepositoryState(reachable=False, detail=f"pgbackrest info failed ({type(exc).__name__})")

    if info.returncode != 0:
        output = info.stderr or info.stdout
        return RepositoryState(
            reachable=False,
            detail=f"pgbackrest info exited {info.returncode}: {_tail(output)}"
                   + _permission_hint(output),
        )

    try:
        parsed = json.loads(info.stdout)
    except json.JSONDecodeError:
        return RepositoryState(reachable=False, detail="pgbackrest info returned unparseable JSON")

    entry = next((item for item in parsed if item.get("name") == stanza), None)
    if entry is None:
        return RepositoryState(
            reachable=False, detail=f"pgbackrest knows no stanza named {stanza!r}"
        )

    labels = tuple(b["label"] for b in entry.get("backup", []) if b.get("label"))
    pg_path = None
    for pg in entry.get("db", []):
        # `info` reports the databases the stanza has covered over its life; the
        # current one is what a backup taken now would copy.
        pg_path = pg.get("path") or pg_path

    options = _read_stanza_options(stanza, config_path, run_as=run_as)
    if pg_path is None:
        pg_path = options.get("pg1-path")

    check = run_pgbackrest(stanza, "--log-level-console=error", "check", timeout=120, run_as=run_as)

    return RepositoryState(
        reachable=True,
        detail=f"{len(labels)} backup(s) in the repository",
        check_ok=check.returncode == 0,
        check_detail=(
            "ok" if check.returncode == 0 else _tail(check.stderr or check.stdout)
        ),
        pg_path=pg_path,
        repo_path=options.get("repo1-path", "/var/lib/pgbackrest"),
        retention_full=_int_or_none(options.get("repo1-retention-full")),
        retention_archive=_int_or_none(options.get("repo1-retention-archive")),
        backup_labels=labels,
    )


# Everything this module reads out of pgbackrest.conf, and nothing else.
_WANTED_OPTIONS = frozenset(
    {"pg1-path", "repo1-path", "repo1-retention-full", "repo1-retention-archive"}
)


def _read_stanza_options(stanza: str, config_path: str, *, run_as: str | None = None) -> dict[str, str]:
    """The stanza's own section of pgbackrest.conf, plus the global section.

    Parsed rather than asked for, because pgBackRest has no command that prints
    its effective configuration. The same two-sources shape as
    `realtime`'s `pg_hba` handling: the probe (`pgbackrest check`) says whether
    it works, and the file says what it is, and a check that only did the first
    could not tell an operator that retention is unset.

    A missing or unreadable file is an empty dict, which surfaces as "retention
    is unset" -- the conservative direction, and the fix is to write the option.

    **Only the four options this module reads are kept.** The file can hold
    `repo1-s3-key` and `repo1-s3-key-secret`, and there is no reason for a
    credential to be carried in a dict that later feeds a dataclass an operator
    prints. Filtering at the point of parsing is cheaper than remembering not to
    log it.
    """
    options: dict[str, str] = {}
    section = "global"
    try:
        for raw in _read_config_lines(config_path, run_as=run_as):
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section not in ("global", stanza) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() not in _WANTED_OPTIONS:
                continue
            # The stanza section wins over global, and it is read second in a
            # file that puts global first -- which is the convention but not a
            # guarantee, so this is ordered explicitly rather than by luck.
            key = key.strip()
            if section == stanza or key not in options:
                options[key] = value.strip()
    except OSError:
        return {}
    return options


def _read_config_lines(config_path: str, *, run_as: str | None = None) -> list[str]:
    """The configuration file's lines, read as whoever can actually read it.

    Mode 0600 owned by the cluster owner on a Debian/Ubuntu install, so the
    direct read is attempted first and the become-the-owner path is the
    fallback rather than the default -- a control plane that can read the file
    should not shell out to do it.
    """
    try:
        with open(config_path, encoding="utf-8") as handle:
            return handle.readlines()
    except PermissionError:
        user = DEFAULT_RUN_AS if run_as is None else run_as
        if not user:
            raise
        proc = _run(["sudo", "-n", "-u", checked_run_as(user), "cat", config_path])
        if proc.returncode != 0:
            raise
        return proc.stdout.splitlines()


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _tail(text: str, limit: int = 400) -> str:
    """The end of a command's output, which is where pgBackRest puts the reason."""
    cleaned = " ".join((text or "").split())
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


def inspect_node(
    admin_conn: psycopg.Connection,
    *,
    stanza: str,
    production: bool = False,
    config_path: str = "/etc/pgbackrest.conf",
    run_as: str | None = None,
) -> BackupReadiness:
    """Everything readiness needs, from the two places it lives."""
    settings = _settings(admin_conn)
    archiver = _archiver(admin_conn)
    repository = inspect_repository(stanza, config_path=config_path, run_as=run_as)

    # `data_directory` from the cluster is more trustworthy than `pg1-path` from
    # the config file -- the file says what pgBackRest was told, and this says
    # what the postmaster is actually running on. They disagreeing is itself a
    # finding, and the ADR-064 comparison should use the real one.
    if settings.get("data_directory") and repository.reachable:
        repository = replace(repository, pg_path=settings["data_directory"])

    return BackupReadiness(
        wal_level=settings.get("wal_level", "unknown"),
        archive_mode=settings.get("archive_mode", "unknown"),
        archive_command=settings.get("archive_command", ""),
        archive_timeout_s=_int_or_none(settings.get("archive_timeout")) or 0,
        archive_failed_count=int(archiver.get("failed_count") or 0),
        archive_last_failed_wal=archiver.get("last_failed_wal"),
        archive_last_archived_wal=archiver.get("last_archived_wal"),
        repository=repository,
        production=production,
        stanza=stanza,
    )


def record_readiness(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    name: str,
    stanza: str,
    production: bool = False,
    config_path: str = "/etc/pgbackrest.conf",
    run_as: str | None = None,
) -> BackupReadiness:
    """Inspect a node and store the result, including the stanza it was checked against.

    The stanza is written to its own column rather than left in JSON: the
    verification pass selects on it, and a node with no `backup_stanza` is the
    canonical "not prepared for backup" state.
    """
    # Validated here as well as in `_pgbackrest`, because `inspect_repository`
    # returns early when pgBackRest is absent -- so without this an unusable
    # stanza could be written to the node row by a control plane that never ran
    # the command that would have rejected it.
    checked_stanza(stanza)
    readiness = inspect_node(
        admin_conn, stanza=stanza, production=production, config_path=config_path, run_as=run_as
    )
    updated = db.execute(
        conn,
        "UPDATE nodes "
        "   SET backup_stanza = %s, "
        "       capacity_json = capacity_json || %s::jsonb, "
        # now() from the database, for `realtime.record_readiness`'s reason: the
        # check is only meaningful against the clock everything else is stamped
        # with.
        "       metrics_json = metrics_json || jsonb_build_object("
        "           'backup_checked_at', now(), "
        "           'backup_failures', %s::jsonb, "
        "           'backup_warnings', %s::jsonb) "
        " WHERE name = %s",
        (
            stanza,
            psycopg.types.json.Jsonb(readiness.as_capacity()),
            psycopg.types.json.Jsonb(readiness.failures),
            psycopg.types.json.Jsonb(readiness.warnings),
            name,
        ),
    )
    if updated == 0:
        raise BackupError(f"no node named {name!r}")
    conn.commit()
    return readiness


# --------------------------------------------------------------------------
# Taking one
# --------------------------------------------------------------------------


@dataclass
class BackupRun:
    """One invocation of pgBackRest, and what became of it."""

    backup_id: int
    node_name: str
    stanza: str
    backup_type: str
    status: str = "running"
    label: str | None = None
    database_bytes: int | None = None
    repository_bytes: int | None = None
    wal_start: str | None = None
    wal_stop: str | None = None
    error: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def start_backup(conn: psycopg.Connection, *, node_id: int, stanza: str, backup_type: str) -> int:
    """Record that a backup is beginning, and return its row id.

    Written before the backup runs, not after it finishes, and that ordering is
    the point. ADR-067's idle-cluster hang produces a pgBackRest that never
    returns; a table populated on success could not distinguish that from a
    backup nobody ever started, and both look identical the next morning --
    which is to say, they look like nothing at all.
    """
    if backup_type not in BACKUP_TYPES:
        raise BackupError(f"unknown backup type {backup_type!r}; expected one of {BACKUP_TYPES}")
    checked_stanza(stanza)
    row = db.one(
        conn,
        "INSERT INTO node_backups (node_id, stanza, backup_type) VALUES (%s, %s, %s) RETURNING id",
        (node_id, stanza, backup_type),
    )
    conn.commit()
    return int(row["id"])


def finish_backup(
    conn: psycopg.Connection,
    backup_id: int,
    *,
    status: str,
    label: str | None = None,
    database_bytes: int | None = None,
    repository_bytes: int | None = None,
    wal_start: str | None = None,
    wal_stop: str | None = None,
    error: str | None = None,
) -> None:
    db.execute(
        conn,
        "UPDATE node_backups "
        "   SET status = %s, finished_at = now(), label = %s, database_bytes = %s, "
        "       repository_bytes = %s, wal_start = %s, wal_stop = %s, error = %s "
        " WHERE id = %s",
        (status, label, database_bytes, repository_bytes, wal_start, wal_stop, error, backup_id),
    )
    conn.commit()


def run_backup(
    conn: psycopg.Connection,
    *,
    node_id: int,
    node_name: str,
    stanza: str,
    backup_type: str = "full",
    process_max: int | None = None,
    timeout_s: int = 6 * 3600,
    run_as: str | None = None,
) -> BackupRun:
    """Take one backup of a node's cluster and record what happened.

    **`--start-fast` is passed unconditionally and there is no way to turn it
    off.** ADR-067: without it, a backup of a cluster nobody is writing to waits
    for a checkpoint that PostgreSQL will never schedule, and a node full of
    sleeping free-tier projects is exactly such a cluster. It costs one
    checkpoint's I/O.

    `process_max` is the only lever that matters for wall-clock -- 2.26x
    measured between 1 and 4 -- and those cores come out of the node the tenants
    are running on, so it is configuration and not a constant here.
    """
    started = datetime.now(UTC)
    backup_id = start_backup(conn, node_id=node_id, stanza=stanza, backup_type=backup_type)
    run = BackupRun(
        backup_id=backup_id, node_name=node_name, stanza=stanza, backup_type=backup_type
    )

    argv = ["--log-level-console=warn", "backup", f"--type={backup_type}", "--start-fast"]
    if process_max is not None:
        argv.append(f"--process-max={process_max}")

    try:
        proc = run_pgbackrest(stanza, *argv, timeout=timeout_s, run_as=run_as)
    except subprocess.TimeoutExpired:
        # The row stays `running` and is aged out by the verification pass. It
        # is deliberately not marked failed here: pgBackRest may still be
        # working, and a repository is not made consistent by this process
        # changing its mind about it.
        run.status = "running"
        run.error = f"pgbackrest did not return within {timeout_s}s"
        log.warning("backup of %s timed out after %ss", node_name, timeout_s)
        return run
    except (OSError, subprocess.SubprocessError) as exc:
        run.status = "failed"
        run.error = f"could not run pgbackrest ({type(exc).__name__})"
        finish_backup(conn, backup_id, status="failed", error=run.error)
        return run

    run.elapsed_s = (datetime.now(UTC) - started).total_seconds()

    if proc.returncode != 0:
        output = proc.stderr or proc.stdout
        run.status = "failed"
        run.error = _tail(output) + _permission_hint(output)
        finish_backup(conn, backup_id, status="failed", error=run.error)
        return run

    # The label and the sizes come from `info`, not from parsing the backup's
    # console output: pgBackRest's log format is not an interface and its JSON
    # is.
    detail = _latest_backup_detail(stanza, run_as=run_as)
    if detail is None:
        # pgBackRest exited 0 and the repository does not show a backup. That
        # should not happen, and recording it as complete on the strength of an
        # exit code is precisely the kind of unverified success this module
        # refuses to write -- the schema requires a label for a reason.
        run.status = "failed"
        run.error = "pgbackrest exited 0 but the repository reports no backup"
        finish_backup(conn, backup_id, status="failed", error=run.error)
        return run

    run.status = "complete"
    run.label = detail["label"]
    run.database_bytes = detail["database_bytes"]
    run.repository_bytes = detail["repository_bytes"]
    run.wal_start = detail["wal_start"]
    run.wal_stop = detail["wal_stop"]
    finish_backup(
        conn,
        backup_id,
        status="complete",
        label=run.label,
        database_bytes=run.database_bytes,
        repository_bytes=run.repository_bytes,
        wal_start=run.wal_start,
        wal_stop=run.wal_stop,
    )
    return run


def _latest_backup_detail(stanza: str, *, run_as: str | None = None) -> dict[str, Any] | None:
    """The newest backup in the repository, as pgBackRest describes it."""
    info = run_pgbackrest(stanza, "--output=json", "info", run_as=run_as)
    if info.returncode != 0:
        return None
    try:
        parsed = json.loads(info.stdout)
    except json.JSONDecodeError:
        return None
    entry = next((item for item in parsed if item.get("name") == stanza), None)
    if entry is None or not entry.get("backup"):
        return None
    newest = entry["backup"][-1]
    info_block = newest.get("info", {})
    return {
        "label": newest.get("label"),
        # `size` is the cluster; `repository.size` is what the repository grew
        # by. Slice 0 measured 9.4:1 between them and the ratio is structural,
        # so conflating the two would make every capacity figure wrong by an
        # order of magnitude in whichever direction was convenient.
        "database_bytes": info_block.get("size"),
        "repository_bytes": (info_block.get("repository") or {}).get("size"),
        "wal_start": (newest.get("archive") or {}).get("start"),
        "wal_stop": (newest.get("archive") or {}).get("stop"),
    }


# --------------------------------------------------------------------------
# What the platform believes, and whether it should
# --------------------------------------------------------------------------


@dataclass
class NodeBackupStatus:
    """One node's backup state, as the control plane can see it.

    Everything here is read from the control-plane database. The verification
    pass deliberately does not open a connection to the node or to the
    repository: it answers "is the platform being told about backups, and are
    they recent", which is a question about the record. Whether the repository
    holds a restorable copy is a different question that only a restore answers,
    and that is slice 2.
    """

    node_id: int
    name: str
    stanza: str | None
    max_age_hours: int
    latest_status: str | None = None
    latest_type: str | None = None
    latest_started_at: datetime | None = None
    latest_age_hours: float | None = None
    latest_error: str | None = None
    last_full_started_at: datetime | None = None
    last_full_age_hours: float | None = None

    @property
    def problems(self) -> list[str]:
        """Why this node's backups should not be relied on."""
        issues: list[str] = []

        if not self.stanza:
            issues.append(
                "not prepared for backup; run `cp-manage node backup-check` "
                "(ADR-067: archive_mode and a pgBackRest stanza are node preconditions, and "
                "archive_mode needs a cluster restart to change)"
            )
            return issues

        if self.latest_started_at is None:
            issues.append("no backup has ever been recorded for this node")
            return issues

        # "No new backup" is a failure, not a thing to wait for. This is the
        # ADR-067 finding stated as code: an idle cluster's untuned backup hangs
        # rather than fails, so silence has to be read as failure or the
        # condition is invisible.
        if self.latest_status == "running":
            age = self.latest_age_hours or 0.0
            if age > STALE_RUNNING_HOURS:
                issues.append(
                    f"a {self.latest_type} backup has been running for {age:.1f}h "
                    f"(over {STALE_RUNNING_HOURS}h). ADR-067: an untuned backup of an idle "
                    "cluster waits for a checkpoint that never comes -- check that it was "
                    "started with --start-fast"
                )
            # A backup running for less than the stale window is in progress and
            # says nothing about whether the last one succeeded, so the age
            # check below still applies. Deliberately not an early return.

        if self.latest_status == "failed":
            issues.append(
                f"the most recent backup failed: {self.latest_error or 'no reason recorded'}"
            )

        if self.latest_age_hours is not None and self.latest_age_hours > self.max_age_hours:
            issues.append(
                f"the most recent backup is {self.latest_age_hours:.1f}h old, over the "
                f"{self.max_age_hours}h this node allows"
            )

        if self.last_full_started_at is None:
            issues.append(
                "no completed *full* backup on record; diff and incr backups restore only "
                "through the full they are rooted on"
            )
        elif self.last_full_age_hours is not None and self.last_full_age_hours > DEFAULT_MAX_FULL_AGE_HOURS:
            issues.append(
                f"the most recent full backup is {self.last_full_age_hours / 24:.1f} days old; "
                "a differential chain rooted on an expired full restores nothing"
            )

        return issues

    @property
    def healthy(self) -> bool:
        return not self.problems


def node_status(conn: psycopg.Connection, *, node_id: int | None = None) -> list[NodeBackupStatus]:
    """Backup state for every node, or one.

    Nodes with no stanza are included rather than filtered out. A node nobody
    has prepared for backup is the most important row in this report, and a
    query that only returned prepared nodes would answer "all healthy" on a
    platform with no backups at all.
    """
    rows = db.query(
        conn,
        """
        SELECT n.id, n.name, n.backup_stanza, n.backup_max_age_hours,
               latest.status        AS latest_status,
               latest.backup_type   AS latest_type,
               latest.started_at    AS latest_started_at,
               latest.error         AS latest_error,
               EXTRACT(EPOCH FROM (now() - latest.started_at)) / 3600 AS latest_age_hours,
               full_b.started_at    AS full_started_at,
               EXTRACT(EPOCH FROM (now() - full_b.started_at)) / 3600 AS full_age_hours
          FROM nodes n
          LEFT JOIN LATERAL (
                SELECT b.status, b.backup_type, b.started_at, b.error
                  FROM node_backups b
                 WHERE b.node_id = n.id
                 ORDER BY b.started_at DESC
                 LIMIT 1
          ) latest ON TRUE
          LEFT JOIN LATERAL (
                SELECT b.started_at
                  FROM node_backups b
                 WHERE b.node_id = n.id AND b.backup_type = 'full' AND b.status = 'complete'
                 ORDER BY b.started_at DESC
                 LIMIT 1
          ) full_b ON TRUE
         -- One node or all of them, without building the WHERE clause from a
         -- string. The cast is what makes the NULL unambiguous to the planner.
         WHERE %s::int IS NULL OR n.id = %s::int
         ORDER BY n.name
        """,
        (node_id, node_id),
    )
    return [
        NodeBackupStatus(
            node_id=row["id"],
            name=row["name"],
            stanza=row["backup_stanza"],
            max_age_hours=row["backup_max_age_hours"] or DEFAULT_MAX_AGE_HOURS,
            latest_status=row["latest_status"],
            latest_type=row["latest_type"],
            latest_started_at=row["latest_started_at"],
            latest_age_hours=float(row["latest_age_hours"]) if row["latest_age_hours"] is not None else None,
            latest_error=row["latest_error"],
            last_full_started_at=row["full_started_at"],
            last_full_age_hours=float(row["full_age_hours"]) if row["full_age_hours"] is not None else None,
        )
        for row in rows
    ]
