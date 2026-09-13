#!/usr/bin/env python3
"""ADR-077 compartments slice 0 -- measure MaluDB's vector compartments before building on them.

A SPIKE ARTEFACT, in the class of `scripts/spike-extension-pinning.py`. Nothing
imports it. It exists so the findings in `specs/vector-compartments-model.md` can
be reproduced, and so the plan's limits and its move/restore carry rest on
measurement rather than on reading function bodies.

It creates and drops its own tenants (`vcspik01`, `vcspik02`) and a role
`mldb_vcspik01_vectors`, so point it only at a disposable or test cluster:

    MALUDB_NODE_ADMIN_DSN=postgresql://cp_ci:cp_ci@127.0.0.1:5432/postgres \\
        scripts/spike-vector-compartments.py run

Every tenant is provisioned through the real provisioning module, so ADR-016's
roles and ADR-018/076's bootstrap are the platform's, not a hand-built copy.

Questions (numbered as in `plans/active/phase-12-vector-compartments.md`):

1. What does a non-superuser definer need to create, insert and search through
   platform wrappers called as `service_role`? Found by starting from no grants.
2. What `owner_schema` do compartments get under a definer with a pinned
   `search_path`?
3. What does exact search cost at realistic sizes and dimensions?
4. What do `ann_build` and an ANN search cost?
5. Does `pg_dump` of a real tenant carry compartments?
6. Can the rows be carried beside the dump so the target searches identically?
"""

# A spike against a disposable node. SQL interpolates module constants and
# integers only; object names come from a permission error on a node this script
# just built.
# ruff: noqa: S608, T201, S311, S101, E501

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.control_plane import provisioning, tenant_bootstrap  # noqa: E402

DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")
SOURCE, TARGET = "vcspik01", "vcspik02"
DEFINER = f"mldb_{SOURCE}_vectors"
# Sizes for question 3 as (vectors, dimensions). Kept modest by default: the
# development node has 3.8 GB, and 20k x 1536 is already ~120 MB of vectors.
SIZES = [tuple(int(x) for x in s.split("x"))
         for s in os.environ.get("VC_SPIKE_SIZES", "1000x384,10000x384,50000x384,1000x1536,20000x1536").split(",")]
ANN_SIZES = [tuple(int(x) for x in s.split("x"))
             for s in os.environ.get("VC_SPIKE_ANN_SIZES", "10000x384,50000x384").split(",")]
SEARCHES = 20
TABLES = ("malu$vector_subject", "malu$vector_verb", "malu$vector_compartment",
          "malu$vector_chunk", "malu$vector_tombstone", "malu$ann_index", "malu$ann_delta")
# Every grant discovery made, in order, so question 6 can replay exactly those.
GRANTS: list[str] = []


def report(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def admin(**kw) -> psycopg.Connection:
    if not DSN:
        sys.exit("MALUDB_NODE_ADMIN_DSN is unset: a superuser DSN for a DISPOSABLE node")
    return psycopg.connect(DSN, **kw)


def dsn_for(database: str) -> str:
    return psycopg.conninfo.make_conninfo(DSN, dbname=database)


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
        conn.commit()
    return names


def teardown() -> None:
    with admin(autocommit=True) as conn:
        for ref in (SOURCE, TARGET):
            db = provisioning.TenantNames.for_ref(ref).database
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for ref in (SOURCE, TARGET):
            for (role,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                                        (f"mldb\\_{ref}\\_%",)).fetchall():
                conn.execute(f'DROP ROLE IF EXISTS "{role}"')


# -- the probe wrappers -------------------------------------------------------

WRAPPERS = """
CREATE SCHEMA maludb;
REVOKE ALL ON SCHEMA maludb FROM PUBLIC;
GRANT USAGE ON SCHEMA maludb TO service_role;
CREATE FUNCTION maludb.vc_create(ns text, subj text, verb text, dim int) RETURNS bigint
 LANGUAGE sql SECURITY DEFINER SET search_path = maludb, maludb_core, public, pg_catalog
 AS $$ SELECT maludb_core.register_vector_compartment(ns, subj, verb, dim, 'customer', 'cosine') $$;
CREATE FUNCTION maludb.vc_insert(cid bigint, body text, emb vector) RETURNS bigint
 LANGUAGE sql SECURITY DEFINER SET search_path = maludb, maludb_core, public, pg_catalog
 AS $$ SELECT maludb_core.register_vector_chunk(cid, body, emb::text::maludb_core.malu_vector, 'customer') $$;
CREATE FUNCTION maludb.vc_search(ns text, subj text, verb text, q vector, k int)
 RETURNS TABLE(chunk_id bigint, source_text text, distance float8)
 LANGUAGE sql SECURITY DEFINER SET search_path = maludb, maludb_core, public, pg_catalog
 AS $$ SELECT r.chunk_id, r.source_text, r.distance
         FROM maludb_core.search_memory_exact(ns, subj, verb, q::text::maludb_core.malu_vector, k, NULL) r $$;
CREATE FUNCTION maludb.vc_ann_build(cid bigint) RETURNS bigint
 LANGUAGE sql SECURITY DEFINER SET search_path = maludb, maludb_core, public, pg_catalog
 AS $$ SELECT maludb_core.ann_build(cid, 16, 64, 32) $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA maludb FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA maludb TO service_role;
"""


def install_wrappers(t: psycopg.Connection) -> None:
    # Roles are cluster-wide, so the target of question 6 finds it already there.
    if not t.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (DEFINER,)).fetchone():
        t.execute(f'CREATE ROLE "{DEFINER}" NOLOGIN')
    t.execute(WRAPPERS)
    for (sig,) in t.execute("SELECT p.oid::regprocedure::text FROM pg_proc p "
                            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'maludb'").fetchall():
        t.execute(f'ALTER FUNCTION {sig} OWNER TO "{DEFINER}"')


def as_service_role(database: str, statement: str, params: tuple = ()) -> list:
    with psycopg.connect(dsn_for(database), autocommit=True) as c:
        c.execute("SET ROLE service_role")
        return c.execute(statement, params).fetchall()


DENIED = re.compile(r"permission denied for (schema|table|sequence|function|view) (\S+)")
NEXT_PRIVILEGE = {None: "SELECT", "SELECT": "INSERT", "INSERT": "UPDATE", "UPDATE": "DELETE"}


_REUSED: dict[str, psycopg.Connection] = {}


def discover_grants(database: str, statement: str, params: tuple, held: dict, *, reuse: bool = False) -> None:
    """Run as service_role; on each denial grant the definer the next privilege it names.

    `reuse` runs on one long-lived connection, so PL/pgSQL's cached plans -- which
    reach code a first call does not -- are exercised too.
    """
    for _ in range(40):
        try:
            if reuse:
                conn = _REUSED.get(database)
                if conn is None or conn.closed:
                    conn = _REUSED[database] = psycopg.connect(dsn_for(database), autocommit=True)
                    conn.execute("SET ROLE service_role")
                conn.execute(statement, params).fetchall()
            else:
                as_service_role(database, statement, params)
            return
        except psycopg.errors.InsufficientPrivilege as exc:
            match = DENIED.search(str(exc))
            if not match:
                raise
        kind, obj = match.groups()
        target = sql.Identifier("maludb_core", obj) if kind != "schema" else sql.Identifier(obj)
        if kind == "schema":
            grant = sql.SQL("GRANT USAGE ON SCHEMA {} TO {}")
            privilege = "USAGE"
        elif kind == "sequence":
            grant, privilege = sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}"), "USAGE"
        elif kind == "function":
            grant, privilege = sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}"), "EXECUTE"
        else:
            privilege = NEXT_PRIVILEGE[held.get(obj, [None])[-1]]
            grant = sql.SQL("GRANT " + privilege + " ON {} TO {}")
        with psycopg.connect(dsn_for(database), autocommit=True) as t:
            statement_sql = grant.format(target, sql.Identifier(DEFINER)).as_string(t)
            t.execute(statement_sql)
        GRANTS.append(statement_sql)
        held.setdefault(obj, []).append(privilege)
    raise SystemExit(f"grant discovery did not converge for {statement}")


# -- loading -----------------------------------------------------------------


def load_compartment(t: psycopg.Connection, ns: str, count: int, dims: int) -> int:
    """A compartment of `count` random unit vectors, loaded directly as superuser.

    Direct rather than through `vc_insert` so the load is minutes not hours; the
    wrapper path is what question 1 measures, and question 3 measures search.
    """
    # Upstream calls its own functions unqualified, so it only works with
    # maludb_core on the path -- which is what the wrappers pin.
    t.execute("SET search_path = maludb_core, public")
    cid = t.execute("SELECT maludb_core.register_vector_compartment(%s, 'doc', 'about', %s, 'spike', 'cosine')",
                    (ns, dims)).fetchone()[0]
    t.execute(
        f"""
        INSERT INTO maludb_core."malu$vector_chunk"
            (compartment_id, source_text, embedding, embedding_dim, embedding_model, embedding_norm)
        SELECT %s, 'chunk ' || g,
               maludb_core.vector_normalize(('[' || array_to_string(
                   ARRAY(SELECT (random() - 0.5)::real FROM generate_series(1, {dims}) WHERE g > 0), ',')
                   || ']')::maludb_core.malu_vector),
               {dims}, 'spike', 1.0
          FROM generate_series(1, %s) g
        """, (cid, count))
    t.execute('UPDATE maludb_core."malu$vector_compartment" SET vector_count = %s WHERE compartment_id = %s',
              (count, cid))
    return cid


def query_vector(dims: int) -> str:
    import random
    return "[" + ",".join(f"{random.uniform(-0.5, 0.5):.5f}" for _ in range(dims)) + "]"


def timed_searches(database: str, ns: str, dims: int, held: dict) -> tuple[float, float]:
    """Median and p95 of one search, on one held connection so connecting is not timed."""
    statement = "SELECT * FROM maludb.vc_search(%s, 'doc', 'about', %s::vector, 10)"
    # A search path not taken before (an ANN index, say) may reach functions the
    # definer has not yet been granted.
    discover_grants(database, statement, (ns, query_vector(dims)), held)
    # A first call passing is not enough: PL/pgSQL checks a function's privilege
    # when a plan reaches it, and a later (generic) plan reaches CASE branches the
    # first did not. So run the whole timed loop once untimed, discovering as it
    # goes, and time a second loop that must not need anything new.
    for _ in range(SEARCHES):
        discover_grants(database, statement, (ns, query_vector(dims)), held, reuse=True)
    samples = []
    with psycopg.connect(dsn_for(database), autocommit=True) as c:
        c.execute("SET ROLE service_role")
        for _ in range(SEARCHES):
            q = query_vector(dims)
            started = time.perf_counter()
            rows = c.execute(statement, (ns, q)).fetchall()
            samples.append(time.perf_counter() - started)
            assert len(rows) == 10, rows
    samples.sort()
    return samples[len(samples) // 2], samples[int(len(samples) * 0.95) - 1]


# -- the run -----------------------------------------------------------------


def run(_args: argparse.Namespace) -> None:
    teardown()
    source = provision(SOURCE)
    print(f"provisioned {source.database}")
    with psycopg.connect(dsn_for(source.database), autocommit=True) as t:
        report("maludb_core", t.execute("SELECT extversion FROM pg_extension WHERE extname='maludb_core'").fetchone()[0])
        report("vector", t.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0])
        install_wrappers(t)

    print("\n1. Grants a non-superuser definer needs")
    held: dict[str, list[str]] = {}
    discover_grants(source.database, "SELECT maludb.vc_create('probe', 'doc', 'about', 3)", (), held)
    probe = as_service_role(source.database, "SELECT maludb.vc_create('probe', 'doc', 'about', 3)")[0][0]
    cid = probe
    discover_grants(source.database, "SELECT maludb.vc_insert(%s, 'hello', '[1,2,3]')", (cid,), held)
    as_service_role(source.database, "SELECT maludb.vc_insert(%s, 'world', '[3,2,1]')", (cid,))
    discover_grants(source.database, "SELECT * FROM maludb.vc_search('probe', 'doc', 'about', '[1,2,2]', 5)", (), held)
    for obj, privileges in sorted(held.items()):
        report(obj, ", ".join(privileges))
    found = as_service_role(source.database, "SELECT source_text FROM maludb.vc_search('probe', 'doc', 'about', '[1,2,2]', 5)")
    report("search through the wrappers as service_role", [r[0] for r in found])
    for role in ("anon", "authenticated"):
        try:
            with psycopg.connect(dsn_for(source.database), autocommit=True) as c:
                c.execute(f"SET ROLE {role}")
                c.execute("SELECT * FROM maludb.vc_search('probe', 'doc', 'about', '[1,2,2]', 5)")
            report(f"{role} calling a wrapper", "ALLOWED")
        except psycopg.errors.InsufficientPrivilege:
            report(f"{role} calling a wrapper", "refused")

    print("\n2. owner_schema")
    with psycopg.connect(dsn_for(source.database), autocommit=True) as t:
        report("compartments by owner_schema",
               t.execute('SELECT owner_schema, count(*) FROM maludb_core."malu$vector_compartment" GROUP BY 1').fetchall())

    print("\n3. Exact search, through the wrapper as service_role (median / p95 of 20)")
    with psycopg.connect(dsn_for(source.database), autocommit=True) as t:
        for count, dims in SIZES:
            ns = f"exact_{count}_{dims}"
            started = time.perf_counter()
            load_compartment(t, ns, count, dims)
            load_s = time.perf_counter() - started
            median, p95 = timed_searches(source.database, ns, dims, held)
            report(f"{count:>6} x {dims:<5}", f"median {median * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms "
                   f"(load {load_s:.0f} s)")
        size = t.execute("SELECT pg_total_relation_size('maludb_core.\"malu$vector_chunk\"')").fetchone()[0]
        vectors = t.execute('SELECT count(*), sum(embedding_dim) FROM maludb_core."malu$vector_chunk"').fetchone()
        report("chunk table size", f"{size / 1e6:.0f} MB for {vectors[0]} vectors "
               f"({size / vectors[1]:.1f} bytes per stored dimension)")

    print("\n4. ANN build and search")
    for count, dims in ANN_SIZES:
        ns = f"exact_{count}_{dims}"
        with psycopg.connect(dsn_for(source.database), autocommit=True) as t:
            cid = t.execute('SELECT compartment_id FROM maludb_core."malu$vector_compartment" WHERE namespace = %s',
                            (ns,)).fetchone()[0]
        try:
            discover_grants(source.database, "SELECT maludb.vc_ann_build(%s)", (cid,), held)
        except psycopg.Error as exc:
            report(f"ann_build {count} x {dims}", f"failed: {str(exc).splitlines()[0]}")
            continue
        # Timed on a second build: discovery repeats the build after each denial,
        # so the first one's wall time is not the build's. Peak memory is the
        # backend's own high-water mark, read from /proc once it has finished.
        with psycopg.connect(dsn_for(source.database), autocommit=True) as c:
            c.execute("SET ROLE service_role")
            pid = c.execute("SELECT pg_backend_pid()").fetchone()[0]
            started = time.perf_counter()
            c.execute("SELECT maludb.vc_ann_build(%s)", (cid,))
            build_s = time.perf_counter() - started
            status = Path(f"/proc/{pid}/status").read_text()
            peak = int(re.search(r"VmHWM:\s+(\d+) kB", status).group(1)) / 1024
        with psycopg.connect(dsn_for(source.database), autocommit=True) as t:
            graph = t.execute('SELECT octet_length(graph_bytes) FROM maludb_core."malu$ann_index" '
                              'WHERE compartment_id = %s', (cid,)).fetchone()[0]
        median, p95 = timed_searches(source.database, ns, dims, held)
        report(f"ann_build {count} x {dims}", f"{build_s:.1f} s, backend peak {peak:.0f} MB, graph {graph / 1e6:.1f} MB; "
               f"search after build median {median * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms")
    report("definer grants after ANN", {k: v for k, v in sorted(held.items())})

    print("\n5. pg_dump of the tenant")
    dump = subprocess.run(["pg_dump", "--no-owner", dsn_for(source.database)],  # noqa: S603, S607
                          capture_output=True, text=True, check=True).stdout
    for table in TABLES:
        report(f"COPY for {table}", "present" if f'"{table}"' in dump and f'COPY maludb_core."{table}"' in dump
               else "absent")
    report("dump mentions a chunk's text", "yes" if "chunk 1\t" in dump or "hello" in dump else "no")

    print("\n6. Carrying the rows beside the dump")
    target = provision(TARGET)
    with psycopg.connect(dsn_for(source.database)) as s, psycopg.connect(dsn_for(target.database)) as d:
        started = time.perf_counter()
        for table in TABLES:
            name = sql.Identifier("maludb_core", table)
            with s.cursor().copy(sql.SQL("COPY {} TO STDOUT (FORMAT binary)").format(name)) as out, \
                    d.cursor().copy(sql.SQL("COPY {} FROM STDIN (FORMAT binary)").format(name)) as into:
                for block in out:
                    into.write(block)
        for (seq, table, column) in s.execute(
                "SELECT s.relname, t.relname, a.attname FROM pg_class s JOIN pg_depend dep ON dep.objid = s.oid "
                "JOIN pg_class t ON t.oid = dep.refobjid JOIN pg_attribute a ON a.attrelid = t.oid "
                "AND a.attnum = dep.refobjsubid JOIN pg_namespace n ON n.oid = s.relnamespace "
                "WHERE s.relkind = 'S' AND n.nspname = 'maludb_core' AND t.relname = ANY(%s)", (list(TABLES),)).fetchall():
            d.execute(sql.SQL("SELECT setval({}, COALESCE((SELECT max({}) FROM {}), 1))").format(
                sql.Literal(f'maludb_core."{seq}"'), sql.Identifier(column), sql.Identifier("maludb_core", table)))
        d.commit()
        report("copy of every table", f"{time.perf_counter() - started:.1f} s")
        for table in TABLES:
            counts = [c.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("maludb_core", table))).fetchone()[0]
                      for c in (s, d)]
            report(f"{table} rows source / target", counts)
    with psycopg.connect(dsn_for(target.database), autocommit=True) as t:
        install_wrappers(t)
        for statement_sql in GRANTS:
            t.execute(statement_sql)
    q = "[1,2,2]"
    before = as_service_role(source.database, "SELECT chunk_id, source_text FROM maludb.vc_search('probe','doc','about',%s::vector,5)", (q,))
    after = as_service_role(target.database, "SELECT chunk_id, source_text FROM maludb.vc_search('probe','doc','about',%s::vector,5)", (q,))
    report("search on the target matches the source", before == after)
    new = as_service_role(target.database, "SELECT maludb.vc_insert(%s, 'after the carry', '[2,2,2]')", (probe,))
    report("an insert on the target after the carry gets a fresh id", new)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").set_defaults(func=run)
    sub.add_parser("teardown").set_defaults(func=lambda _a: teardown())
    args = parser.parse_args()
    try:
        args.func(args)
    finally:
        for conn in _REUSED.values():
            conn.close()
        if args.command == "run" and not os.environ.get("VC_SPIKE_KEEP"):
            teardown()


if __name__ == "__main__":
    main()
