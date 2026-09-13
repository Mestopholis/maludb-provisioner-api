#!/usr/bin/env python3
"""ADR-076 grants slice 0 — can a PostgREST pre-request check replace ADR-018's revoke?

A SPIKE ARTEFACT, in the class of `scripts/spike-datamodel.py`. Nothing imports
it. Findings in `specs/extension-grants-model.md`.

It provisions one throwaway tenant through the real provisioning module, restores
`EXECUTE` on extension functions to the six customer roles **on that tenant
only**, runs a real PostgREST from `workers.render_config`, and measures:

1. What request settings a pre-request function sees, for an RPC call.
2. What PostgREST lists (OpenAPI) and can call (/rpc) of the extension functions
   once `anon` holds `EXECUTE` -- before any check.
3. Whether a pre-request check refuses /rpc to extension functions, with what
   status and body, before the function body runs, without touching a customer's
   own functions or table requests -- and what it costs.
4. Whether the customer -- the executor or client role in `SET ROLE` admin, the
   path the SQL console and direct connections take -- can remove or bypass it.

    MALUDB_NODE_ADMIN_DSN=... MALUDB_PLATFORM_OWNER=postgres \\
    MALUDB_POSTGREST_BIN=/usr/local/bin/postgrest \\
        scripts/spike-extension-grants.py run

Uses a disposable node: it creates and drops `mldb_gnspk001*`.
"""

# A spike against a disposable node. SQL interpolates module constants and names
# read back from the catalogue of the tenant it just built; the one URL it opens
# is its own PostgREST on loopback; the one process it starts is that PostgREST.
# ruff: noqa: S603, S608, S310, S105

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.control_plane import provisioning, tenant_bootstrap, workers  # noqa: E402

DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")
POSTGREST = os.environ.get("MALUDB_POSTGREST_BIN", "postgrest")
REF = "gnspk001"
CHECK = "maludb_platform.refuse_extension_rpc"


def need_dsn() -> str:
    if not DSN:
        sys.exit("MALUDB_NODE_ADMIN_DSN is unset: a superuser DSN for a DISPOSABLE node")
    return DSN


def dsn_for(database: str, *, user: str | None = None, password: str | None = None) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(need_dsn())
    parts["dbname"] = database
    if user:
        parts["user"], parts["password"] = user, password
    return psycopg.conninfo.make_conninfo(**parts)


def one(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def report(label: str, value) -> None:
    print(f"  {label:<64} {value}")


def provision() -> tuple[provisioning.TenantNames, dict]:
    names = provisioning.TenantNames.for_ref(REF)
    passwords = {k: provisioning.generate_password()
                 for k in ("authenticator", "auth", "admin", "executor", "client", "storage")}
    with psycopg.connect(need_dsn()) as conn:
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
            tenant_bootstrap.sync_extension_allowlist(t)
            tenant_bootstrap.verify(t)
        provisioning.verify_isolation(conn, names)
        provisioning.set_direct_sql_access(conn, names, enabled=True)
        conn.commit()
    return names, passwords


def teardown() -> None:
    with psycopg.connect(need_dsn(), autocommit=True) as conn:
        db = provisioning.TenantNames.for_ref(REF).database
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for (role,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                                    (f"mldb\\_{REF}\\_%",)).fetchall():
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


# --------------------------------------------------------------------------
# the tenant: customer objects, then ADR-076 decision 4 by hand


CUSTOMER_SQL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE public.docs (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    body text NOT NULL,
    embedding vector(3) NOT NULL
);
INSERT INTO public.docs (body, embedding) VALUES ('a', '[1,0,0]'), ('b', '[0,1,0]');
CREATE FUNCTION public.match_documents(query vector(3), k int)
RETURNS TABLE (body text, distance double precision) LANGUAGE sql STABLE AS
$$ SELECT body, embedding <=> query FROM public.docs ORDER BY embedding <=> query LIMIT k $$;
CREATE FUNCTION public.request_settings() RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'request.path', current_setting('request.path', true),
        'request.method', current_setting('request.method', true),
        'request.headers', current_setting('request.headers', true)::jsonb -> 'content-profile',
        'role', current_user)
$$;
CREATE SEQUENCE public.bump_seq;
CREATE FUNCTION public.bump() RETURNS bigint LANGUAGE sql VOLATILE AS $$ SELECT nextval('public.bump_seq') $$;
-- shares a name with pg_trgm's similarity(text, text)
CREATE FUNCTION public.similarity(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'customer:' || t $$;
GRANT USAGE, SELECT ON SEQUENCE public.bump_seq TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.match_documents(vector, int), public.request_settings(), public.bump(),
      public.similarity(text) TO anon, authenticated, service_role;
"""


def restore_grants(t, names: provisioning.TenantNames) -> int:
    roles = ["anon", "authenticated", "service_role", names.admin, names.client, names.executor]
    funcs = [r[0] for r in t.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_depend d ON d.objid = p.oid AND d.classid = 'pg_proc'::regclass AND d.deptype = 'e' "
        "JOIN pg_extension e ON e.oid = d.refobjid WHERE e.extname <> 'maludb_core'").fetchall()]
    grantees = ", ".join(f'"{r}"' for r in roles)
    # The trigger would re-revoke on the next extension DDL; nothing below runs any.
    for f in funcs:
        t.execute(f"GRANT EXECUTE ON FUNCTION {f} TO {grantees}")
    return len(funcs)


# ADR-076 as amended by grants slice 0: refuse a name if ANY function of it in
# the exposed schema is extension-owned. "every" is the rule as first written,
# kept so the bypass it allows stays measured.
MATCH = {
    "any": """EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
             WHERE p.proname = fn AND n.nspname = 'public')""",
    "every": """EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.proname = fn AND n.nspname = 'public')
        AND NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.proname = fn AND n.nspname = 'public'
               AND NOT EXISTS (SELECT 1 FROM pg_depend d
                                WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                                  AND d.deptype = 'e'))""",
}


def check_sql(*, extra_refused: str = "", error: str = "PT403", match: str = "any") -> str:
    condition = MATCH[match]
    raise_stmt = {
        "PT403": "RAISE EXCEPTION 'function % is not available over the Data API', fn USING ERRCODE = 'PT403'",
        "PGRST": ("RAISE SQLSTATE 'PGRST' USING "
                  "message = json_build_object('code', 'MLDB404', 'message', "
                  "'function ' || fn || ' is not available over the Data API')::text, "
                  "detail = json_build_object('status', 404, 'headers', json_build_object())::text"),
    }[error]
    return f"""
CREATE OR REPLACE FUNCTION {CHECK}() RETURNS void
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    path text := current_setting('request.path', true);
    fn   text;
BEGIN
    IF path IS NULL OR left(path, 5) <> '/rpc/' THEN
        RETURN;
    END IF;
    fn := substr(path, 6);
    IF {extra_refused} ({condition}) THEN
        {raise_stmt};
    END IF;
END $fn$;
GRANT EXECUTE ON FUNCTION {CHECK}() TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA maludb_platform TO anon, authenticated, service_role;
"""


# --------------------------------------------------------------------------
# PostgREST


class Api:
    def __init__(self, port: int, secret: str):
        self.port = port
        self.tokens = {
            "anon": None,
            "authenticated": jwt.encode({"role": "authenticated", "sub": "u1", "exp": int(time.time()) + 3600},
                                        secret, algorithm="HS256"),
            "service_role": jwt.encode({"role": "service_role", "exp": int(time.time()) + 3600},
                                       secret, algorithm="HS256"),
        }

    def call(self, method: str, path: str, *, role: str = "anon", body=None,
             content_type: str = "application/json") -> tuple[int, str]:
        headers = {"Content-Type": content_type}
        if self.tokens[role]:
            headers["Authorization"] = f"Bearer {self.tokens[role]}"
        data = None
        if body is not None:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except OSError as exc:
            return 0, str(exc)


def wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def brief(status_body: tuple[int, str]) -> str:
    status, body = status_body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "code" in parsed:
            return f"{status} {parsed.get('code', '')} {str(parsed.get('message', ''))[:70]}"
        if isinstance(parsed, dict):
            return f"{status} {json.dumps(parsed)[:160]}"
        return f"{status} {str(parsed)[:80]}"
    except ValueError:
        return f"{status} {body[:80]}"


# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    teardown()
    workdir = Path(tempfile.mkdtemp(prefix="grants-spike-"))
    proc = None
    try:
        names, passwords = provision()
        tdsn = dsn_for(names.database)
        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute(CUSTOMER_SQL)
            ext_names = sorted({r[0] for r in t.execute(
                "SELECT DISTINCT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "JOIN pg_depend d ON d.objid = p.oid AND d.classid = 'pg_proc'::regclass AND d.deptype = 'e' "
                "WHERE n.nspname = 'public'").fetchall()})
            report("extension function names in public (distinct)", len(ext_names))
            report("EXECUTE restored by hand on extension functions", restore_grants(t, names))

        secret = secrets.token_urlsafe(48)
        settings = workers.WorkerSettings(
            project_ref=REF, database=names.database, authenticator_role=names.authenticator,
            authenticator_password=passwords["authenticator"], jwt_secret=secret, port=args.port)
        conf = workdir / "postgrest.conf"

        def write_conf(pre_request: str | None) -> None:
            text = workers.render_config(settings)
            if pre_request is not None:
                text += f'db-pre-request = "{pre_request}"\n'
            conf.write_text(text)
            conf.chmod(0o600)

        def reload() -> None:
            with psycopg.connect(tdsn, autocommit=True) as t:
                t.execute("NOTIFY pgrst, 'reload config'")
                t.execute("NOTIFY pgrst, 'reload schema'")
            time.sleep(1.5)

        write_conf(None)
        proc = subprocess.Popen([POSTGREST, str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        api = Api(args.port, secret)
        if not wait_for(lambda: api.call("GET", "/")[0] == 200, 30):
            sys.exit("PostgREST never answered")

        print("\nQ1  what a pre-request function can read, as seen from an RPC call")
        report("POST /rpc/request_settings as anon", brief(api.call("POST", "/rpc/request_settings", body={})))
        report("GET  /rpc/request_settings as anon", brief(api.call("GET", "/rpc/request_settings")))

        print("\nQ2  with EXECUTE restored and no check")
        for role in ("anon", "authenticated", "service_role"):
            status, body = api.call("GET", "/", role=role)
            paths = json.loads(body).get("paths", {}) if status == 200 else {}
            rpc = [p[len("/rpc/"):] for p in paths if p.startswith("/rpc/")]
            listed_ext = sorted(set(rpc) & set(ext_names))
            report(f"OpenAPI for {role}: rpc paths / of them extension names",
                   f"{len(rpc)} / {len(listed_ext)}")
            if role == "anon":
                report("  extension names listed for anon", ", ".join(listed_ext) or "none")
        callable_names, statuses, errors = [], {}, []
        for name in ext_names:
            best = None
            for kwargs in ({"body": {}}, {"body": "bf", "content_type": "text/plain"}):
                status, body = api.call("POST", f"/rpc/{name}", **kwargs)
                statuses[status] = statuses.get(status, 0) + 1
                if status >= 500:
                    errors.append(f"{name} {kwargs.get('content_type', 'json')}: {brief((status, body))}")
                if 200 <= status < 300:
                    best = status
            if best:
                callable_names.append(name)
        report("extension names callable over /rpc as anon (2xx)", f"{len(callable_names)}: "
               + ", ".join(callable_names))
        report("status counts across attempts", dict(sorted(statuses.items())))
        for line in errors:
            report("  5xx", line)
        report("gen_salt as anon, text body 'bf'",
               brief(api.call("POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))
        report("match_documents as anon", brief(api.call(
            "POST", "/rpc/match_documents", body={"query": "[1,0,0]", "k": 1})))
        report("match_documents as authenticated", brief(api.call(
            "POST", "/rpc/match_documents", role="authenticated", body={"query": "[1,0,0]", "k": 1})))
        report("insert into docs (uuid_generate_v4 default) as authenticated", brief(api.call(
            "POST", "/docs", role="authenticated", body={"body": "c", "embedding": "[0,0,1]"})))

        print("\nQ3  the pre-request check")
        latency_without = _latency(api)
        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute(check_sql())
        write_conf(CHECK)
        reload()
        report("gen_salt as anon, text body", brief(api.call(
            "POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))
        report("gen_salt as service_role, text body", brief(api.call(
            "POST", "/rpc/gen_salt", role="service_role", body="bf", content_type="text/plain")))
        report("dearmor as anon (reached, and errored, before the check)", brief(api.call(
            "POST", "/rpc/dearmor", body="x", content_type="text/plain")))
        still = [n for n in callable_names
                 if 200 <= api.call("POST", f"/rpc/{n}", body="bf", content_type="text/plain")[0] < 300
                 or 200 <= api.call("POST", f"/rpc/{n}", body={})[0] < 300]
        report("previously callable extension names still callable", still or "none")
        report("customer match_documents as anon", brief(api.call(
            "POST", "/rpc/match_documents", body={"query": "[1,0,0]", "k": 1})))
        report("customer similarity(t) (shares pg_trgm's name) [any]", brief(api.call(
            "POST", "/rpc/similarity", body={"t": "x"})))
        report("pg_trgm similarity via the shared name, two args", brief(api.call(
            "POST", "/rpc/similarity", body={"": "a"})))
        report("table request GET /docs as anon", brief(api.call("GET", "/docs?select=body")))
        report("OpenAPI root as anon", api.call("GET", "/")[0])
        latency_with = _latency(api)
        report("median latency /rpc/request_settings without / with check",
               f"{latency_without:.2f} ms / {latency_with:.2f} ms")

        # Does the refused function's body run? A sequence is not transactional,
        # so a nextval inside a rolled-back call still shows.
        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute(check_sql(extra_refused="fn = 'bump' OR"))
            t.execute("SELECT nextval('public.bump_seq')")  # so last_value moves on every call
            before = one(t, "SELECT last_value FROM public.bump_seq")
        reload()
        refused = api.call("POST", "/rpc/bump", body={})
        with psycopg.connect(tdsn, autocommit=True) as t:
            after = one(t, "SELECT last_value FROM public.bump_seq")
            t.execute(check_sql())
        reload()
        control = api.call("POST", "/rpc/bump", body={})
        with psycopg.connect(tdsn, autocommit=True) as t:
            after_control = one(t, "SELECT last_value FROM public.bump_seq")
        report("refused /rpc/bump: status / sequence before -> after", f"{brief(refused)} / {before} -> {after}")
        report("control (not refused): status / sequence", f"{brief(control)} / {after_control}")

        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute(check_sql(error="PGRST"))
        reload()
        report("refusal raised as SQLSTATE PGRST with status 404", brief(api.call(
            "POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))
        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute(check_sql())
        reload()

        print("\nQ4  can the customer remove or bypass the check?")
        attempts = [
            ("ALTER ROLE authenticator IN DATABASE ... SET pgrst.db_pre_request",
             f'ALTER ROLE "{names.authenticator}" IN DATABASE "{names.database}" SET pgrst.db_pre_request = \'\''),
            ("ALTER ROLE authenticator SET pgrst.db_pre_request",
             f'ALTER ROLE "{names.authenticator}" SET pgrst.db_pre_request = \'\''),
            ("ALTER DATABASE ... SET pgrst.db_pre_request",
             f'ALTER DATABASE "{names.database}" SET pgrst.db_pre_request = \'\''),
            ("CREATE OR REPLACE the check function",
             f"CREATE OR REPLACE FUNCTION {CHECK}() RETURNS void LANGUAGE sql AS 'SELECT'"),
            ("DROP the check function", f"DROP FUNCTION {CHECK}()"),
            ("ALTER FUNCTION ... OWNER / RENAME", f"ALTER FUNCTION {CHECK}() RENAME TO gone"),
            ("REVOKE EXECUTE on the check from anon", f"REVOKE EXECUTE ON FUNCTION {CHECK}() FROM anon"),
            ("CREATE FUNCTION in maludb_platform",
             "CREATE FUNCTION maludb_platform.x() RETURNS void LANGUAGE sql AS 'SELECT'"),
        ]
        for role_login in ("executor", "client"):
            login = getattr(names, role_login)
            with psycopg.connect(dsn_for(names.database, user=login, password=passwords[role_login]),
                                 autocommit=True) as c:
                c.execute(f'SET ROLE "{names.admin}"')
                for label, stmt in attempts:
                    try:
                        c.execute(stmt)
                        outcome = "SUCCEEDED"
                    except psycopg.Error as exc:
                        outcome = f"{exc.diag.sqlstate} {str(exc).splitlines()[0][:60]}"
                    report(f"as {role_login} -> admin: {label}", outcome)
        report("check still refuses gen_salt afterwards", brief(api.call(
            "POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))

        # The self-inflicted bypass: a customer function that makes the name not
        # extension-only. Measured, because it decides whether the check is by
        # name or by resolved function.
        with psycopg.connect(dsn_for(names.database, user=names.executor, password=passwords["executor"]),
                             autocommit=True) as c:
            c.execute(f'SET ROLE "{names.admin}"')
            c.execute("CREATE FUNCTION public.gen_salt(n integer) RETURNS text LANGUAGE sql AS $$ SELECT 'mine' $$")
            c.execute("GRANT EXECUTE ON FUNCTION public.gen_salt(integer) TO anon")
        for match in ("every", "any"):
            with psycopg.connect(tdsn, autocommit=True) as t:
                t.execute(check_sql(match=match))
            reload()
            report(f"[{match}] customer adds gen_salt(integer); anon text 'bf'", brief(api.call(
                "POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))
            report(f"[{match}] customer adds gen_salt(integer); anon {{\"n\": 1}}", brief(api.call(
                "POST", "/rpc/gen_salt", body={"n": 1})))

        print("\nQ4b precedence: the file says the check, the database says none (set as superuser)")
        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute('DROP FUNCTION public.gen_salt(integer)')
            t.execute(f'ALTER ROLE "{names.authenticator}" IN DATABASE "{names.database}" '
                      "SET pgrst.db_pre_request = ''")
        reload()
        report("gen_salt as anon with an empty in-database setting", brief(api.call(
            "POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))
        with psycopg.connect(tdsn, autocommit=True) as t:
            t.execute(f'ALTER ROLE "{names.authenticator}" IN DATABASE "{names.database}" '
                      "RESET pgrst.db_pre_request")
        reload()
        report("after RESET", brief(api.call("POST", "/rpc/gen_salt", body="bf", content_type="text/plain")))
        return 0
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        if not args.keep:
            teardown()


def _latency(api: Api, n: int = 200) -> float:
    samples = []
    for _ in range(n):
        started = time.monotonic()
        api.call("POST", "/rpc/request_settings", body={})
        samples.append((time.monotonic() - started) * 1000)
    return statistics.median(samples)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="questions 1-4")
    p.add_argument("--port", type=int, default=3961)
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_run)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
