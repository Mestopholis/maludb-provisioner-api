"""The memory worker: writes queued ingests into memory spaces (ADR-079, memory slice 5a).

ADR-079 decision 6. A dedicated process -- not the provisioner, which holds every
node's superuser credential (ADR-038) -- that connects to each tenant database
**as that project's memory writer**, the login memory slice 1 measured: CONNECT on
its own database, CREATE on its spaces, EXECUTE on seven facades, nothing else.
The pipeline's guard reads `session_user`, which is why it connects as the writer
rather than narrowing some other connection.

**It never reads a node's admin credential.** A tenant is reached at the node's
`internal_host` on `MALUDB_MEMORY_DB_PORT`, with the writer's own sealed
password. So what this process can reach is what the writers can.

**Per item, and every item.** Each item is written in its own transaction --
the source document and the edge together -- so one bad item is rolled back and
reported while the rest are written. Upstream's facades can decline an item as
well as fail on one (memory slice 0, finding 2a), so a result is recorded for
every item: the statement it became, or the platform's reading of why not. The
request ends `succeeded`, `partial` or `failed`, and its items are cleared from
the control plane when it does; only the results stay.

**Text** (slice 5b). A text ingest is extracted with the space's extraction
model and each edge embedded with its embedding model, using the project's own
provider keys (decision 4), before anything is written. Provider calls leave
through `maludb-egress-proxy` (`MALUDB_MEMORY_EGRESS_PROXY`), which the worker
refuses to start without in production. An item's document and edges commit
together; an edge the pipeline refuses is rolled back to its savepoint and
reported, and an item none of whose edges could be written leaves nothing
behind. A failure that would repeat for every item -- a refused key, a limit that
outlasted the retries -- stops the ingest instead of spending the customer's
quota on the rest. The plan's `memory_max_items` is held here as well as at
admission, because how many memories a text holds is known only after
extraction.

**Its own control-plane role** (slice 5c). The worker connects as a member of
`cp_memory_worker`, which reads the columns and rows `memory_worker_grants`
lists and nothing else: no node's admin credential, no tenant's database
password or signing key. It refuses to start in production when the role it
connected as can read more, or is not that role at all -- the check is the
privilege, not the configuration, as the gateway's is (ADR-072).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import timedelta

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from services.control_plane import config as config_module
from services.control_plane import (
    crypto,
    db,
    entitlements,
    memory_worker_grants,
    model_providers,
    provider_keys,
    provisioning,
)
from services.control_plane import logging as cp_logging

log = logging.getLogger("maludb.memory_worker")

IDLE_SLEEP_SECONDS = 1.0
# A request left running by a worker that died is failed after this long.
ABANDONED_AFTER = timedelta(minutes=15)
SERVING_STATUSES = ("PROVISIONED", "ACTIVE")
MAX_ERROR = 300


def claim(conn: psycopg.Connection) -> dict | None:
    """Take the oldest pending ingest, marking it running. `FOR UPDATE SKIP LOCKED`."""
    db.execute(
        conn,
        "UPDATE memory_ingests SET state = 'failed', items_json = NULL, completed_at = now(), "
        "       detail = 'the platform stopped before finishing this ingest; send it again' "
        " WHERE state = 'running' AND coalesce(heartbeat_at, started_at) < now() - %s",
        (ABANDONED_AFTER,),
    )
    # Least recently served project first, then oldest: a text ingest spends seconds
    # per item at a provider, and oldest-first alone would let one project posting
    # text continuously hold the worker while every other project's ingests wait.
    #
    # The row was written by the gateway role, which may write any row for its own
    # node's projects -- so the space is joined on the ingest's project too, rather
    # than trusted to be that project's because the gateway said so.
    row = db.one(
        conn,
        """
        SELECT i.id, i.project_id, i.kind, i.items_json, s.id AS space_id, s.name AS space, s.schema_name,
               s.extraction_provider, s.extraction_model, s.embedding_provider, s.embedding_model,
               p.project_ref, p.database_name, p.status, n.internal_host, pl.code AS plan_code, pl.config_json
          FROM memory_ingests i
          JOIN memory_spaces s ON s.id = i.space_id AND s.project_id = i.project_id
          JOIN projects p ON p.id = i.project_id
          LEFT JOIN nodes n ON n.id = p.node_id
          LEFT JOIN plans pl ON pl.id = p.plan_id
         WHERE i.state = 'pending' AND p.deleted_at IS NULL AND s.state = 'active'
         ORDER BY (SELECT max(j.started_at) FROM memory_ingests j WHERE j.project_id = i.project_id) NULLS FIRST,
                  i.requested_at
         LIMIT 1
           FOR UPDATE OF i SKIP LOCKED
        """,
    )
    if row is None:
        return None
    db.execute(conn, "UPDATE memory_ingests SET state = 'running', started_at = now() WHERE id = %s", (row["id"],))
    return row


def _vector(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _reason(exc: psycopg.Error) -> str:
    """What a customer is told about an item that was not written: the database's
    primary message about their data, never a connection detail."""
    primary = (exc.diag.message_primary if exc.diag else None) or type(exc).__name__
    return primary[:MAX_ERROR]


def write_items(writer: psycopg.Connection, schema: str, items: list[dict]) -> list[dict]:
    """Write each item as its own document and edge. Returns one result per item."""
    space = sql.Identifier(schema)
    upload = sql.SQL("SELECT {}.maludb_upload_document(p_title => %s, p_content_text => %s, "
                     "p_source_type => 'note')").format(space)
    edge = sql.SQL(
        "SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', p_source_id => %s, "
        "p_subject_text => %s, p_verb_text => %s, p_embedding => %s::maludb_core.malu_vector, "
        "p_embedding_model => %s, p_source_span => %s, p_document_id => %s)"
    ).format(space)
    results = []
    for index, item in enumerate(items):
        try:
            with writer.transaction():
                document = writer.execute(upload, (item["subject"][:200], item["text"])).fetchone()[0]
                statement = writer.execute(edge, (
                    document, item["subject"], item["verb"], _vector(item["embedding"]),
                    item.get("embedding_model") or "customer", item["text"], document,
                )).fetchone()[0]
            if statement is None:
                results.append({"index": index, "written": False, "reason": "declined by the memory pipeline"})
            else:
                results.append({"index": index, "written": True, "statement_id": statement, "document_id": document})
        except psycopg.Error as exc:
            results.append({"index": index, "written": False, "reason": _reason(exc)})
    return results


class _NothingWritten(Exception):
    """Rolls back an item's document when none of its edges was written."""


def write_text_items(writer: psycopg.Connection, schema: str, items: list[dict], *, extract, embed,
                     embedding_model: str, remaining: int, beat=lambda: None) -> list[dict]:
    """Extract, embed and write each text item. Returns one result per item.

    `extract(text)` and `embed(texts)` are the provider calls, bound to the space's
    models and the project's keys; `remaining` is how many memories the plan still
    allows. A result carries `memories`, the number of edges it stored.
    """
    space = sql.Identifier(schema)
    upload = sql.SQL("SELECT {}.maludb_upload_document(p_title => %s, p_content_text => %s, "
                     "p_source_type => 'note')").format(space)
    edge_sql = sql.SQL(
        "SELECT {}.maludb_memory_ingest_edge(p_source_kind => 'document', p_source_id => %s, "
        "p_subject_text => %s, p_verb_text => %s, p_embedding => %s::maludb_core.malu_vector, "
        "p_embedding_model => %s, p_source_span => %s, p_document_id => %s)"
    ).format(space)
    limit_reason = "this project has reached its plan's stored memory limit"
    results: list[dict] = []
    stopped: str | None = None
    for index, item in enumerate(items):
        if stopped is None and remaining <= 0:
            stopped = limit_reason
        if stopped is not None:
            results.append({"index": index, "written": False, "memories": 0, "reason": stopped})
            continue
        result: dict = {"index": index, "written": False, "memories": 0}
        try:
            extraction = extract(item["text"])
            skipped = list(extraction.rejected)
            if extraction.truncated:
                skipped.append({"edge": model_providers.MAX_EDGES,
                                "reason": f"{extraction.truncated} edges beyond the {model_providers.MAX_EDGES} "
                                          "one text may hold were not written"})
            edges = extraction.edges
            if len(edges) > remaining:
                skipped.append({"edge": remaining, "reason": limit_reason})
                edges = edges[:remaining]
            if not edges:
                result["reason"] = "no memories were found in the text"
            else:
                vectors = embed([model_providers.edge_text(edge) for edge in edges])
                written_edges: list[dict] = []
                try:
                    with writer.transaction():
                        title = item.get("title") or item["text"][:200]
                        document = writer.execute(upload, (title, item["text"])).fetchone()[0]
                        for position, (edge, vector) in enumerate(zip(edges, vectors, strict=True)):
                            try:
                                with writer.transaction():
                                    statement = writer.execute(edge_sql, (
                                        document, edge["subject_text"], edge["verb_text"], _vector(vector),
                                        embedding_model, edge["source_span"], document,
                                    )).fetchone()[0]
                            except psycopg.Error as exc:
                                skipped.append({"edge": position, "reason": _reason(exc)})
                                continue
                            if statement is None:
                                skipped.append({"edge": position, "reason": "declined by the memory pipeline"})
                            else:
                                written_edges.append({"statement_id": statement, "subject": edge["subject_text"],
                                                      "verb": edge["verb_text"]})
                        if not written_edges:
                            raise _NothingWritten
                except _NothingWritten:
                    result["reason"] = "none of the text's memories could be written"
                else:
                    result.update(written=True, document_id=document, memories=len(written_edges),
                                  edges=written_edges)
                    remaining -= len(written_edges)
            if skipped:
                result["skipped"] = skipped
        except model_providers.ProviderError as exc:
            result["reason"] = str(exc)
            if exc.fatal:
                stopped = str(exc)
        except psycopg.Error as exc:
            result["reason"] = _reason(exc)
        results.append(result)
        beat()
    return results


def finish(conn: psycopg.Connection, ingest: dict, results: list[dict] | None, *, detail: str | None = None) -> None:
    written = sum(1 for r in results or [] if r["written"])
    failed = len(results or []) - written
    stored = sum(r.get("memories", 1) for r in results or [] if r["written"])
    state = "failed" if not written else ("partial" if failed else "succeeded")
    db.execute(
        conn,
        "UPDATE memory_ingests SET state = %s, results_json = %s, written = %s, failed = %s, detail = %s, "
        "       items_json = NULL, completed_at = now() WHERE id = %s",
        (state, Jsonb(results) if results is not None else None, written, failed, detail, ingest["id"]),
    )
    if stored:
        db.execute(conn, "UPDATE memory_spaces SET item_count = item_count + %s WHERE id = %s",
                   (stored, ingest["space_id"]))


def writer_dsn(ingest: dict, *, password: str, port: int) -> str:
    names = provisioning.TenantNames.for_ref(ingest["project_ref"])
    return make_conninfo(host=ingest["internal_host"], port=port, dbname=names.database,
                         user=names.memwriter, password=password)


def _stored(conn: psycopg.Connection, project_id) -> int:
    return db.one(conn, "SELECT coalesce(sum(item_count), 0)::bigint AS n FROM memory_spaces WHERE project_id = %s",
                  (project_id,))["n"]


def _text_calls(conn: psycopg.Connection, ingest: dict, *, key_ring: crypto.KeyRing, models):
    """The extract and embed calls for a text ingest, or the reason it cannot run."""
    keys = {}
    for provider in (ingest["extraction_provider"], ingest["embedding_provider"]):
        keys[provider] = provider_keys.load_key(conn, project_id=ingest["project_id"], provider=provider,
                                                key_ring=key_ring)
        if keys[provider] is None:
            return None, (f"this project has no {provider} API key; a manager can set one with "
                          f"PUT /v1/projects/{{ref}}/maludb/memory/provider-keys/{provider}")

    def extract(text):
        return models.extract(ingest["extraction_provider"], ingest["extraction_model"],
                              keys[ingest["extraction_provider"]], text)

    def embed(texts):
        return models.embed(ingest["embedding_provider"], ingest["embedding_model"],
                            keys[ingest["embedding_provider"]], texts)

    return (extract, embed), None


def run_once(*, key_ring: crypto.KeyRing, writer_connect=None, port: int | None = None, models=None) -> bool:
    """Claim and write one ingest. False when there was nothing to do.

    `writer_connect(ingest, password)` opens the writer's connection; the default
    reaches the node's internal host. Tests pass one that reaches the local node.
    `models` makes the provider calls for a text ingest; the default leaves through
    `MALUDB_MEMORY_EGRESS_PROXY`.
    """
    with db.connection() as conn:
        ingest = claim(conn)
        conn.commit()
    if ingest is None:
        return False

    port = port or int(os.environ.get("MALUDB_MEMORY_DB_PORT", "5432"))
    try:
        unreachable = not ingest["internal_host"] and writer_connect is None
        if ingest["status"] not in SERVING_STATUSES or unreachable:
            with db.connection() as conn:
                finish(conn, ingest, None, detail="the project is not available; send the ingest again later")
                conn.commit()
            return True
        calls = None
        with db.connection() as conn:
            password = provisioning.load_credential(conn, project_id=ingest["project_id"],
                                                    credential_type="db_memwriter", key_ring=key_ring)
            if ingest["kind"] == "text":
                if not (ingest["extraction_provider"] and ingest["embedding_provider"]):
                    refused = "the memory space no longer names its models; set them and send the ingest again"
                else:
                    calls, refused = _text_calls(conn, ingest, key_ring=key_ring,
                                                 models=models or _default_models())
                if calls is None:
                    finish(conn, ingest, None, detail=refused)
                    conn.commit()
                    return True
                allowed = entitlements.resolve(ingest["plan_code"], ingest["config_json"])
                remaining = allowed.memory_max_items - _stored(conn, ingest["project_id"])
        connect = writer_connect or (lambda row, pw: psycopg.connect(writer_dsn(row, password=pw, port=port),
                                                                     autocommit=True))

        def beat() -> None:
            with db.connection() as conn:
                db.execute(conn, "UPDATE memory_ingests SET heartbeat_at = now() WHERE id = %s", (ingest["id"],))
                conn.commit()

        with connect(ingest, password) as writer:
            if calls is None:
                results = write_items(writer, ingest["schema_name"], ingest["items_json"])
            else:
                results = write_text_items(writer, ingest["schema_name"], ingest["items_json"], extract=calls[0],
                                           embed=calls[1], embedding_model=ingest["embedding_model"],
                                           remaining=remaining, beat=beat)
        with db.connection() as conn:
            finish(conn, ingest, results)
            conn.commit()
        log.info("memory ingest %s for project %s: %s of %s written", ingest["id"], ingest["project_ref"],
                 sum(1 for r in results if r["written"]), len(results),
                 extra={"extra_fields": {"project_ref": ingest["project_ref"]}})
    except Exception as exc:  # noqa: BLE001 - one project's failure must not stop the rest
        with db.connection() as conn:
            finish(conn, ingest, None, detail="the platform could not reach this project's memory; it has been "
                                              "logged and the ingest can be sent again")
            conn.commit()
        # The type only: a connection error's text names the node's host and port.
        log.error("memory ingest %s for project %s failed (%s)", ingest["id"], ingest["project_ref"],
                  type(exc).__name__, extra={"extra_fields": {"project_ref": ingest["project_ref"]}})
    return True


_models: model_providers.Models | None = None


def _default_models() -> model_providers.Models:
    global _models
    if _models is None:
        _models = model_providers.Models(proxy=os.environ.get("MALUDB_MEMORY_EGRESS_PROXY") or None)
    return _models


__all__ = ["assert_narrowed", "claim", "finish", "run_once", "write_items", "write_text_items", "writer_dsn"]


def assert_narrowed(conn: psycopg.Connection, *, environment: str) -> None:
    """Refuse to run in production as anything wider than `cp_memory_worker`.

    Two questions, both asked of the database. Can this role read what a memory
    worker must not -- a node's admin credential, a tenant's other credentials,
    users, keys, billing? And is it the memory worker at all? A role that is not
    sees no rows through the policies and would claim nothing forever, which is
    safe and silent; saying so here is the difference between a log line and a
    queue that never drains.
    """
    role = db.one(conn, "SELECT current_user AS role")["role"]
    wider = memory_worker_grants.violations(conn, role)
    member = db.one(conn, "SELECT public.is_memory_worker() AS member")["member"]
    gateways = memory_worker_grants.gateway_members(conn) if member else []
    conn.rollback()
    problems = []
    if wider:
        problems.append(f"this memory worker's database role {role!r} can read {', '.join(wider)}, so a "
                        "compromise of it reaches the fleet rather than memory (ADR-079 decision 6)")
    if not member:
        problems.append(f"role {role!r} is not a member of {memory_worker_grants.GROUP_ROLE}, so its row "
                        "policies match nothing and no ingest will ever be claimed")
    if gateways:
        problems.append(f"gateway roles {gateways} are also memory workers, which lets them read their "
                        "node's tenant credentials")
    if not problems:
        return
    message = "; ".join(problems) + (". Create a LOGIN role in cp_memory_worker, run `cp-manage memory-worker "
                                     "grant`, and point this worker's MALUDB_CONTROL_PLANE_DATABASE_URL at it")
    if environment == "production":
        raise RuntimeError(message)
    log.warning("%s -- refused in production; allowed here because MALUDB_ENV=%s", message, environment)


def main() -> int:
    cfg = config_module.load()
    cp_logging.configure()
    if cfg.is_production and not os.environ.get("MALUDB_MEMORY_EGRESS_PROXY"):
        raise SystemExit("MALUDB_MEMORY_EGRESS_PROXY must name maludb-egress-proxy in production")
    db.init_pool(cfg.database_url)
    key_ring = crypto.KeyRing(cfg.kek)
    with db.connection() as conn:
        assert_narrowed(conn, environment=cfg.environment)
        key_ring.load(conn)

    stopping = False

    def stop(signum, _frame) -> None:  # noqa: ANN001 - signal handler signature
        nonlocal stopping
        stopping = True
        log.info("memory worker stopping on signal %s", signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("memory worker started")
    try:
        while not stopping:
            if not run_once(key_ring=key_ring):
                time.sleep(IDLE_SLEEP_SECONDS)
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

