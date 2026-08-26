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
    cp-manage subscription create --ref abcd1234 --plan pro
    cp-manage subscription set-state --ref abcd1234 --state past_due
    cp-manage subscription show --ref abcd1234
    cp-manage subscription reconcile --ref abcd1234
    cp-manage subscription drift
    cp-manage billing price set --plan pro --price price_123
    cp-manage billing price list
    cp-manage billing events
    cp-manage billing status
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
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

from services.control_plane import (
    api_keys,
    auth_workers,
    billing,
    config,
    crypto,
    db,
    entitlements,
    jobs,
    mail,
    maintenance,
    nodes,
    plan_apply,
    plan_change,
    provisioning,
    realtime,
    realtime_workers,
    storage,
    stripe_api,
    subscriptions,
    tenant_bootstrap,
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
        # Both clauses of this used to be wrong after ADR-041: it named two of
        # the three revoked roles and said the third was untouched. An operator
        # reads this during a quota incident.
        print(f"  writes are revoked from {', '.join(storage.RESTRICTED_ROLES)} and the")
        print("  project's own admin role. Reads, deletes and truncates still work, so")
        print("  the project can delete its way back under and the next pass restores")
        print("  writes. The admin role owns the customer's tables and can grant itself")
        print("  INSERT back (ADR-040); each pass revokes it again.")
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


def _cmd_extensions_sync(args: argparse.Namespace) -> int:
    """Make every tenant's extension allowlist equal `specs/extension-allowlist.yaml`.

    ADR-045 makes adding an extension "a review and a merge rather than a
    release", which is only true if something carries the merged file out to
    the tenants. This is that thing, and without it the allowlist would be
    frozen at whatever each project was provisioned with -- a fleet where what a
    customer may install depends on the month they signed up.

    Removal is the half that matters and the reason this is not provisioning-
    only: taking an extension off the list is how a security decision gets
    reversed, and a tenant that never hears about it keeps the old permission.
    Already-installed extensions are left alone; this governs the next install.

    Idempotent, and it reports per project so a partial fleet is visible rather
    than averaged into a total.
    """
    spec = Path(args.spec) if getattr(args, "spec", None) else None
    with db.connection() as conn:
        rows = db.query(
            conn,
            "SELECT project_ref FROM projects "
            " WHERE database_name IS NOT NULL AND node_id IS NOT NULL AND deleted_at IS NULL "
            " ORDER BY project_ref",
        )

    if not rows:
        print("no provisioned projects")
        return 0

    failures = 0
    for row in rows:
        ref = row["project_ref"]
        try:
            with db.connection() as conn:
                _, admin_conn, tenant_connect, _ = _project_context(conn, ref)
            try:
                names = provisioning.TenantNames.for_ref(ref)
                with tenant_connect(names.database) as tenant_conn:
                    added, removed = tenant_bootstrap.sync_extension_allowlist(tenant_conn, spec)
            finally:
                admin_conn.close()
        except Exception as exc:  # noqa: BLE001 - one bad tenant must not stop the fleet
            failures += 1
            # Never the exception's text: a connection failure can echo a DSN.
            print(f"{ref}: FAILED ({type(exc).__name__})", file=sys.stderr)
            continue
        if added or removed:
            print(f"{ref}: +{added} -{removed}")

    print(f"{len(rows) - failures}/{len(rows)} projects synced")
    return 1 if failures else 0


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
            grace_days=settings.billing_grace_days,
            # None on a deployment that is not selling. `end_expired_grace`
            # then defers rather than cancelling, because it cannot end the
            # subscription at the provider -- and revoking a plan while the
            # provider keeps trying to collect for it is the one outcome worth
            # avoiding at the cost of a few days' service.
            billing_client=(
                stripe_api.Client(settings.stripe_secret_key,
                                  base_url=settings.stripe_api_base)
                if settings.stripe_secret_key else None
            ),
            # Not optional, and its absence was not visible. `run_all` defaults
            # it to None and `measure_object_storage` falls back to the
            # tenant's own `storage.objects` when it gets one -- which is the
            # figure slice 3 replaced precisely because a customer who reaches
            # `service_role` can rewrite it. The pass still ran, still recorded
            # a number and still enforced against it, so nothing failed; the
            # only symptom was that the trustworthy source was never consulted.
            # Slice 7 found it by wiring a second pass through the same
            # argument.
            config=settings,
            storage_node=args.node,
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


def _cmd_project_backfill_client(args: argparse.Namespace) -> int:
    """Give an already-provisioned project the ADR-047 client role.

    Every project provisioned before that ADR has the role's step in its
    pipeline but has already passed the point where the pipeline runs, and a
    project can only be sent back through provisioning while it is not serving.
    Same shape and same reasoning as `backfill-executor`, which exists for the
    same reason one ADR earlier.

    Idempotent by the predicate the provisioning step uses -- the role *and* a
    live credential, because a role nobody has the password for is not a
    finished backfill. It never touches the other roles: the failure this
    avoids is a backfill that resets the authenticator password and stops every
    PostgREST worker on the node.
    """
    with db.connection() as conn:
        project_id, admin_conn, _, key_ring = _project_context(conn, args.ref)
        names = provisioning.TenantNames.for_ref(args.ref)
        allowed = entitlements.for_project(conn, project_id)
        try:
            existing = db.one(
                conn,
                "SELECT count(*) AS live FROM project_credentials "
                " WHERE project_id = %s AND revoked_at IS NULL "
                "   AND credential_type = 'db_client'",
                (project_id,),
            )
            if existing["live"] and provisioning.has_client_role(admin_conn, names):
                print(f"project {args.ref}: client role already present")
                return 0

            password = provisioning.generate_password()
            provisioning.create_client_role(
                admin_conn, names, password=password,
                connection_limit=allowed.database_connections,
            )
            provisioning.grant_client_connect(admin_conn, names)
            provisioning.store_credential(
                conn,
                project_id=project_id,
                credential_type="db_client",
                role_name=names.client,
                secret=password,
                key_ring=key_ring,
            )
            provisioning.apply_plan_settings(
                admin_conn, names, settings=allowed.postgres_settings()
            )
            # And the plan's answer about direct access, which also forces the
            # admin role NOLOGIN -- the door ADR-047 closes on every project
            # provisioned before it.
            provisioning.set_direct_sql_access(
                admin_conn, names, enabled=allowed.direct_database_access,
                connection_limit=allowed.database_connections,
            )
            conn.commit()
            admin_conn.commit()
        finally:
            admin_conn.close()
            password = ""
    print(f"project {args.ref}: client role {names.client} created")
    if not allowed.direct_database_access:
        print("  NOLOGIN: this project's plan does not include direct database access")
    return 0


def _cmd_project_backfill_storage(args: argparse.Namespace) -> int:
    """Give an already-provisioned project its Phase 10 storage role and schema.

    Third of these, same shape and same reasoning as `backfill-executor` and
    `backfill-client`: a project provisioned before the step existed has passed
    the point where the pipeline runs, and a project can only be sent back
    through provisioning while it is not serving.

    This one does more than the other two, and has to. The role is only half of
    it -- bootstrap 012 hands the `storage` schema to that role and **raises**
    if it is absent, so an existing tenant needs the role created first and the
    pending bootstrap files applied second. Doing it in the other order is the
    failure mode the exception exists to make loud.

    Idempotent by the provisioning step's own predicate: the role *and* a live
    credential, because a role nobody has the password for is not a finished
    backfill. Bootstrap `apply` is idempotent on its own -- an already-recorded
    version is skipped, and a changed checksum is an error rather than a silent
    re-run.

    It never touches the other roles. The failure this avoids is a backfill
    that resets the authenticator password and stops every PostgREST worker on
    the node.
    """
    with db.connection() as conn:
        project_id, admin_conn, tenant_connect, key_ring = _project_context(conn, args.ref)
        names = provisioning.TenantNames.for_ref(args.ref)
        allowed = entitlements.for_project(conn, project_id)
        password = ""
        try:
            existing = db.one(
                conn,
                "SELECT count(*) AS live FROM project_credentials "
                " WHERE project_id = %s AND revoked_at IS NULL "
                "   AND credential_type = 'db_storage'",
                (project_id,),
            )
            if not (existing["live"] and provisioning.has_storage_role(admin_conn, names)):
                password = provisioning.generate_password()
                provisioning.create_storage_role(admin_conn, names, password=password)
                provisioning.grant_storage_connect(admin_conn, names)
                provisioning.store_credential(
                    conn,
                    project_id=project_id,
                    credential_type="db_storage",
                    role_name=names.storage,
                    secret=password,
                    key_ring=key_ring,
                )
                provisioning.apply_plan_settings(
                    admin_conn, names, settings=allowed.postgres_settings()
                )
                conn.commit()
                admin_conn.commit()
                print(f"project {args.ref}: storage role {names.storage} created")
            else:
                print(f"project {args.ref}: storage role already present")

            with tenant_connect(names.database) as tenant_conn:
                applied = tenant_bootstrap.bootstrap_project(
                    conn, tenant_conn, project_id=project_id
                )
        finally:
            admin_conn.close()
            password = ""

    if applied:
        print(f"project {args.ref}: bootstrap applied {', '.join(applied)}")
    else:
        print(f"project {args.ref}: bootstrap already current")
    return 0


def _cmd_project_rotate_client(args: argparse.Namespace) -> int:
    """Replace a project's direct-connection password, from node admin.

    The customer-facing rotation route does this as the client role itself,
    because ADR-038 keeps node credentials out of the public application. This
    is the operator's version, and the recovery for the one window that route
    has: a control-plane commit failing after the node accepted a change leaves
    the stored credential behind the real one, and only something holding node
    admin can put them back in step.
    """
    with db.connection() as conn:
        project_id, admin_conn, _, key_ring = _project_context(conn, args.ref)
        names = provisioning.TenantNames.for_ref(args.ref)
        password = provisioning.generate_password()
        try:
            if not provisioning.has_client_role(admin_conn, names):
                raise ValueError(
                    f"project {args.ref} has no client role; run `project backfill-client` first"
                )
            admin_conn.execute(
                psycopg.sql.SQL("ALTER ROLE {role} PASSWORD {password}").format(
                    role=psycopg.sql.Identifier(names.client),
                    password=psycopg.sql.Literal(password),
                )
            )
            provisioning.store_credential(
                conn,
                project_id=project_id,
                credential_type="db_client",
                role_name=names.client,
                secret=password,
                key_ring=key_ring,
            )
            admin_conn.commit()
            conn.commit()
        finally:
            admin_conn.close()
            password = ""
    print(f"{args.ref}: direct-connection password replaced")
    print("  existing sessions survive until they end; use pg_terminate_backend to cut them")
    return 0


def _cmd_project_plan_apply(args: argparse.Namespace) -> int:
    """Make a project's plan true on its node.

    The other half of a plan change. Entitlements read per request -- the
    gateway's limits, the storage quota, the console's ceilings -- change the
    moment the plan row does. Entitlements written into the node during
    provisioning do not change at all, and before this command the only way to
    move them was to re-provision.

    Explicit rather than on a timer, because `direct-sql --disable` is an
    operator's incident control and a reconciler running by itself would undo
    it. `cp-manage plan drift` is what says a project needs this.
    """
    with db.connection() as conn:
        project_id, admin_conn, _, _ = _project_context(conn, args.ref)
        allowed = entitlements.for_project(conn, project_id)
        names = provisioning.TenantNames.for_ref(args.ref)
        try:
            report = (
                plan_apply.inspect(admin_conn, names, allowed)
                if args.dry_run
                else plan_apply.apply(admin_conn, names, allowed)
            )
        finally:
            admin_conn.close()

    if report.missing_roles:
        print(f"{args.ref}: refused -- role(s) absent: {', '.join(report.missing_roles)}")
        print("  this project is mid-provision or predates a role; try")
        print("  `cp-manage project retry`, or the backfill for the role named above:")
        print("  `backfill-executor`, `backfill-client` or `backfill-storage`.")
        return 1

    verb = "would correct" if args.dry_run else "corrected"
    changes = report.divergences if args.dry_run else report.corrected
    if not changes:
        print(f"{args.ref} ({report.plan_code}): already matches its plan")
        return 0

    print(f"{args.ref} ({report.plan_code}): {verb} {len(changes)}")
    for divergence in changes:
        print(f"  [{divergence.direction}] {divergence}")
    return 0


def _cmd_project_set_plan(args: argparse.Namespace) -> int:
    """Move a project to another plan, and make it true on the node.

    Takes no money. Until slice 4 gives a billing provider a route, this is the
    only way a project's plan changes after creation -- `api/projects.py`
    refuses any plan but the default at creation, which is Phase 07's finding
    about a customer naming their own plan.
    """
    with db.connection() as conn:
        project_id, admin_conn, _, _ = _project_context(conn, args.ref)
        try:
            change = plan_change.change_plan(
                conn, admin_conn, project_id=project_id, to_plan_code=args.plan,
            )
        finally:
            admin_conn.close()

    if change.unchanged:
        print(f"{args.ref}: already on {change.to_plan}")
        return 0

    print(f"{args.ref}: {change.from_plan} -> {change.to_plan}")
    for divergence in change.corrected or []:
        print(f"  applied: {divergence}")
    if not change.corrected:
        print("  the node already matched the new plan")
    if change.closed_request:
        print("  closed the project's open upgrade request")
    print("  database, project ref, node and API keys unchanged (ADR-006)")
    return 0


def _cmd_project_plan_history(args: argparse.Namespace) -> int:
    """What plans a project has been on, and anything that failed on the way."""
    with db.connection() as conn:
        row = db.one(
            conn,
            "SELECT id FROM projects WHERE project_ref = %s AND deleted_at IS NULL",
            (args.ref,),
        )
        if row is None:
            raise ValueError(f"no project with ref {args.ref}")
        rows = plan_change.history(conn, row["id"])

    if not rows:
        print(f"{args.ref}: no recorded plan change")
        return 0
    for entry in rows:
        when = entry["completed_at"] or entry["started_at"]
        line = (f"{when:%Y-%m-%d %H:%M}  {entry['from_plan_code']} -> "
                f"{entry['to_plan_code']}  {entry['state']}")
        print(line)
        if entry["error"]:
            print(f"    {entry['error']}")
    return 0


def _enforced_plan(conn, project_id: uuid.UUID) -> str:
    """The plan the platform is enforcing, as opposed to the one being paid for.

    Every command in the `subscription` group prints both, because the whole
    point of ADR-048 is that they are separate facts and can disagree.
    """
    return db.one(
        conn,
        "SELECT pl.code FROM projects pr JOIN plans pl ON pl.id = pr.plan_id WHERE pr.id = %s",
        (project_id,),
    )["code"]


def _moment(text: str | None) -> datetime | None:
    """An optional ISO-8601 argument, demanding a timezone.

    A naive timestamp compares wrongly against the `TIMESTAMPTZ` columns these
    reach, and a billing period silently shifted by the operator's local offset
    is a number shown to a customer that is off by hours.
    """
    if not text:
        return None
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"{text!r} needs a timezone, e.g. 2026-08-19T10:00:00+00:00")
    return moment


def _when(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d}" if moment else "?"


def _subscription_project_id(conn, project_ref: str) -> uuid.UUID:
    """A project ref to an id, without opening a node connection.

    Everything in this group except `reconcile` is control-plane-only, and
    `_project_context` decrypts a node admin DSN it would have no use for. That
    is not merely wasteful: it means `subscription show` on a project whose node
    is unreachable would fail, and reading what a customer is being charged is
    exactly the thing that should still work during an incident.
    """
    row = db.one(
        conn,
        "SELECT id FROM projects WHERE project_ref = %s AND deleted_at IS NULL",
        (project_ref,),
    )
    if row is None:
        raise ValueError(f"no project with ref {project_ref}")
    return row["id"]


def _as_of(args: argparse.Namespace) -> datetime | None:
    """The moment a billing fact became true, per whoever is asserting it.

    Absent means now, which is honest for an operator: they are the source. It
    exists as a flag at all so that a state recorded late -- a payment that
    failed on Tuesday and is being entered on Thursday -- orders correctly
    against whatever slice 4's webhooks have written since.
    """
    return _moment(getattr(args, "as_of", None))


def _cmd_subscription_create(args: argparse.Namespace) -> int:
    """Record that a project is being paid for. Changes no entitlement.

    Two commands rather than one on purpose. This writes the billing fact and
    stops; `subscription reconcile` is what makes it true on the project, and
    separating them is the whole of ADR-048. A single command that did both
    would put the platform back where slice 1 left it -- unable to tell a plan
    somebody is paying for from a plan somebody typed.
    """
    with db.connection() as conn:
        project_id = _subscription_project_id(conn, args.ref)
        subscription = subscriptions.create(
            conn,
            project_id=project_id,
            plan_code=args.plan,
            state=args.state,
            as_of=_as_of(args),
            period_start=_moment(args.period_start),
            period_end=_moment(args.period_end),
        )
        entitled = subscriptions.entitled_plan_code(conn, project_id)
        current = _enforced_plan(conn, project_id)

    print(f"{args.ref}: subscription {subscription.state} on {subscription.plan_code}")
    if current == entitled:
        print(f"  the project is already on {entitled}")
    else:
        print(f"  the project is on {current} and this entitles {entitled}")
        print("  nothing has been applied; run `cp-manage subscription reconcile`")
    return 0


def _cmd_subscription_set_state(args: argparse.Namespace) -> int:
    """Assert the subscription's current truth. Changes no entitlement."""
    with db.connection() as conn:
        project_id = _subscription_project_id(conn, args.ref)
        before = subscriptions.for_project(conn, project_id)
        subscription = subscriptions.record_state(
            conn,
            project_id=project_id,
            state=args.state,
            as_of=_as_of(args),
            plan_code=args.plan,
            period_start=_moment(args.period_start),
            period_end=_moment(args.period_end),
        )
        entitled = subscriptions.entitled_plan_code(conn, project_id)
        current = _enforced_plan(conn, project_id)

    print(f"{args.ref}: {before.state} -> {subscription.state} on {subscription.plan_code}")
    if current == entitled:
        print(f"  the project is already on {entitled}")
    else:
        print(f"  the project is on {current} and this entitles {entitled}")
        print("  nothing has been applied; run `cp-manage subscription reconcile`")
    return 0


def _cmd_subscription_show(args: argparse.Namespace) -> int:
    """What is being paid for, what is enforced, and whether they agree."""
    with db.connection() as conn:
        project_id = _subscription_project_id(conn, args.ref)
        live = subscriptions.for_project(conn, project_id)
        entitled = subscriptions.entitled_plan_code(conn, project_id)
        current = _enforced_plan(conn, project_id)
        past = subscriptions.history(conn, project_id)

    print(f"{args.ref}: enforced plan {current}")
    if live is None:
        print("  no live subscription")
    else:
        period = ""
        if live.period_start or live.period_end:
            period = (f"  period {_when(live.period_start)} to {_when(live.period_end)}")
        print(f"  {live.state} on {live.plan_code}, as of {live.state_as_of:%Y-%m-%d %H:%M}"
              f"{period}")
        if live.state == "past_due":
            # From `state_since`, not `state_as_of`. The two differ exactly when
            # a state has been re-asserted, which for `past_due` is every
            # dunning retry -- so the number an operator needs is the one that
            # does not move.
            grace = config.load().billing_grace_days
            ends = live.state_since + timedelta(days=grace)
            print(f"  payment failed {live.state_since:%Y-%m-%d %H:%M}; "
                  f"grace of {grace}d ends {ends:%Y-%m-%d %H:%M} "
                  f"(ADR-051: writes stop, data is kept)")
    if current == entitled:
        print("  billing and entitlement agree")
    else:
        print(f"  DIVERGED: billing entitles {entitled}, the project is on {current}")

    canceled = [row for row in past if row["state"] == "canceled"]
    if canceled:
        print(f"  {len(canceled)} earlier subscription(s), canceled")
    return 0


def _cmd_subscription_reconcile(args: argparse.Namespace) -> int:
    """Make the enforced plan match the paid-for plan. The only half that acts.

    Runs `plan_change`, so it takes a node connection and everything ADR-006
    promises about an upgrade applies unchanged: same database, same ref, same
    node, same API keys.
    """
    with db.connection() as conn:
        project_id = _subscription_project_id(conn, args.ref)
        entitled = subscriptions.entitled_plan_code(conn, project_id)
        if args.dry_run:
            current = _enforced_plan(conn, project_id)
            if current == entitled:
                print(f"{args.ref}: already on {entitled}; nothing to do")
            else:
                print(f"{args.ref}: would move {current} -> {entitled}")
            return 0

        _, admin_conn, _, _ = _project_context(conn, args.ref)
        try:
            result = subscriptions.reconcile(conn, admin_conn, project_id=project_id)
        finally:
            admin_conn.close()

    if not result.changed:
        print(f"{args.ref}: already on {result.entitled_plan_code}; nothing to do")
        return 0

    change = result.change
    print(f"{args.ref}: {change.from_plan} -> {change.to_plan}")
    for divergence in change.corrected or []:
        print(f"  applied: {divergence}")
    if not change.corrected:
        print("  the node already matched the new plan")
    if change.closed_request:
        print("  closed the project's open upgrade request")
    print("  database, project ref, node and API keys unchanged (ADR-006)")
    return 0


def _cmd_subscription_drift(args: argparse.Namespace) -> int:
    """Which projects' plans disagree with what is being paid for them.

    Reports and does not correct, on `plans drift`'s precedent. Moving a
    project between plans is a change that should have somebody's name on it,
    and a reconciler on a timer has nobody's.
    """
    with db.connection() as conn:
        diverged = subscriptions.drift(conn)

    if not diverged:
        print("every project's plan matches what is being paid for it")
        return 0

    unbilled = [d for d in diverged if d.direction == "unbilled"]
    for divergence in diverged:
        print(f"[{divergence.direction}] {divergence.project_ref}: {divergence}")
    print(f"{len(diverged)} project(s) diverged")
    if unbilled:
        # Named rather than left to be counted: every project on the platform is
        # unbilled the day this ships, because `project set-plan` takes no money
        # and there was nowhere to record that any had been taken.
        print(f"  {len(unbilled)} on a plan no subscription pays for; "
              "record a subscription or move them back")
    return 1


def _billing_client(*, required: bool = True):
    """A Stripe client from this deployment's configuration.

    Built here rather than held as a module global so that the mode -- test or
    live -- is read from the key in front of the operator running the command,
    and a command that maps a price cannot write a live-mode row on a test key.
    """
    cfg = config.load()
    if not cfg.stripe_secret_key:
        if required:
            raise SystemExit(
                "MALUDB_STRIPE_SECRET_KEY is unset; this command needs it to talk "
                "to Stripe and to know whether it is in test or live mode"
            )
        return None
    return stripe_api.Client(cfg.stripe_secret_key, base_url=cfg.stripe_api_base)


def _cmd_billing_price_set(args: argparse.Namespace) -> int:
    """Map a plan to a Stripe price, after checking the product is eligible.

    **The check is the point of the command.** ADR-052 keeps prices in Stripe
    and the mapping here, which makes this a two-line write -- but an
    ineligible product does not fail at checkout, it silently drops that
    transaction out of Managed Payments and makes MaluDB the seller of record
    for it (ADR-049). That is a tax liability acquired without an error
    message. `--unverified` exists for a deployment with no network path to
    Stripe and says so in its own name.
    """
    client = _billing_client(required=not args.unverified)
    livemode = args.livemode if args.livemode is not None else (
        client.livemode if client else False
    )

    tax_code = None
    if client is not None and not args.unverified:
        try:
            tax_code = billing.verify_tax_code(client, args.price)
        except stripe_api.StripeError as exc:
            print(f"could not check the price with Stripe: {exc}")
            return 1

    with db.connection() as conn:
        billing.set_price(
            conn, plan_code=args.plan, price_id=args.price, livemode=livemode,
            tax_code=tax_code,
        )

    mode = "live" if livemode else "test"
    print(f"{args.plan} -> {args.price} ({mode} mode)")
    if tax_code:
        print(f"  product tax code {tax_code}, eligible for Managed Payments")
    else:
        print("  tax code NOT checked -- an ineligible product silently leaves "
              "Managed Payments and makes this platform the seller of record")
    return 0


def _cmd_billing_price_list(args: argparse.Namespace) -> int:
    """The mapping, and the paid plans that have none."""
    with db.connection() as conn:
        rows = billing.prices(conn, livemode=args.livemode)
        missing_test = billing.unmapped_plans(conn, livemode=False)
        missing_live = billing.unmapped_plans(conn, livemode=True)

    if not rows:
        print("no prices are mapped")
    for row in rows:
        mode = "live" if row["livemode"] else "test"
        code = row["tax_code"] or "tax code unchecked"
        print(f"{row['plan_code']:<16} {row['price_id']:<32} {mode:<5} {code}")

    for mode, missing in (("test", missing_test), ("live", missing_live)):
        if missing:
            # Worth naming rather than counting: a plan with no price cannot be
            # bought, and nothing says so until a customer tries.
            print(f"  {mode} mode has no price for: {', '.join(missing)}")
    return 0


def _cmd_billing_price_rm(args: argparse.Namespace) -> int:
    client = _billing_client(required=False)
    livemode = args.livemode if args.livemode is not None else (
        client.livemode if client else False
    )
    with db.connection() as conn:
        removed = billing.remove_price(conn, plan_code=args.plan, livemode=livemode)
    mode = "live" if livemode else "test"
    print(f"{args.plan}: {'removed' if removed else 'no mapping'} ({mode} mode)")
    return 0 if removed else 1


def _cmd_billing_events(args: argparse.Namespace) -> int:
    """The last events the provider delivered, and what became of each.

    The place to look when a customer says they paid and nothing happened. An
    outcome of `refused` names something this platform declined to act on and
    the note says why; `failed` is a bug.
    """
    with db.connection() as conn:
        rows = billing.events(conn, limit=args.limit)

    if not rows:
        print("no provider events have been received")
        return 0

    for row in rows:
        mode = "live" if row["livemode"] else "test"
        ref = row["project_ref"] or "-"
        print(f"{row['received_at']:%Y-%m-%d %H:%M} {row['outcome']:<9} "
              f"{row['event_type']:<34} {ref:<10} {mode}")
        if row["note"] and row["outcome"] not in ("applied", "ignored"):
            print(f"    {row['note']}")

    stuck = [r for r in rows if r["outcome"] == "received"]
    if stuck:
        print(f"  {len(stuck)} event(s) still 'received' -- the handler did not "
              "finish; look for an exception in the logs")
    return 0


def _cmd_billing_status(args: argparse.Namespace) -> int:
    """Whether this deployment can take money, and what is waiting to be applied."""
    cfg = config.load()
    client = _billing_client(required=False)

    print(f"secret key:     {'set' if cfg.stripe_secret_key else 'MISSING'}")
    print(f"webhook secret: {'set' if cfg.stripe_webhook_secret else 'MISSING'}")
    if client is not None:
        print(f"mode:           {'LIVE' if client.livemode else 'test'}")

    with db.connection() as conn:
        pending = subscriptions.pending_reconciliation(conn)
        missing = billing.unmapped_plans(
            conn, livemode=client.livemode if client else False
        )
        grace = subscriptions.in_grace(conn, grace_days=cfg.billing_grace_days)

    print(f"grace period:   {cfg.billing_grace_days} day(s) (ADR-051)")
    if cfg.billing_grace_days == 0:
        # Honoured, because a grace period is configuration and zero is a thing
        # an operator may mean. Said out loud, because it is almost never what
        # somebody meant: Stripe's first dunning retry is hours away, so a
        # transient decline would cost a customer their plan before the card was
        # tried again.
        print("                WARNING: zero grace cancels on the first failed "
              "payment, before the provider has retried the card")
    if grace:
        # Named while there is still time to act on it. A number that only
        # appears once the grace has run out is not a warning.
        print(f"in grace:       {len(grace)} project(s) with a failed payment")
        for row in grace[:10]:
            print(f"                {row['project_ref']}: {row['plan_code']} until "
                  f"{row['expires_at']:%Y-%m-%d %H:%M}")

    if missing:
        print(f"unsellable:     {', '.join(missing)} (no price mapped)")
    if pending:
        # Not an error. The maintenance pass applies these, and seeing a few
        # here means it has not run since the last event rather than that
        # anything is wrong. A number that never falls is the signal.
        print(f"pending:        {len(pending)} subscription(s) awaiting reconciliation")
        for row in pending[:10]:
            print(f"                {row['project_ref']}: {row['state']}")
    else:
        print("pending:        nothing awaiting reconciliation")
    return 0


def _cmd_plan_drift(args: argparse.Namespace) -> int:
    """Which projects' nodes disagree with their plans, and which way.

    The fleet view. `withheld` is a plan change that never reached the node --
    before Phase 09 slice 0 that was every plan change. `excess` is a project
    getting more than its plan grants, which is either an operator's incident
    measure or a privilege nobody is paying for, and the two are worth telling
    apart before correcting either.
    """
    diverged = 0
    unreachable = 0
    with db.connection() as conn:
        key_ring = crypto.KeyRing(config.load().kek)
        key_ring.load(conn)
        rows = plan_apply.project_rows(conn)
        by_node: dict[int, list[dict]] = {}
        for row in rows:
            by_node.setdefault(row["node_id"], []).append(row)

        for node_id, node_rows in by_node.items():
            dsn = nodes.admin_dsn(conn, node_id=node_id, key_ring=key_ring)
            try:
                admin_conn = psycopg.connect(dsn)
            except psycopg.Error as exc:
                # The type, never `str(exc)`. A psycopg connection error can
                # echo the DSN it failed on, and this one is a node superuser
                # credential -- the same line `sql_console.ConsoleError` draws,
                # and `main()` does not catch OperationalError, so an escaping
                # one would print a traceback carrying it.
                unreachable += 1
                print(f"node {node_id}: unreachable ({type(exc).__name__})", file=sys.stderr)
                continue
            try:
                for row in node_rows:
                    names = provisioning.TenantNames.for_ref(row["project_ref"])
                    allowed = entitlements.resolve(row["plan_code"], row["config_json"])
                    report = plan_apply.inspect(admin_conn, names, allowed)
                    if report.clean:
                        continue
                    diverged += 1
                    print(f"{row['project_ref']} ({report.plan_code}):")
                    for divergence in report.divergences:
                        print(f"  [{divergence.direction}] {divergence}")
                    for role in report.missing_roles:
                        print(f"  [absent] {role}")
            finally:
                admin_conn.close()

    print(f"\n{len(rows)} project(s) compared, {diverged} diverging")
    if diverged:
        print("`cp-manage project plan-apply --ref <ref>` applies one.")
    if unreachable:
        # Non-zero, because a report that could not read some nodes has not
        # answered the question it was asked, and a green exit would say it had.
        print(f"{unreachable} node(s) could not be read", file=sys.stderr)
        return 1
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

    plans_drift = plans.add_parser(
        "drift", help="projects whose node disagrees with their plan"
    )
    plans_drift.set_defaults(func=_cmd_plan_drift)

    plans_list = plans.add_parser("list", help="show the catalogue and what each plan grants")
    plans_list.set_defaults(func=_cmd_plans_list)

    # ADR-048. A group of its own rather than more `project` subcommands,
    # because the distinction it exists to draw is between billing and
    # entitlement -- and putting `subscription set-state` next to `project
    # set-plan` is the clearest place to see that only one of them acts.
    subscription = sub.add_parser(
        "subscription", help="what is being paid for, kept apart from what is enforced"
    ).add_subparsers(dest="command", required=True)

    sub_create = subscription.add_parser(
        "create", help="record that a project is being paid for (applies nothing)"
    )
    sub_create.add_argument("--ref", required=True)
    sub_create.add_argument("--plan", required=True, help="the plan this subscription entitles")
    sub_create.add_argument(
        "--state", default="active", choices=[s for s in subscriptions.STATES if s != "canceled"],
        help="default active; a subscription cannot be created canceled",
    )
    sub_create.add_argument(
        "--as-of", dest="as_of",
        help="when this became true, ISO-8601 with a timezone; default now",
    )
    sub_create.add_argument("--period-start", dest="period_start")
    sub_create.add_argument("--period-end", dest="period_end")
    sub_create.set_defaults(func=_cmd_subscription_create)

    sub_state = subscription.add_parser(
        "set-state", help="assert the subscription's current truth (applies nothing)"
    )
    sub_state.add_argument("--ref", required=True)
    sub_state.add_argument("--state", required=True, choices=list(subscriptions.STATES))
    sub_state.add_argument(
        "--plan", help="also change which plan this subscription entitles"
    )
    sub_state.add_argument(
        "--as-of", dest="as_of",
        help="when this became true, ISO-8601 with a timezone; default now. A fact "
             "older than the one on record is refused as stale",
    )
    sub_state.add_argument("--period-start", dest="period_start")
    sub_state.add_argument("--period-end", dest="period_end")
    sub_state.set_defaults(func=_cmd_subscription_set_state)

    sub_show = subscription.add_parser(
        "show", help="what is being paid for, what is enforced, and whether they agree"
    )
    sub_show.add_argument("--ref", required=True)
    sub_show.set_defaults(func=_cmd_subscription_show)

    sub_reconcile = subscription.add_parser(
        "reconcile",
        help="move the project onto the plan its subscription entitles -- the only "
             "command in this group that changes anything",
    )
    sub_reconcile.add_argument("--ref", required=True)
    sub_reconcile.add_argument(
        "--dry-run", action="store_true", help="report what would change and change nothing"
    )
    sub_reconcile.set_defaults(func=_cmd_subscription_reconcile)

    sub_drift = subscription.add_parser(
        "drift", help="which projects' plans disagree with what is being paid for them"
    )
    sub_drift.set_defaults(func=_cmd_subscription_drift)

    # ADR-049. Separate from `subscription` because the distinction is the same
    # one that group draws, one layer out: `subscription` is what MaluDB
    # believes, `billing` is what the provider was told and what it said back.
    billing_group = sub.add_parser(
        "billing", help="the payment provider: prices, events, and whether it is configured"
    ).add_subparsers(dest="command", required=True)

    price = billing_group.add_parser(
        "price", help="map a plan to a provider price (ADR-052: no amounts are stored)"
    ).add_subparsers(dest="subcommand", required=True)

    price_set = price.add_parser("set", help="map a plan to a Stripe price id")
    price_set.add_argument("--plan", required=True)
    price_set.add_argument("--price", required=True, help="a Stripe price id, price_...")
    price_set.add_argument(
        "--livemode", dest="livemode", action="store_true", default=None,
        help="force live mode; by default it is read from the secret key",
    )
    price_set.add_argument(
        "--unverified", action="store_true",
        help="skip the Managed Payments tax-code check. An ineligible product "
             "does not fail -- it silently makes this platform the seller of "
             "record for that transaction",
    )
    price_set.set_defaults(func=_cmd_billing_price_set)

    price_list = price.add_parser("list", help="the mapping, and paid plans that have none")
    price_list.add_argument(
        "--livemode", dest="livemode", action="store_true", default=None,
        help="only live-mode rows; default shows both",
    )
    price_list.set_defaults(func=_cmd_billing_price_list)

    price_rm = price.add_parser("rm", help="remove a mapping")
    price_rm.add_argument("--plan", required=True)
    price_rm.add_argument("--livemode", dest="livemode", action="store_true", default=None)
    price_rm.set_defaults(func=_cmd_billing_price_rm)

    billing_events = billing_group.add_parser(
        "events", help="what the provider delivered, and what became of each"
    )
    billing_events.add_argument("--limit", type=int, default=50)
    billing_events.set_defaults(func=_cmd_billing_events)

    billing_status = billing_group.add_parser(
        "status", help="whether this deployment can take money, and what is waiting"
    )
    billing_status.set_defaults(func=_cmd_billing_status)

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

    backfill_client = project.add_parser(
        "backfill-client",
        help="give an already-provisioned project the ADR-047 direct-connection role",
    )
    backfill_client.add_argument("--ref", required=True)
    backfill_client.set_defaults(func=_cmd_project_backfill_client)

    backfill_storage = project.add_parser(
        "backfill-storage",
        help="give an already-provisioned project its storage role and storage schema",
    )
    backfill_storage.add_argument("--ref", required=True)
    backfill_storage.set_defaults(func=_cmd_project_backfill_storage)

    rotate_client = project.add_parser(
        "rotate-client-credential",
        help="replace a project's direct-connection password using node admin",
    )
    rotate_client.add_argument("--ref", required=True)
    rotate_client.set_defaults(func=_cmd_project_rotate_client)

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

    plan_apply_cmd = project.add_parser(
        "plan-apply",
        help="re-assert a project's plan on its node (role settings, direct SQL, "
             "connection limits) -- the half of a plan change that is not instant",
    )
    plan_apply_cmd.add_argument("--ref", required=True)
    plan_apply_cmd.add_argument(
        "--dry-run", action="store_true", help="report what would change and change nothing"
    )
    plan_apply_cmd.set_defaults(func=_cmd_project_plan_apply)

    set_plan = project.add_parser(
        "set-plan",
        help="move a project to another plan and apply it to the node (takes no payment)",
    )
    set_plan.add_argument("--ref", required=True)
    set_plan.add_argument("--plan", required=True, help="the target plan's code")
    set_plan.set_defaults(func=_cmd_project_set_plan)

    plan_history = project.add_parser(
        "plan-history", help="what plans this project has been on"
    )
    plan_history.add_argument("--ref", required=True)
    plan_history.set_defaults(func=_cmd_project_plan_history)

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

    extensions_group = sub.add_parser(
        "extensions", help="the ADR-045 extension allowlist"
    ).add_subparsers(dest="command", required=True)
    ext_sync = extensions_group.add_parser(
        "sync", help="push specs/extension-allowlist.yaml to every provisioned tenant"
    )
    ext_sync.add_argument("--spec", help="read a different allowlist file (for a rehearsal)")
    ext_sync.set_defaults(func=_cmd_extensions_sync)

    maint = sub.add_parser("maintenance", help="the periodic passes").add_subparsers(
        dest="command", required=True
    )
    run = maint.add_parser("run", help="retry, measure storage, and sleep idle workers")
    run.add_argument("--idle-minutes", type=int, default=maintenance.DEFAULT_IDLE_MINUTES)
    run.add_argument("--platform-owner", default=os.environ.get("MALUDB_PLATFORM_OWNER", "postgres"))
    run.add_argument("--dry-run", action="store_true",
                     help="report what would happen without doing it")
    run.add_argument(
        "--node", default=None,
        help="the node this host is, for the storage-tenant reconciliation; only needed "
             "when more than one node has storage-registered projects",
    )
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
            realtime_workers.RealtimeWorkerError, plan_change.PlanChangeError,
            subscriptions.SubscriptionError, billing.BillingError,
            stripe_api.StripeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
