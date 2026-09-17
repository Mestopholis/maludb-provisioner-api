"""A node's backup runner (ADR-086, free slice 7b).

Runs **on the node, as `postgres`** -- pgBackRest reads the data directory and its configuration
is the cluster owner's -- from `maludb-node-backup@.service`:

    python -m services.control_plane.node_backup full     # maludb-node-backup@full
    python -m services.control_plane.node_backup diff     # maludb-node-backup@diff
    python -m services.control_plane.node_backup check    # maludb-node-backup@check

It records what it did through the node's **backup recorder** role (migration 0058), which may
execute three functions for its own node and holds no table privilege. It holds no KEK, reads
no control-plane configuration, and imports nothing that would need either: the environment is
the recorder's DSN and the stanza.

- `backup` opens a `running` row *before* pgBackRest starts (ADR-067's hang leaves a row the
  maintenance pass can age out), runs the backup with `--start-fast`, and closes the row with
  pgBackRest's own label and sizes from `info`, never from a parsed console log.
- `check` inspects the repository where it lives -- `pgbackrest check`, `info`, the repository
  options, and whether it shares a filesystem with the data directory -- and records the report
  for `cp-manage node backup-check` to join with the cluster settings it reads remotely.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess  # noqa: S404 - pgBackRest is a command
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import psycopg

from services.control_plane import backup
from services.control_plane import logging as cp_logging

log = logging.getLogger("maludb.node_backup")

# Six hours, as `backup.run_backup`: past this the row stays `running` and the pass reports it.
BACKUP_TIMEOUT_S = 6 * 3600
LOCAL_POSTGRES = "dbname=postgres connect_timeout=5"


@dataclass(frozen=True)
class Settings:
    database_url: str
    stanza: str
    process_max: int | None = None
    config_path: str = "/etc/pgbackrest.conf"

    @classmethod
    def from_environment(cls) -> Settings:
        url = os.environ.get("MALUDB_BACKUP_RECORDER_DATABASE_URL", "").strip()
        if not url:
            raise SystemExit("MALUDB_BACKUP_RECORDER_DATABASE_URL is required: the backup recorder's DSN (ADR-086)")
        stanza = os.environ.get("MALUDB_BACKUP_STANZA", "").strip()
        if not stanza:
            raise SystemExit("MALUDB_BACKUP_STANZA is required: the node's pgBackRest stanza")
        raw = os.environ.get("MALUDB_BACKUP_PROCESS_MAX", "").strip()
        process_max = int(raw) if raw else None
        if process_max is not None and not 1 <= process_max <= 16:
            raise SystemExit("MALUDB_BACKUP_PROCESS_MAX must be 1..16; its cores come out of the node's tenants")
        return cls(
            database_url=url,
            stanza=backup.checked_stanza(stanza),
            process_max=process_max,
            config_path=os.environ.get("MALUDB_BACKUP_CONFIG", "").strip() or "/etc/pgbackrest.conf",
        )


def _connect(settings: Settings, connect=psycopg.connect) -> psycopg.Connection:
    return connect(settings.database_url, connect_timeout=10, autocommit=True)


def _diag(exc: psycopg.Error) -> str:
    """The server's message and never the DSN."""
    return f"{type(exc).__name__}: {(exc.diag.message_primary or '').strip()}"


def take_backup(settings: Settings, backup_type: str, *, connect=psycopg.connect,
                pgbackrest=backup.run_pgbackrest, latest=backup._latest_backup_detail) -> int:  # noqa: SLF001
    """Take one backup and record it. 0 complete, 1 failed or still running.

    Two short connections, one each side of pgBackRest, rather than one held open for a backup
    that may take hours: an idle TLS session across that long is exactly the kind of thing a
    network drops, and the row would then never be closed.
    """
    with _connect(settings, connect) as conn:
        backup_id = conn.execute("SELECT public.start_node_backup(%s)", (backup_type,)).fetchone()[0]

    def finish(status: str, *, detail: dict | None = None, error: str | None = None) -> None:
        detail = detail or {}
        with _connect(settings, connect) as conn:
            conn.execute(
                "SELECT public.finish_node_backup(%s, %s, %s, %s, %s, %s, %s, %s)",
                (backup_id, status, detail.get("label"), detail.get("database_bytes"),
                 detail.get("repository_bytes"), detail.get("wal_start"), detail.get("wal_stop"), error),
            )

    argv = ["--log-level-console=warn", "backup", f"--type={backup_type}", "--start-fast"]
    if settings.process_max is not None:
        argv.append(f"--process-max={settings.process_max}")
    started = datetime.now(UTC)
    try:
        # run_as="" -- this process already is the cluster's owner; no sudo.
        proc = pgbackrest(settings.stanza, *argv, timeout=BACKUP_TIMEOUT_S, run_as="")
    except subprocess.TimeoutExpired:
        log.error("backup %s of %s did not return within %ss; the row stays running",
                  backup_id, settings.stanza, BACKUP_TIMEOUT_S)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        finish("failed", error=f"could not run pgbackrest ({type(exc).__name__})")
        log.error("backup %s: could not run pgbackrest (%s)", backup_id, type(exc).__name__)
        return 1

    if proc.returncode != 0:
        error = backup._tail(proc.stderr or proc.stdout)  # noqa: SLF001
        finish("failed", error=error)
        log.error("backup %s failed: %s", backup_id, error)
        return 1

    detail = latest(settings.stanza, run_as="")
    if detail is None or not detail.get("label"):
        finish("failed", error="pgbackrest exited 0 but the repository reports no backup")
        log.error("backup %s: pgbackrest exited 0 and info shows no backup", backup_id)
        return 1
    finish("complete", detail=detail)
    log.info("%s backup %s complete in %.1fs: %s", backup_type, backup_id,
             (datetime.now(UTC) - started).total_seconds(), detail["label"])
    return 0


def _data_directory(connect=psycopg.connect) -> str | None:
    """The data directory the postmaster runs on, over the local socket as `postgres`."""
    try:
        with connect(LOCAL_POSTGRES, autocommit=True) as conn:
            return conn.execute("SHOW data_directory").fetchone()[0]
    except psycopg.Error:
        return None


def check(settings: Settings, *, connect=psycopg.connect, inspect=backup.inspect_repository,
          data_directory=_data_directory) -> int:
    """Inspect the repository here and record the report. 0 when recorded (whatever it says)."""
    state = inspect(settings.stanza, config_path=settings.config_path, run_as="")
    actual = data_directory()
    if actual and state.reachable:
        # The postmaster's data directory over the config file's pg1-path, as `inspect_node` does.
        state = replace(state, pg_path=actual)
    report = backup.repository_report(state, stanza=settings.stanza)
    with _connect(settings, connect) as conn:
        node = conn.execute("SELECT public.record_node_backup_check(%s)", (json.dumps(report),)).fetchone()[0]
    log.info("recorded the repository check for %s: reachable=%s check_ok=%s co_located=%s backups=%d",
             node, report["reachable"], report["check_ok"], report["co_located"], len(report["backup_labels"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_backup", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("full", "diff", "incr", "check"),
                        help="a backup of that type, or check: inspect the repository and record the report")
    args = parser.parse_args(argv)
    cp_logging.configure()
    settings = Settings.from_environment()
    try:
        return check(settings) if args.command == "check" else take_backup(settings, args.command)
    except psycopg.Error as exc:
        log.error("%s: could not record through the backup recorder: %s", args.command, _diag(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
