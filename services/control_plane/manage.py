"""Operator commands for node administration and provisioning recovery.

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

Provisioning recovery is here for a second reason as well: `cleanup` can drop a
tenant database, and a destructive operation should be something a person types
with a flag on it, not something a retry loop reaches on its own.

    cp-manage project failed
    cp-manage project retry --ref abcd1234
    cp-manage project cleanup --ref abcd1234 [--allow-database-drop]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

from services.control_plane import config, crypto, db, jobs, nodes, provisioning


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


# --------------------------------------------------------------------------
# Projects. Retry and cleanup are operator actions on tenants that failed to
# provision; cleanup can destroy a database, so it is deliberately a command
# somebody has to type rather than anything automatic.
# --------------------------------------------------------------------------


def _project_context(conn, project_ref: str):
    """Resolve a project to the pieces the provisioning code needs.

    Returns (project_id, admin_conn, tenant_connect, key_ring). The admin DSN is
    a live credential: it is decrypted here, handed straight to psycopg, and
    never printed or logged.
    """
    row = db.one(
        conn,
        "SELECT id, node_id, status FROM projects WHERE project_ref = %s AND deleted_at IS NULL",
        (project_ref,),
    )
    if row is None:
        raise ValueError(f"no project with ref {project_ref}")
    if row["node_id"] is None:
        raise ValueError(f"project {project_ref} has no node placement")

    key_ring = crypto.KeyRing(config.load().kek)
    key_ring.load(conn)
    dsn = nodes.admin_dsn(conn, node_id=row["node_id"], key_ring=key_ring)
    admin_conn = psycopg.connect(dsn)

    def tenant_connect(database: str):
        parsed = psycopg.conninfo.conninfo_to_dict(dsn)
        parsed["dbname"] = database
        return psycopg.connect(psycopg.conninfo.make_conninfo(**parsed), autocommit=True)

    return row["id"], admin_conn, tenant_connect, key_ring


def _cmd_project_failed(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        rows = db.query(
            conn,
            """
            SELECT p.project_ref, p.status, p.database_name, p.failed_at, p.retry_after,
                   j.attempt, j.error_code
              FROM projects p
              LEFT JOIN LATERAL (
                    SELECT attempt, error_code FROM provisioning_jobs
                     WHERE project_id = p.id ORDER BY attempt DESC LIMIT 1) j ON true
             WHERE p.status IN ('RETRY_WAIT', 'FAILED') AND p.deleted_at IS NULL
             ORDER BY p.failed_at
            """,
        )
    if not rows:
        print("no projects awaiting retry or failed")
        return 0
    print(f"{'REF':<14} {'STATUS':<12} {'ATTEMPT':<8} {'DATABASE':<24} {'ERROR':<22} RETRY AFTER")
    for row in rows:
        retry = row["retry_after"].isoformat(timespec="seconds") if row["retry_after"] else "-"
        print(
            f"{row['project_ref']:<14} {row['status']:<12} {str(row['attempt'] or '-'):<8} "
            f"{(row['database_name'] or '-'):<24} {(row['error_code'] or '-'):<22} {retry}"
        )
    return 0


def _cmd_project_retry(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        project_id, admin_conn, tenant_connect, key_ring = _project_context(conn, args.ref)
        try:
            names = jobs.provision(
                conn,
                admin_conn,
                project_id=project_id,
                key_ring=key_ring,
                platform_owner=args.platform_owner,
                tenant_connect=tenant_connect,
            )
        finally:
            admin_conn.close()
    print(f"project {args.ref} provisioned: database {names.database}")
    return 0


def _cmd_project_cleanup(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        project_id, admin_conn, tenant_connect, _ = _project_context(conn, args.ref)
        try:
            report = jobs.cleanup(
                conn,
                admin_conn,
                project_id=project_id,
                tenant_connect=tenant_connect,
                allow_database_drop=args.allow_database_drop,
            )
        finally:
            admin_conn.close()

    if report.refused_because:
        print(f"refused: {report.refused_because}")
        print(f"database {report.retained_database} was left in place")
        return 1
    print(f"dropped database: {report.dropped_database or 'none'}")
    print(f"dropped roles: {', '.join(report.dropped_roles) or 'none'}")
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

    project = sub.add_parser("project", help="tenant provisioning recovery").add_subparsers(
        dest="command", required=True
    )

    failed = project.add_parser("failed", help="list projects awaiting retry or failed")
    failed.set_defaults(func=_cmd_project_failed)

    retry = project.add_parser("retry", help="resume provisioning for a project")
    retry.add_argument("--ref", required=True)
    retry.add_argument("--platform-owner", default=os.environ.get("MALUDB_PLATFORM_OWNER", "postgres"))
    retry.set_defaults(func=_cmd_project_retry)

    cleanup = project.add_parser(
        "cleanup",
        help="reclaim roles, and optionally the database, from a failed project",
    )
    cleanup.add_argument("--ref", required=True)
    cleanup.add_argument(
        "--allow-database-drop",
        action="store_true",
        help="permit dropping the tenant database. It is still refused if the project was ever "
             "provisioned or the database holds any tenant-created object.",
    )
    cleanup.set_defaults(func=_cmd_project_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_pool(_connect())
    try:
        return int(args.func(args))
    except (ValueError, nodes.PlacementError, provisioning.ProvisioningError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
