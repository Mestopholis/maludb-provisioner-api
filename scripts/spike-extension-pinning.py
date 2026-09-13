#!/usr/bin/env python3
"""ADR-075 slice 0 — measure what moving `vector` under live tenants does.

A SPIKE ARTEFACT, in the class of `scripts/spike-datamodel.py`. Nothing imports
it. It exists so the findings in `specs/extension-pinning-model.md` can be
reproduced, and so the pinning plan's rollout design rests on measurement.

It needs a node whose `vector` package it may replace while tenants are
connected -- which is exactly what must never be pointed at a shared cluster. It
was run against a disposable container:

    podman run -d --name pin-spike -e POSTGRES_PASSWORD=... \\
        -p 127.0.0.1:5442:5432 docker.io/library/postgres:17
    # inside: postgresql-17-pgvector=0.8.4-*, and maludb_core built from
    # the commit CI pins (MALUDB_CORE_REF)

    MALUDB_NODE_ADMIN_DSN=postgresql://postgres:...@127.0.0.1:5442/postgres \\
        scripts/spike-extension-pinning.py run \\
        --node-shell "podman exec -i pin-spike" \\
        --swap "apt-get install -y -q postgresql-17-pgvector=0.8.6-1.pgdg13+1" \\
        --to 0.8.6

Every tenant is provisioned through the real provisioning module, so ADR-016's
roles and ADR-018's hardening trigger are the platform's, not a hand-built copy.

Questions (numbered as in `plans/active/phase-12-extension-pinning.md`):

1. What do `vector`'s upgrade scripts contain between the two versions?
2. After the package moves and before any `ALTER EXTENSION`, do tenants still
   answer through HNSW and IVFFlat -- in a session opened before the swap and in
   a new one -- and does `maludb_core`'s own HNSW index still serve?
3. What does `ALTER EXTENSION vector UPDATE` lock, for how long, does it block a
   concurrent reader or writer, and does it rebuild or invalidate any index?
4. Does ADR-018's trigger revoke a function a `vector` update adds? (Synthetic:
   no real 0.8.x step adds one, so a one-function step is installed for it.)
5. What `CREATE EXTENSION` does a dump carry -- the path moves and restores
   take -- and so what version does a tenant arrive at on its new node?
"""

# A spike against a disposable node. The SQL it builds interpolates module
# constants and validated version strings, never input from elsewhere; the one
# process it starts is the operator's own --node-shell command.
# ruff: noqa: S602, S603, S607, S608

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.control_plane import provisioning, tenant_bootstrap  # noqa: E402

DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")
REFS = ("pinspk01", "pinspk02")
ROWS = int(os.environ.get("PIN_SPIKE_ROWS", "20000"))
DIMS = 64
SYNTHETIC_SUFFIX = "spike"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def need_dsn() -> str:
    if not DSN:
        sys.exit("MALUDB_NODE_ADMIN_DSN is unset: a superuser DSN for a DISPOSABLE node")
    return DSN


def admin(**kw) -> psycopg.Connection:
    return psycopg.connect(need_dsn(), **kw)


def dsn_for(database: str) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(need_dsn())
    parts["dbname"] = database
    return psycopg.conninfo.make_conninfo(**parts)


def one(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def report(label: str, value) -> None:
    print(f"  {label:<60} {value}")


def node_shell(prefix: str, command: str, *, stdin: str | None = None) -> float:
    started = time.monotonic()
    proc = subprocess.run(f"{prefix} sh -c {shlex.quote(command)}", shell=True,
                          input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"node command failed ({proc.returncode}): {command}\n{proc.stderr[-2000:]}")
    return time.monotonic() - started


# --------------------------------------------------------------------------
# tenants, built the way the platform builds them


def provision(ref: str) -> provisioning.TenantNames:
    names = provisioning.TenantNames.for_ref(ref)
    passwords = {k: provisioning.generate_password()
                 for k in ("authenticator", "auth", "admin", "executor", "client", "storage")}
    with admin() as conn:
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
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            provisioning.install_extension(
                t, pins=dict(t.execute("SELECT name, default_version FROM pg_available_extensions "
                                        "WHERE name IN ('vector', 'maludb_core')").fetchall()))
            tenant_bootstrap.apply(t)
            tenant_bootstrap.verify(t)
        provisioning.verify_isolation(conn, names)
        conn.commit()
    return names


def teardown() -> None:
    with admin(autocommit=True) as conn:
        for ref in REFS:
            db = provisioning.TenantNames.for_ref(ref).database
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for ref in REFS:
            for (role,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                                        (f"mldb\\_{ref}\\_%",)).fetchall():
                conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def load_vectors(t) -> None:
    t.execute(f"CREATE TABLE public.items (id bigserial PRIMARY KEY, category int NOT NULL, "
              f"embedding vector({DIMS}) NOT NULL)")
    t.execute(
        f"INSERT INTO public.items (category, embedding) "
        f"SELECT (random() * 20)::int, "
        f"       (SELECT array_agg(random())::vector({DIMS}) FROM generate_series(1, {DIMS}) WHERE g > 0) "
        f"  FROM generate_series(1, {ROWS}) AS g"
    )
    t.execute("SET maintenance_work_mem = '64MB'")
    t.execute("CREATE INDEX items_hnsw ON public.items USING hnsw (embedding vector_l2_ops)")
    t.execute("CREATE INDEX items_ivfflat ON public.items USING ivfflat (embedding vector_cosine_ops) "
              "WITH (lists = 100)")
    t.execute("ANALYZE public.items")


PROBE = f"(SELECT array_agg(0.5::real)::vector({DIMS}) FROM generate_series(1, {DIMS}))"


def ann_query(t, index: str) -> tuple[str, int]:
    """Run a nearest-neighbour query that must use `index`; return the plan's scan and row count."""
    op = "<->" if index == "items_hnsw" else "<=>"
    t.execute("SET enable_seqscan = off")
    t.execute("SET enable_indexscan = on")
    plan = "\n".join(r[0] for r in t.execute(
        f"EXPLAIN SELECT id FROM public.items ORDER BY embedding {op} {PROBE} LIMIT 10").fetchall())
    rows = len(t.execute(f"SELECT id FROM public.items ORDER BY embedding {op} {PROBE} LIMIT 10").fetchall())
    t.execute("RESET enable_seqscan")
    used = index if index in plan else f"NOT {index}: {plan.splitlines()[0]}"
    return used, rows


def index_state(t) -> dict[str, tuple[int, bool]]:
    return {name: (relfilenode, valid) for name, relfilenode, valid in t.execute(
        "SELECT c.relname, c.relfilenode, i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_am am ON am.oid = c.relam WHERE am.amname IN ('hnsw', 'ivfflat') "
        "ORDER BY c.relname").fetchall()}


def vector_library_mapping(conn, pid: int) -> str:
    """What vector.so a backend has mapped, read from its /proc maps."""
    maps = one(conn, "SELECT pg_read_file(%s)", (f"/proc/{pid}/maps",)) or ""
    lines = {line.split(None, 5)[-1] for line in maps.splitlines() if "vector.so" in line}
    return ", ".join(sorted(lines)) or "vector.so not mapped"


# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    for v in (args.to,):
        if not VERSION_RE.match(v):
            sys.exit(f"not a version: {v}")
    teardown()
    if args.reset:
        report("reset the node's package first", f"{node_shell(args.node_shell, args.reset):.1f} s")
    try:
        with admin(autocommit=True) as a:
            sharedir = one(a, "SELECT setting FROM pg_config WHERE name = 'SHAREDIR'")
            from_version = one(a, "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
            report("PostgreSQL", one(a, "SELECT version()").split(" on ")[0])
            report("vector default_version before the swap", from_version)

        print("\nsetup  two tenants, provisioned and loaded")
        tenants = []
        for ref in REFS:
            started = time.monotonic()
            names = provision(ref)
            with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
                load_vectors(t)
                report(f"{ref}: provisioned + {ROWS} rows + HNSW + IVFFlat",
                       f"{time.monotonic() - started:.1f} s, extversion "
                       f"{one(t, 'SELECT extversion FROM pg_extension WHERE extname = %s', ('vector',))}")
            tenants.append(names)
        t1, t2 = tenants

        # A session opened before the package moves, and kept: PostgREST's pool
        # holds connections exactly like this.
        old = psycopg.connect(dsn_for(t1.database), autocommit=True)
        ann_query(old, "items_hnsw")
        old_pid = old.info.backend_pid

        print(f"\nQ1  upgrade scripts {from_version} -> {args.to} on the node's package")
        swap_seconds = node_shell(args.node_shell, args.swap)
        with admin(autocommit=True) as a:
            to_available = one(a, "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
            report("package swap took", f"{swap_seconds:.1f} s")
            report("vector default_version after the swap", to_available)
            files = sorted(f for f in (r[0] for r in a.execute(
                "SELECT pg_ls_dir(%s)", (f"{sharedir}/extension",)).fetchall())
                if f.startswith("vector--") and f.count("--") == 2)
            steps = _steps(files, from_version, args.to)
            for step in steps:
                body = one(a, "SELECT pg_read_file(%s)", (f"{sharedir}/extension/{step}",))
                ddl = len(re.findall(r"^\s*(CREATE|ALTER|DROP|GRANT|REVOKE)\b", body, re.M | re.I))
                report(step, f"{len(body)} bytes, {ddl} DDL statements")

        print("\nQ2  after the swap, before any ALTER EXTENSION")
        report("old session: vector.so mapping", vector_library_mapping(old, old_pid))
        report("old session: HNSW query", ann_query(old, "items_hnsw"))
        with psycopg.connect(dsn_for(t1.database), autocommit=True) as t:
            ann_query(t, "items_hnsw")  # loads the library into this backend
            report("new session: vector.so mapping", vector_library_mapping(t, t.info.backend_pid))
            report("new session: extversion (unchanged)",
                   one(t, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
            report("new session: HNSW query", ann_query(t, "items_hnsw"))
            report("new session: IVFFlat query", ann_query(t, "items_ivfflat"))
            report("maludb_core_version()", one(t, "SELECT maludb_core.maludb_core_version()"))
            t.execute("SET enable_seqscan = off")
            t.execute('INSERT INTO maludb_core."malu$vector_demo" (label, embedding) '
                      "SELECT 'pin-spike', array_agg(random())::vector(8) FROM generate_series(1, 8)")
            plan = "\n".join(r[0] for r in t.execute(
                'EXPLAIN SELECT 1 FROM maludb_core."malu$vector_demo" '
                "ORDER BY embedding <=> '[1,1,1,1,1,1,1,1]' LIMIT 1").fetchall())
            report("maludb_core's own HNSW index serves", "malu$vector_demo_embedding_hnsw" in plan)
            report("maludb_core functions whose body uses vector operators", one(t,
                   "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                   "WHERE n.nspname = 'maludb_core' AND p.prosrc ~ '<=>|<->|<#>|::vector'"))
            before_idx = index_state(t)

        print(f"\nQ3  ALTER EXTENSION vector UPDATE TO '{args.to}' with a reader and a writer running")
        stop = threading.Event()
        stats = {"reads": 0, "writes": 0, "read_max_ms": 0.0, "write_max_ms": 0.0, "errors": []}

        def loop(kind: str) -> None:
            with psycopg.connect(dsn_for(t1.database), autocommit=True) as c:
                c.execute("SET lock_timeout = '2s'")
                while not stop.is_set():
                    started = time.monotonic()
                    try:
                        if kind == "reads":
                            c.execute("SET enable_seqscan = off")
                            c.execute(f"SELECT id FROM public.items ORDER BY embedding <-> {PROBE} LIMIT 10").fetchall()
                        else:
                            c.execute(f"INSERT INTO public.items (category, embedding) VALUES (1, {PROBE})")
                    except psycopg.Error as exc:
                        stats["errors"].append(f"{kind}: {exc.diag.sqlstate} {exc}")
                    ms = (time.monotonic() - started) * 1000
                    stats[kind] += 1
                    key = "read_max_ms" if kind == "reads" else "write_max_ms"
                    stats[key] = max(stats[key], ms)

        threads = [threading.Thread(target=loop, args=(k,)) for k in ("reads", "writes")]
        for th in threads:
            th.start()
        time.sleep(1)
        with psycopg.connect(dsn_for(t1.database)) as u:
            started = time.monotonic()
            u.execute(f"ALTER EXTENSION vector UPDATE TO '{args.to}'")
            alter_ms = (time.monotonic() - started) * 1000
            with admin(autocommit=True) as a:
                locks = a.execute(
                    "SELECT locktype, mode, "
                    "coalesce(relation::regclass::text, classid::regclass::text || ':' || objid, '') "
                    "FROM pg_locks WHERE pid = %s AND granted ORDER BY 1, 2, 3",
                    (u.info.backend_pid,)).fetchall()
                blocked = a.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = %s "
                    "AND wait_event_type = 'Lock'", (t1.database,)).fetchone()[0]
            time.sleep(1)  # the transaction stays open, so any blocking shows in the loops
            u.commit()
            commit_ms = (time.monotonic() - started) * 1000 - alter_ms
        time.sleep(1)
        stop.set()
        for th in threads:
            th.join()
        report("ALTER EXTENSION statement", f"{alter_ms:.1f} ms")
        report("commit, after holding the transaction open ~1 s", f"{commit_ms:.0f} ms later")
        report("locks held by the updating transaction", "")
        for locktype, mode, obj in locks:
            print(f"      {locktype:<14} {mode:<24} {obj}")
        report("sessions waiting on a lock while it was open", blocked)
        report("reader: queries / worst latency", f"{stats['reads']} / {stats['read_max_ms']:.0f} ms")
        report("writer: inserts / worst latency", f"{stats['writes']} / {stats['write_max_ms']:.0f} ms")
        report("errors in reader or writer", stats["errors"][:3] or "none")
        with psycopg.connect(dsn_for(t1.database), autocommit=True) as t:
            after_idx = index_state(t)
            report("extversion after", one(t, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
            report("indexes rebuilt (relfilenode changed)",
                   [n for n in after_idx if after_idx[n][0] != before_idx.get(n, (None,))[0]] or "none")
            report("indexes invalid after", [n for n, (_, ok) in after_idx.items() if not ok] or "none")
            tenant_bootstrap.verify(t)
            report("tenant_bootstrap.verify after the update", "passes")

        print(f"\nQ4  a vector update that adds a function (synthetic step {args.to} -> {args.to}{SYNTHETIC_SUFFIX})")
        synthetic = f"{sharedir}/extension/vector--{args.to}--{args.to}{SYNTHETIC_SUFFIX}.sql"
        node_shell(args.node_shell, f"cat > {shlex.quote(synthetic)}", stdin=(
            "CREATE FUNCTION pin_spike_added(vector) RETURNS double precision\n"
            "  AS 'SELECT vector_norm($1)' LANGUAGE sql IMMUTABLE;\n"))
        try:
            with psycopg.connect(dsn_for(t2.database), autocommit=True) as t:
                t.execute(f"ALTER EXTENSION vector UPDATE TO '{args.to}'")
                t.execute(f"ALTER EXTENSION vector UPDATE TO '{args.to}{SYNTHETIC_SUFFIX}'")
                oid = one(t, "SELECT 'public.pin_spike_added(vector)'::regprocedure::oid")
                report("added function is an extension member",
                       one(t, "SELECT count(*) FROM pg_depend WHERE objid = %s AND deptype = 'e'", (oid,)) == 1)
                for role in ("anon", "authenticated", "service_role"):
                    report(f"{role} may EXECUTE it",
                           one(t, "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, oid)))
                tenant_bootstrap.verify(t)
                report("tenant_bootstrap.verify after the synthetic update", "passes")
        finally:
            node_shell(args.node_shell, f"rm -f {shlex.quote(synthetic)}")

        print("\nQ5  what a dump carries for the extension (the move and restore path)")
        host = psycopg.conninfo.conninfo_to_dict(need_dsn())
        env = {**os.environ, "PGPASSWORD": host.get("password", "")}
        dump = subprocess.run(
            ["pg_dump", "--schema-only", "-h", host.get("host", "127.0.0.1"), "-p", str(host.get("port", 5432)),
             "-U", host.get("user", "postgres"), t1.database],
            capture_output=True, text=True, env=env)
        for line in dump.stdout.splitlines():
            if line.startswith("CREATE EXTENSION"):
                print(f"      {line}")
        if dump.returncode != 0:
            report("pg_dump failed", dump.stderr[-300:])
        old.close()
    finally:
        if not args.keep:
            teardown()
    return 0


def _steps(files: list[str], start: str, target: str) -> list[str]:
    """The chain of update scripts from start to target, following the file names."""
    edges = {}
    for f in files:
        a, b = f[len("vector--"):-len(".sql")].split("--")
        edges.setdefault(a, []).append(b)
    chain, current = [], start
    while current != target:
        nxt = [b for b in edges.get(current, []) if _key(b) <= _key(target)]
        if not nxt:
            return chain
        best = max(nxt, key=_key)
        chain.append(f"vector--{current}--{best}.sql")
        current = best
    return chain


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="questions 1-5")
    p.add_argument("--node-shell", required=True,
                   help='a command prefix that runs a shell ON THE NODE, e.g. "podman exec -i pin-spike"')
    p.add_argument("--swap", required=True, help="the package command that moves vector on the node")
    p.add_argument("--to", required=True, help="the vector version the swap installs")
    p.add_argument("--reset", help="a package command run first, to put the starting version back")
    p.add_argument("--keep", action="store_true", help="leave the tenants in place")
    p.set_defaults(func=cmd_run)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
