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
    cp-manage plans sync
    cp-manage plans list
    cp-manage node realtime-check --name n1
    cp-manage realtime slots [--node n1]
    cp-manage project realtime --ref abcd1234 --enable|--disable
    cp-manage project realtime-recover --ref abcd1234
    cp-manage project realtime-worker --ref abcd1234 --start|--stop|--status

`node realtime-check` belongs in node build rather than in provisioning: three
of the five Realtime preconditions need a cluster restart, so a node checked
after it has tenants costs downtime to fix (ADR-031, ADR-032).

Provisioning recovery is here for a second reason as well: `cleanup` can drop a
tenant database, and a destructive operation should be something a person types
with a flag on it, not something a retry loop reaches on its own.

    cp-manage project failed
    cp-manage project retry --ref abcd1234
    cp-manage project cleanup --ref abcd1234 [--allow-database-drop]

Project API keys, until the dashboard owns issuance in Phase 07:

    cp-manage key issue --ref abcd1234 --type publishable --name web
    cp-manage key list --ref abcd1234
    cp-manage key reveal --ref abcd1234 --id <uuid>
    cp-manage key revoke --ref abcd1234 --id <uuid>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg

from services.control_plane import (
    api_keys,
    auth_workers,
    config,
    crypto,
    db,
    entitlements,
    jobs,
    mail,
    maintenance,
    nodes,
    provisioning,
    realtime,
    realtime_workers,
    storage,
)


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


def _project_id(conn, project_ref: str) -> uuid.UUID:
    row = db.one(
        conn,
        "SELECT id FROM projects WHERE project_ref = %s AND deleted_at IS NULL",
        (project_ref,),
    )
    if row is None:
        raise ValueError(f"no project with ref {project_ref}")
    return row["id"]


def _cmd_key_issue(args: argparse.Namespace) -> int:
    """Mint a project API key.

    The plaintext is printed once and never again. That is the point of the
    Class A storage for secret keys (ADR-023) rather than an inconvenience:
    there is nothing in the database that could reproduce it.
    """
    settings = config.load()
    with db.connection() as conn:
        project_id = _project_id(conn, args.ref)
        key_ring = None
        if args.type == api_keys.PUBLISHABLE:
            key_ring = crypto.KeyRing(settings.kek)
            key_ring.load(conn)
        issued = api_keys.create(
            conn,
            project_id=project_id,
            key_type=args.type,
            pepper=settings.token_pepper,
            key_ring=key_ring,
            name=args.name,
        )
        conn.commit()

    print(f"issued {issued.key_type} key for {args.ref}")
    print(f"  id:         {issued.id}")
    print(f"  identifier: {issued.key_identifier}")
    if issued.key_type == api_keys.SECRET:
        print("\n  This is shown once. It is stored hashed and cannot be recovered.")
    print(f"\n{issued.plaintext}")
    return 0


def _cmd_key_list(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        rows = api_keys.list_for_project(conn, project_id=_project_id(conn, args.ref))
    if not rows:
        print(f"no api keys for {args.ref}")
        return 0
    print(f"{'ID':<38} {'TYPE':<12} {'NAME':<16} {'LAST USED':<22} STATUS")
    for row in rows:
        used = row["last_used_at"].isoformat(timespec="seconds") if row["last_used_at"] else "never"
        status = "revoked" if row["revoked_at"] else "live"
        print(f"{str(row['id']):<38} {row['key_type']:<12} {(row['name'] or '-'):<16} {used:<22} {status}")
    return 0


def _cmd_key_reveal(args: argparse.Namespace) -> int:
    """Show a publishable key again. There is deliberately no secret equivalent."""
    settings = config.load()
    with db.connection() as conn:
        project_id = _project_id(conn, args.ref)
        key_ring = crypto.KeyRing(settings.kek)
        key_ring.load(conn)
        print(api_keys.reveal_publishable(
            conn, key_id=uuid.UUID(args.id), project_id=project_id, key_ring=key_ring
        ))
    return 0


def _cmd_key_revoke(args: argparse.Namespace) -> int:
    with db.connection() as conn:
        revoked = api_keys.revoke(
            conn, key_id=uuid.UUID(args.id), project_id=_project_id(conn, args.ref)
        )
        conn.commit()
    if not revoked:
        print(f"no live key {args.id} for {args.ref}", file=sys.stderr)
        return 1
    print(f"revoked {args.id}")
    return 0


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


def _cmd_project_backfill_executor(args: argparse.Namespace) -> int:
    """Give an already-provisioned project the ADR-039 executor role.

    Every project provisioned before that ADR has the role's step in its
    pipeline but has already passed the point where the pipeline runs. Re-running
    `provision` would reach the new step, but a project can only be sent back
    through provisioning while it is not serving -- and this must work on a
    project that is.

    Idempotent by the same predicate the provisioning step uses, so running it
    across a fleet twice is a no-op the second time. It never touches the other
    three roles: the failure this exists to avoid is a backfill that resets the
    authenticator password and stops every PostgREST worker on the node.
    """
    with db.connection() as conn:
        project_id, admin_conn, _, key_ring = _project_context(conn, args.ref)
        names = provisioning.TenantNames.for_ref(args.ref)
        try:
            existing = db.one(
                conn,
                "SELECT count(*) AS live FROM project_credentials "
                " WHERE project_id = %s AND revoked_at IS NULL "
                "   AND credential_type = 'db_executor'",
                (project_id,),
            )
            if existing["live"] and provisioning.has_executor_role(admin_conn, names):
                print(f"project {args.ref}: executor already present")
                return 0

            password = provisioning.generate_password()
            provisioning.create_executor_role(admin_conn, names, password=password)
            provisioning.grant_executor_connect(admin_conn, names)
            provisioning.store_credential(
                conn,
                project_id=project_id,
                credential_type="db_executor",
                role_name=names.executor,
                secret=password,
                key_ring=key_ring,
            )
            conn.commit()
            admin_conn.commit()
        finally:
            admin_conn.close()
    print(f"project {args.ref}: executor role {names.executor} created")
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


def _cmd_project_email(args: argparse.Namespace) -> int:
    """Configure a project's sending mode.

    The hook secret is minted here and never shown: the Auth worker reads it
    back through the key ring, and a human never needs to see it. The customer's
    MaluMail key on custom_domain is read from an environment variable rather
    than an argument, so it does not land in shell history or a process listing.
    """
    key_ring = crypto.KeyRing(config.load().kek)
    with db.connection() as conn:
        key_ring.load(conn)
        project = db.one(
            conn,
            "SELECT id, display_name FROM projects WHERE project_ref = %s AND deleted_at IS NULL",
            (args.ref,),
        )
        if project is None:
            raise ValueError(f"no project with ref {args.ref}")

        customer_key = os.environ.get("MALUDB_PROJECT_MALUMAIL_KEY", "").strip()
        if args.mode == "custom_domain" and not customer_key:
            raise ValueError(
                "custom_domain needs the customer's MaluMail key in "
                "MALUDB_PROJECT_MALUMAIL_KEY; passing it as an argument would put it "
                "in shell history and the process list"
            )

        secret = mail.generate_hook_secret()
        hook = key_ring.seal(
            secret.encode(),
            aad=crypto.aad_for("project_email_settings", "hook", str(project["id"])),
        )
        sealed_key = None
        if customer_key:
            sealed_key = key_ring.seal(
                customer_key.encode(),
                aad=crypto.aad_for("project_email_settings", "malumail", str(project["id"])),
            )

        db.execute(
            conn,
            """
            INSERT INTO project_email_settings
                (project_id, sender_mode, sender_address, sender_name,
                 hook_ciphertext, hook_nonce, hook_key_version,
                 malumail_ciphertext, malumail_nonce, malumail_key_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (project_id) DO UPDATE SET
                sender_mode = EXCLUDED.sender_mode,
                sender_address = EXCLUDED.sender_address,
                sender_name = EXCLUDED.sender_name,
                hook_ciphertext = EXCLUDED.hook_ciphertext,
                hook_nonce = EXCLUDED.hook_nonce,
                hook_key_version = EXCLUDED.hook_key_version,
                malumail_ciphertext = EXCLUDED.malumail_ciphertext,
                malumail_nonce = EXCLUDED.malumail_nonce,
                malumail_key_version = EXCLUDED.malumail_key_version,
                updated_at = now()
            """,
            (project["id"], args.mode, args.sender, args.sender_name,
             hook.ciphertext, hook.nonce, hook.key_version,
             sealed_key.ciphertext if sealed_key else None,
             sealed_key.nonce if sealed_key else None,
             sealed_key.key_version if sealed_key else None),
        )
        conn.commit()

    print(f"{args.ref}: sender_mode={args.mode} from={args.sender}")
    print("a new hook secret was generated; restart the Auth worker to pick it up:")
    print(f"  cp-manage project retry --ref {args.ref}")
    return 0


def _cmd_email_reconcile(args: argparse.Namespace) -> int:
    """Pull MaluMail's suppression list into the control plane.

    MaluMail has no webhooks (ADR-029), so bounces and complaints arrive only as
    entries on a list. Run this on a schedule: a suppressed address the platform
    has not heard about still fails, but it fails after spending an API call and
    a quota unit rather than before.
    """
    settings = config.load()
    if not settings.malumail_api_key:
        raise ValueError("MALUMAIL_API is not set; nothing to reconcile against")
    client = mail.MaluMail(settings.malumail_api_key)
    with db.connection() as conn:
        added = mail.reconcile_suppressions(conn, client, pepper=settings.token_pepper)
    print(f"added {added} suppression(s)")
    return 0


def _cmd_project_storage(args: argparse.Namespace) -> int:
    """Measure a project and enforce the result.

    Prints what it found before saying what it did, because the interesting
    question when a customer asks why they were cut off is which number
    produced the decision.
    """
    with db.connection() as conn:
        project_id, admin_conn, tenant_connect, _ = _project_context(conn, args.ref)
        try:
            usage = storage.evaluate(
                conn, admin_conn, project_id=project_id, tenant_connect=tenant_connect
            )
        finally:
            admin_conn.close()

    mb = 1024 * 1024
    print(f"{args.ref}: {usage.billable_bytes / mb:.1f} MB of "
          f"{usage.quota_bytes / mb:.1f} MB ({usage.fraction:.0%})")
    print(f"  on disk including the maludb_core baseline: {usage.gross_bytes / mb:.1f} MB")
    print(f"  state: {usage.state}")
    if usage.state == storage.RESTRICTED:
        print("  writes are revoked from anon and authenticated. Reads, deletes and")
        print("  truncates still work, and service_role is untouched -- the project")
        print("  can delete its way back under and the next pass restores writes.")
    return 0


def _cmd_storage_report(args: argparse.Namespace) -> int:
    """What every project is using, least recently measured first."""
    mb = 1024 * 1024
    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT project_ref, database_bytes, storage_baseline_bytes, storage_state, "
            "       database_measured_at FROM projects "
            " WHERE database_name IS NOT NULL AND deleted_at IS NULL "
            " ORDER BY database_measured_at NULLS FIRST",
        )
    if not rows:
        print("no provisioned projects")
        return 0
    print(f"{'REF':<14} {'STATE':<12} {'ON DISK':<12} {'MEASURED':<22}")
    for row in rows:
        size = f"{row['database_bytes'] / mb:.1f} MB" if row["database_bytes"] else "never"
        when = (row["database_measured_at"].isoformat(timespec="seconds")
                if row["database_measured_at"] else "never")
        print(f"{row['project_ref']:<14} {row['storage_state']:<12} {size:<12} {when:<22}")
    return 0


def _cmd_maintenance_run(args: argparse.Namespace) -> int:
    """Every periodic pass. Intended for a systemd timer or cron.

    Deliberately a command rather than a daemon (see `maintenance`): a scheduler
    an operator can read, run by hand, and stop beats a long-lived process
    inside the control plane that is a second thing to supervise and a second
    thing to notice has died.
    """
    from services.control_plane.workers import SystemdSupervisor

    settings = config.load()
    with db.connection() as conn:
        key_ring = crypto.KeyRing(settings.kek)
        key_ring.load(conn)

        if args.dry_run:
            print(f"would sleep {maintenance.sleepable_now(conn, idle_minutes=args.idle_minutes)} "
                  f"worker(s) idle for {args.idle_minutes}m")
            for node in maintenance.unenforced_capacity(conn):
                print(f"  node {node['name']} is over a ceiling: {node['reason']}")
            return 0

        results = maintenance.run_all(
            conn,
            key_ring=key_ring,
            platform_owner=args.platform_owner,
            supervisor=SystemdSupervisor(),
            auth_supervisor=auth_workers.supervisor(),
            realtime_supervisor=realtime_workers.supervisor(),
            idle_minutes=args.idle_minutes,
        )

    failed = 0
    for name, result in results.items():
        print(f"{name}: {result}")
        for line in result.detail:
            print(f"  {line}")
        failed += result.failed
    # Non-zero when something failed, so a timer surfaces it rather than
    # succeeding quietly with problems in its output.
    return 1 if failed else 0


def _cmd_capacity_report(args: argparse.Namespace) -> int:
    """Which nodes are over a ceiling, and by how much.

    Worth running before the enforcement in this slice reaches production: a
    node already past its warm or connection ceiling starts refusing placement,
    and an operator should learn that here rather than from a failed
    provisioning run.
    """
    with db.connection() as conn:
        rows = db.query(conn, "SELECT id FROM nodes ORDER BY name")
        if not rows:
            print("no nodes registered")
            return 0
        print(f"{'NAME':<20} {'WARM':<12} {'CONNECTIONS':<16} {'SLOTS':<12} STATUS")
        for row in rows:
            capacity = nodes.capacity_of(conn, row["id"])
            warm = f"{capacity.current_warm_projects}/{capacity.max_warm_projects}"
            conns = f"{capacity.projected_connections}/{capacity.usable_connections}"
            # Slots are the third ceiling and, at PostgreSQL's defaults, the
            # tightest: ten against a warm ceiling of roughly 24 projects.
            slots = (f"{capacity.committed_slots}/{capacity.usable_replication_slots}"
                     if capacity.realtime_ready else "unprepared")
            print(f"{capacity.name:<20} {warm:<12} {conns:<16} {slots:<12} "
                  f"{capacity.rejection_reason() or 'accepting'}")
    return 0


def _cmd_node_limits(args: argparse.Namespace) -> int:
    """Read a node's real connection settings and record them.

    The defaults are PostgreSQL's, and a production node will have been tuned.
    Guessing high is the dangerous direction: it lets placement fill a node past
    the point where tenants start failing to connect.
    """
    settings = config.load()
    with db.connection() as conn:
        key_ring = crypto.KeyRing(settings.kek)
        key_ring.load(conn)
        node = db.one(conn, "SELECT id FROM nodes WHERE name = %s", (args.name,))
        if node is None:
            raise ValueError(f"no node named {args.name}")
        admin_conn = psycopg.connect(
            nodes.admin_dsn(conn, node_id=node["id"], key_ring=key_ring)
        )
        try:
            limits = nodes.record_node_limits(conn, admin_conn, name=args.name)
        finally:
            admin_conn.close()
    print(f"{args.name}: max_connections={limits['max_connections']} "
          f"reserved={limits['reserved_connections']}")
    return 0


def _cmd_project_realtime(args: argparse.Namespace) -> int:
    """Turn a project's Realtime on or off.

    Enabling creates the project's replicator role -- the one tenant role that
    holds `REPLICATION` -- and takes one of the node's replication slots, of
    which there are ten. Both are why this is a command somebody types rather
    than something an upgrade does on its own.
    """
    with db.connection() as conn:
        project_id, admin_conn, tenant_connect, key_ring = _project_context(conn, args.ref)
        try:
            if args.enable:
                result = realtime.enable(
                    conn, admin_conn, project_id=project_id,
                    key_ring=key_ring, tenant_connect=tenant_connect,
                    # The project's Realtime server keeps its tenant registry in
                    # a database of its own, built here because this is the last
                    # place holding a node-admin connection: the gateway wakes
                    # slept workers and must never hold one.
                    metadata_connect=tenant_connect,
                )
            else:
                result = realtime.disable(
                    conn, admin_conn, project_id=project_id, tenant_connect=tenant_connect,
                    # Stops the server before the slots it holds open are
                    # dropped, and takes its metadata database with it.
                    supervisor=realtime_workers.supervisor(), key_ring=key_ring,
                )
        finally:
            admin_conn.close()

    print(f"{args.ref}: Realtime {result.detail} (slots: {', '.join(result.slot_names)})")
    if args.enable:
        print(f"  subscribe by adding tables to the {realtime.PUBLICATION} publication:")
        print(f"    ALTER PUBLICATION {realtime.PUBLICATION} ADD TABLE your_table;")
        print("  a table not in the publication produces no events, which is upstream's")
        print("  behaviour and is not an error the client can see.")
        print("  the project's Realtime server starts on the first connection; start it now")
        print(f"  with: cp-manage project realtime-worker --ref {args.ref} --start")
    else:
        print("  the slot is released, so the node stops retaining WAL for this project.")
        print("  the server, its metadata database and its credentials are gone too.")
    return 0


def _cmd_project_realtime_worker(args: argparse.Namespace) -> int:
    """Start, stop or report a project's Realtime server.

    Rarely needed: the gateway wakes a slept instance on the first connection
    and the maintenance pass sleeps an idle one. It exists because waking costs
    tens of seconds -- a BEAM boot and a migration run, not PostgREST's third of
    a second -- so an operator who knows traffic is coming should be able to pay
    that cost before a customer does.
    """
    with db.connection() as conn:
        project_id = _project_id(conn, args.ref)
        supervisor = realtime_workers.supervisor()
        if args.action == "status":
            row = db.one(
                conn,
                "SELECT realtime_enabled, realtime_worker_state, realtime_port, "
                "       realtime_registered_at FROM projects WHERE id = %s",
                (project_id,),
            )
            print(f"{args.ref}: enabled={row['realtime_enabled']} "
                  f"state={row['realtime_worker_state']} port={row['realtime_port']} "
                  f"registered={row['realtime_registered_at'] or 'never'}")
            return 0
        if args.action == "stop":
            realtime_workers.stop_worker(conn, project_id=project_id, supervisor=supervisor)
            print(f"{args.ref}: Realtime stopped; its slots stay reserved and its tenant "
                  "registered")
            return 0

        key_ring = crypto.KeyRing(config.load().kek)
        key_ring.load(conn)
        elapsed = realtime_workers.start_worker(
            conn, project_id=project_id, key_ring=key_ring, config=config.load(),
            supervisor=supervisor,
        )
        print(f"{args.ref}: Realtime ready in {elapsed:.1f}s and the tenant is registered")
        return 0


def _cmd_project_realtime_recover(args: argparse.Namespace) -> int:
    """Re-create a slot that was invalidated, deliberately and with a record.

    Not automatic, and not part of the maintenance pass. ADR-032 makes
    invalidation a project-visible incident; a platform that repaired it quietly
    would turn a reportable failure back into a silent one, which is the outcome
    the whole design exists to avoid.
    """
    with db.connection() as conn:
        project_id, admin_conn, tenant_connect, _ = _project_context(conn, args.ref)
        try:
            result = realtime.recover_slot(
                conn, project_id=project_id, tenant_connect=tenant_connect
            )
        finally:
            admin_conn.close()

    print(f"{args.ref}: {result.detail} ({', '.join(result.slot_names)})")
    print("  changes written while the slot was invalid were NOT delivered and cannot be")
    print("  recovered. If the customer needs them, they have to be re-read from the table.")
    return 0


def _cmd_node_realtime_check(args: argparse.Namespace) -> int:
    """Check and record whether a node may host Realtime at all.

    Three of the five preconditions need a cluster restart, which is an outage
    for every tenant already on the node, so this belongs in node build rather
    than in provisioning. A node checked afterwards costs downtime to fix.

    The physical-replication probe is the one that matters. ADR-031: without the
    `pg_hba.conf` reject, the first project to enable Realtime holds a role that
    can take a byte-level copy of every tenant database on the cluster,
    regardless of which one it has CONNECT on.
    """
    settings = config.load()
    with db.connection() as conn:
        key_ring = crypto.KeyRing(settings.kek)
        key_ring.load(conn)
        node = db.one(conn, "SELECT id FROM nodes WHERE name = %s", (args.name,))
        if node is None:
            raise ValueError(f"no node named {args.name}")
        dsn = nodes.admin_dsn(conn, node_id=node["id"], key_ring=key_ring)
        admin_conn = psycopg.connect(dsn)
        try:
            readiness = realtime.record_readiness(conn, admin_conn, name=args.name, dsn=dsn)
        finally:
            admin_conn.close()

    print(f"{args.name}: {'ready for Realtime' if readiness.ready else 'NOT ready for Realtime'}")
    print(f"  wal_level                 {readiness.wal_level}")
    print(f"  max_replication_slots     {readiness.max_replication_slots}")
    print(f"  max_wal_senders           {readiness.max_wal_senders}")
    keep = ("unbounded" if readiness.max_slot_wal_keep_mb < 0
            else f"{readiness.max_slot_wal_keep_mb} MB")
    print(f"  max_slot_wal_keep_size    {keep}")
    print(f"  physical replication      {readiness.probe_detail}")
    for rule in readiness.permissive_hba_rules:
        print(f"    pg_hba admits physical replication: {rule}")
    for failure in readiness.failures:
        print(f"  ! {failure}")
    if not readiness.ready:
        print("  see specs/realtime-replication-model.md, 'Required node preparation'")
    # Non-zero so a node-build script fails on an unprepared node rather than
    # printing the reason into a log nobody reads.
    return 0 if readiness.ready else 1


def _cmd_realtime_slots(args: argparse.Namespace) -> int:
    """What each prepared node's replication slots actually look like.

    Reads the node rather than the control plane's belief about it, because the
    interesting cases are precisely where the two disagree.
    """
    settings = config.load()
    exit_code = 0
    with db.connection() as conn:
        key_ring = crypto.KeyRing(settings.kek)
        key_ring.load(conn)
        rows = db.query(
            conn,
            "SELECT id, name FROM nodes WHERE (%s IS NULL OR name = %s) ORDER BY name",
            (args.node, args.node),
        )
        if not rows:
            print("no nodes registered" if args.node is None else f"no node named {args.node}")
            return 0
        for row in rows:
            capacity = nodes.capacity_of(conn, row["id"])
            print(f"{row['name']}: {capacity.committed_slots} committed of "
                  f"{capacity.usable_replication_slots} usable "
                  f"({capacity.realtime_rejection_reason() or 'accepting Realtime projects'})")
            try:
                admin_conn = psycopg.connect(
                    nodes.admin_dsn(conn, node_id=row["id"], key_ring=key_ring)
                )
            except Exception as exc:  # noqa: BLE001 - never print the DSN
                print(f"  could not reach the node ({type(exc).__name__})")
                exit_code = 1
                continue
            try:
                for slot in realtime.slots_on_node(admin_conn):
                    safe = ("-" if slot.safe_wal_size is None
                            else f"{slot.safe_wal_size / 1024 / 1024:.0f} MB before invalidation")
                    flag = "  ** INVALIDATED" if slot.invalidated else ""
                    print(f"  {slot.slot_name:<32} {slot.slot_type:<9} "
                          f"{'active' if slot.active else 'idle':<7} "
                          f"{slot.wal_status or '-':<10} {safe}{flag}")
                    if slot.invalidated:
                        exit_code = 1
            finally:
                admin_conn.close()
    if exit_code:
        print("an invalidated slot means that project is not receiving changes; re-creating")
        print("the slot resumes from the present and does not replay the gap (ADR-032)")
    return exit_code


def _cmd_project_direct_sql(args: argparse.Namespace) -> int:
    """Turn a project's direct PostgreSQL access on or off.

    Provisioning already applies the plan's entitlement, so this is for the case
    the plan does not cover: an upgrade taking effect before the next
    provisioning run, or an operator revoking access during an incident.
    """
    with db.connection() as conn:
        project_id, admin_conn, _, _ = _project_context(conn, args.ref)
        project = db.one(
            conn, "SELECT project_ref FROM projects WHERE id = %s", (project_id,)
        )
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        allowed = entitlements.for_project(conn, project_id)
        try:
            provisioning.set_direct_sql_access(
                admin_conn, names, enabled=args.enable,
                connection_limit=allowed.database_connections,
            )
            active = provisioning.has_direct_sql_access(admin_conn, names)
        finally:
            admin_conn.close()

    print(f"{args.ref}: direct SQL {'enabled' if active else 'disabled'}")
    if args.enable and not allowed.direct_database_access:
        print("  note: this project's plan does not entitle it to direct SQL, so the next")
        print("  provisioning run will turn it off again. Change the plan to make it stick.")
    if not args.enable:
        print("  existing sessions survive until they end; use pg_terminate_backend if")
        print("  access must stop immediately.")
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

    node_limits = node.add_parser(
        "limits", help="read and record a node's real connection settings"
    )
    node_limits.add_argument("--name", required=True)
    node_limits.set_defaults(func=_cmd_node_limits)

    realtime_check = node.add_parser(
        "realtime-check",
        help="check and record whether this node may host Realtime (ADR-031, ADR-032)",
    )
    realtime_check.add_argument("--name", required=True)
    realtime_check.set_defaults(func=_cmd_node_realtime_check)

    plans = sub.add_parser("plans", help="the plan catalogue").add_subparsers(
        dest="command", required=True
    )
    plans_sync = plans.add_parser(
        "sync", help="seed or refresh the catalogue from specs/plans-and-limits.yaml"
    )
    plans_sync.add_argument(
        "--with-limits",
        action="store_true",
        help="also pin the spec's limits into plans.config_json (default: leave them to entitlements)",
    )
    plans_sync.set_defaults(func=_cmd_plans_sync)

    plans_list = plans.add_parser("list", help="show the catalogue and what each plan grants")
    plans_list.set_defaults(func=_cmd_plans_list)

    project = sub.add_parser("project", help="tenant provisioning recovery").add_subparsers(
        dest="command", required=True
    )

    failed = project.add_parser("failed", help="list projects awaiting retry or failed")
    failed.set_defaults(func=_cmd_project_failed)

    retry = project.add_parser("retry", help="resume provisioning for a project")
    retry.add_argument("--ref", required=True)
    retry.add_argument("--platform-owner", default=os.environ.get("MALUDB_PLATFORM_OWNER", "postgres"))
    retry.set_defaults(func=_cmd_project_retry)

    backfill = project.add_parser(
        "backfill-executor",
        help="create the ADR-039 executor role for a project provisioned before it existed",
    )
    backfill.add_argument("--ref", required=True)
    backfill.set_defaults(func=_cmd_project_backfill_executor)

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

    key = sub.add_parser("key", help="project API keys").add_subparsers(dest="command", required=True)

    issue = key.add_parser("issue", help="mint a key; the plaintext is printed once")
    issue.add_argument("--ref", required=True)
    issue.add_argument("--type", required=True, choices=list(api_keys.KEY_TYPES))
    issue.add_argument("--name", help="a label, so an operator can tell four live keys apart")
    issue.set_defaults(func=_cmd_key_issue)

    key_list = key.add_parser("list", help="list a project's keys; never shows key material")
    key_list.add_argument("--ref", required=True)
    key_list.set_defaults(func=_cmd_key_list)

    reveal = key.add_parser(
        "reveal",
        help="show a publishable key again. Secret keys are hashed and have no equivalent.",
    )
    reveal.add_argument("--ref", required=True)
    reveal.add_argument("--id", required=True)
    reveal.set_defaults(func=_cmd_key_reveal)

    key_revoke = key.add_parser("revoke", help="revoke a key immediately")
    key_revoke.add_argument("--ref", required=True)
    key_revoke.add_argument("--id", required=True)
    key_revoke.set_defaults(func=_cmd_key_revoke)

    email = project.add_parser("email", help="configure a project's sending mode")
    email.add_argument("--ref", required=True)
    email.add_argument("--mode", choices=["platform_default", "custom_domain"],
                       default="platform_default")
    email.add_argument("--sender", required=True, help="From address; must be on a verified domain")
    email.add_argument("--sender-name", default=None)
    email.set_defaults(func=_cmd_project_email)

    direct_sql = project.add_parser(
        "direct-sql", help="turn a project's direct PostgreSQL access on or off"
    )
    direct_sql.add_argument("--ref", required=True)
    mode = direct_sql.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable", dest="enable", action="store_true")
    mode.add_argument("--disable", dest="enable", action="store_false")
    direct_sql.set_defaults(func=_cmd_project_direct_sql)

    project_realtime = project.add_parser(
        "realtime", help="turn Realtime on or off for a project (ADR-031: creates a "
                         "REPLICATION role and takes one of the node's slots)"
    )
    project_realtime.add_argument("--ref", required=True)
    realtime_toggle = project_realtime.add_mutually_exclusive_group(required=True)
    realtime_toggle.add_argument("--enable", dest="enable", action="store_true")
    realtime_toggle.add_argument("--disable", dest="enable", action="store_false")
    project_realtime.set_defaults(func=_cmd_project_realtime)

    realtime_recover = project.add_parser(
        "realtime-recover",
        help="re-create an invalidated replication slot; the gap is NOT replayed",
    )
    realtime_recover.add_argument("--ref", required=True)
    realtime_recover.set_defaults(func=_cmd_project_realtime_recover)

    project_storage = project.add_parser(
        "storage", help="measure one project's storage and enforce its quota"
    )
    project_storage.add_argument("--ref", required=True)
    project_storage.set_defaults(func=_cmd_project_storage)

    mail_group = sub.add_parser("email", help="platform email operations").add_subparsers(
        dest="command", required=True
    )
    reconcile = mail_group.add_parser(
        "reconcile-suppressions", help="pull MaluMail's suppression list into the control plane"
    )
    reconcile.set_defaults(func=_cmd_email_reconcile)

    storage_group = sub.add_parser("storage", help="storage accounting").add_subparsers(
        dest="command", required=True
    )
    report = storage_group.add_parser("report", help="what every project is using")
    report.set_defaults(func=_cmd_storage_report)

    maint = sub.add_parser("maintenance", help="the periodic passes").add_subparsers(
        dest="command", required=True
    )
    run = maint.add_parser("run", help="retry, measure storage, and sleep idle workers")
    run.add_argument("--idle-minutes", type=int, default=maintenance.DEFAULT_IDLE_MINUTES)
    run.add_argument("--platform-owner", default=os.environ.get("MALUDB_PLATFORM_OWNER", "postgres"))
    run.add_argument("--dry-run", action="store_true",
                     help="report what would happen without doing it")
    run.set_defaults(func=_cmd_maintenance_run)

    capacity = sub.add_parser("capacity", help="node capacity").add_subparsers(
        dest="command", required=True
    )
    capacity_report = capacity.add_parser("report", help="which nodes are over a ceiling")
    capacity_report.set_defaults(func=_cmd_capacity_report)

    realtime_worker = project.add_parser(
        "realtime-worker", help="start, stop or report a project's Realtime server"
    )
    realtime_worker.add_argument("--ref", required=True)
    worker_action = realtime_worker.add_mutually_exclusive_group(required=True)
    worker_action.add_argument("--start", dest="action", action="store_const", const="start")
    worker_action.add_argument("--stop", dest="action", action="store_const", const="stop")
    worker_action.add_argument("--status", dest="action", action="store_const", const="status")
    realtime_worker.set_defaults(func=_cmd_project_realtime_worker)

    realtime_group = sub.add_parser("realtime", help="replication slots").add_subparsers(
        dest="command", required=True
    )
    slots = realtime_group.add_parser(
        "slots", help="every replication slot a node holds, and which are invalidated"
    )
    slots.add_argument("--node", default=None, help="limit to one node")
    slots.set_defaults(func=_cmd_realtime_slots)

    return parser


def _plans_spec_path() -> Path:
    """`specs/plans-and-limits.yaml`, relative to this file rather than to cwd.

    An operator runs this from wherever they happen to be standing.
    """
    return Path(__file__).resolve().parent.parent.parent / "specs" / "plans-and-limits.yaml"


def _cmd_plans_sync(args: argparse.Namespace) -> int:
    """Seed the plan catalogue from the spec.

    **Nothing seeded this before**, which meant a freshly deployed control plane
    could not create a project at all: `default_plan` looks for the code `free`,
    finds nothing, and the route answers 503. Every environment that worked had
    had its plans inserted by hand or by a test fixture.

    What this writes is *identity*, not policy: the code, the display name and
    whether the plan is offered. The numbers stay in `entitlements.DEFAULTS`,
    keyed by the same codes, so there is one source for them rather than two
    that can drift -- `plans.config_json` exists to override those defaults for
    a particular deployment, and a sync that filled it in would turn every
    upgrade of the defaults into a migration. `--with-limits` writes them anyway
    for a deployment that wants its numbers pinned in the database.

    Idempotent, and it never deletes: a plan that has left the spec is marked
    inactive, because projects reference plans and a deleted row would either
    fail a foreign key or orphan a customer's project.
    """
    import yaml

    spec_path = _plans_spec_path()
    if not spec_path.is_file():
        print(f"no plans spec at {spec_path}", file=sys.stderr)
        return 2
    spec = yaml.safe_load(spec_path.read_text()) or {}
    plans = spec.get("plans") or {}
    if not plans:
        print(f"{spec_path} lists no plans", file=sys.stderr)
        return 2

    with db.connection() as conn:
        seen: list[str] = []
        for code, body in plans.items():
            body = body or {}
            name = body.get("name") or code.title()
            config = {"limits": body.get("limits", {})} if args.with_limits else {}
            db.execute(
                conn,
                """
                INSERT INTO plans (code, name, config_json, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (code) DO UPDATE
                    SET name = EXCLUDED.name,
                        is_active = TRUE,
                        config_json = CASE WHEN %s THEN EXCLUDED.config_json
                                           ELSE plans.config_json END
                """,
                (code, name, psycopg.types.json.Jsonb(config), args.with_limits),
            )
            seen.append(code)

        # Retired rather than removed.
        retired = db.execute(
            conn,
            "UPDATE plans SET is_active = FALSE WHERE NOT (code = ANY(%s)) AND is_active",
            (seen,),
        )
        conn.commit()

    print(f"synced {len(seen)} plan(s): {', '.join(sorted(seen))}")
    if retired:
        print(f"marked {retired} plan(s) inactive; no rows were deleted")
    if not args.with_limits:
        print("limits come from entitlements defaults by plan code; --with-limits pins them here")
    return 0


def _cmd_plans_list(args: argparse.Namespace) -> int:  # noqa: ARG001 - uniform signature
    """What the catalogue currently offers, and what a new project would get."""
    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT code, name, is_active, config_json FROM plans ORDER BY code",
        )
    if not rows:
        print("the plan catalogue is empty; run `cp-manage plans sync`")
        return 1
    for row in rows:
        allowed = entitlements.resolve(row["code"], row["config_json"])
        state = "active" if row["is_active"] else "inactive"
        pinned = "pinned" if (row["config_json"] or {}).get("limits") else "defaults"
        print(
            f"{row['code']:<12} {state:<9} {pinned:<9} "
            f"projects={allowed.max_projects} storage={allowed.database_storage_bytes} "
            f"direct_db={allowed.direct_database_access} "
            # Shown next to direct_db because the pair is the whole of ADR-039
            # and is the thing most likely to be misread: a plan can grant SQL
            # without granting a credential, and free does exactly that.
            f"sql_console={allowed.sql_console}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_pool(_connect())
    try:
        return int(args.func(args))
    except (ValueError, nodes.PlacementError, provisioning.ProvisioningError,
            api_keys.ApiKeyError, realtime.RealtimeError,
            realtime_workers.RealtimeWorkerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
