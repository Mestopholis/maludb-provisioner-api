#!/usr/bin/env python3
"""Memory pipeline slice 0 -- measure what exposing maludb_core's memory pipeline takes.

A SPIKE ARTEFACT, in the same class as `scripts/spike-datamodel.py`. Nothing
imports it. It exists so the findings in `specs/maludb-memory-pipeline-model.md`
can be reproduced against real bootstrapped tenants, and so the draft ADR-079's
open delivery question rests on measurement.

    set -a; . ./.dev/test.env; set +a
    scripts/spike-memory-pipeline.py run            # questions 1-5
    scripts/spike-memory-pipeline.py run --keep     # leave the tenant behind

Questions (numbered as in the spec):

1. Can a narrow per-project definer (ADR-077's pattern) answer
   `maludb_memory_search` with the facade's results, called through PostgREST?
2. What does ingest by the platform cost, what does it write, and is a memory
   searchable the moment its ingest commits?
3. What does each memory space cost, do spaces isolate, and what do the
   `current_schema()` and definer-view hazards actually do?
4. Which functions does a space get, which run the session-user guard, which run
   as their definer, and who owns them?
5. Can a platform worker drain `malu$model_request` over the node connection --
   no BYPASSRLS, no maludb_llm_admin -- end to end into harvested, searchable
   memory?

Point it only at a disposable node: it grants a probe role privileges on
maludb_core tables on purpose, and starts PostgREST on a loopback port.
"""


# A spike against a disposable node, following spike-datamodel.py's convention.
# SQL identifiers come from module constants (validated below) or catalogue
# rows; the one URL it opens is localhost; the one process it starts is PostgREST.
# ruff: noqa: S603, S607, S608, S310, S311

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
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
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.control_plane import maludb_vectors, provisioning, tenant_bootstrap, workers  # noqa: E402

DSN = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
OWNER = os.environ.get("MALUDB_PLATFORM_OWNER", "postgres")
POSTGREST = os.environ.get("MALUDB_POSTGREST_BIN", "postgrest")

REF = "mps00001"
SPACE_A, SPACE_B = "space_a", "space_b"
EXTRA_SPACES = ("space_c", "space_d", "space_e")
READER = f"mldb_{REF}_memread"       # the candidate per-project read definer
PROBE = f"mldb_{REF}_probe"          # a role used only to demonstrate hazards
WORKER = f"mldb_{REF}_modelw"        # a narrow model-worker role, for comparison
DIM = 384
NAMESPACE = "default"
SUBJECTS = [f"subject-{i:02d}" for i in range(20)]
VERBS = ["decided", "owns", "prefers", "blocked_by", "shipped"]
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
for _name in (SPACE_A, SPACE_B, *EXTRA_SPACES, READER, PROBE, WORKER):
    if not _IDENT.match(_name):
        raise SystemExit(f"not a plain identifier: {_name}")

GUARD = "_memory_schema_assert_manageable"

# Memory slice 1: the per-project memory-writer login (ADR-079 decision 6). Two
# tenants, because "reach into another tenant database" cannot be measured on
# one. The writer follows the platform's role-naming convention.
WREF, WREF2 = "mws00001", "mws00002"
WRITER = "mldb_{ref}_memwriter"
PIPELINE_FACADES = (
    "maludb_upload_document",
    "maludb_memory_ingest_edge",
    "maludb_memory_request_extraction",
    "maludb_memory_harvest_extractions",
    "maludb_memory_set_model_config",
    "maludb_register_model_provider",
    "maludb_register_model_alias",
    "maludb_memory_search",
)
for _name in (WREF, WREF2):
    if not re.match(r"^[a-z0-9]+$", _name):
        raise SystemExit(f"not a plain ref: {_name}")


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


def one(conn, query, params=()):
    row = conn.execute(query, params).fetchone()
    return None if row is None else row[0]


def report(label: str, value) -> None:
    print(f"  {label:<60} {value}")


def _grant_count(conn, role: str) -> int:
    return one(conn, "SELECT count(*) FROM information_schema.role_routine_grants WHERE grantee = %s", (role,))


def first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0]


def pct(values: list[float]) -> str:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return f"median {statistics.median(ordered) * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms (n={len(ordered)})"


def embedding(seed: str) -> list[float]:
    rng = random.Random(hashlib.sha256(seed.encode()).digest())
    return [round(rng.uniform(-1, 1), 6) for _ in range(DIM)]


def vec(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


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
            provisioning.install_extension(
                t, pins=dict(t.execute("SELECT name, default_version FROM pg_available_extensions "
                                        "WHERE name IN ('vector', 'maludb_core')").fetchall()))
            tenant_bootstrap.apply(t)
        provisioning.verify_isolation(conn, names)
        conn.commit()
    return {"names": names, "passwords": passwords}


def teardown(ref: str) -> None:
    with admin(autocommit=True) as conn:
        db = provisioning.TenantNames.for_ref(ref).database
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db)))
        rows = conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
                            (f"mldb\\_{ref}\\_%",)).fetchall()
        for (role,) in rows:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def enable_space(t, space: str) -> dict:
    size_before = one(t, "SELECT pg_database_size(current_database())")
    started = time.monotonic()
    t.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(space)))
    row = t.execute("SELECT enabled_version, object_count FROM maludb_core.enable_memory_schema(%s)",
                    (space,)).fetchone()
    elapsed = time.monotonic() - started
    t.execute("CHECKPOINT")
    return {
        "space": space, "version": row[0], "objects": row[1], "seconds": elapsed,
        "bytes": one(t, "SELECT pg_database_size(current_database())") - size_before,
        "relations": one(t, "SELECT count(*) FROM pg_class WHERE relnamespace = %s::regnamespace", (space,)),
        "functions": one(t, "SELECT count(*) FROM pg_proc WHERE pronamespace = %s::regnamespace", (space,)),
    }


# --------------------------------------------------------------------------
# privilege discovery: grant exactly what each denial names


_DENIED = re.compile(r"permission denied for (table|view|function|schema|sequence) (\S+)")


_ESCALATE = ("SELECT", "UPDATE", "INSERT", "DELETE")


def discover(grantor, role: str, attempt, *, limit: int = 60):
    """Call `attempt` until it stops being refused, granting `role` what each refusal names.

    Returns (denials, result). A relation denial names the relation, not the
    operation, so a relation refused again is escalated SELECT -> UPDATE -> INSERT
    -> DELETE and the level reached is reported -- it can be one higher than
    strictly needed, as the vectors spike found. A function denial names no
    signature, so every overload outside pg_catalog is granted, and whether any
    runs as its definer is recorded: that is the fact the spec needs.
    """
    denials: list[str] = []
    levels: dict[str, int] = {}
    for _ in range(limit):
        try:
            return denials, attempt()
        except psycopg.errors.InsufficientPrivilege as exc:
            message = first_line(exc)
            match = _DENIED.search(message)
            if not match:
                exc.denials = denials
                raise
            kind, name = match.group(1), match.group(2)
            ident = sql.Identifier(role)
            if kind == "schema":
                grantor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(name), ident))
            elif kind in ("table", "view"):
                level = levels.get(name, -1) + 1
                if level >= len(_ESCALATE):
                    exc.denials = denials
                    raise
                levels[name] = level
                grantor.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(_ESCALATE[level]), sql.SQL(_qualified_relation(grantor, name)), ident))
                message += f"  [granted {_ESCALATE[level]}]"
            elif kind == "sequence":
                grantor.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(
                    sql.SQL(_qualified_relation(grantor, name)), ident))
            else:
                rows = grantor.execute(
                    "SELECT p.oid::regprocedure::text, p.prosecdef, pg_get_userbyid(p.proowner) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE p.proname = %s AND n.nspname <> 'pg_catalog'", (name,)).fetchall()
                for signature, secdef, owner in rows:
                    grantor.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), ident))
                    if secdef:
                        message += f"  [granted {signature}: SECURITY DEFINER owned by {owner}]"
            denials.append(message)
    raise RuntimeError(f"still refused after {limit} grants: {denials[-3:]}")


def _qualified_relation(conn, name: str) -> str:
    rows = conn.execute(
        "SELECT c.oid::regclass::text, n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = %s AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY n.nspname = 'maludb_core' DESC", (name,)).fetchall()
    if not rows:
        raise RuntimeError(f"no relation named {name}")
    return rows[0][0] if "." in rows[0][0] else f"{rows[0][1]}.{rows[0][0]}"


# --------------------------------------------------------------------------
# counting what a write touches


def table_counts(t) -> dict[str, int]:
    tables = [r[0] for r in t.execute(
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'maludb_core'::regnamespace "
        "AND c.relkind IN ('r', 'p') ORDER BY 1").fetchall()]
    union = sql.SQL(" UNION ALL ").join(
        sql.SQL("SELECT {name}, count(*) FROM {table}").format(
            name=sql.Literal(name), table=sql.Identifier("maludb_core", name))
        for name in tables)
    return dict(t.execute(union).fetchall())


def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in sorted(after) if after[k] != before.get(k, 0)}


# --------------------------------------------------------------------------
# the facades, called as the platform


def ingest_edge(t, space: str, *, doc: int, subject: str, verb: str, span: str, emb: list[float]) -> int:
    return one(t, sql.SQL(
        "SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', p_source_id => %s, "
        "p_subject_text => %s, p_verb_text => %s, "
        "p_predicate => %s::jsonb, p_embedding => %s::maludb_core.malu_vector, "
        "p_embedding_model => 'stub-384', p_source_span => %s, p_confidence => 0.8, "
        "p_namespace => %s, p_document_id => %s)").format(sql.Identifier(space)),
        (doc, subject, verb, json.dumps([{"attr_name": "status", "value_text": "open"}]),
         vec(emb), span, NAMESPACE, doc))


def upload(t, space: str, title: str, text: str) -> int:
    return one(t, sql.SQL(
        "SELECT {}.maludb_upload_document(p_title => %s, p_content_text => %s, p_source_type => 'note')"
    ).format(sql.Identifier(space)), (title, text))


def facade_search(t, space: str, emb: list[float], *, subject=None, verb=None, limit=10) -> list[tuple]:
    return t.execute(sql.SQL(
        "SELECT chunk_id, statement_id, document_id, source_text, distance, rank_no, subject_name, verb_name "
        "FROM {}.maludb_memory_search(%s::maludb_core.malu_vector, %s, %s, %s, %s)").format(sql.Identifier(space)),
        (vec(emb), subject, verb, NAMESPACE, limit)).fetchall()


def extraction_payload(i: int) -> dict:
    who, other = SUBJECTS[i % len(SUBJECTS)], SUBJECTS[(i + 7) % len(SUBJECTS)]
    return {
        "document": {"title": f"meeting note {i}", "content_text": f"{who} decided to ship the importer; "
                     f"{other} owns the rollout. Standup {i} happened on 2026-09-{(i % 28) + 1:02d}.",
                     "source_type": "note"},
        "subjects": [
            {"key": "a", "name": who, "type": "person", "aliases": [who.upper()]},
            {"key": "b", "name": other, "type": "person"},
            {"key": "p", "name": "importer", "type": "software"},
            {"key": "e", "name": f"standup {i}", "type": "meeting",
             "occurred_at": f"2026-09-{(i % 28) + 1:02d}T09:00:00Z"},
        ],
        "verbs": [{"name": "decided"}, {"name": "owns"}],
        "edges": [
            {"subject": "a", "verb": "decided", "object": "p", "confidence": 0.9,
             "source_span": f"{who} decided to ship the importer",
             "attributes": [{"attr_name": "status", "value_text": "decided"}]},
            {"subject": "b", "verb": "owns", "object": "p", "source_span": f"{other} owns the rollout"},
            {"subject": "a", "verb": "attended", "object": "e"},
            {"subject": "$source", "verb": "mentions", "object": "a"},
        ],
        "relationships": [{"from": "a", "to": "b", "relationship_type": "works_with"}],
    }


# --------------------------------------------------------------------------
# question 4


def inventory(t, space: str) -> None:
    catalogue = t.execute(
        "SELECT p.oid::regprocedure::text, n.nspname, p.proname, p.prosecdef, pg_get_userbyid(p.proowner), "
        "       CASE WHEN l.lanname IN ('sql', 'plpgsql') THEN pg_get_functiondef(p.oid) ELSE '' END, "
        "       coalesce(p.proconfig::text, '') "
        "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang "
        " WHERE n.nspname IN ('maludb_core', %s) AND p.prokind = 'f'", (space,)).fetchall()
    core = {r[2]: r for r in catalogue if r[1] == "maludb_core"}
    pattern = re.compile(r"(?<![\w$])(?:maludb_core\.)?(" + "|".join(
        sorted(map(re.escape, core), key=len, reverse=True)) + r")\s*\(")

    def body(row) -> str:
        return row[5].split("$function$", 1)[-1] if row[5] else ""

    calls = {name: set(pattern.findall(body(row))) - {name} for name, row in core.items()}
    reaches_guard: dict[str, bool] = {name: GUARD in c for name, c in calls.items()}
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if not reaches_guard[name] and any(reaches_guard.get(c) for c in callees):
                reaches_guard[name], changed = True, True
    reaches_definer: dict[str, bool] = {name: any(core[c][3] for c in cs) for name, cs in calls.items()}
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if not reaches_definer[name] and any(reaches_definer.get(c) for c in callees):
                reaches_definer[name], changed = True, True

    rows = []
    for row in sorted(r for r in catalogue if r[1] == space):
        direct = sorted(set(pattern.findall(body(row))))
        rows.append({
            "function": row[2], "definer": row[3], "owner": row[4],
            "pinned": f"search_path={space}" in row[6],
            "calls": direct,
            "guard": any(c == GUARD or reaches_guard.get(c) for c in direct),
            "via_definer": any(core[c][3] or reaches_definer.get(c) for c in direct),
            "current_schema": "current_schema()" in body(row)
                              or any("current_schema()" in body(core[c]) for c in direct),
        })
    print(f"\nQ4  guard inventory of {space} ({len(rows)} functions)")
    report("SECURITY DEFINER / invoker", f"{sum(r['definer'] for r in rows)} / {sum(not r['definer'] for r in rows)}")
    report("owners", sorted({r["owner"] for r in rows}))
    report("reach the session_user guard", sum(r["guard"] for r in rows))
    report("call no maludb_core function", sum(not r["calls"] for r in rows))
    report("invoker, not guarded, read current_schema() (direct)", sum(
        (not r["definer"]) and (not r["guard"]) and r["current_schema"] for r in rows))
    report("pin search_path to the space", sum(r["pinned"] for r in rows))
    print("    cs() = the body or a direct callee mentions current_schema()")
    print(f"    {'function':<44} {'definer':<8} {'guard':<6} {'cs()':<5} calls")
    for r in rows:
        print(f"    {r['function']:<44} {str(r['definer']):<8} {str(r['guard']):<6} "
              f"{str(r['current_schema']):<5} {','.join(r['calls'])[:90]}")
    pipeline = ("_memory_search_for_schema", "_memory_ingest_extraction_for_schema",
                "_memory_ingest_edge_for_schema", "_memory_request_extraction_for_schema",
                "_memory_harvest_extractions_for_schema", "_upload_document_for_schema",
                "_memory_set_model_config_for_schema", "_vector_compartment_for_svpor",
                "exact_vector_search_sql", "register_vector_chunk", "register_episode", "episode_get",
                GUARD)
    print("    maludb_core callees on the pipeline path:")
    for name in pipeline:
        if name in core:
            r = core[name]
            print(f"    {name:<44} definer={r[3]!s:<5} owner={r[4]:<8} "
                  f"guard_direct={GUARD in calls[name]!s:<5} guard_reachable={reaches_guard[name]}")
    views = t.execute(
        "SELECT count(*) FILTER (WHERE coalesce(reloptions::text, '') LIKE '%%security_invoker=true%%'), "
        "       count(*) FILTER (WHERE coalesce(reloptions::text, '') NOT LIKE '%%security_invoker=true%%'), "
        "       string_agg(relname, ', ' ORDER BY relname) FILTER "
        "         (WHERE coalesce(reloptions::text, '') NOT LIKE '%%security_invoker=true%%') "
        "FROM pg_class WHERE relnamespace = %s::regnamespace AND relkind = 'v'", (space,)).fetchone()
    report("views security_invoker / owner-rights", f"{views[0]} / {views[1]}")
    report("owner-rights views", views[2])
    report("EXECUTE on the search facade held by", one(
        t, "SELECT string_agg(DISTINCT pg_get_userbyid(a.grantee), ', ') FROM pg_proc p "
           "CROSS JOIN LATERAL aclexplode(p.proacl) a "
           "WHERE p.pronamespace = %s::regnamespace AND p.proname = 'maludb_memory_search'", (space,)))


# --------------------------------------------------------------------------
# questions 2, 3 and 5 write; 1 reads


def seed_space(t, space: str, *, marker: str, edges: int) -> dict:
    """Ingest `edges` embedded edges into `space`, timing each call."""
    doc = upload(t, space, f"{marker} doc", f"{marker} source document")
    times = []
    for i in range(edges):
        subject, verb = SUBJECTS[i % len(SUBJECTS)], VERBS[i % len(VERBS)]
        emb = embedding(f"{marker}:{i}")
        started = time.monotonic()
        ingest_edge(t, space, doc=doc, subject=subject, verb=verb,
                    span=f"{marker} memory {i}: {subject} {verb}", emb=emb)
        times.append(time.monotonic() - started)
    return {"doc": doc, "times": times}


def cmd_run(args) -> int:
    names = provisioning.TenantNames.for_ref(REF)
    teardown(REF)
    proc = None
    workdir = Path(tempfile.mkdtemp(prefix="mps-spike-"))
    try:
        tenant = provision(REF)
        database = names.database
        with psycopg.connect(dsn_for(database), autocommit=True) as t:
            print(f"tenant {database}, maludb_core "
                  f"{one(t, 'SELECT extversion FROM pg_extension WHERE extname = %s', ('maludb_core',))}, "
                  f"{one(t, 'SHOW server_version')}")

            # ---------------------------------------------------------- Q3 cost
            print("\nQ3a  cost of each memory space")
            base = one(t, "SELECT pg_database_size(current_database())")
            report("database size before any space (MB)", f"{base / 1e6:.1f}")
            for space in (SPACE_A, SPACE_B, *EXTRA_SPACES):
                c = enable_space(t, space)
                report(f"enable {space}", f"{c['seconds']:.2f} s, {c['bytes'] / 1e6:.2f} MB, "
                       f"{c['objects']} objects ({c['relations']} relations, {c['functions']} functions)")
            report("maludb_core tables with rows after 5 empty spaces",
                   {k: v for k, v in table_counts(t).items() if v})

            inventory(t, SPACE_A)

            # ---------------------------------------------------------- Q2 ingest
            print("\nQ2  ingest by the platform (superuser session, guard passes)")
            before = table_counts(t)
            times = []
            for i in range(args.docs):
                started = time.monotonic()
                upload(t, SPACE_A, f"doc {i}", f"document body {i}")
                times.append(time.monotonic() - started)
            report("maludb_upload_document", pct(times))
            report("  rows per call", {k: v / args.docs for k, v in delta(before, table_counts(t)).items()})

            before = table_counts(t)
            seeded = seed_space(t, SPACE_A, marker="A", edges=args.edges)
            report(f"maludb_memory_ingest_edge, {DIM}-dim embedding", pct(seeded["times"]))
            report("  rows written (whole batch, 1 upload included)", delta(before, table_counts(t)))

            before = table_counts(t)
            times, skipped = [], {}
            for i in range(args.extractions):
                started = time.monotonic()
                result = one(t, sql.SQL("SELECT {}.maludb_memory_ingest_extraction(%s::jsonb)").format(
                    sql.Identifier(SPACE_A)), (json.dumps(extraction_payload(i)),))
                times.append(time.monotonic() - started)
                for item in result["skipped"]:
                    key = f"{item['section']}: {item['reason'][:90]}"
                    skipped[key] = skipped.get(key, 0) + 1
            report("maludb_memory_ingest_extraction (4 subjects, 4 edges, 1 rel)", pct(times))
            report("  skipped items across all payloads", skipped or "none")
            report("  last result", json.dumps({k: result[k] for k in ("created", "resolved")}))
            report("  rows written (whole batch)", delta(before, table_counts(t)))
            report("  extraction subjects reachable by memory_search",
                   len(facade_search(t, SPACE_A, embedding("x"), subject="importer")))

            print("\nQ2b read-after-write")
            with psycopg.connect(dsn_for(database), autocommit=True) as other:
                visible, first_rank = 0, 0
                dirty_before = one(t, 'SELECT count(*) FROM maludb_core."malu$embedding_dirty"')
                for i in range(args.raw):
                    emb = embedding(f"raw:{i}")
                    ingest_edge(t, SPACE_A, doc=seeded["doc"], subject="raw-probe", verb="noted",
                                span=f"raw probe {i}", emb=emb)
                    hits = facade_search(other, SPACE_A, emb, subject="raw-probe", limit=1)
                    visible += bool(hits and hits[0][3] == f"raw probe {i}")
                    first_rank += bool(hits and hits[0][5] == 1)
                report("searched from another session right after commit",
                       f"{visible}/{args.raw} found, {first_rank}/{args.raw} at rank 1")
                report("malu$embedding_dirty rows added by those ingests",
                       one(t, 'SELECT count(*) FROM maludb_core."malu$embedding_dirty"') - dirty_before)
                t.execute("BEGIN")
                emb = embedding("uncommitted")
                ingest_edge(t, SPACE_A, doc=seeded["doc"], subject="raw-probe", verb="noted",
                            span="uncommitted", emb=emb)
                report("same transaction sees its own ingest",
                       bool(facade_search(t, SPACE_A, emb, subject="raw-probe", limit=1)[0][3] == "uncommitted"))
                report("another session sees it before commit",
                       any(h[3] == "uncommitted" for h in facade_search(other, SPACE_A, emb, subject="raw-probe")))
                t.execute("ROLLBACK")

            # ---------------------------------------------------------- Q3 isolation data
            seed_space(t, SPACE_B, marker="B", edges=args.edges // 4)

            # ---------------------------------------------------------- Q1 wrapper
            print("\nQ1  search without superuser code")
            _build_reader(t, names)
            try:
                with psycopg.connect(dsn_for(database, user=names.authenticator,
                                             password=tenant["passwords"]["authenticator"]),
                                     autocommit=True) as pg:
                    pg.execute("SET ROLE service_role")

                    def call(i=1, subject="subject-01", verb=None, space=SPACE_A):
                        return pg.execute(
                            "SELECT chunk_id, statement_id, document_id, source_text, distance, rank_no, "
                            "subject_name, verb_name FROM maludb.memory_search(%s, %s::vector, %s, %s, %s, 10)",
                            (space, vec(embedding(f"A:{i}")), subject, verb, NAMESPACE)).fetchall()

                    denials, _ = discover(t, READER, call)
                    report("denials before the reader could search", len(denials))
                    for d in denials:
                        print(f"      {d}")
                    # past the generic-plan switch, varying the branch taken
                    late = []
                    for i in range(15):
                        verb = VERBS[i % 5] if i % 2 else None
                        more, _ = discover(t, READER, lambda i=i, verb=verb: call(i, SUBJECTS[i % 20], verb))
                        late += more
                    report("further denials over 15 calls on one connection", late or "none")
                    _report_reader_grants(t)

                    mismatches, compared = 0, 0
                    for i in range(args.compare):
                        subject = SUBJECTS[i % len(SUBJECTS)]
                        verb = VERBS[i % len(VERBS)] if i % 3 == 0 else None
                        want = facade_search(t, SPACE_A, embedding(f"A:{i}"), subject=subject, verb=verb)
                        got = call(i, subject, verb)
                        compared += len(want)
                        mismatches += _normalise(want) != _normalise(got)
                    report(f"wrapper vs facade on {args.compare} queries",
                           f"{mismatches} differing result sets, {compared} rows compared")
                    times_facade, times_wrapper = [], []
                    for i in range(20):
                        started = time.monotonic()
                        facade_search(t, SPACE_A, embedding(f"A:{i}"), subject=SUBJECTS[i % 20])
                        times_facade.append(time.monotonic() - started)
                        started = time.monotonic()
                        call(i, SUBJECTS[i % 20])
                        times_wrapper.append(time.monotonic() - started)
                    report("facade as platform", pct(times_facade))
                    report("wrapper as service_role (authenticator session)", pct(times_wrapper))

                    _vectors_beside_spaces(database, names, pg)

                    print("\nQ3b isolation between spaces")
                    probe = embedding("B:1")
                    b_subject = SUBJECTS[1]
                    a_rows = facade_search(t, SPACE_A, probe, subject=b_subject, limit=50)
                    b_rows = facade_search(t, SPACE_B, probe, subject=b_subject, limit=50)
                    report("(a) facade: space_b markers in a search of space_a",
                           sum(r[3].startswith("B ") for r in a_rows))
                    report("(a) facade: space_b rows in a search of space_b",
                           sum(r[3].startswith("B ") for r in b_rows))
                    wa = pg.execute("SELECT source_text FROM maludb.memory_search(%s, %s::vector, %s, NULL, %s, 50)",
                                    (SPACE_A, vec(probe), b_subject, NAMESPACE)).fetchall()
                    wb = pg.execute("SELECT source_text FROM maludb.memory_search(%s, %s::vector, %s, NULL, %s, 50)",
                                    (SPACE_B, vec(probe), b_subject, NAMESPACE)).fetchall()
                    report("(b) wrapper: space_b markers in a search of space_a",
                           sum(r[0].startswith("B ") for r in wa))
                    report("(b) wrapper: space_a markers in a search of space_b",
                           sum(r[0].startswith("A ") for r in wb))
                    try:
                        call(space="public")
                        report("(b) wrapper, unregistered space 'public'", "RETURNED")
                    except psycopg.Error as exc:
                        report("(b) wrapper, unregistered space 'public'", f"refused: {first_line(exc)}")
                    with psycopg.connect(dsn_for(database), autocommit=True) as su:
                        su.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(READER)))
                        report("what the reader's grants alone expose (compartments by owner_schema)",
                               dict(su.execute('SELECT owner_schema, count(*) '
                                               'FROM "maludb_core"."malu$vector_compartment" '
                                               "GROUP BY 1 ORDER BY 1").fetchall()))

                    print("\nQ1c the facade itself, from the same session")
                    report("session_user / current_user on the request path",
                           one(pg, "SELECT session_user || ' / ' || current_user"))
                    try:
                        pg.execute("SELECT * FROM maludb.memory_search_facade_super(%s, %s::vector, 'subject-01')",
                                   (SPACE_A, vec(embedding("A:1"))))
                        report("facade wrapper owned by the superuser", "WORKS")
                    except psycopg.Error as exc:
                        report("facade wrapper owned by the superuser", f"FAILS: {first_line(exc)}")

                    def via_reader():
                        return pg.execute("SELECT * FROM maludb.memory_search_facade_reader(%s, %s::vector, "
                                          "'subject-01')", (SPACE_A, vec(embedding("A:1")))).fetchall()
                    try:
                        denials, _ = discover(t, READER, via_reader, limit=10)
                        report("facade wrapper owned by the reader", f"WORKS after {denials}")
                    except psycopg.Error as exc:
                        report("facade wrapper owned by the reader, after granting each denial",
                               f"FAILS: {first_line(exc)}")
                        for d in getattr(exc, "denials", []):
                            print(f"      {d}")

                    _hazards(t)

                    # ------------------------------------------------------ PostgREST path
                    if not args.no_postgrest:
                        proc = _postgrest_check(t, names, tenant, workdir, args.port, seeded)
            finally:
                pass

            # ---------------------------------------------------------- Q5 extraction worker
            _extraction(t, args)
        return 0
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        if not args.keep:
            teardown(REF)
            print(f"\ndropped {names.database} and every mldb_{REF}_* role")


def _vectors_beside_spaces(database: str, names, pg) -> None:
    """ADR-077's shipped wrappers, installed as enablement installs them, beside two spaces."""
    print("\nQ1d the shipped vector wrappers (ADR-077) in a database that also has memory spaces")
    limits = type("Allowed", (), {"vector_max_count": 1_000_000, "vector_max_dimension": 2000,
                                  "vector_max_compartments": 1000, "plan_code": "spike"})()
    with psycopg.connect(dsn_for(database)) as v:
        maludb_vectors._build(v, names, limits)  # noqa: SLF001
        v.commit()
    report("customer compartments created through the wrappers", 0)
    listed = pg.execute("SELECT namespace, subject, verb, vector_count FROM maludb.vector_compartments()").fetchall()
    report("maludb.vector_compartments() rows returned to service_role", len(listed))
    subject, verb = SUBJECTS[1], "owns"
    hits = pg.execute("SELECT content FROM maludb.vector_search(%s, %s, %s, %s::vector, 50)",
                      (NAMESPACE, subject, verb, vec(embedding("A:1")))).fetchall()
    report(f"maludb.vector_search('{NAMESPACE}', '{subject}', '{verb}') rows / space markers",
           f"{len(hits)} / {sorted({h[0][:1] for h in hits})}")
    try:
        with pg.transaction():
            gone = one(pg, "SELECT maludb.vector_compartment_delete(%s, %s, %s)", (NAMESPACE, subject, verb))
            left = pg.execute("SELECT count(*) FROM maludb.vector_compartments() WHERE subject = %s AND verb = %s",
                              (subject, verb)).fetchone()[0]
            report("maludb.vector_compartment_delete on that name (rolled back)",
                   f"returned {gone}; same-named compartments left {left}")
            raise psycopg.Rollback
    except psycopg.Error as exc:
        report("maludb.vector_compartment_delete on that name", f"refused: {first_line(exc)}")
    with psycopg.connect(dsn_for(database), autocommit=True) as su:
        report("  compartments with that name, by owner_schema, after rollback", su.execute(
            'SELECT c.owner_schema, count(*) FROM maludb_core."malu$vector_compartment" c '
            'JOIN maludb_core."malu$vector_subject" s ON s.subject_id = c.subject_id '
            "WHERE s.subject_name = %s GROUP BY 1 ORDER BY 1", (subject,)).fetchall())


def _normalise(rows) -> list[tuple]:
    return [(r[0], r[1], r[2], r[3], round(r[4], 9), r[5], r[6], r[7]) for r in rows]


_SEARCH_BODY = """
    RETURN QUERY
    WITH matching AS (
        SELECT c.compartment_id, s.subject_name AS s_name, v.verb_name AS v_name
          FROM malu$vector_compartment c
          JOIN malu$vector_subject s ON s.owner_schema = c.owner_schema AND s.namespace = c.namespace
                                    AND s.subject_id = c.subject_id
          JOIN malu$vector_verb v ON v.owner_schema = c.owner_schema AND v.namespace = c.namespace
                                 AND v.verb_id = c.verb_id
         WHERE c.owner_schema = v_space
           AND c.namespace = coalesce(memory_search.namespace, 'default')
           AND (memory_search.subject IS NULL OR s.subject_name = memory_search.subject)
           AND (memory_search.verb IS NULL OR v.verb_name = memory_search.verb)
    ), hits AS (
        SELECT h.chunk_id AS h_chunk, h.source_text AS h_text, h.distance AS h_distance,
               h.similarity AS h_similarity, m.compartment_id AS h_compartment, m.s_name, m.v_name
          FROM matching m
          CROSS JOIN LATERAL exact_vector_search_sql(m.compartment_id, query::text::malu_vector,
                                                     match_count, memory_search.metric) h
    ), ranked AS (
        SELECT hits.*, row_number() OVER (ORDER BY h_distance, h_compartment, h_chunk)::integer AS h_rank
          FROM hits
    )
    SELECT r.h_chunk, vc.statement_id, vc.document_id, r.h_text, r.h_distance, r.h_similarity, r.h_rank,
           r.s_name, r.v_name
      FROM ranked r JOIN malu$vector_chunk vc ON vc.chunk_id = r.h_chunk
     WHERE r.h_rank <= match_count
     ORDER BY r.h_rank;
"""


def _build_reader(t, names) -> None:
    """The candidate: a NOLOGIN definer holding nothing, a registry of spaces, one wrapper."""
    t.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(READER)))
    t.execute("CREATE SCHEMA IF NOT EXISTS maludb")
    t.execute("CREATE SCHEMA IF NOT EXISTS maludb_private")
    t.execute("REVOKE ALL ON SCHEMA maludb_private FROM PUBLIC")
    t.execute("CREATE TABLE maludb_private.memory_spaces (space name PRIMARY KEY)")
    t.execute("INSERT INTO maludb_private.memory_spaces VALUES (%s), (%s)", (SPACE_A, SPACE_B))
    reader = sql.Identifier(READER)
    # As ADR-077's definer: without USAGE the pinned search_path silently skips a
    # schema, and the failure is "relation does not exist", not a denial.
    t.execute(sql.SQL("GRANT USAGE ON SCHEMA maludb_private, maludb_core, public TO {}").format(reader))
    t.execute(sql.SQL("GRANT SELECT ON maludb_private.memory_spaces TO {}").format(reader))
    returns = ("RETURNS TABLE(chunk_id bigint, statement_id bigint, document_id bigint, source_text text, "
               "distance double precision, similarity double precision, rank_no integer, "
               "subject_name text, verb_name text)")
    t.execute(
        "CREATE FUNCTION maludb.memory_search(space text, query vector, subject text DEFAULT NULL, "
        "verb text DEFAULT NULL, namespace text DEFAULT 'default', match_count integer DEFAULT 20, "
        f"metric text DEFAULT NULL) {returns} "
        "LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = maludb_core, public, pg_temp AS $f$\n"
        "#variable_conflict use_column\n"
        "DECLARE v_space name;\nBEGIN\n"
        "    SELECT s.space INTO v_space FROM maludb_private.memory_spaces s WHERE s.space = memory_search.space;\n"
        "    IF v_space IS NULL THEN\n"
        "        RAISE EXCEPTION 'no memory space %', memory_search.space USING ERRCODE = 'PT404';\n"
        "    END IF;\n"
        "    IF query IS NULL OR (memory_search.subject IS NULL AND memory_search.verb IS NULL) THEN\n"
        "        RAISE EXCEPTION 'query and a subject or verb are required' USING ERRCODE = 'PT400';\n"
        "    END IF;\n"
        f"{_SEARCH_BODY}\nEND\n$f$"
    )
    # The same query shape through the facade, twice: owned by the reader (the
    # ADR-077 pattern) and by the superuser (ADR-074 slice 0's shape).
    for suffix in ("reader", "super"):
        t.execute(
            f"CREATE FUNCTION maludb.memory_search_facade_{suffix}(space text, query vector, subject text) "
            f"{returns} LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = maludb_core, public, pg_temp "
            "AS $f$ BEGIN RETURN QUERY EXECUTE format("
            "'SELECT * FROM %I.maludb_memory_search($1::text::malu_vector, $2)', space) "
            "USING query, subject; END $f$")
    for fn in ("memory_search(text,vector,text,text,text,integer,text)",
               "memory_search_facade_reader(text,vector,text)"):
        t.execute(sql.SQL("ALTER FUNCTION maludb.{} OWNER TO {}").format(sql.SQL(fn), reader))
    t.execute("GRANT USAGE ON SCHEMA maludb TO service_role")
    for fn in ("memory_search(text,vector,text,text,text,integer,text)",
               "memory_search_facade_reader(text,vector,text)", "memory_search_facade_super(text,vector,text)"):
        t.execute(f"REVOKE ALL ON FUNCTION maludb.{fn} FROM PUBLIC")
        t.execute(f"GRANT EXECUTE ON FUNCTION maludb.{fn} TO service_role")


def _report_reader_grants(t) -> None:
    grants = t.execute(
        "SELECT n.nspname || '.' || c.relname, string_agg(a.privilege_type, ',' ORDER BY a.privilege_type), "
        "       c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE a.grantee = %s::regrole GROUP BY 1, 3 ORDER BY 1",
        (READER,)).fetchall()
    report("reader's relation grants (name, privileges, RLS enabled)", grants)
    funcs = t.execute(
        "SELECT p.oid::regprocedure::text, p.prosecdef FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE a.grantee = %s::regrole AND n.nspname NOT IN ('maludb') ORDER BY 1", (READER,)).fetchall()
    report("reader's function grants", len(funcs))
    for signature, secdef in funcs:
        print(f"      {signature}{'  SECURITY DEFINER' if secdef else ''}")
    report("reader role: login / members / member of",
           one(t, "SELECT rolcanlogin || ' / ' || (SELECT count(*) FROM pg_auth_members WHERE roleid = r.oid) "
                  "|| ' / ' || (SELECT count(*) FROM pg_auth_members WHERE member = r.oid) "
                  "FROM pg_roles r WHERE rolname = %s", (READER,)))


def _hazards(t) -> None:
    print("\nQ3c hazards, demonstrated")
    with psycopg.connect(dsn_for(one(t, "SELECT current_database()")), autocommit=True) as su:
        # 1. current_schema() follows search_path, not the facade's schema.
        su.execute(sql.SQL("SET search_path = {}, maludb_core, public").format(sql.Identifier(SPACE_B)))
        report("current_schema() after SET search_path = space_b", one(su, "SELECT current_schema()"))
        eid = one(su, sql.SQL("SELECT {}.maludb_register_episode('note', 'written via space_a facade', "
                              "NULL, '{{}}'::jsonb, now(), NULL, 'internal', 'provided')")
                  .format(sql.Identifier(SPACE_A)))
        report("space_a.maludb_register_episode stored owner_schema",
               one(su, 'SELECT owner_schema FROM maludb_core."malu$episode_object" WHERE episode_id = %s', (eid,)))
        report("  because the facade pins", one(su, "SELECT proconfig::text FROM pg_proc WHERE oid = "
                                                 "%s::regprocedure", (f"{SPACE_A}.maludb_register_episode("
                                                 "text,text,text,jsonb,timestamptz,timestamptz,text,text)",)))
        eid = one(su, "SELECT maludb_core.register_episode('note', 'written via maludb_core', NULL, "
                      "'{}'::jsonb, now(), NULL, 'internal')")
        report("maludb_core.register_episode (unpinned) stored owner_schema",
               one(su, 'SELECT owner_schema FROM maludb_core."malu$episode_object" WHERE episode_id = %s', (eid,)))
        su.execute("RESET search_path")

        # 2. invoker views: RLS keys on current_schema(), so the path decides.
        su.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(PROBE)))
        probe = sql.Identifier(PROBE)
        for space in (SPACE_A, SPACE_B):
            su.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(space), probe))
        su.execute(sql.SQL("GRANT USAGE ON SCHEMA maludb_core TO {}").format(probe))
        su.execute(sql.SQL("GRANT SELECT ON {}.maludb_svpor_statement, {}.maludb_subject TO {}").format(
            sql.Identifier(SPACE_B), sql.Identifier(SPACE_B), probe))
        su.execute(sql.SQL('GRANT SELECT ON maludb_core."malu$svpor_statement" TO {}').format(probe))
        truth = one(su, sql.SQL("SELECT count(*) FROM {}.maludb_svpor_statement").format(sql.Identifier(SPACE_B)))
        subjects = one(su, sql.SQL("SELECT count(*) FROM {}.maludb_subject").format(sql.Identifier(SPACE_B)))
        report("space_b rows as platform: statements / subjects", f"{truth} / {subjects}")
        su.execute(sql.SQL("SET ROLE {}").format(probe))
        for path in (SPACE_B, SPACE_A, "maludb_core", "''"):
            su.execute(f"SET search_path = {path}")
            stmts = one(su, sql.SQL("SELECT count(*) FROM {}.maludb_svpor_statement").format(sql.Identifier(SPACE_B)))
            subs = one(su, sql.SQL("SELECT count(*) FROM {}.maludb_subject").format(sql.Identifier(SPACE_B)))
            report(f"probe, search_path={path}: invoker statements / owner-rights subjects", f"{stmts} / {subs}")
        su.execute("RESET search_path")
        # 3. the base table directly: RLS is on, not forced, so path again decides.
        for space in (SPACE_A, SPACE_B):
            su.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(space)))
            report(f"probe SELECTs base malu$svpor_statement, search_path={space}",
                   dict(su.execute('SELECT owner_schema, count(*) FROM maludb_core."malu$svpor_statement" '
                                   "GROUP BY 1").fetchall()))
        su.execute("RESET ROLE")


def _postgrest_check(t, names, tenant, workdir: Path, port: int, seeded: dict):
    print("\nQ1b the wrapper through PostgREST (authenticator login, JWT role service_role)")
    secret = secrets.token_urlsafe(48)
    settings = workers.WorkerSettings(
        project_ref=REF, database=names.database, authenticator_role=names.authenticator,
        authenticator_password=tenant["passwords"]["authenticator"], jwt_secret=secret, port=port,
        exposed_schema="public, maludb",
        pre_request=workers.pre_request_for(tenant_bootstrap.latest_version()),
    )
    conf = workdir / "postgrest.conf"
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(workers.render_config(settings))
    report("pre-request in config", settings.pre_request)
    proc = subprocess.Popen([POSTGREST, str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    token = jwt.encode({"role": "service_role", "exp": int(time.time()) + 3600}, secret, algorithm="HS256")
    anon = jwt.encode({"role": "anon", "exp": int(time.time()) + 3600}, secret, algorithm="HS256")

    def rpc(bearer: str | None, body: dict) -> tuple[int, object, float]:
        headers = {"Content-Type": "application/json", "Content-Profile": "maludb", "Accept-Profile": "maludb"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/rpc/memory_search",
                                     data=json.dumps(body).encode(), method="POST", headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read()), time.monotonic() - started
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), time.monotonic() - started
        except OSError as e:
            return 0, str(e), time.monotonic() - started

    body = {"space": SPACE_A, "query": vec(embedding("A:3")), "subject": SUBJECTS[3], "match_count": 5}
    deadline = time.monotonic() + 40
    status = 0
    while time.monotonic() < deadline:
        status, payload, _ = rpc(token, body)
        if status not in (0, 503):
            break
        time.sleep(0.3)
    report("service_role POST /rpc/memory_search", f"HTTP {status}, {len(payload) if status == 200 else payload}")
    if status == 200:
        want = facade_search(t, SPACE_A, embedding("A:3"), subject=SUBJECTS[3], limit=5)
        same = [p["chunk_id"] for p in payload] == [w[0] for w in want]
        report("same chunk ids, same order as the facade as platform", same)
        report("rank-1 source_text", payload[0]["source_text"] if payload else None)
    status, payload, _ = rpc(anon, body)
    report("anon JWT", f"HTTP {status}, {payload.get('message') if isinstance(payload, dict) else payload}")
    status, payload, _ = rpc(token, {**body, "space": "maludb_core"})
    report("service_role, space not registered", f"HTTP {status}, {payload.get('message')}")
    times = [rpc(token, {**body, "query": vec(embedding(f"A:{i}")), "subject": SUBJECTS[i % 20]})[2]
             for i in range(30)]
    report("HTTP round trip", pct(times))
    # an extension function straight through /rpc is still refused
    req = urllib.request.Request(f"http://127.0.0.1:{port}/rpc/maludb_memory_search", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Content-Profile": SPACE_A,
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            report("POST /rpc/maludb_memory_search in the space schema", f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        report("POST /rpc/maludb_memory_search in the space schema",
               f"HTTP {e.code}, {json.loads(e.read() or b'{}').get('code')} (space not in db-schemas)")
    return proc


def _extraction(t, args) -> None:
    print("\nQ5  extraction: request -> platform worker -> harvest -> search")
    database = one(t, "SELECT current_database()")
    with psycopg.connect(dsn_for(database), autocommit=True) as su:
        su.execute(sql.SQL("SET search_path = {}, maludb_core, public").format(sql.Identifier(SPACE_A)))
        su.execute("SELECT maludb_core.register_model_provider('mps-stub', 'stub', 'platform-stub')")
        su.execute("SELECT maludb_core.register_model_alias('mps-extract', 'mps-stub', 'stub-extractor', "
                   "NULL, NULL, NULL, NULL, '{}'::jsonb)")
        su.execute("RESET search_path")
        report("provider / alias owner_schema (registered with search_path=space_a)", su.execute(
            "SELECT (SELECT owner_schema FROM maludb_core.\"malu$model_provider\" WHERE provider_name = 'mps-stub') "
            "|| ' / ' || (SELECT owner_schema FROM maludb_core.\"malu$model_alias\" WHERE alias_name = 'mps-extract')"
        ).fetchone()[0])
        su.execute(sql.SQL("SELECT {}.maludb_memory_set_model_config('mps-extract', NULL, 'stub-384', %s)")
                   .format(sql.Identifier(SPACE_A)), (NAMESPACE,))
        doc = upload(su, SPACE_A, "extraction source", "long document")
        times, requests = [], []
        for i in range(args.requests):
            started = time.monotonic()
            requests.append(one(su, sql.SQL("SELECT {}.maludb_memory_request_extraction('document', %s, %s, %s)")
                                .format(sql.Identifier(SPACE_A)),
                                (doc, f"extract-{i:03d} worked on the parser with extract-{i + 1:03d}", NAMESPACE)))
            times.append(time.monotonic() - started)
        report("maludb_memory_request_extraction", pct(times))
        report("request rows: owner_schema, account_id, status", su.execute(
            'SELECT owner_schema, account_id, status, count(*) FROM maludb_core."malu$model_request" '
            "WHERE request_id = ANY(%s) GROUP BY 1, 2, 3", (requests,)).fetchall())

    # The reference stub is not harvestable: its output is text, not the edge JSON.
    with psycopg.connect(dsn_for(database), autocommit=True) as su:
        su.execute("SET search_path = maludb_core, public")  # upstream resolves its tables unqualified
        try:
            su.execute("SELECT maludb_core.mc_stub_process(%s)", (requests[0],))
            report("mc_stub_process with search_path=maludb_core", "WORKS")
        except psycopg.Error as exc:
            report("mc_stub_process with search_path=maludb_core", f"FAILS: {first_line(exc)}")
            report("  ", exc.diag.message_detail)
        su.execute(sql.SQL("SET search_path = {}, maludb_core, public").format(sql.Identifier(SPACE_A)))
        su.execute("SELECT maludb_core.mc_stub_process(%s)", (requests[0],))
        su.execute("RESET search_path")
        harvested = su.execute(sql.SQL("SELECT status, edge_count FROM {}.maludb_memory_harvest_extractions(100, NULL)")
                               .format(sql.Identifier(SPACE_A))).fetchall()
        report("mc_stub_process then harvest (upstream's in-DB stub)", harvested)
        report("  recorded error", one(su, 'SELECT error FROM maludb_core."malu$memory_extraction" '
                                           "WHERE request_id = %s", (requests[0],)))

    # (a) the node's superuser connection: nothing granted, nothing assumed.
    with psycopg.connect(dsn_for(database)) as w:
        report("worker session_user / rolbypassrls / member of maludb_llm_*", one(
            w, "SELECT session_user || ' / ' || rolbypassrls || ' / ' || "
               "(SELECT count(*) FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
               " WHERE m.member = r.oid AND g.rolname LIKE 'maludb_llm%%') "
               "FROM pg_roles r WHERE rolname = session_user"))
        drained, times = _drain(w, limit=args.requests - 3)
        report("superuser worker drained", f"{drained} requests, {pct(times)} per claim+respond")

    # (b) a narrow role, not BYPASSRLS, granted what each denial names.
    with psycopg.connect(dsn_for(database), autocommit=True) as su:
        su.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(WORKER)))
        with psycopg.connect(dsn_for(database)) as w:
            w.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(WORKER)))
            w.commit()

            def attempt():
                try:
                    return _drain(w, limit=1)
                except psycopg.Error:
                    w.rollback()
                    raise

            denials, (drained, _) = discover(su, WORKER, attempt)
            report("narrow worker denials", len(denials))
            for d in denials:
                print(f"      {d}")
            report("narrow worker drained after grants", drained)
            # Escalation can overshoot by a level: take each table privilege away
            # again and keep it away if a fresh request still drains without it.
            unneeded = []
            for relation, privilege in su.execute(
                    "SELECT c.oid::regclass::text, a.privilege_type FROM pg_class c "
                    "CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE a.grantee = %s::regrole "
                    "AND c.relkind = 'r' ORDER BY 1, 2", (WORKER,)).fetchall():
                su.execute(sql.SQL("REVOKE {} ON {} FROM {}").format(
                    sql.SQL(privilege), sql.SQL(relation), sql.Identifier(WORKER)))
                su.execute(sql.SQL("SELECT {}.maludb_memory_request_extraction('document', %s, %s, %s)")
                           .format(sql.Identifier(SPACE_A)),
                           (doc, "extract-900 minimising with extract-901", NAMESPACE))
                try:
                    ok = attempt()[0] == 1
                except psycopg.Error:
                    ok = False
                if ok:
                    unneeded.append(f"{privilege} on {relation}")
                else:
                    su.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                        sql.SQL(privilege), sql.SQL(relation), sql.Identifier(WORKER)))
            report("granted by escalation but not needed", unneeded or "none")
            report("narrow worker's grants", su.execute(
                "SELECT c.relname || ':' || string_agg(a.privilege_type, ',') FROM pg_class c "
                "CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE a.grantee = %s::regrole GROUP BY c.relname",
                (WORKER,)).fetchall())
        with psycopg.connect(dsn_for(database)) as w:
            drained2, _ = _drain(w, limit=100)
        report("remaining drained by the superuser worker", drained2)

        started = time.monotonic()
        harvested = su.execute(sql.SQL("SELECT status, count(*), sum(edge_count) FROM "
                                       "{}.maludb_memory_harvest_extractions(100, NULL) GROUP BY 1")
                               .format(sql.Identifier(SPACE_A))).fetchall()
        report("harvest as platform", f"{harvested} in {time.monotonic() - started:.2f} s")
        hits = facade_search(su, SPACE_A, embedding("extract-004"), subject="extract-004", limit=3)
        report("search for a harvested subject right after harvest", [(h[3], h[5]) for h in hits])
        report("harvested edges visible in space_a.maludb_svpor_statement", one(
            su, sql.SQL("SELECT count(*) FROM {}.maludb_svpor_statement WHERE metadata_jsonb ->> "
                        "'extraction_model' = 'mps-extract'").format(sql.Identifier(SPACE_A))))
        report("claims / facts written by the whole run", su.execute(
            'SELECT (SELECT count(*) FROM maludb_core."malu$claim") || \' / \' || '
            '(SELECT count(*) FROM maludb_core."malu$fact")').fetchone()[0])


def _drain(w, *, limit: int) -> tuple[int, list[float]]:
    """The modeld polling contract, with a stub model that answers in the harvest's JSON."""
    drained, times = 0, []
    while drained < limit:
        started = time.monotonic()
        row = w.execute(
            'SELECT request_id, rendered_prompt, owner_schema FROM maludb_core."malu$model_request" '
            "WHERE status = 'pending' ORDER BY submitted_at LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
        if row is None:
            w.rollback()
            break
        request_id, prompt, owner_schema = row
        w.execute('UPDATE maludb_core."malu$model_request" SET status = \'running\', started_at = now() '
                  "WHERE request_id = %s", (request_id,))
        chunk = prompt.rsplit("CHUNK:\n", 1)[-1]
        who, other = re.findall(r"extract-\d{3}", chunk)[:2]
        output = {"candidate_edges": [
            {"subject_text": who, "subject_type": "person", "verb_text": "worked_on",
             "predicate": [{"attr_name": "status", "value_text": "active"}],
             "source_span": chunk, "confidence": 0.7, "embedding": embedding(who), "embedding_model": "stub-384"},
            {"subject_text": other, "subject_type": "person", "verb_text": "collaborated",
             "predicate": {"with": who}, "source_span": chunk, "embedding": embedding(other)},
        ]}
        text = json.dumps(output)
        w.execute(
            'INSERT INTO maludb_core."malu$model_response" (request_id, status, output_text, output_hash, '
            "output_json, finish_reason, prompt_tokens, completion_tokens, latency_ms, adapter_name, owner_schema) "
            "VALUES (%s, 'succeeded', %s, %s, %s::jsonb, 'stop', %s, %s, 0, 'platform-stub', %s)",
            (request_id, text, hashlib.sha256(text.encode()).hexdigest(), text,
             (len(prompt) + 3) // 4, (len(text) + 3) // 4, owner_schema))
        w.execute('UPDATE maludb_core."malu$model_request" SET status = \'succeeded\', finished_at = now() '
                  "WHERE request_id = %s", (request_id,))
        w.commit()
        drained += 1
        times.append(time.monotonic() - started)
    return drained, times


# --------------------------------------------------------------------------
# memory slice 1: the per-project writer login
#
# Decision 6 of ADR-079 says the memory worker connects to each tenant database
# as a per-project *writer login* with CREATE on that project's spaces only,
# because the pipeline facades run `_memory_schema_assert_manageable`, which
# checks `has_schema_privilege(session_user, <space>, 'CREATE')`. Slice 0 marked
# this NOT measured. This subcommand measures it end to end.


def writer_dsn(names, password: str) -> str:
    return dsn_for(names.database, user=WRITER.format(ref=names.project_ref), password=password)


def build_writer(t, names, *, space: str, password: str) -> str:
    """The narrow writer, granted the minimal set found by measurement: a LOGIN
    with no attribute worth having, CONNECT on its own database only, USAGE on
    maludb_core, USAGE + CREATE on one space, and EXECUTE on that space's
    pipeline facades. No table, sequence or role-membership grant."""
    role = WRITER.format(ref=names.project_ref)
    ident = sql.Identifier(role)
    with admin(autocommit=True) as a:
        a.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS NOINHERIT").format(ident, sql.Literal(password)))
        a.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(names.database), ident))
    t.execute(sql.SQL("GRANT USAGE ON SCHEMA maludb_core TO {}").format(ident))
    t.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(sql.Identifier(space), ident))
    for fn in PIPELINE_FACADES:
        t.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {}.{} TO {}").format(
            sql.Identifier(space), sql.Identifier(fn), ident))
    return role


def cmd_writer(args) -> int:  # noqa: C901
    space, other = SPACE_A, SPACE_B
    names = provisioning.TenantNames.for_ref(WREF)
    names2 = provisioning.TenantNames.for_ref(WREF2)
    role = WRITER.format(ref=WREF)
    teardown(WREF)
    teardown(WREF2)
    with admin(autocommit=True) as a:
        a.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    password = provisioning.generate_password()
    try:
        provision(WREF)
        provision(WREF2)
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            for sp in (space, other):
                enable_space(t, sp)
            print(f"tenant {names.database}, maludb_core "
                  f"{one(t, 'SELECT extversion FROM pg_extension WHERE extname = %s', ('maludb_core',))}; "
                  f"spaces {space}, {other}")
            build_writer(t, names, space=space, password=password)

        # ------------------------------------------------------------ Q1a guard needs CREATE
        print("\nQ1a  the guard: does a facade need CREATE, or only USAGE + EXECUTE?")
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            t.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
                sql.Identifier(space), sql.Identifier(role)))
        with psycopg.connect(writer_dsn(names, password), autocommit=True) as w:
            report("writer has CREATE on the space after revoke",
                   one(w, "SELECT has_schema_privilege(session_user, %s, 'CREATE')", (space,)))
            try:
                w.execute(sql.SQL("SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', "
                                  "p_source_id => 1, p_subject_text => 's', p_verb_text => 'v', "
                                  "p_embedding => %s::maludb_core.malu_vector, p_embedding_model => 'stub-384')")
                          .format(sql.Identifier(space)), (vec(embedding("g")),))
                report("ingest_edge with USAGE+EXECUTE but no CREATE", "WORKS (guard not enforced)")
            except psycopg.Error as exc:
                report("ingest_edge with USAGE+EXECUTE but no CREATE", f"REFUSED: {first_line(exc)}")
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            t.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
                sql.Identifier(space), sql.Identifier(role)))

        # ------------------------------------------------------------ Q1b end to end
        print("\nQ1b  the writer runs the whole pipeline through the space facades")
        with psycopg.connect(writer_dsn(names, password), autocommit=True) as w:
            def wf(label, query, params=()):
                try:
                    rows = w.execute(query, params).fetchall()
                    report(label, f"OK {rows[:1]}")
                    return rows
                except psycopg.Error as exc:
                    report(label, f"ERR {first_line(exc)}")
                    return None

            wf("register_model_provider", sql.SQL("SELECT {}.maludb_register_model_provider("
               "'mws-stub', 'stub', 'platform-stub')").format(sql.Identifier(space)))
            wf("register_model_alias", sql.SQL("SELECT {}.maludb_register_model_alias("
               "'mws-extract', 'mws-stub', 'stub-extractor', NULL, NULL, NULL, NULL, '{{}}'::jsonb)")
               .format(sql.Identifier(space)))
            wf("set_model_config", sql.SQL("SELECT {}.maludb_memory_set_model_config("
               "'mws-extract', NULL, 'stub-384', 'default')").format(sql.Identifier(space)))
            doc = one(w, sql.SQL("SELECT {}.maludb_upload_document(p_title => 'src', "
                                 "p_content_text => 'long doc', p_source_type => 'note')")
                      .format(sql.Identifier(space)))
            report("upload_document", doc)
            emb = embedding("k1")
            cid = ingest_edge(w, space, doc=doc, subject="alpha", verb="owns", span="alpha owns", emb=emb)
            report("ingest_edge", cid)
            report("search finds the ingested edge",
                   [(r[3], r[5]) for r in facade_search(w, space, emb, subject="alpha", limit=3)])
            req = one(w, sql.SQL("SELECT {}.maludb_memory_request_extraction('document', %s, %s, 'default')")
                      .format(sql.Identifier(space)), (doc, "extract-001 worked on the parser with extract-002"))
            report("request_extraction", req)
        with psycopg.connect(dsn_for(names.database)) as su:  # the model worker drains (any connection)
            drained, _ = _drain(su, limit=10)
            report("model worker drained (superuser connection, decision 6 keeps this the worker's)", drained)
        with psycopg.connect(writer_dsn(names, password), autocommit=True) as w:
            report("harvest_extractions as the writer",
                   w.execute(sql.SQL("SELECT status, edge_count FROM {}.maludb_memory_harvest_extractions(100, NULL)")
                             .format(sql.Identifier(space))).fetchall())
            report("search finds a harvested subject",
                   [r[3] for r in facade_search(w, space, embedding("extract-001"), subject="extract-001", limit=3)])

        # ------------------------------------------------------------ Q2 reach
        print("\nQ2  what those grants reach beyond the space")
        with psycopg.connect(dsn_for(names.database), autocommit=True) as t:
            seed_space(t, other, marker="B", edges=20)  # give the other space something to leak
        with psycopg.connect(writer_dsn(names, password), autocommit=True) as w:
            report(f"has CREATE / USAGE on {other} (not granted)",
                   w.execute("SELECT has_schema_privilege(session_user, %s, 'CREATE'), "
                             "has_schema_privilege(session_user, %s, 'USAGE')", (other, other)).fetchone())

            def denied(label, query, params=()):
                try:
                    w.execute(query, params).fetchall()
                    report(label, "REACHED (BAD)")
                except psycopg.Error as exc:
                    report(label, f"denied: {first_line(exc)}")

            denied(f"(a) {other} facade ingest_edge",
                   sql.SQL("SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', p_source_id => 1, "
                           "p_subject_text => 'x', p_verb_text => 'v', "
                           "p_embedding => %s::maludb_core.malu_vector, p_embedding_model => 'stub-384')")
                   .format(sql.Identifier(other)), (vec(embedding("z")),))
            denied(f"(a) {other} facade search",
                   sql.SQL("SELECT * FROM {}.maludb_memory_search(%s::maludb_core.malu_vector, 'x', NULL, "
                           "'default', 5)").format(sql.Identifier(other)), (vec(embedding("z")),))
            denied("(a) the _for_schema twin with the other space's name",
                   "SELECT maludb_core._memory_ingest_edge_for_schema(%s, 'document', 1, 'x', 'v')", (other,))
            denied("(a) base malu$vector_compartment directly",
                   'SELECT count(*) FROM maludb_core."malu$vector_compartment"')
            denied("(a) base table via SET search_path to the other space",
                   sql.SQL('SET search_path = {}, maludb_core; SELECT count(*) FROM "malu$vector_compartment"')
                   .format(sql.Identifier(other)))
            denied("(c) an ungranted maludb_core function (register_claim)",
                   "SELECT maludb_core.register_claim('s', 'v', 'o', 'decl', 'src', 'h', '{}'::jsonb, NULL, "
                   "'{}'::jsonb, 'default')")
            denied("(c) pg_authid", "SELECT count(*) FROM pg_authid")
            denied("(c) create a table in maludb_core", "CREATE TABLE maludb_core.wr_probe (x int)")
        report("(c) maludb_core functions granted to PUBLIC (piggyback surface)", 0)
        with psycopg.connect(dsn_for(names.database)) as t:
            report("  measured: maludb_core functions with a PUBLIC EXECUTE grant",
                   one(t, "SELECT count(*) FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a "
                          "WHERE p.pronamespace = 'maludb_core'::regnamespace AND a.grantee = 0"))
            report("  measured: maludb_core functions with default (PUBLIC) ACL",
                   one(t, "SELECT count(*) FROM pg_proc WHERE pronamespace = 'maludb_core'::regnamespace "
                          "AND proacl IS NULL"))
        print("  (b) another tenant database on the same cluster:")
        try:
            with psycopg.connect(dsn_for(names2.database, user=role, password=password)):
                report(f"  CONNECT to {names2.database}", "SUCCEEDED (BAD)")
        except psycopg.Error as exc:
            report(f"  CONNECT to {names2.database}", f"refused: {first_line(exc)}")

        # ------------------------------------------------------------ Q3 escalation
        _writer_escalation(names, password, space)

        # ------------------------------------------------------------ Q4 move / restore
        _writer_move_restore(names, role, password, space)
        return 0
    finally:
        if not args.keep:
            teardown(WREF)
            teardown(WREF2)
            with admin(autocommit=True) as a:
                a.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            print(f"\ndropped {names.database}, {names2.database}, and {role}")


def _writer_escalation(names, password: str, space: str) -> None:
    """Q3: CREATE on the space is a write primitive against a superuser-owned
    schema. Try to make superuser-owned definer code run a writer-created object."""
    print("\nQ3  privilege escalation through CREATE on the space schema")
    with psycopg.connect(dsn_for(names.database)) as t:
        report("space definers' search_path settings (space NOT first => shadows invisible)",
               sorted({r[0] for r in t.execute(
                   "SELECT DISTINCT proconfig::text FROM pg_proc WHERE pronamespace = %s::regnamespace "
                   "AND prosecdef", (space,)).fetchall()}))
        report("space definers pinned space-first (must fully-qualify their calls)",
               t.execute("SELECT oid::regprocedure::text FROM pg_proc WHERE pronamespace = %s::regnamespace "
                         "AND prosecdef AND proconfig::text LIKE %s", (space, f'{{"search_path={space},%')).fetchall())
    with psycopg.connect(writer_dsn(names, password), autocommit=True) as w:
        def attempt(label, fn):
            try:
                fn()
                report(label, "no error")
            except psycopg.Error as exc:
                report(label, f"blocked: {first_line(exc)}")

        w.execute(sql.SQL('DROP TABLE IF EXISTS {}.wr_probe').format(sql.Identifier(space)))
        # 1. shadow a base table / helper in the space schema
        attempt("create a shadow malu$vector_chunk table in the space",
                lambda: w.execute(sql.SQL('CREATE TABLE {}."malu$vector_chunk" (chunk_id bigint)')
                                  .format(sql.Identifier(space))))
        attempt("create a shadow register_vector_chunk() in the space",
                lambda: w.execute(sql.SQL("CREATE FUNCTION {}.register_vector_chunk(bigint, text, "
                                          "maludb_core.malu_vector, text) RETURNS bigint LANGUAGE sql "
                                          "AS $$ SELECT 999::bigint $$").format(sql.Identifier(space))))
        emb = embedding("shadow")
        before = len(facade_search(w, space, emb, subject="shadowtest", limit=50))
        ingest_edge(w, space, doc=1, subject="shadowtest", verb="v", span="shadow", emb=emb)
        after = len(facade_search(w, space, emb, subject="shadowtest", limit=50))
        report("ingest after shadowing used the REAL maludb_core table (shadow ignored)", after > before)
        w.execute(sql.SQL('DROP TABLE IF EXISTS {}."malu$vector_chunk"').format(sql.Identifier(space)))
        w.execute(sql.SQL("DROP FUNCTION IF EXISTS {}.register_vector_chunk(bigint, text, "
                          "maludb_core.malu_vector, text)").format(sql.Identifier(space)))
        # 2. pg_temp hijack (definer paths end in pg_temp)
        attempt("create pg_temp.vector_dims / vector_normalize", lambda: (
            w.execute("CREATE FUNCTION pg_temp.vector_dims(maludb_core.malu_vector) RETURNS integer "
                      "LANGUAGE sql AS $$ SELECT 1 $$"),
            w.execute("CREATE FUNCTION pg_temp.vector_normalize(maludb_core.malu_vector) "
                      "RETURNS maludb_core.malu_vector LANGUAGE sql AS $$ SELECT $1 $$")))
        attempt("ingest_edge with pg_temp shadows present (maludb_core wins => no hijack)",
                lambda: ingest_edge(w, space, doc=1, subject="hj", verb="v", span="hj", emb=embedding("hj")))
        # 3. triggers on superuser-owned tables / views
        attempt("trigger on maludb_core.malu$vector_chunk",
                lambda: w.execute('CREATE TRIGGER wtr AFTER INSERT ON maludb_core."malu$vector_chunk" '
                                  "FOR EACH ROW EXECUTE FUNCTION pg_temp.vector_dims()"))
        attempt("INSTEAD OF trigger on a space view",
                lambda: w.execute(sql.SQL("CREATE TRIGGER wtr2 INSTEAD OF INSERT ON {}.maludb_memory "
                                          "FOR EACH ROW EXECUTE FUNCTION pg_temp.vector_dims()")
                                  .format(sql.Identifier(space))))
        # 4. replace/drop a superuser-owned facade
        attempt("CREATE OR REPLACE the superuser-owned search facade",
                lambda: w.execute(sql.SQL("CREATE OR REPLACE FUNCTION {}.maludb_memory_search("
                                          "maludb_core.malu_vector, text, text, text, integer, text) "
                                          "RETURNS void LANGUAGE sql AS $$ SELECT $$").format(sql.Identifier(space))))
        attempt("DROP the superuser-owned ingest facade",
                lambda: w.execute(sql.SQL("DROP FUNCTION {}.maludb_memory_ingest_edge").format(sql.Identifier(space))))


def _writer_move_restore(names, role: str, password: str, space: str) -> None:
    """Q4: one dump/restore round trip. Are the writer's grants carried, and are
    they lost if the role is absent on the target (as create_vectors_role is)?"""
    print("\nQ4  move / restore: does the writer role need creating on the target first?")
    base = psycopg.conninfo.conninfo_to_dict(need_dsn())
    workdir = Path(tempfile.mkdtemp(prefix="mws-restore-"))
    dump = str(workdir / "tenant.dump")
    target_present = f"{names.database}_r_present"
    target_absent = f"{names.database}_r_absent"
    restore_opts = "-c session_replication_role=replica"  # ADR-078
    try:
        subprocess.run(["pg_dump", dsn_for(names.database), "-Fc", "-f", dump],
                       check=True, capture_output=True)
        grant_lines = subprocess.run(["pg_restore", "-l", dump], check=True, capture_output=True, text=True).stdout
        with admin(autocommit=True) as a:
            for tgt in (target_present, target_absent):
                a.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(tgt)))
                a.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(tgt), sql.Identifier(OWNER)))
        # role present
        env = os.environ.copy()
        env["PGOPTIONS"] = restore_opts
        r1 = subprocess.run(["pg_restore", "-d", dsn_for(target_present), dump],
                            capture_output=True, text=True, env=env)
        with psycopg.connect(dsn_for(target_present)) as c:
            report("role PRESENT: restore exit / writer function grants / CREATE on space",
                   f"{r1.returncode} / "
                   f"{_grant_count(c, role)}"
                   f" / {one(c, 'SELECT has_schema_privilege(%s, %s, %s)', (role, space, 'CREATE'))}")
        # role absent: drop the role (revoking first), then restore
        with admin(autocommit=True) as a:
            for db in (names.database, target_present):
                with psycopg.connect(dsn_for(db), autocommit=True) as c:
                    c.execute(sql.SQL("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {} FROM {}").format(
                        sql.Identifier(space), sql.Identifier(role)))
                    c.execute(sql.SQL("REVOKE ALL ON SCHEMA {}, maludb_core FROM {}").format(
                        sql.Identifier(space), sql.Identifier(role)))
                    c.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                        sql.Identifier(db), sql.Identifier(role)))
            a.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        r2 = subprocess.run(["pg_restore", "-d", dsn_for(target_absent), dump],
                            capture_output=True, text=True, env=env)
        role_errors = r2.stderr.count(f'role "{role}" does not exist')
        with psycopg.connect(dsn_for(target_absent)) as c:
            report("role ABSENT: 'role does not exist' errors / writer grants restored",
                   f"{role_errors} / "
                   f"{_grant_count(c, role)}")
        report("=> the writer role must be created on the target before load, like create_vectors_role",
               "confirmed" if role_errors and not r1.returncode else "review")
        # restore the role so the outer cleanup can drop it uniformly
        with admin(autocommit=True) as a:
            a.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                              "NOREPLICATION NOBYPASSRLS NOINHERIT").format(
                                  sql.Identifier(role), sql.Literal(password)))
        _ = base, grant_lines
    finally:
        with admin(autocommit=True) as a:
            for tgt in (target_present, target_absent):
                a.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(tgt)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="questions 1-5")
    p.add_argument("--edges", type=int, default=1000, help="embedded edges ingested into space_a")
    p.add_argument("--extractions", type=int, default=50)
    p.add_argument("--docs", type=int, default=50)
    p.add_argument("--raw", type=int, default=50, help="read-after-write probes")
    p.add_argument("--compare", type=int, default=40, help="wrapper-vs-facade queries")
    p.add_argument("--requests", type=int, default=10, help="extraction requests")
    p.add_argument("--port", type=int, default=3997)
    p.add_argument("--no-postgrest", action="store_true")
    p.add_argument("--keep", action="store_true")
    p.set_defaults(func=cmd_run)

    w = sub.add_parser("writer", help="memory slice 1: the per-project writer login (ADR-079 decision 6)")
    w.add_argument("--keep", action="store_true", help="leave both tenants and the writer role behind")
    w.set_defaults(func=cmd_writer)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
