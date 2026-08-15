"""Operator commands for node administration.

Deliberately a CLI, not HTTP routes. Registering nodes and recording health are
platform-staff operations, and `docs/ACCOUNTS.md` describes staff access as
explicit, time-bounded and audited -- a model that does not exist yet. Adding an
admin HTTP surface now would mean either inventing that model prematurely or
authenticating operators against customer credentials. Neither is worth doing
ahead of need; when the staff model lands, HTTP handlers can call these same
functions.

    cp-manage node register --name n1 --hostname n1.maludb.internal ...
    cp-manage node status --name n1 --status active
    cp-manage node health --name n1 --free-disk-bytes 500000000000
    cp-manage node list
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from services.control_plane import db, nodes


def _connect() -> str:
    url = os.environ.get("MALUDB_CONTROL_PLANE_DATABASE_URL", "").strip()
    if not url:
        print("MALUDB_CONTROL_PLANE_DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(2)
    return url


def _cmd_register(args: argparse.Namespace) -> int:
    capacity: dict[str, object] = {}
    if args.max_projects is not None:
        capacity["max_projects"] = args.max_projects
    if args.max_warm_projects is not None:
        capacity["max_warm_projects"] = args.max_warm_projects
    if args.min_free_disk_bytes is not None:
        capacity["min_free_disk_bytes"] = args.min_free_disk_bytes

    with db.connection() as conn:
        node_id = nodes.register_node(
            conn,
            name=args.name,
            hostname=args.hostname,
            internal_host=args.internal_host,
            node_pool=args.node_pool,
            capacity=capacity,
        )
        conn.commit()
    print(f"registered node {args.name} (id {node_id}) in 'maintenance'")
    print("it will not receive projects until: cp-manage node status --name "
          f"{args.name} --status active")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        nodes.set_status(conn, name=args.name, status=args.status)
        conn.commit()
    print(f"{args.name} -> {args.status}")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    metrics = json.loads(args.metrics) if args.metrics else {}
    if args.free_disk_bytes is not None:
        metrics["free_disk_bytes"] = args.free_disk_bytes
    with db.connection() as conn:
        nodes.record_health(conn, name=args.name, metrics=metrics)
        conn.commit()
    print(f"recorded health for {args.name}: {metrics}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT id, name, node_pool, status, last_health_at FROM nodes ORDER BY name",
        )
        print(f"{'NAME':<20} {'POOL':<10} {'STATUS':<12} {'PROJECTS':<12} {'HEALTH':<22} ELIGIBLE")
        for row in rows:
            capacity = nodes.capacity_of(conn, row["id"])
            reason = capacity.rejection_reason()
            health = row["last_health_at"].isoformat(timespec="seconds") if row["last_health_at"] else "never"
            print(
                f"{row['name']:<20} {row['node_pool']:<10} {row['status']:<12} "
                f"{f'{capacity.current_projects}/{capacity.max_projects}':<12} {health:<22} "
                f"{'yes' if row['status'] == 'active' and not reason else (reason or 'not active')}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cp-manage", description="MaluDB control-plane operator commands")
    sub = parser.add_subparsers(dest="group", required=True)
    node = sub.add_parser("node", help="node administration").add_subparsers(dest="command", required=True)

    register = node.add_parser("register", help="register or update a node")
    register.add_argument("--name", required=True)
    register.add_argument("--hostname", required=True)
    register.add_argument("--internal-host", required=True)
    register.add_argument("--node-pool", default="shared")
    register.add_argument("--max-projects", type=int)
    register.add_argument("--max-warm-projects", type=int)
    register.add_argument("--min-free-disk-bytes", type=int)
    register.set_defaults(func=_cmd_register)

    status = node.add_parser("status", help="set node lifecycle status")
    status.add_argument("--name", required=True)
    status.add_argument("--status", required=True, choices=["active", "draining", "maintenance", "unhealthy"])
    status.set_defaults(func=_cmd_status)

    health = node.add_parser("health", help="record a health report")
    health.add_argument("--name", required=True)
    health.add_argument("--free-disk-bytes", type=int)
    health.add_argument("--metrics", help="additional metrics as a JSON object")
    health.set_defaults(func=_cmd_health)

    listing = node.add_parser("list", help="list nodes and placement eligibility")
    listing.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_pool(_connect())
    try:
        return int(args.func(args))
    except (ValueError, nodes.PlacementError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
