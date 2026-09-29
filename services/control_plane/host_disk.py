"""What this host's disk is doing, measured once for the two things that ask.

Two callers, and they must not disagree. `deploy preflight` asks when an operator runs it;
`maludb-alerts.timer` asks every five minutes and mails the answer. If those two held separate
copies of "too full" and "too big", the day they drifted is the day a green preflight and an open
alert describe the same host -- and an operator would have no way to tell which was lying. Same
reason `alerts.PASS_STALE_MINUTES` is pinned to preflight's own bound.

So this module measures and describes; it decides nothing about severity, because a preflight
finding and a mailed condition are different things and each caller phrases its own.

**Why the disk is worth measuring at all.** On 2026-09-29 a single 19.6 GB PostgreSQL log filled a
control plane's root filesystem. PostgreSQL could not write, so it rejected connections
mid-recovery, and the six units above it reported a pool timeout or a restart loop -- naming a disk
in none of them. Free space is the symptom; a log that cannot be rotated is the cause, and both are
measured here because catching only the symptom leaves the operator to guess.

Nothing here opens a log. It reads sizes, which is why the alerting unit can do it as `maludb-cp`:
the log directory is world-executable, so `stat` works, while the files are `postgres:adm 640` and
their *contents* -- audited SQL included -- stay unreadable to it. That is a property worth keeping.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from dataclasses import dataclass

#: Where Debian's PostgreSQL writes its log. The filesystem carrying it is usually the filesystem
#: carrying everything else, which is what made a runaway log an outage rather than a full log
#: directory.
DEFAULT_LOG_DIR = "/var/log/postgresql"

#: Where maludb-control-plane-backup.service writes its nightly dumps (free slice 7d).
DEFAULT_DUMP_DIR = "/var/backups/maludb-control-plane"

#: Too full. Ten percent of a 30 GB root is 3 GB -- room for a control-plane dump and a day of
#: logs, not room to be relaxed about. A floor for *noticing*, not a capacity model;
#: `docs/CAPACITY.md` owns the node-side arithmetic.
FREE_MIN_PERCENT = 10.0

#: Too big for one file, which means rotation is not running. Independent of free space: the file
#: that stopped the control plane reached 19.6 GB because weekly `copytruncate` as `su root root`
#: could neither copy it (the copy needs as much free space as the file) nor truncate it (root on
#: these hosts has no CAP_DAC_OVERRIDE, and the log is postgres-owned). A gigabyte is far above any
#: healthy daily volume and far below the disk, so it is a finding while it is still cheap to fix.
LOG_FILE_MAX_BYTES = 1024**3

#: Suffixes that are not a growing log. A compressed archive is rotation *working*, so counting one
#: would report a healthy host; `.sample` is Debian's shipped example, which is not a log at all.
NOT_A_LIVE_LOG = frozenset({".gz", ".xz", ".zst", ".bz2", ".sample"})


def log_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MALUDB_PREFLIGHT_LOG_DIR", "").strip() or DEFAULT_LOG_DIR)


def dump_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MALUDB_CONTROL_PLANE_BACKUP_DIR", "").strip() or DEFAULT_DUMP_DIR)


def float_env(name: str, default: float) -> float:
    """A numeric override, ignoring a value that is not a number rather than dying on it.

    A preflight that crashes on a typo in a threshold tells an operator nothing about the
    deployment, and an alerting timer that crashes on one stops telling them anything at all --
    which is the failure mode this whole slice exists to avoid. Falling back silently would be its
    own trap, so every caller prints the threshold it actually applied and the report is the place
    to notice the typo.
    """
    try:
        return float(os.environ.get(name, "").strip())
    except ValueError:
        return default


def free_min_percent() -> float:
    return float_env("MALUDB_DISK_FREE_MIN_PERCENT", FREE_MIN_PERCENT)


def log_file_max_bytes() -> float:
    return float_env("MALUDB_LOG_FILE_MAX_MB", LOG_FILE_MAX_BYTES / 1024**2) * 1024**2


@dataclass(frozen=True)
class Filesystem:
    """One filesystem this host writes to, and how much room is left on it."""

    what: str
    path: pathlib.Path
    free_bytes: int
    total_bytes: int

    @property
    def percent_free(self) -> float:
        return self.free_bytes / self.total_bytes * 100 if self.total_bytes else 0.0

    def describe(self) -> str:
        return (f"{self.path} ({self.what}): {self.free_bytes / 1024**3:.1f} GiB free, "
                f"{self.percent_free:.0f}%")

    def short(self) -> str:
        """For a mail subject, where the path matters more than the phrasing."""
        return f"{self.path} is {100 - self.percent_free:.0f}% full ({self.free_bytes / 1024**3:.1f} GiB free)"


def filesystems() -> list[Filesystem]:
    """The filesystems behind the paths this host writes to, one entry each.

    Deduplicated by device: on a default install the log and the dumps are on the same filesystem,
    and reporting it twice would read as two findings. A path that does not exist is skipped rather
    than reported -- the check that owns that path says so in its own words.
    """
    out: list[Filesystem] = []
    seen: set[int] = set()
    for what, path in (("PostgreSQL's log", log_dir()), ("the control-plane dumps", dump_dir())):
        try:
            device = path.stat().st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        out.append(Filesystem(what=what, path=path, free_bytes=usage.free, total_bytes=usage.total))
    return out


@dataclass(frozen=True)
class LogSurvey:
    """The uncompressed logs in one directory. `problem` is set when nothing could be measured."""

    directory: pathlib.Path
    files: int = 0
    total_bytes: int = 0
    largest_bytes: int = 0
    largest_name: str | None = None
    #: "unreadable" (mode 640 postgres:adm and we are neither) or "missing" (PostgreSQL is
    #: elsewhere). Either way the answer is *unknown*, which is not the same as fine.
    problem: str | None = None

    def describe(self) -> str:
        return (f"{self.files} uncompressed file(s), {self.total_bytes / 1024**3:.2f} GiB; largest "
                f"{self.largest_name} at {self.largest_bytes / 1024**3:.2f} GiB")


def survey_logs(directory: pathlib.Path | None = None) -> LogSurvey:
    directory = directory or log_dir()
    try:
        sizes = [(f.stat().st_size, f.name) for f in directory.iterdir()
                 if f.is_file() and f.suffix not in NOT_A_LIVE_LOG]
    except PermissionError:
        return LogSurvey(directory=directory, problem="unreadable")
    except OSError:
        return LogSurvey(directory=directory, problem="missing")
    if not sizes:
        return LogSurvey(directory=directory)
    largest, name = max(sizes)
    return LogSurvey(directory=directory, files=len(sizes), total_bytes=sum(s for s, _ in sizes),
                     largest_bytes=largest, largest_name=name)
