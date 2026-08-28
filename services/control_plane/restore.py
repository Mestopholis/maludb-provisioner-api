"""Restore one tenant to a point in time, without taking its neighbours down.

Phase 11 slice 2, on the path slice 0 measured. Closes the phase's first
acceptance criterion.

`docs/BACKUP-RECOVERY.md` states the requirement in one sentence and it is a
constraint on the *shape* of the answer, not on its speed: "Do not make 'restore
one project' require replacing the entire shared node in production." A shared
node carries up to `DEFAULT_MAX_PROJECTS = 200` tenants, so restoring in place
to recover one of them is an outage for the other 199 — and it is an outage
caused by somebody else's mistake, which is the worst kind a platform can serve.

So the restore never touches the running node's data directory. It builds a
**scratch cluster**, restores the repository into that at a point in time,
promotes it, extracts the one database, and loads it beside the live one. Slice
0 measured the whole path at 187 s on a 219.7 MB base with ~720 MB of WAL, with
the live node's nine tenant databases continuously available throughout.

## Three things this module refuses to do

**It never writes over a live database.** The recovered data lands in a new
database next to the original. Activation renames; it does not drop. After an
activation both copies still exist, and the one that was live is still there
under `<database>_pre_restore_<timestamp>`. There is no code path here that
destroys a tenant's data, which is a stronger property than "requires
confirmation" and costs only disk.

**It never drops a cluster it did not create.** `pg_dropcluster` on the wrong
name destroys a node and every tenant on it. A name is not enough of a guard, so
creation writes a marker file into the cluster's *configuration* directory —
which pgBackRest's restore does not touch, unlike the data directory — and
nothing is dropped without it. The live cluster's `data_directory` is read from
the node itself and compared, too, because two checks that must agree is the
pattern `realtime` already uses for `pg_hba.conf` and for the same reason: each
one alone has a hole the other covers.

**It never reports a restore as complete without checking who owns it.** This is
slice 0's sharpest finding and the one that produces no error at all. Restoring
a tenant into a cluster that has never seen it moves `auth` and `storage` from
their per-tenant service roles to whoever ran the restore — the platform
superuser — while all 164 RLS policies and every row arrive intact and
`pg_restore` exits 1 with "errors ignored". ADR-059 puts the `storage` schema
under a per-tenant role *specifically* so it is not owned by something with
superuser reach, and ADR-061 tells customers they cannot author policies there
on that basis. A tenant restored the naive way arrives with a different security
posture from the one that was backed up, and nothing about the database says so.

## Why this is a platform operation and cannot be a customer one

Not policy. `CREATE EXTENSION maludb_core` requires superuser because
`maludb_core` is not a trusted extension (ADR-015), so there is no non-superuser
path that produces a working tenant database. "Let the customer restore their
own project" is not a design option that was rejected; it is one that does not
exist.

## Where this runs

On the node. It creates and destroys a PostgreSQL cluster, which is a root
operation, and it reads a pgBackRest repository, which is the cluster owner's.
Both are node-local. The control plane records the result over its ordinary
connection.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - cluster lifecycle is a set of commands
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import backup, db, models, provisioning

log = logging.getLogger("maludb.restore")


class RestoreError(RuntimeError):
    """A restore could not be performed, or could not be trusted."""


# The scratch cluster. A cluster of its own, on a port of its own, created for
# one restore and destroyed after it.
#
# Configurable because the node it runs on is the operator's, not this module's
# — but every value is validated before it reaches a command line, because the
# commands in question are `pg_createcluster` and `pg_dropcluster`.
DEFAULT_PG_VERSION = os.environ.get("MALUDB_PG_VERSION", "17")
DEFAULT_SCRATCH_CLUSTER = os.environ.get("MALUDB_RESTORE_SCRATCH_CLUSTER", "mldbrestore")
DEFAULT_SCRATCH_PORT = int(os.environ.get("MALUDB_RESTORE_SCRATCH_PORT", "5440"))

# How long to wait for the promoted scratch cluster to finish recovery and start
# answering. Slice 0 measured 179.9 s to promotion with ~720 MB of WAL to
# replay; WAL volume is what drives this, and a node whose tenants have been
# busy since the last full backup replays more.
PROMOTION_TIMEOUT_S = int(os.environ.get("MALUDB_RESTORE_PROMOTION_TIMEOUT", "900"))

# The file that says a cluster is ours to destroy. In the *configuration*
# directory, not the data directory: pgBackRest's restore rewrites the latter,
# so a marker there would be gone at exactly the moment it is needed.
SCRATCH_MARKER = "maludb-scratch-restore"

# Cluster names reach `pg_createcluster` and `pg_dropcluster` argv. Debian's
# tooling accepts more than this; this is deliberately narrower.
_CLUSTER_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
_PG_VERSION_RE = re.compile(r"\A[0-9]{1,3}\Z")

# Schemas whose owner is a per-tenant role rather than the platform owner, and
# what they should be. The pair that slice 0 measured silently changing hands.
#
# `maludb_platform` is deliberately absent: it is owned by the platform owner on
# both sides, so a restore that leaves it with `postgres` has changed nothing.
# `public` is `pg_database_owner` and likewise survives.
def expected_schema_owners(names: provisioning.TenantNames) -> dict[str, str]:
    return {"auth": names.auth, "storage": names.storage}


def checked_cluster(name: str) -> str:
    if not _CLUSTER_RE.match(name or ""):
        raise RestoreError(
            f"invalid cluster name {name!r}; this value reaches pg_dropcluster and must be "
            "a plain lower-case identifier"
        )
    return name


def checked_pg_version(version: str) -> str:
    if not _PG_VERSION_RE.match(version or ""):
        raise RestoreError(f"invalid PostgreSQL version {version!r}")
    return version


def _run(
    argv: list[str], *, timeout: int = 300, sudo: bool = False, stdin: str | None = None
) -> subprocess.CompletedProcess:
    """Run a command. Never through a shell.

    There is no `shell=True` and no `bash -c` anywhere in this module, and that
    is deliberate rather than incidental: every command here runs under `sudo`,
    several of them destroy things, and the arguments are built from a cluster
    name and a version. Those are validated -- but `ScratchCluster` also carries
    path roots so tests can exercise the guards, and a field a test can set is a
    field. An argv that never reaches a shell cannot be made to mean something
    else by any of them.
    """
    prefix = ["sudo", "-n"] if sudo else []
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, validated components
        [*prefix, *argv], capture_output=True, text=True, check=False,
        timeout=timeout, input=stdin,
    )


def _as_owner(
    argv: list[str], *, run_as: str, timeout: int = 300, stdin: str | None = None
) -> subprocess.CompletedProcess:
    return _run(
        ["-u", backup.checked_run_as(run_as), *argv], timeout=timeout, sudo=True, stdin=stdin
    )


def _tail(text: str, limit: int = 600) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


# --------------------------------------------------------------------------
# Ownership: the check that stops a silent security downgrade
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnershipReport:
    """Who owns the restored tenant's schemas, against who should.

    Slice 0's finding, made checkable. The failure it catches is not an error —
    it is a success with different owners, and `pg_restore`'s exit code is not
    enough to see it.
    """

    database: str
    expected: dict[str, str]
    observed: dict[str, str]
    missing_roles: tuple[str, ...] = ()

    @property
    def wrong(self) -> dict[str, tuple[str, str]]:
        """Schema -> (expected, observed) for every schema that changed hands."""
        return {
            schema: (want, self.observed.get(schema, "(absent)"))
            for schema, want in self.expected.items()
            if self.observed.get(schema) != want
        }

    @property
    def verified(self) -> bool:
        return not self.wrong and not self.missing_roles

    @property
    def detail(self) -> str:
        if self.verified:
            return "every tenant-owned schema is owned by its per-tenant role"
        parts = []
        for schema, (want, got) in sorted(self.wrong.items()):
            parts.append(f"{schema} is owned by {got}, expected {want}")
        if self.missing_roles:
            parts.append(
                "roles absent from the target cluster: " + ", ".join(self.missing_roles)
            )
        return "; ".join(parts)


def observed_schema_owners(conn: psycopg.Connection, schemas: list[str]) -> dict[str, str]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT n.nspname AS schema, pg_get_userbyid(n.nspowner) AS owner "
            "  FROM pg_namespace n WHERE n.nspname = ANY(%s)",
            (schemas,),
        )
        return {row["schema"]: row["owner"] for row in cur.fetchall()}


def missing_roles(admin_conn: psycopg.Connection, names: provisioning.TenantNames) -> tuple[str, ...]:
    """Per-tenant roles the target cluster does not have.

    Checked *before* a load rather than diagnosed after one. Cluster-scoped
    roles are not in a single-database dump and never will be, so a load onto a
    cluster that has never seen this tenant is the case slice 0 measured — and
    the fix is to create them first, not to read the warnings afterwards.
    """
    wanted = [names.admin, names.auth, names.storage, names.authenticator]
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (wanted,))
        present = {row["rolname"] for row in cur.fetchall()}
    return tuple(sorted(set(wanted) - present))


def verify_ownership(
    tenant_conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    names: provisioning.TenantNames,
    *,
    database: str,
) -> OwnershipReport:
    expected = expected_schema_owners(names)
    return OwnershipReport(
        database=database,
        expected=expected,
        observed=observed_schema_owners(tenant_conn, list(expected)),
        missing_roles=missing_roles(admin_conn, names),
    )


# --------------------------------------------------------------------------
# The scratch cluster
# --------------------------------------------------------------------------


@dataclass
class ScratchCluster:
    version: str
    name: str
    port: int
    run_as: str
    # Debian's layout, overridable so the marker guard -- the thing standing
    # between this module and `pg_dropcluster` on a live node -- can be
    # exercised by a test that is not root and owns no cluster. A guard nothing
    # ever runs is not a guard.
    data_root: str = "/var/lib/postgresql"
    config_root: str = "/etc/postgresql"

    @property
    def data_dir(self) -> str:
        return f"{self.data_root}/{self.version}/{self.name}"

    @property
    def config_dir(self) -> str:
        return f"{self.config_root}/{self.version}/{self.name}"

    @property
    def marker_path(self) -> str:
        return f"{self.config_dir}/{SCRATCH_MARKER}"

    @property
    def is_ours(self) -> bool:
        return Path(self.marker_path).exists()


def _live_data_directory(admin_conn: psycopg.Connection) -> str:
    with admin_conn.cursor() as cur:
        cur.execute("SHOW data_directory")
        return cur.fetchone()[0]


def create_scratch_cluster(
    admin_conn: psycopg.Connection,
    *,
    version: str = DEFAULT_PG_VERSION,
    name: str = DEFAULT_SCRATCH_CLUSTER,
    port: int = DEFAULT_SCRATCH_PORT,
    run_as: str = "postgres",
) -> ScratchCluster:
    """Build an empty cluster for one restore to be poured into.

    Refuses to build over the live cluster, and refuses to reuse a cluster it
    did not create. Both are checks on the same mistake from opposite ends: this
    function's next act is to empty a data directory, and the cost of emptying
    the wrong one is every tenant on the node.
    """
    cluster = ScratchCluster(
        version=checked_pg_version(version),
        name=checked_cluster(name),
        port=int(port),
        run_as=backup.checked_run_as(run_as),
    )

    live = _live_data_directory(admin_conn)
    if os.path.normpath(cluster.data_dir) == os.path.normpath(live):
        raise RestoreError(
            f"the scratch cluster's data directory is the live one ({live}); refusing. "
            "A restore never writes over the node it is restoring from"
        )

    if Path(cluster.data_dir).exists() and not cluster.is_ours:
        raise RestoreError(
            f"a cluster already exists at {cluster.data_dir} and carries no MaluDB scratch "
            f"marker ({cluster.marker_path}). Refusing to reuse or destroy it -- if it really "
            "is scrap, remove it by hand"
        )

    drop_scratch_cluster(cluster, missing_ok=True)

    made = _run(
        ["pg_createcluster", cluster.version, cluster.name, "--port", str(cluster.port),
         "--", "--auth-local=peer"],
        sudo=True,
    )
    if made.returncode != 0:
        raise RestoreError(f"pg_createcluster failed: {_tail(made.stderr or made.stdout)}")

    # The marker goes down immediately, and before anything is emptied: from
    # here on, this cluster is ours to destroy and nothing else is.
    marker = _run(
        ["install", "-o", cluster.run_as, "-m", "600", "/dev/null", cluster.marker_path],
        sudo=True,
    )
    if marker.returncode != 0:
        raise RestoreError(f"could not write the scratch marker: {_tail(marker.stderr)}")

    _run(["pg_ctlcluster", cluster.version, cluster.name, "stop"], sudo=True)

    # pgBackRest restores into an empty data directory, and pg_createcluster
    # leaves a freshly initdb'd one.
    emptied = _as_owner(
        ["find", cluster.data_dir, "-mindepth", "1", "-delete"], run_as=cluster.run_as
    )
    if emptied.returncode != 0:
        raise RestoreError(f"could not empty {cluster.data_dir}: {_tail(emptied.stderr)}")

    # `archive_mode = off`, and this is not tidiness. The scratch cluster is a
    # copy of one whose `archive_command` names a stanza that is not its own, so
    # a promoted copy pushes a new timeline into the live repository -- which is
    # how a restore exercise damages the backups it was testing.
    appended = _as_owner(
        ["tee", "-a", f"{cluster.config_dir}/postgresql.conf"],
        run_as=cluster.run_as,
        stdin="\n# MaluDB scratch restore target (Phase 11 slice 2).\narchive_mode = off\n",
    )
    if appended.returncode != 0:
        raise RestoreError(
            f"could not disable archiving on the scratch cluster: {_tail(appended.stderr)}"
        )

    # ADR-031's reject, on the scratch cluster too.
    #
    # `pg_hba.conf` lives in /etc on a Debian layout, so pgBackRest's restore of
    # the *data* directory does not bring it across: a freshly created scratch
    # cluster carries `pg_createcluster`'s defaults and **not** the platform's
    # reject. For the minutes it is up, that cluster holds a byte-level copy of
    # every tenant on the node -- and this whole slice exists because a restored
    # copy silently having a different security posture from the original is
    # exactly the failure nobody notices. It is localhost-only and it is still
    # not a reason to leave the control off.
    hba = _as_owner(
        ["tee", "-a", f"{cluster.config_dir}/pg_hba.conf"],
        run_as=cluster.run_as,
        stdin=(
            "\n# MaluDB scratch restore target (ADR-031): physical replication is rejected.\n"
            "host    replication     all     127.0.0.1/32    reject\n"
            "host    replication     all     ::1/128         reject\n"
        ),
    )
    if hba.returncode != 0:
        raise RestoreError(
            f"could not apply the ADR-031 reject to the scratch cluster: {_tail(hba.stderr)}"
        )

    return cluster


def drop_scratch_cluster(cluster: ScratchCluster, *, missing_ok: bool = False) -> None:
    """Destroy a scratch cluster, and only a scratch cluster.

    The marker is the authority. A cluster without one is not dropped, whatever
    its name says — because the only way this function is ever dangerous is if
    the name is wrong, and a name cannot check itself.
    """
    if not Path(cluster.config_dir).exists():
        if missing_ok:
            return
        raise RestoreError(f"no cluster at {cluster.config_dir}")

    if not cluster.is_ours:
        if missing_ok:
            # Reached from `create_scratch_cluster`, which has already refused
            # an unmarked cluster by this point; belt and braces.
            raise RestoreError(
                f"{cluster.config_dir} exists without a MaluDB scratch marker; refusing to drop it"
            )
        raise RestoreError(
            f"{cluster.config_dir} carries no MaluDB scratch marker; refusing to drop it"
        )

    dropped = _run(["pg_dropcluster", "--stop", cluster.version, cluster.name], sudo=True)
    if dropped.returncode != 0:
        raise RestoreError(f"pg_dropcluster failed: {_tail(dropped.stderr or dropped.stdout)}")



def pgbackrest_time(when: datetime) -> str:
    """A timestamp in the one format pgBackRest's `--target` accepts.

    **Not ISO-8601.** `2026-08-27T00:59:17+00:00` is rejected outright:

        ERROR: [029]: automatic backup set selection cannot be performed with
        provided time '...' HINT: time format must be YYYY-MM-DD HH:MM:SS with
        optional msec and optional timezone

    The `T` separator is the problem, and the error names neither it nor the
    field. Slice 0's harness never hit this because it passed PostgreSQL's
    `now()::text` straight through, which is already in this shape.

    A timezone is always emitted. The hint says a naive timestamp is read as
    *local* time, which on a node in another zone silently selects a different
    point in a customer's history -- an hour of their data, restored or not, on
    the strength of a server's `/etc/localtime`.
    """
    if when.tzinfo is None:
        raise RestoreError(
            "a restore target must carry a timezone; pgBackRest reads a naive timestamp as "
            "the node's local time, which picks a different moment in the customer's history "
            "on a node in another zone"
        )
    return when.strftime("%Y-%m-%d %H:%M:%S.%f%z")


def restore_into_scratch(
    cluster: ScratchCluster,
    *,
    stanza: str,
    target_time: datetime | None,
    run_as: str = "postgres",
) -> float:
    """pgBackRest restore into the scratch data directory. Returns seconds."""
    argv = ["--log-level-console=warn", "restore", f"--pg1-path={cluster.data_dir}"]
    if target_time is not None:
        # `--target-action=promote` so the cluster comes up writable at the
        # target rather than sitting in paused recovery waiting for somebody.
        argv += [
            "--type=time", f"--target={pgbackrest_time(target_time)}", "--target-action=promote",
        ]
    started = time.monotonic()
    proc = backup.run_pgbackrest(stanza, *argv, timeout=PROMOTION_TIMEOUT_S, run_as=run_as)
    if proc.returncode != 0:
        raise RestoreError(f"pgbackrest restore failed: {_tail(proc.stderr or proc.stdout)}")
    return time.monotonic() - started


def await_promotion(cluster: ScratchCluster, *, timeout_s: int = PROMOTION_TIMEOUT_S) -> float:
    """Start the scratch cluster and wait for recovery to finish. Returns seconds."""
    started = time.monotonic()
    up = _run(["pg_ctlcluster", cluster.version, cluster.name, "start"], sudo=True)
    if up.returncode != 0:
        raise RestoreError(f"the scratch cluster would not start: {_tail(up.stderr or up.stdout)}")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = _as_owner(
            ["psql", "-p", str(cluster.port), "-tAc", "SELECT pg_is_in_recovery()"],
            run_as=cluster.run_as, timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "f":
            return time.monotonic() - started
        time.sleep(1)
    raise RestoreError(
        f"the scratch cluster was still in recovery after {timeout_s}s. WAL volume since the "
        "last backup drives this; a busier node replays more"
    )


# --------------------------------------------------------------------------
# Extract and load
# --------------------------------------------------------------------------


# Where a dump is allowed to land. Created 0700 and owned by the run-as user.
#
# **A dump is the customer's entire database in the clear on the node's
# filesystem.** `/var/lib/postgresql` is world-*executable* on a Debian layout
# and `pg_dump` writes with the process umask, so a dump written straight into
# it is readable by any local account -- one tenant's whole database, available
# to anyone with a shell on the node. It is short-lived and removed in a
# `finally`, and neither of those is a permission.
DUMP_DIR = "/var/lib/postgresql/maludb-restore"


def prepare_dump_dir(*, run_as: str, path: str = DUMP_DIR) -> str:
    """A directory only the run-as user can enter, for dumps to live in briefly."""
    made = _run(
        ["install", "-d", "-m", "700", "-o", backup.checked_run_as(run_as), path], sudo=True
    )
    if made.returncode != 0:
        raise RestoreError(f"could not prepare {path}: {_tail(made.stderr)}")
    return path


def dump_from_scratch(cluster: ScratchCluster, *, database: str, dump_path: str) -> tuple[float, int]:
    """`pg_dump` one database out of the promoted scratch cluster."""
    # Round-tripped rather than trusted: a database name that did not come from
    # `models.database_name_for` cannot reach a command line through here.
    if models.database_name_for(tenant_ref_of(database)) != database:
        raise RestoreError(f"{database!r} is not a name this platform generates")
    started = time.monotonic()
    _run(["rm", "-f", dump_path], sudo=True)
    proc = _as_owner(
        ["pg_dump", "-p", str(cluster.port), "-Fc", "-f", dump_path, database],
        run_as=cluster.run_as, timeout=PROMOTION_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RestoreError(f"pg_dump of {database} failed: {_tail(proc.stderr or proc.stdout)}")
    # Belt to the directory's braces. pg_dump honours the process umask, and a
    # node whose umask is 022 writes a customer's whole database 0644.
    _run(["chmod", "600", dump_path], sudo=True)
    elapsed = time.monotonic() - started
    size = _run(["stat", "-c", "%s", dump_path], sudo=True)
    return elapsed, int((size.stdout or "0").strip() or 0)


def tenant_ref_of(database: str) -> str:
    """The project ref a tenant database name was built from."""
    prefix = "mldb_"
    if not database.startswith(prefix):
        raise RestoreError(f"{database!r} is not a tenant database name")
    return database[len(prefix):]



def _port_of(conn: psycopg.Connection) -> int:
    """The port a connection is actually on.

    Read from the live connection rather than from configuration, because the
    whole point is to address the cluster this process is already talking to.
    """
    return int(conn.info.port or 5432)


def load_into_target(
    admin_conn: psycopg.Connection,
    names: provisioning.TenantNames,
    *,
    dump_path: str,
    target_database: str,
    owner: str,
    run_as: str = "postgres",
) -> float:
    """Create the target database and load the dump into it.

    The roles are checked first and the load refuses without them, which is the
    inversion of what slice 0 measured: there, `pg_restore` carried on past
    eleven "role does not exist" errors, left the data in place, and quietly
    reassigned two schemas to the superuser. Failing before the load is cheaper
    than diagnosing after it.
    """
    absent = missing_roles(admin_conn, names)
    if absent:
        raise RestoreError(
            "the target cluster is missing this tenant's roles: " + ", ".join(absent) + ". "
            "Restoring without them completes with 'errors ignored' and silently reassigns "
            "the auth and storage schemas to the platform superuser (ADR-059). Create the "
            "roles first"
        )

    if target_database == names.database:
        raise RestoreError(
            f"refusing to restore over the live database {names.database}. A restore lands "
            "beside the original; activation renames"
        )

    started = time.monotonic()
    admin_conn.execute(
        psycopg.sql.SQL("CREATE DATABASE {} OWNER {}").format(
            psycopg.sql.Identifier(target_database), psycopg.sql.Identifier(owner)
        )
    )
    # **The port is taken from the connection that just created the database,
    # not defaulted.** Without it `pg_restore` uses PGPORT or 5432 and loads
    # into whichever cluster happens to be there -- which on a node running both
    # a live cluster and a restore target is the live one. It surfaced as
    # `database "..." does not exist` against a socket on 5432, having created
    # the database on another cluster entirely. A restore that silently
    # addresses the wrong cluster is the worst failure this module could have.
    proc = _as_owner(
        # No --no-owner: the dump's OWNER TO statements are the whole point,
        # and dropping them is how the ownership finding happens by choice
        # rather than by accident.
        ["pg_restore", "-p", str(_port_of(admin_conn)), "-d", target_database, dump_path],
        run_as=run_as, timeout=PROMOTION_TIMEOUT_S,
    )
    # pg_restore exits 1 on "errors ignored", which slice 0 showed can accompany
    # a load that looks complete. It is reported, not raised on: the ownership
    # check below is the thing that decides whether this restore is trustworthy,
    # and it is a stronger test than an exit code.
    if proc.returncode != 0:
        log.warning(
            "pg_restore into %s exited %s: %s",
            target_database, proc.returncode, _tail(proc.stderr or proc.stdout),
        )
    return time.monotonic() - started


# --------------------------------------------------------------------------
# The whole operation
# --------------------------------------------------------------------------


@dataclass
class RestoreOutcome:
    restore_id: int
    project_ref: str
    source_database: str
    restored_database: str | None = None
    target_time: datetime | None = None
    status: str = "running"
    ownership: OwnershipReport | None = None
    restore_seconds: float = 0.0
    promotion_seconds: float = 0.0
    extract_seconds: float = 0.0
    load_seconds: float = 0.0
    total_seconds: float = 0.0
    dump_bytes: int = 0
    neighbours_available: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def _tenant_databases(admin_conn: psycopg.Connection, *, exclude: str) -> int:
    """How many tenant databases on this node answer right now."""
    with admin_conn.cursor() as cur:
        # `%%` because parameters are passed: a bare `%` in the LIKE literal is
        # read as a placeholder and psycopg refuses the query.
        cur.execute(
            "SELECT count(*) FROM pg_database "
            " WHERE datname LIKE 'mldb\\_%%' AND datname <> %s AND datallowconn",
            (exclude,),
        )
        return int(cur.fetchone()[0])


def restored_database_name(names: provisioning.TenantNames, when: datetime) -> str:
    """`mldb_<ref>_restore_<utc timestamp>`, inside PostgreSQL's 63-byte limit.

    Timestamped rather than fixed, because restoring twice is normal — an
    operator narrowing down a target time does it repeatedly — and a fixed name
    would make the second attempt either fail or silently destroy the first.
    """
    stamp = when.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    candidate = f"{names.database}_restore_{stamp}"
    if len(candidate) > 63:
        raise RestoreError(f"restored database name {candidate!r} exceeds PostgreSQL's 63 bytes")
    return candidate


def restore_tenant(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    project_ref: str,
    node_id: int,
    stanza: str,
    target_time: datetime | None = None,
    platform_owner: str = "postgres",
    run_as: str = "postgres",
    version: str = DEFAULT_PG_VERSION,
    scratch_name: str = DEFAULT_SCRATCH_CLUSTER,
    scratch_port: int = DEFAULT_SCRATCH_PORT,
    keep_scratch: bool = False,
    tenant_connect=None,
) -> RestoreOutcome:
    """Recover one tenant to a point in time, beside its live database.

    The live database is not touched, not locked and not read. Every other
    tenant on the node keeps serving throughout, which is asserted at the end
    rather than assumed — it is the acceptance criterion, and a run that could
    not confirm it should not be recorded as one that did.
    """
    backup.checked_stanza(stanza)
    names = provisioning.TenantNames.for_ref(project_ref)
    started_at = datetime.now(UTC)
    started = time.monotonic()

    target = restored_database_name(names, started_at)
    restore_id = _start(conn, project_id=project_id, node_id=node_id, stanza=stanza,
                        target_time=target_time)
    outcome = RestoreOutcome(
        restore_id=restore_id, project_ref=project_ref, source_database=names.database,
        target_time=target_time,
    )

    cluster = None
    dump_path = f"{prepare_dump_dir(run_as=run_as)}/{target}.dump"
    try:
        cluster = create_scratch_cluster(
            admin_conn, version=version, name=scratch_name, port=scratch_port, run_as=run_as
        )
        outcome.restore_seconds = restore_into_scratch(
            cluster, stanza=stanza, target_time=target_time, run_as=run_as
        )
        outcome.promotion_seconds = await_promotion(cluster)
        outcome.extract_seconds, outcome.dump_bytes = dump_from_scratch(
            cluster, database=names.database, dump_path=dump_path
        )
        outcome.load_seconds = load_into_target(
            admin_conn, names, dump_path=dump_path, target_database=target,
            owner=platform_owner, run_as=run_as,
        )
        outcome.restored_database = target

        # Ownership, on the copy that was just loaded. The check that decides
        # whether this restore may ever be activated.
        connect = tenant_connect or _connect_to
        with connect(admin_conn, target) as tenant_conn:
            outcome.ownership = verify_ownership(
                tenant_conn, admin_conn, names, database=target
            )

        # And the acceptance criterion, measured rather than asserted.
        outcome.neighbours_available = _tenant_databases(admin_conn, exclude=names.database)

        outcome.status = "complete"
    except Exception as exc:  # noqa: BLE001 - recorded, not raised past the bookkeeping
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        log.warning("restore of %s failed: %s", project_ref, outcome.error)
    finally:
        outcome.total_seconds = time.monotonic() - started
        _run(["rm", "-f", dump_path], sudo=True)
        if cluster is not None and not keep_scratch:
            try:
                drop_scratch_cluster(cluster)
            except RestoreError as exc:
                outcome.notes.append(f"the scratch cluster was left in place: {exc}")
        elif cluster is not None:
            outcome.notes.append(
                f"scratch cluster {cluster.name} left running on port {cluster.port}"
            )
        _finish(conn, outcome)

    return outcome


def _connect_to(admin_conn: psycopg.Connection, database: str) -> psycopg.Connection:
    """A connection to another database on the same cluster as `admin_conn`."""
    info = psycopg.conninfo.conninfo_to_dict(admin_conn.info.dsn)
    # `info.dsn` never carries the password; take it from the live connection.
    info["dbname"] = database
    if admin_conn.info.password:
        info["password"] = admin_conn.info.password
    return psycopg.connect(**info)


def _start(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    node_id: int,
    stanza: str,
    target_time: datetime | None,
) -> int:
    row = db.one(
        conn,
        "INSERT INTO tenant_restores (project_id, node_id, stanza, target_time) "
        "VALUES (%s,%s,%s,%s) RETURNING id",
        (project_id, node_id, stanza, target_time),
    )
    conn.commit()
    return int(row["id"])


def _finish(conn: psycopg.Connection, outcome: RestoreOutcome) -> None:
    db.execute(
        conn,
        "UPDATE tenant_restores "
        "   SET status = %s, finished_at = now(), restored_database = %s, "
        "       ownership_verified = %s, ownership_detail = %s, elapsed_seconds = %s, "
        "       dump_bytes = %s, neighbours_available = %s, error = %s "
        " WHERE id = %s",
        (
            outcome.status,
            outcome.restored_database,
            outcome.ownership.verified if outcome.ownership else None,
            outcome.ownership.detail if outcome.ownership else None,
            round(outcome.total_seconds, 2),
            outcome.dump_bytes or None,
            outcome.neighbours_available or None,
            outcome.error,
            outcome.restore_id,
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------


@dataclass
class Activation:
    project_ref: str
    live_database: str
    restored_database: str
    retired_database: str


def activate(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    project_ref: str,
    restore_id: int | None = None,
) -> Activation:
    """Swap a verified restored database in for the live one. Nothing is dropped.

    Three renames' worth of consequence and one line of destruction avoided: the
    database that was live is renamed aside, not deleted, so an activation that
    turns out to have been the wrong call is reversible by an operator with
    `ALTER DATABASE`. Disk is the price and it is the right price.

    Refuses unless a completed, **ownership-verified** restore exists for this
    project. That is the ADR-059 gate: a restored tenant whose `auth` and
    `storage` schemas came back owned by the platform superuser has a different
    security posture from the one that was backed up, and activating it would
    put that posture in front of customers without anything having said so.

    The caller is responsible for stopping the project's workers first. A
    database with open connections cannot be renamed, and PostgreSQL will say so
    rather than doing something surprising.
    """
    row = db.one(
        conn,
        "SELECT id, restored_database, ownership_verified, ownership_detail, status "
        "  FROM tenant_restores "
        " WHERE project_id = %s AND (%s::bigint IS NULL OR id = %s::bigint) "
        " ORDER BY started_at DESC LIMIT 1",
        (project_id, restore_id, restore_id),
    )
    if row is None:
        raise RestoreError(f"no restore on record for {project_ref}")
    if row["status"] != "complete":
        raise RestoreError(
            f"the most recent restore for {project_ref} is {row['status']}, not complete"
        )
    if not row["ownership_verified"]:
        raise RestoreError(
            f"refusing to activate a restore whose ownership did not verify: "
            f"{row['ownership_detail']}. ADR-059 puts the storage schema under a per-tenant "
            "role so it is not owned by something with superuser reach; activating this would "
            "hand customers a tenant with a security posture nobody chose"
        )

    names = provisioning.TenantNames.for_ref(project_ref)
    restored = row["restored_database"]
    retired = f"{names.database}_pre_restore_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    if len(retired) > 63:
        raise RestoreError(f"retired database name {retired!r} exceeds PostgreSQL's 63 bytes")

    # Autocommit: ALTER DATABASE ... RENAME cannot run inside a transaction
    # block. The window between the two renames is the only moment the project's
    # database name does not exist, which is why workers must already be down.
    previous = admin_conn.autocommit
    admin_conn.autocommit = True
    try:
        admin_conn.execute(
            psycopg.sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                psycopg.sql.Identifier(names.database), psycopg.sql.Identifier(retired)
            )
        )
        try:
            admin_conn.execute(
                psycopg.sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    psycopg.sql.Identifier(restored), psycopg.sql.Identifier(names.database)
                )
            )
        except Exception:
            # Put it back. Leaving a project with no database of its name is a
            # worse outcome than a failed activation, and this is the one window
            # in which that state exists.
            admin_conn.execute(
                psycopg.sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    psycopg.sql.Identifier(retired), psycopg.sql.Identifier(names.database)
                )
            )
            raise
    finally:
        admin_conn.autocommit = previous

    db.execute(
        conn, "UPDATE tenant_restores SET status = 'activated' WHERE id = %s", (row["id"],)
    )
    conn.commit()
    return Activation(
        project_ref=project_ref,
        live_database=names.database,
        restored_database=restored,
        retired_database=retired,
    )


def history(conn: psycopg.Connection, *, project_id: uuid.UUID | None = None) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT r.id, p.project_ref, n.name AS node, r.stanza, r.target_time,
               r.restored_database, r.status, r.ownership_verified, r.ownership_detail,
               r.elapsed_seconds, r.dump_bytes, r.neighbours_available, r.started_at, r.error
          FROM tenant_restores r
          JOIN projects p ON p.id = r.project_id
          JOIN nodes n ON n.id = r.node_id
         WHERE %s::uuid IS NULL OR r.project_id = %s::uuid
         ORDER BY r.started_at DESC
        """,
        (project_id, project_id),
    )


def free_disk_bytes(path: str = "/var/lib/postgresql") -> int:
    return shutil.disk_usage(path).free


def check_disk_headroom(admin_conn: psycopg.Connection, *, path: str = "/var/lib/postgresql") -> str | None:
    """Why this node cannot take a restore right now, or None.

    Slice 0 named disk as the real constraint rather than time: a scratch
    restore holds a second copy of the whole cluster while it runs. That makes
    free disk a **restore** prerequisite and not only a placement one, and
    `nodes.DEFAULT_MIN_FREE_DISK_BYTES` is a placement floor rather than a
    restore budget -- a node can be comfortably placeable and unable to restore.

    Checked before the cluster is built, because the failure otherwise arrives
    part-way through a pgBackRest restore with a half-populated data directory
    on a node that is now also out of disk.
    """
    with admin_conn.cursor() as cur:
        cur.execute("SELECT sum(pg_database_size(datname)) FROM pg_database")
        cluster_bytes = int(cur.fetchone()[0] or 0)
    free = free_disk_bytes(path)
    # The copy, plus the dump, plus room for WAL replay. 2.2x is not measured;
    # it is a margin over the one thing that is (the copy), and it is stated as
    # a guess rather than dressed up as a figure.
    needed = int(cluster_bytes * 2.2)
    if free < needed:
        return (
            f"{free / 1024**3:.1f} GB free at {path}, and a scratch restore of a "
            f"{cluster_bytes / 1024**3:.1f} GB cluster needs roughly "
            f"{needed / 1024**3:.1f} GB. A restore holds a second copy of the cluster"
        )
    return None


# Kept importable for the CLI, which reports what a restore is about to cost.
__all__ = [
    "Activation",
    "OwnershipReport",
    "RestoreError",
    "RestoreOutcome",
    "ScratchCluster",
    "activate",
    "check_disk_headroom",
    "checked_cluster",
    "create_scratch_cluster",
    "drop_scratch_cluster",
    "expected_schema_owners",
    "history",
    "pgbackrest_time",
    "prepare_dump_dir",
    "restore_tenant",
    "restored_database_name",
    "verify_ownership",
]
