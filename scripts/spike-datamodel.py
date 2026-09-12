#!/usr/bin/env python3
"""Phase 12 slice 0 — measure what the data-model graph actually allows.

A SPIKE ARTEFACT, in the same class as `scripts/bench-backup.py`. Nothing imports
it. It exists so the findings in `specs/maludb-datamodel-model.md` can be
reproduced against a real bootstrapped tenant, and so ADR-074's delivery design
rests on measurement rather than on what the extension's comments say.

Each check provisions through the real provisioning module, so what is measured
is a MaluDB tenant as the platform builds one -- ADR-016 roles, ADR-018 hardening
and all -- not a database set up by hand.

    MALUDB_NODE_ADMIN_DSN=... MALUDB_PLATFORM_OWNER=... \\
        scripts/spike-datamodel.py run            # questions 1-5 and 7
    scripts/spike-datamodel.py reload             # question 6, needs PostgREST

Questions (numbered as in `plans/active/phase-12-maludb-features.md`):

1. Can a platform-owned SECURITY DEFINER wrapper call the facades on the path
   PostgREST takes -- logged in as the authenticator, `SET ROLE service_role`?
2. Does `describe` consider the caller's privileges?
3. Can any customer-controlled role reach the facades, even transitively?
4. Does enabling write anything into `public`?
5. What does refresh cost, and does it grow storage?
6. Does adding a schema to `db-schemas` need a PostgREST restart?
7. What does `ALTER EXTENSION ... UPDATE` do to an enabled schema?
"""


# A spike against a disposable node, following bench-backup.py's convention. The
# SQL it builds interpolates module constants and loop counters, never input; the
# one URL it opens is localhost; the one process it starts is PostgREST.
# ruff: noqa: S603, S608, S310

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.control_plane import provisioning, tenant_bootstrap  # noqa: E402

DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")
POSTGREST = os.environ.get("MALUDB_POSTGREST_BIN", "postgrest")
MEMORY_SCHEMA = "maludb_memory"
PREVIOUS_VERSION = os.environ.get("DM_PREVIOUS_VERSION", "0.103.0")
CURRENT_VERSION = os.environ.get("DM_CURRENT_VERSION", "0.104.0")

CUSTOMER_ROLES = ("anon", "authenticated", "service_role")


def need_dsn() -> str:
    if not DSN:
        sys.exit("MALUDB_NODE_ADMIN_DSN is unset: a superuser DSN for a disposable node")
    return DSN


def admin(**kw) -> psycopg.Connection:
    return psycopg.connect(need_dsn(), **kw)


def dsn_for(database: str, *, user: str | None = None, password: str | None = None) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(need_dsn())
    parts["dbname"] = database
    if user:
        parts["user"], parts["password"] = user, password
    return psycopg.conninfo.make_conninfo(**parts)


def one(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


def report(label: str, value) -> None:
    print(f"  {label:<52} {value}")


# --------------------------------------------------------------------------
# a tenant, built the way the platform builds one


def provision(ref: str) -> dict:
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
            provisioning.install_extension(t)
            tenant_bootstrap.apply(t)
        provisioning.verify_isolation(conn, names)
        conn.commit()
    return {"names": names, "passwords": passwords}


def teardown(*refs: str, extra_databases: tuple[str, ...] = ()) -> None:
    with admin(autocommit=True) as conn:
        for ref in refs:
            db = provisioning.TenantNames.for_ref(ref).database
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for db in extra_databases:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for ref in refs:
            rows = conn.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                (f"mldb\\_{ref}\\_%",),
            ).fetchall()
            for (role,) in rows:
                conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        for role in ("dm_spike_nopriv",):
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def enable(tconn) -> int:
    tconn.execute(f'CREATE SCHEMA IF NOT EXISTS "{MEMORY_SCHEMA}"')
    return one(tconn, "SELECT object_count FROM maludb_core.enable_memory_schema(%s)",
               (MEMORY_SCHEMA,))


# --------------------------------------------------------------------------
# questions 1-5 and 7


def cmd_run(args) -> int:
    ref, other = "dmspk001", "dmspk002"
    upgrade_db = "mldb_dmspkupg"
    teardown(ref, other, extra_databases=(upgrade_db,))
    try:
        tenant = provision(ref)
        names = tenant["names"]
        provision(other)

        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            print("\nQ4  does enabling write into public?")
            before = _public_inventory(t)
            report("objects enabled", enable(t))
            after = _public_inventory(t)
            report("objects added to public", len(after - before))
            report("objects removed from public", len(before - after))

            t.execute("CREATE TABLE public.customers (id bigint PRIMARY KEY, email text)")
            t.execute("CREATE TABLE public.orders (id bigint PRIMARY KEY, "
                      "customer_id bigint REFERENCES public.customers(id))")
            t.execute("CREATE TABLE public.salaries (id bigint PRIMARY KEY, ssn text)")
            t.execute("REVOKE ALL ON public.salaries FROM PUBLIC, anon, authenticated, service_role")

            print("\nQ3  can a customer-controlled role reach the facades?")
            for role in (*CUSTOMER_ROLES, names.authenticator, names.admin,
                         names.executor, names.client):
                usage = one(t, "SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, MEMORY_SCHEMA))
                execute = one(
                    t, "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    (role, f"{MEMORY_SCHEMA}.maludb_datamodel_describe(text)"),
                )
                report(role, f"USAGE={usage} EXECUTE={execute}")
            reach = one(t, _REACH_SQL, ([*CUSTOMER_ROLES, names.authenticator, names.admin,
                                         names.executor, names.client],))
            report("customer roles reaching a maludb_* role", reach)
            report("facade owner / SECURITY DEFINER", one(
                t, "SELECT pg_get_userbyid(proowner) || ' / ' || prosecdef FROM pg_proc "
                   "WHERE proname = 'maludb_datamodel_describe'"))

            print("\nQ2  does describe consider the caller's privileges?")
            t.execute("CREATE ROLE dm_spike_nopriv NOLOGIN")
            t.execute(f'GRANT USAGE ON SCHEMA "{MEMORY_SCHEMA}" TO dm_spike_nopriv')
            t.execute(f"GRANT EXECUTE ON FUNCTION {MEMORY_SCHEMA}.maludb_datamodel_describe(text) "
                      "TO dm_spike_nopriv")
            t.execute("SET ROLE dm_spike_nopriv")
            report("role can SELECT public.salaries",
                   one(t, "SELECT has_table_privilege('public.salaries', 'SELECT')"))
            described = one(t, f"SELECT {MEMORY_SCHEMA}.maludb_datamodel_describe('public.salaries')")
            report("describe returned salaries' columns",
                   [c["name"] for c in (described or {}).get("columns", [])])
            try:
                one(t, f"SELECT {MEMORY_SCHEMA}.maludb_datamodel_describe('auth.users')")
                report("describe auth.users", "RETURNED -- schema guard did not hold")
            except psycopg.Error as exc:
                report("describe auth.users", f"refused: {str(exc).splitlines()[0][:60]}")
            t.execute("RESET ROLE")

            print("\nQ1  can a platform wrapper call the facades on PostgREST's path?")
            t.execute("CREATE SCHEMA maludb")
            t.execute(
                "CREATE FUNCTION maludb.datamodel_describe(relation text) RETURNS jsonb "
                "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp "
                f"AS $$ SELECT {MEMORY_SCHEMA}.maludb_datamodel_describe(relation) $$"
            )
            t.execute("REVOKE ALL ON FUNCTION maludb.datamodel_describe(text) FROM PUBLIC")
            t.execute("GRANT USAGE ON SCHEMA maludb TO service_role")
            t.execute("GRANT EXECUTE ON FUNCTION maludb.datamodel_describe(text) TO service_role")

        with psycopg.connect(dsn_for(names.database, user=names.authenticator,
                                     password=tenant["passwords"]["authenticator"]),
                             autocommit=True) as pg:
            pg.execute("SET ROLE service_role")
            report("session_user / current_user",
                   one(pg, "SELECT session_user || ' / ' || current_user"))
            try:
                one(pg, "SELECT maludb.datamodel_describe('public.orders')")
                report("wrapper as service_role", "WORKS")
            except psycopg.Error as exc:
                report("wrapper as service_role", f"FAILS: {str(exc).splitlines()[0][:70]}")

        print("\nQ5  what does refresh cost?")
        other_db = provisioning.TenantNames.for_ref(other).database
        with psycopg.connect(dsn_for(other_db), autocommit=True) as t:
            enable(t)
            report("refresh, no customer tables (s)", f"{_timed_refresh(t):.2f}")
            _build_large_schema(t, tables=args.tables)
            report(f"refresh, {args.tables} tables (s)", f"{_timed_refresh(t):.2f}")
            t.execute("CHECKPOINT")
            size = one(t, "SELECT pg_database_size(current_database())")
            for _ in range(5):
                _timed_refresh(t)
            grown = one(t, "SELECT pg_database_size(current_database())") - size
            report("storage growth over 5 refreshes (bytes)", grown)
            report("edges visible after repeats",
                   one(t, f"SELECT count(*) FROM {MEMORY_SCHEMA}.maludb_edge"))

        print("\nQ7  what does ALTER EXTENSION UPDATE do to an enabled schema?")
        with admin(autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{upgrade_db}"')
        with psycopg.connect(dsn_for(upgrade_db), autocommit=True) as t:
            t.execute(f"CREATE EXTENSION maludb_core VERSION '{PREVIOUS_VERSION}' CASCADE")
            report(f"enabled at {PREVIOUS_VERSION}", enable(t))
            t.execute("CREATE TABLE public.orders (id bigint PRIMARY KEY)")
            t.execute(f"ALTER EXTENSION maludb_core UPDATE TO '{CURRENT_VERSION}'")
            report("datamodel facades after ALTER EXTENSION", _datamodel_facades(t))
            report("objects on re-enable", enable(t))
            report("datamodel facades after re-enable", _datamodel_facades(t))
            _timed_refresh(t)
            edges = one(t, f"SELECT count(*) FROM {MEMORY_SCHEMA}.maludb_edge")
            enable(t)
            report("edges before / after a second re-enable",
                   f"{edges} / {one(t, f'SELECT count(*) FROM {MEMORY_SCHEMA}.maludb_edge')}")
        return 0
    finally:
        if not args.keep:
            teardown(ref, other, extra_databases=(upgrade_db,))


_REACH_SQL = """
WITH RECURSIVE m(member, granted) AS (
    SELECT r.rolname, g.rolname FROM pg_auth_members am
      JOIN pg_roles r ON r.oid = am.member JOIN pg_roles g ON g.oid = am.roleid
     WHERE r.rolname = ANY(%s)
  UNION
    SELECT m.member, g.rolname FROM m
      JOIN pg_roles mr ON mr.rolname = m.granted
      JOIN pg_auth_members am ON am.member = mr.oid
      JOIN pg_roles g ON g.oid = am.roleid
)
SELECT count(*) FROM m WHERE granted LIKE 'maludb%%'
"""


def _public_inventory(t) -> set[str]:
    with t.cursor() as cur:
        cur.execute(
            "SELECT 'rel:' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = 'public' "
            "UNION ALL SELECT 'fn:' || p.oid::regprocedure::text || coalesce(array_to_string(p.proacl, ','), '') "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
        )
        return {r[0] for r in cur.fetchall()}


def _datamodel_facades(t) -> int:
    return one(t, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                  "WHERE n.nspname = %s AND p.proname LIKE 'maludb_datamodel%%'", (MEMORY_SCHEMA,))


def _timed_refresh(t) -> float:
    started = time.monotonic()
    t.execute(f"SELECT {MEMORY_SCHEMA}.maludb_datamodel_refresh('datamodel', ARRAY['public']::name[])")
    return time.monotonic() - started


def _build_large_schema(t, *, tables: int) -> None:
    for i in range(1, tables + 1):
        fk = f"REFERENCES public.t{i - 1}(id)" if i > 1 else ""
        t.execute(f"CREATE TABLE public.t{i} (id bigint PRIMARY KEY, parent_id bigint {fk}, "
                  "name text, amount numeric, created_at timestamptz)")
    for i in range(1, min(50, tables - 1) + 1):
        t.execute(f"CREATE VIEW public.v{i} AS SELECT a.id, b.name FROM public.t{i + 1} a "
                  f"JOIN public.t{i} b ON b.id = a.parent_id")
        t.execute(f"CREATE FUNCTION public.f{i}(p bigint) RETURNS bigint LANGUAGE sql "
                  f"AS $f$ SELECT count(*) FROM public.t{i} WHERE parent_id = p $f$")


# --------------------------------------------------------------------------
# question 6


def cmd_reload(args) -> int:
    ref = "dmspk003"
    teardown(ref)
    proc = None
    workdir = Path(tempfile.mkdtemp(prefix="dm-spike-"))
    try:
        tenant = provision(ref)
        names = tenant["names"]
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            t.execute("CREATE SCHEMA maludb")
            t.execute("CREATE FUNCTION maludb.ping() RETURNS text LANGUAGE sql AS $$ SELECT 'pong' $$")
            t.execute("GRANT USAGE ON SCHEMA maludb TO anon")
            t.execute("GRANT EXECUTE ON FUNCTION maludb.ping() TO anon")

        conf = workdir / "postgrest.conf"
        uri = dsn_for(names.database, user=names.authenticator,
                      password=tenant["passwords"]["authenticator"])

        def write(schemas: str) -> None:
            conf.write_text(
                f'db-uri = "{uri}"\ndb-schemas = "{schemas}"\ndb-anon-role = "anon"\n'
                f'server-host = "127.0.0.1"\nserver-port = {args.port}\n'
            )
            conf.chmod(0o600)

        write("public")
        proc = subprocess.Popen([POSTGREST, str(conf)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        _wait_for(lambda: _ping(args.port)[0] in (200, 404, 406), 30)
        print("\nQ6  does adding a schema to db-schemas need a restart?")
        report("before (db-schemas=public)", _ping(args.port))

        write("public, maludb")
        started = time.monotonic()
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            t.execute("NOTIFY pgrst, 'reload config'")
            t.execute("NOTIFY pgrst, 'reload schema'")
        served = _wait_for(lambda: "pong" in _ping(args.port)[1], 30)
        report("after NOTIFY reload config + schema",
               f"{_ping(args.port)} in {time.monotonic() - started:.2f}s" if served else "never served")
        report("same process (no restart)", proc.poll() is None)
        return 0
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        if not args.keep:
            teardown(ref)


def _ping(port: int) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/rpc/ping", data=b"{}", method="POST",
                                 headers={"Content-Profile": "maludb",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()[:60]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}").get("code", "")
    except OSError:
        return 0, "unreachable"


def _wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="questions 1-5 and 7")
    p.add_argument("--tables", type=int, default=300)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("reload", help="question 6; needs a PostgREST binary")
    p.add_argument("--port", type=int, default=3999)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_reload)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
