"""The node's half of the maintenance pass: sleep idle workers (ADR-083).

Runs **on a node**, as `maludb-gateway`, connected with the gateway's own narrowed role
(`MALUDB_GATEWAY_DATABASE_URL`). It needs nothing the gateway does not already hold:

- the gateway role reads and updates its node's `projects` rows -- its row policy shows it
  no other node's -- including the worker states and last-activity times it writes itself;
- `deploy/50-maludb-gateway.rules` lets `maludb-gateway` start and stop exactly the
  per-project PostgREST, GoTrue and Realtime units.

**It holds no KEK and no node credential**, and imports nothing that reaches one:
`tests/test_node_maintenance.py` walks its import graph. Its queries name the node
explicitly as well as relying on the row policy, so a mistake that ran it with a wider role
still touches only this node.

The control-plane half (`cp-manage maintenance run --skip sleep`) does everything else.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from services.control_plane import logging as cp_logging
from services.control_plane import supervision

log = logging.getLogger(__name__)

# `maintenance.DEFAULT_IDLE_MINUTES` and `REALTIME_IDLE_MINUTES`, restated: `maintenance`
# imports the key ring and every node-credential pass.
DEFAULT_IDLE_MINUTES = 15
REALTIME_IDLE_MINUTES = 60


@dataclass(frozen=True)
class Kind:
    """One kind of per-project worker: its state columns and its unit."""

    name: str
    state_column: str
    active_column: str
    template: str


KINDS = (
    Kind("api", "worker_state", "worker_last_active_at", supervision.POSTGREST_TEMPLATE),
    Kind("auth", "auth_worker_state", "auth_worker_last_active_at", supervision.GOTRUE_TEMPLATE),
    Kind("realtime", "realtime_worker_state", "realtime_worker_last_active_at", supervision.REALTIME_TEMPLATE),
)

# Column names above are literals of this module; nothing a request says reaches this SQL.
_IDLE = """
    SELECT id, project_ref FROM projects
     WHERE node_id = %s AND {state} = 'RUNNING' AND deleted_at IS NULL
       AND ({active} IS NULL OR {active} < now() - make_interval(mins => %s))
     ORDER BY {active} NULLS FIRST
"""
_STOPPED = "UPDATE projects SET {state} = 'STOPPED' WHERE id = %s AND node_id = %s AND {state} = 'RUNNING'"


@dataclass
class Result:
    slept: int = 0
    failed: int = 0
    detail: list[str] = field(default_factory=list)


class NotAGateway(RuntimeError):
    """The connection's role is not mapped to a node, so there is nothing it may sleep."""


def node_id(conn: psycopg.Connection) -> int:
    """Which node this role serves, from the database (ADR-072's `gateway_node_id()`)."""
    row = conn.execute("SELECT public.gateway_node_id() AS node").fetchone()
    node = row["node"] if isinstance(row, dict) else row[0]
    if node is None:
        raise NotAGateway(
            "this database role is not mapped to a node (nodes.gateway_role), so it sleeps nothing. "
            "Run the node pass with the gateway's own MALUDB_GATEWAY_DATABASE_URL"
        )
    return int(node)


def sleep_idle(
    conn: psycopg.Connection,
    *,
    node: int,
    supervisors: dict[str, supervision.Supervisor],
    idle_minutes: int = DEFAULT_IDLE_MINUTES,
    realtime_idle_minutes: int = REALTIME_IDLE_MINUTES,
) -> Result:
    """Stop this node's workers nothing has used recently. The databases are untouched."""
    result = Result()
    for kind in KINDS:
        supervisor = supervisors.get(kind.name)
        if supervisor is None:
            continue
        minutes = realtime_idle_minutes if kind.name == "realtime" else idle_minutes
        idle = conn.execute(
            _IDLE.format(state=kind.state_column, active=kind.active_column), (node, minutes)
        ).fetchall()
        conn.commit()
        for project in idle:
            try:
                supervisor.stop(project["project_ref"])
                conn.execute(_STOPPED.format(state=kind.state_column), (project["id"], node))
                conn.commit()
                result.slept += 1
                result.detail.append(f"slept {kind.name} worker for {project['project_ref']}")
            except supervision.WorkerError as exc:
                conn.rollback()
                result.failed += 1
                result.detail.append(f"could not sleep {kind.name} worker for {project['project_ref']}: {exc}")
                log.warning("could not sleep %s worker for %s: %s", kind.name, project["project_ref"], exc)
    return result


def run(conn: psycopg.Connection, *, supervisors: dict[str, supervision.Supervisor], idle_minutes: int,
        realtime_idle_minutes: int) -> Result:
    """One recorded run: a row before, the counts after, so a run that dies leaves evidence."""
    node = node_id(conn)
    run_id = conn.execute(
        "INSERT INTO node_maintenance_runs (node_id) VALUES (%s) RETURNING id", (node,)
    ).fetchone()["id"]
    conn.commit()
    result = sleep_idle(conn, node=node, supervisors=supervisors, idle_minutes=idle_minutes,
                        realtime_idle_minutes=realtime_idle_minutes)
    conn.execute(
        "UPDATE node_maintenance_runs SET finished_at = now(), slept = %s, failed = %s WHERE id = %s",
        (result.slept, result.failed, run_id),
    )
    conn.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sleep this node's idle workers (ADR-083).")
    parser.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_MINUTES)
    parser.add_argument("--realtime-idle-minutes", type=int, default=REALTIME_IDLE_MINUTES)
    args = parser.parse_args(argv)
    cp_logging.configure()
    dsn = os.environ.get("MALUDB_GATEWAY_DATABASE_URL", "").strip()
    if not dsn:
        print("MALUDB_GATEWAY_DATABASE_URL is required: the node pass runs as the gateway's own role",
              file=sys.stderr)
        return 2
    supervisors = {kind.name: supervision.SystemdSupervisor(template=kind.template) for kind in KINDS}
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            result = run(conn, supervisors=supervisors, idle_minutes=args.idle_minutes,
                         realtime_idle_minutes=args.realtime_idle_minutes)
    except NotAGateway as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in result.detail:
        print(line)
    print(f"slept {result.slept}, failed {result.failed}")
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
