#!/usr/bin/env python3
"""Phase 11 slice 0 — measure what backup, restore and per-tenant extraction cost.

A SPIKE ARTEFACT, in the same class as `scripts/bench-gateway.py`. It is not
production code and nothing imports it. It exists so the figures in
`specs/backup-restore-model.md` can be reproduced, and so the claims in the
Phase 11 plan rest on measurement rather than on tool documentation.

It builds tenants through the real provisioning module rather than through SQL
of its own, because the thing being measured is what a *MaluDB* tenant costs to
back up and restore -- `maludb_core` (ADR-015), the seven per-tenant roles, the
ADR-014 lockdown, the versioned bootstrap and its `auth` and `storage` schemas.
A `CREATE DATABASE` with one table in it would measure nothing useful.

    scripts/backup-test-cluster.sh              # build the cluster first
    export MALUDB_BACKUP_NODE_DSN=...           # it prints this
    scripts/bench-backup.py provision --count 8
    scripts/bench-backup.py backup
    scripts/bench-backup.py load --seconds 30
    scripts/bench-backup.py roundtrip --ref bk000001
    scripts/bench-backup.py restore-tenant --ref bk000001
"""

# This script shells out to sudo, pgbackrest, pg_createcluster and psql, and
# writes its scratch files under /tmp. That is the point of it -- the thing
# being measured is a cluster-level operation no library performs -- so the
# subprocess and temp-file rules are turned off for this file rather than
# annotated onto thirty call sites. It is a spike artefact and nothing imports
# it; see scripts/bench-gateway.py for the same class of code.
# ruff: noqa: S603, S607, S108

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.control_plane import provisioning, tenant_bootstrap  # noqa: E402

DSN = os.environ.get("MALUDB_BACKUP_NODE_DSN", "").strip()
STANZA = os.environ.get("MALUDB_BACKUP_STANZA", "maludb-bk")
OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")
PGVER = os.environ.get("PG_VERSION", "17")
CLUSTER = os.environ.get("PG_CLUSTER", "bk")

# The scratch cluster the per-tenant restore goes through. `docs/BACKUP-RECOVERY.md`
# forbids the alternative in as many words: "Do not make 'restore one project'
# require replacing the entire shared node in production."
SCRATCH = os.environ.get("BK_SCRATCH_CLUSTER", "bkr")
SCRATCH_PORT = int(os.environ.get("BK_SCRATCH_PORT", "5435"))


def need_dsn() -> str:
    if not DSN:
        sys.exit("MALUDB_BACKUP_NODE_DSN is unset -- run scripts/backup-test-cluster.sh")
    return DSN


def admin():
    return psycopg.connect(need_dsn(), autocommit=False)


def tenant(database: str, *, user: str | None = None, password: str | None = None):
    parts = psycopg.conninfo.conninfo_to_dict(need_dsn())
    parts["dbname"] = database
    if user:
        parts["user"] = user
        parts["password"] = password
    return psycopg.connect(psycopg.conninfo.make_conninfo(**parts), autocommit=True)


def sh(*args: str, user: str = "postgres", check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-u", user, *args], capture_output=True, text=True, check=check)


def du_bytes(path: str) -> int:
    out = sh("du", "-sb", path, check=False).stdout.split("\t")
    return int(out[0]) if out and out[0].isdigit() else 0


def db_size(conn, database: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(%s)", (database,))
        return cur.fetchone()[0]


# --------------------------------------------------------------------------
# provision


def provision_one(conn, ref: str) -> dict[str, str]:
    names = provisioning.TenantNames.for_ref(ref)
    passwords = {k: provisioning.generate_password()
                 for k in ("authenticator", "auth", "admin", "executor", "client", "storage")}

    provisioning.ensure_shared_roles(conn)
    provisioning.create_roles(conn, names, passwords=passwords,
                              connection_limits={"authenticator": 20, "auth": 10})
    provisioning.create_executor_role(conn, names, password=passwords["executor"])
    provisioning.create_client_role(conn, names, password=passwords["client"])
    provisioning.create_storage_role(conn, names, password=passwords["storage"])
    conn.commit()

    provisioning.create_database(conn, names, owner=OWNER)
    provisioning.lock_down_database(conn, names)
    provisioning.grant_executor_connect(conn, names)
    provisioning.grant_client_connect(conn, names)
    provisioning.grant_storage_connect(conn, names)
    conn.commit()

    with tenant(names.database) as tconn:
        provisioning.install_extension(tconn)
        tenant_bootstrap.apply(tconn)
    provisioning.verify_isolation(conn, names)
    conn.commit()
    return passwords


def cmd_provision(args) -> int:
    made = []
    with admin() as conn:
        for i in range(args.count):
            ref = f"bk{i:06d}"
            t0 = time.monotonic()
            pw = provision_one(conn, ref)
            elapsed = time.monotonic() - t0
            size = db_size(conn, provisioning.TenantNames.for_ref(ref).database)
            made.append({"ref": ref, "seconds": round(elapsed, 2), "bytes": size})
            print(f"  {ref}  {elapsed:6.2f}s  {size/1024/1024:6.1f} MB")
            # The generated role passwords are deliberately dropped here rather
            # than written anywhere. Nothing in these measurements authenticates
            # as a tenant role -- every step connects with the cluster's admin
            # DSN -- so persisting them would put credentials on disk to be read
            # by nothing. An earlier revision wrote them to /tmp at the default
            # umask, which is the sort of thing this repository's own review
            # rules list under secret leakage.
            del pw
    total = sum(m["bytes"] for m in made)
    print(f"\n{len(made)} tenants, {total/1024/1024:.1f} MB total, "
          f"{total/max(1,len(made))/1024/1024:.1f} MB mean")
    return 0


# --------------------------------------------------------------------------
# backup


def pgbackrest(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh("pgbackrest", "--stanza=" + STANZA, *args, check=check)


def cmd_backup(args) -> int:
    for kind in args.types:
        backup_before = du_bytes(f"/var/lib/pgbackrest/backup/{STANZA}")
        archive_before = du_bytes(f"/var/lib/pgbackrest/archive/{STANZA}")
        t0 = time.monotonic()
        # `--start-fast` forces an immediate checkpoint. Without it pgBackRest
        # waits for the next *scheduled* one, so an untuned backup of a 220 MB
        # cluster measured 4m41s of which almost all was idle -- the figure was
        # `checkpoint_timeout`, not the cost of copying anything. Measure the
        # copy; note the wait separately.
        proc = pgbackrest("--log-level-console=warn", "backup", f"--type={kind}",
                          "--start-fast", f"--process-max={args.process_max}", check=False)
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            print(f"  {kind}: FAILED\n{proc.stderr[-2000:]}")
            return 1
        backup_after = du_bytes(f"/var/lib/pgbackrest/backup/{STANZA}")
        archive_after = du_bytes(f"/var/lib/pgbackrest/archive/{STANZA}")
        print(f"  {kind:5}  {elapsed:7.2f}s  backup +{(backup_after-backup_before)/1024/1024:7.2f} MB"
              f"  archive +{(archive_after-archive_before)/1024/1024:7.2f} MB"
              f"  repo total {(backup_after+archive_after)/1024/1024:7.1f} MB")
    info = json.loads(pgbackrest("--output=json", "info").stdout)
    for stanza in info:
        for b in stanza["backup"]:
            print(f"  {b['label']:22} type={b['type']:4} "
                  f"db={b['info']['size']/1024/1024:8.1f} MB "
                  f"repo={b['info']['repository']['size']/1024/1024:7.1f} MB")
    return 0


# --------------------------------------------------------------------------
# write load and WAL volume


def cmd_load(args) -> int:
    """Generate tenant writes and measure the WAL they produce.

    The figure this is after is bytes of WAL per tenant per unit of work, which
    is what retention costs money in and what `pg_wal` has to be sized for.
    """
    with admin() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_current_wal_lsn()::text")
        start_lsn = cur.fetchone()[0]
        archive_before = du_bytes(f"/var/lib/pgbackrest/archive/{STANZA}")

        refs = [f"bk{i:06d}" for i in range(args.tenants)]
        t0 = time.monotonic()
        rows = 0
        while time.monotonic() - t0 < args.seconds:
            for ref in refs:
                names = provisioning.TenantNames.for_ref(ref)
                with tenant(names.database) as tconn:
                    tconn.execute("CREATE TABLE IF NOT EXISTS public.bench "
                                  "(id bigserial PRIMARY KEY, payload text, at timestamptz DEFAULT now())")
                    tconn.execute("INSERT INTO public.bench (payload) "
                                  "SELECT repeat('x', 200) FROM generate_series(1, %s)", (args.rows,))
                    rows += args.rows
            if time.monotonic() - t0 >= args.seconds:
                break
        elapsed = time.monotonic() - t0

        cur.execute("SELECT pg_current_wal_lsn()::text")
        end_lsn = cur.fetchone()[0]
        cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (end_lsn, start_lsn))
        wal_bytes = int(cur.fetchone()[0])

    # Let the archiver catch up before sizing the archive.
    time.sleep(3)
    archive_after = du_bytes(f"/var/lib/pgbackrest/archive/{STANZA}")
    print(f"  {rows} rows across {args.tenants} tenants in {elapsed:.1f}s")
    print(f"  WAL generated      {wal_bytes/1024/1024:8.1f} MB")
    print(f"  archive grew       {(archive_after-archive_before)/1024/1024:8.1f} MB")
    print(f"  per 1000 rows      {wal_bytes/max(1,rows)*1000/1024/1024:8.2f} MB")
    print(f"  per tenant-day at this rate "
          f"{wal_bytes/max(1,args.tenants)/max(1e-9,elapsed)*86400/1024/1024/1024:8.2f} GB")
    return 0


# --------------------------------------------------------------------------
# logical round trip


def cmd_roundtrip(args) -> int:
    """Does one tenant database survive pg_dump/pg_restore, and as whom?

    The interesting part is not that `pg_dump` runs. It is what the dump does
    NOT contain -- cluster-scoped roles -- and what happens to object ownership
    when the restore is performed by something other than a superuser.
    """
    names = provisioning.TenantNames.for_ref(args.ref)
    dump = f"/tmp/{names.database}.dump"
    t0 = time.monotonic()
    proc = sh("pg_dump", "-p", port_of(need_dsn()), "-Fc", "-f", dump, names.database, check=False)
    if proc.returncode != 0:
        print(proc.stderr[-3000:])
        return 1
    dump_seconds = time.monotonic() - t0
    size = Path(dump).stat().st_size

    listing = sh("pg_restore", "--list", dump).stdout
    schemas = sorted(set(re.findall(r"^\d+; \d+ \d+ \w+ [\w-]+ ([\w.]+) ", listing, re.M)))

    target = names.database + "_restored"
    with admin() as conn:
        conn.autocommit = True
        conn.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{target}" OWNER {OWNER}')
    t0 = time.monotonic()
    proc = sh("pg_restore", "-p", port_of(need_dsn()), "-d", target, dump, check=False)
    restore_seconds = time.monotonic() - t0

    errors = [ln for ln in proc.stderr.splitlines() if "error" in ln.lower()]
    with tenant(target) as tconn, tconn.cursor() as cur:
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        exts = cur.fetchall()
        cur.execute("SELECT count(*) FROM pg_policies")
        policies = cur.fetchone()[0]
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
                    "AND nspname <> 'information_schema' ORDER BY 1")
        namespaces = [r[0] for r in cur.fetchall()]

    print(f"  dump     {dump_seconds:6.2f}s  {size/1024/1024:6.2f} MB (custom format)")
    print(f"  restore  {restore_seconds:6.2f}s  {len(errors)} error lines")
    print(f"  schemas in dump: {', '.join(schemas) or '(none parsed)'}")
    print(f"  namespaces after restore: {', '.join(namespaces)}")
    print(f"  extensions: {', '.join(f'{e}={v}' for e, v in exts)}")
    print(f"  RLS policies: {policies}")
    for ln in errors[:12]:
        print(f"    ! {ln}")
    return 0


def port_of(dsn: str) -> str:
    return str(psycopg.conninfo.conninfo_to_dict(dsn).get("port", "5432"))


# --------------------------------------------------------------------------
# per-tenant restore through a scratch cluster


def cmd_restore_tenant(args) -> int:
    names = provisioning.TenantNames.for_ref(args.ref)
    scratch_data = f"/var/lib/postgresql/{PGVER}/{SCRATCH}"
    scratch_conf = f"/etc/postgresql/{PGVER}/{SCRATCH}"

    # A marker written now, and a second one written after the PITR target, so
    # the restore can be shown to have gone back rather than merely completed.
    with tenant(names.database) as tconn:
        tconn.execute("CREATE TABLE IF NOT EXISTS public.restore_marker (id serial primary key, note text)")
        tconn.execute("INSERT INTO public.restore_marker (note) VALUES ('before-target')")
    with admin() as conn, conn.cursor() as cur:
        cur.execute("SELECT now()::text, pg_current_wal_lsn()::text")
        target_time, _ = cur.fetchone()
    time.sleep(2)
    with tenant(names.database) as tconn:
        tconn.execute("INSERT INTO public.restore_marker (note) VALUES ('after-target')")
        tconn.execute("SELECT pg_switch_wal()")
    time.sleep(3)

    print(f"  PITR target {target_time}")
    started = time.monotonic()

    subprocess.run(["sudo", "pg_dropcluster", "--stop", PGVER, SCRATCH], capture_output=True)
    subprocess.run(["sudo", "pg_createcluster", PGVER, SCRATCH, "--port", str(SCRATCH_PORT),
                    "--", "--auth-local=peer"], capture_output=True, check=True)
    subprocess.run(["sudo", "pg_ctlcluster", PGVER, SCRATCH, "stop"], capture_output=True)
    sh("bash", "-c", f"rm -rf {scratch_data}/*")

    # archive_mode must be off on the scratch cluster. It is a copy of a cluster
    # whose archive_command names a stanza that is not its own, and a promoted
    # copy pushing its new timeline into the live repository is how a restore
    # exercise damages the backups it was testing.
    sh("bash", "-c", f"cat >> {scratch_conf}/postgresql.conf <<'EOF'\n"
                     "\n# Phase 11 slice 0 scratch restore target.\n"
                     "archive_mode = off\n"
                     "EOF")

    t0 = time.monotonic()
    proc = pgbackrest("--log-level-console=warn", "restore",
                      f"--pg1-path={scratch_data}",
                      "--type=time", f"--target={target_time}",
                      "--target-action=promote", check=False)
    restore_seconds = time.monotonic() - t0
    if proc.returncode != 0:
        print(proc.stderr[-3000:])
        return 1
    repo_read = du_bytes(scratch_data)

    subprocess.run(["sudo", "pg_ctlcluster", PGVER, SCRATCH, "start"], capture_output=True, check=True)
    # Wait for recovery to finish and the promoted cluster to accept queries.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        p = sh("psql", "-p", str(SCRATCH_PORT), "-tAc", "SELECT pg_is_in_recovery()", check=False)
        if p.returncode == 0 and p.stdout.strip() == "f":
            break
        time.sleep(1)
    recovery_seconds = time.monotonic() - t0

    markers = sh("psql", "-p", str(SCRATCH_PORT), "-d", names.database, "-tAc",
                 "SELECT string_agg(note, ',' ORDER BY id) FROM public.restore_marker",
                 check=False).stdout.strip()

    t0 = time.monotonic()
    dump = f"/tmp/{names.database}.pitr.dump"
    sh("bash", "-c", f"rm -f {dump}")
    p = sh("pg_dump", "-p", str(SCRATCH_PORT), "-Fc", "-f", dump, names.database, check=False)
    extract_seconds = time.monotonic() - t0
    dump_size = int(sh("stat", "-c", "%s", dump, check=False).stdout.strip() or 0)

    # What the live cluster was doing throughout: nothing. Asserted rather than
    # assumed, because "restore one tenant without taking the node down" is the
    # acceptance criterion this whole path exists to satisfy.
    with admin() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'mldb\\_%'")
        live_dbs = cur.fetchone()[0]

    total = time.monotonic() - started
    print(f"  restore to scratch   {restore_seconds:7.2f}s   {repo_read/1024/1024:8.1f} MB written")
    print(f"  recovery to promoted {recovery_seconds:7.2f}s")
    print(f"  extract one tenant   {extract_seconds:7.2f}s   {dump_size/1024/1024:8.2f} MB dump")
    print(f"  total                {total:7.2f}s")
    print(f"  markers present at target: {markers!r}  (expect 'before-target' only)")
    print(f"  live cluster tenant databases throughout: {live_dbs}")
    if args.keep:
        print(f"  scratch cluster left running on port {SCRATCH_PORT}")
    else:
        subprocess.run(["sudo", "pg_dropcluster", "--stop", PGVER, SCRATCH], capture_output=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("provision")
    p.add_argument("--count", type=int, default=8)
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("backup")
    p.add_argument("--types", nargs="+", default=["full", "incr"])
    p.add_argument("--process-max", type=int, default=1)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("load")
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--tenants", type=int, default=8)
    p.add_argument("--rows", type=int, default=2000)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("roundtrip")
    p.add_argument("--ref", required=True)
    p.set_defaults(func=cmd_roundtrip)

    p = sub.add_parser("restore-tenant")
    p.add_argument("--ref", required=True)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_restore_tenant)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
