"""A node's health reporter, and the permission model of the role it uses (ADR-080).

Placement refuses a node whose last health report is older than
`nodes.HEALTH_STALE_AFTER`. Something on the node has to send that report, and
before this nothing could: `cp-manage node health` needs the control plane's own
role and the KEK, neither of which belongs on a node.

This runs on the node as `maludb-node-reporter`. It needs no KEK, reads no
configuration beyond its own environment, and connects to the control plane's
database as a role that holds exactly one privilege: EXECUTE on
`public.report_node_health(bigint)` (migration 0050). What that function writes,
and for which node, is decided inside the database.

**It reports only while local PostgreSQL answers.** A report is a claim that the
node can take a project, and a node whose cluster is down cannot -- so rather
than report "alive" from a process that merely runs, it stops reporting, and
placement stops choosing the node within `HEALTH_STALE_AFTER`.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass

import psycopg
from psycopg import pq, sql

from services.control_plane import db, memory_worker_grants
from services.control_plane import logging as cp_logging

log = logging.getLogger(__name__)

FUNCTION = "public.report_node_health(bigint)"
DEFAULT_DATA_DIRECTORY = "/var/lib/postgresql"
DEFAULT_LOCAL_POSTGRES = "host=127.0.0.1 port=5432 dbname=postgres connect_timeout=5"
DEFAULT_INTERVAL_SECONDS = 60
# Below nodes.HEALTH_STALE_AFTER (five minutes) with room for two missed reports.
MAX_INTERVAL_SECONDS = 90


# -- the permission model, applied from the control plane ---------------------


def statements(role: str) -> list[sql.Composed]:
    """Everything the reporter's role is granted: the schema, and the one function."""
    ident = sql.Identifier(role)
    return [
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident),
        sql.SQL("GRANT EXECUTE ON FUNCTION public.report_node_health(bigint) TO {}").format(ident),
    ]


def refusal(conn: psycopg.Connection, *, role: str, node: str) -> str | None:
    """Why this role must not become this node's reporter, or None. Expects `dict_row` (the pool's)."""
    row = db.one(conn, "SELECT rolsuper, rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = %s", (role,))
    if row is None:
        return f"no role named {role!r}; create it first as a superuser: CREATE ROLE {role} LOGIN PASSWORD '<strong>'"
    if row["rolsuper"]:
        return f"role {role!r} is a superuser; a reporter must hold nothing but the report"
    if not row["rolcanlogin"]:
        return f"role {role!r} cannot log in; the reporter connects as it, and identity is the login (session_user)"
    if db.one(conn, "SELECT pg_catalog.pg_has_role(%s, c.relowner, 'MEMBER') AS yes FROM pg_catalog.pg_class c "
                    "WHERE c.oid = 'public.nodes'::regclass", (role,))["yes"]:
        return f"role {role!r} owns the control plane's tables (or is a member of their owner)"
    gateway = db.one(conn, "SELECT name FROM nodes WHERE gateway_role = %s", (role,))
    if gateway is not None:
        return f"role {role!r} is the gateway role of node {gateway['name']!r}; give the reporter its own role"
    if db.one(conn, "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles g WHERE g.rolname = ANY(%s) "
                    "AND pg_catalog.pg_has_role(%s, g.oid, 'MEMBER')) AS yes",
              ([memory_worker_grants.GROUP_ROLE, memory_worker_grants.EMBEDDER_GROUP_ROLE], role))["yes"]:
        return f"role {role!r} is a member of a memory worker group; give the reporter its own role"
    recorder = db.one(conn, "SELECT name FROM nodes WHERE backup_recorder_role = %s", (role,))
    if recorder is not None:
        return f"role {role!r} records backups for node {recorder['name']!r}; give the reporter its own role"
    clash = db.one(conn, "SELECT name FROM nodes WHERE health_reporter_role = %s AND name <> %s", (role, node))
    if clash is not None:
        return f"role {role!r} already reports for node {clash['name']!r}; one role per node (ADR-080)"
    return None


def wider_than_the_model(conn: psycopg.Connection, role: str) -> list[str]:
    """Every privilege on a control-plane table or view this role holds. The model grants none."""
    rows = db.query(
        conn,
        """
        SELECT c.relname || ':' || p.privilege AS privilege_on
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE']) AS p(privilege)
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
           AND (pg_catalog.has_table_privilege(%s, c.oid, p.privilege)
                OR pg_catalog.has_any_column_privilege(%s, c.oid, p.privilege))
         ORDER BY 1
        """,
        (role, role),
    )
    rows += db.query(
        conn,
        """
        SELECT c.relname || ':DELETE' AS privilege_on
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND pg_catalog.has_table_privilege(%s, c.oid, 'DELETE')
        """,
        (role,),
    )
    return sorted(r["privilege_on"] for r in rows)


def can_report(conn: psycopg.Connection, role: str) -> bool:
    return bool(db.one(conn, "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE') AS yes",
                       (role, FUNCTION))["yes"])


# -- the reporter, on the node -------------------------------------------------


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_directory: str = DEFAULT_DATA_DIRECTORY
    local_postgres: str = DEFAULT_LOCAL_POSTGRES
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS

    @classmethod
    def from_environment(cls) -> Settings:
        url = os.environ.get("MALUDB_NODE_REPORTER_DATABASE_URL", "").strip()
        if not url:
            raise SystemExit("MALUDB_NODE_REPORTER_DATABASE_URL is required: the reporter role's DSN (ADR-080)")
        interval = int(os.environ.get("MALUDB_NODE_REPORT_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
        if not 5 <= interval <= MAX_INTERVAL_SECONDS:
            raise SystemExit(f"MALUDB_NODE_REPORT_INTERVAL_SECONDS must be 5..{MAX_INTERVAL_SECONDS}; "
                             "a slower report lets the node go stale between reports")
        return cls(
            database_url=url,
            data_directory=os.environ.get("MALUDB_NODE_DATA_DIRECTORY", "").strip() or DEFAULT_DATA_DIRECTORY,
            local_postgres=os.environ.get("MALUDB_NODE_LOCAL_POSTGRES", "").strip() or DEFAULT_LOCAL_POSTGRES,
            interval_seconds=interval,
        )


def free_disk_bytes(path: str) -> int:
    """Bytes available to an unprivileged writer, as `df` reports them."""
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


def local_postgres_answers(conninfo: str) -> bool:
    """Whether the node's cluster is accepting connections. PQping authenticates nothing, so
    no credential is needed; PQPING_REJECT is a server in startup, shutdown or recovery, which
    cannot take a project either."""
    return pq.PGconn.ping(conninfo.encode()) == pq.Ping.OK


def report_once(settings: Settings, *, connect=psycopg.connect) -> str | None:
    """Send one report. Returns the node reported for, or None when nothing was sent."""
    if not local_postgres_answers(settings.local_postgres):
        log.warning("local PostgreSQL is not answering; not reporting, so placement stops choosing this node")
        return None
    free = free_disk_bytes(settings.data_directory)
    with connect(settings.database_url, connect_timeout=10, autocommit=True) as conn:
        node = conn.execute("SELECT public.report_node_health(%s)", (free,)).fetchone()[0]
    log.info("reported health for %s: free_disk_bytes=%d", node, free)
    return node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maludb-node-reporter", description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", help="send one report and exit (0 sent, 1 not)")
    args = parser.parse_args(argv)
    cp_logging.configure()
    settings = Settings.from_environment()

    if args.once:
        try:
            return 0 if report_once(settings) else 1
        except psycopg.Error as exc:
            # The type and the server's message; never the DSN.
            log.error("report failed: %s: %s", type(exc).__name__, (exc.diag.message_primary or "").strip())
            return 1

    stopping = False

    def stop(signum, _frame) -> None:  # noqa: ANN001 - signal handler signature
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            report_once(settings)
        except psycopg.Error as exc:
            log.error("report failed: %s: %s", type(exc).__name__, (exc.diag.message_primary or "").strip())
        except OSError as exc:
            log.error("report failed: %s", exc)
        deadline = time.monotonic() + settings.interval_seconds
        while not stopping and time.monotonic() < deadline:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
