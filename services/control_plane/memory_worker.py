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

Not yet: raw text embedded and extracted with the customer's provider keys
(slice 5b), and a control-plane database role narrowed to what this process
reads (tracked in `plans/active/memory-spaces.md`).
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
from services.control_plane import crypto, db, provisioning
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
        " WHERE state = 'running' AND started_at < now() - %s",
        (ABANDONED_AFTER,),
    )
    # The row was written by the gateway role, which may write any row for its own
    # node's projects -- so the space is joined on the ingest's project too, rather
    # than trusted to be that project's because the gateway said so.
    row = db.one(
        conn,
        """
        SELECT i.id, i.project_id, i.items_json, s.id AS space_id, s.name AS space, s.schema_name,
               p.project_ref, p.database_name, p.status, n.internal_host
          FROM memory_ingests i
          JOIN memory_spaces s ON s.id = i.space_id AND s.project_id = i.project_id
          JOIN projects p ON p.id = i.project_id
          LEFT JOIN nodes n ON n.id = p.node_id
         WHERE i.state = 'pending' AND p.deleted_at IS NULL
         ORDER BY i.requested_at
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


def finish(conn: psycopg.Connection, ingest: dict, results: list[dict] | None, *, detail: str | None = None) -> None:
    written = sum(1 for r in results or [] if r["written"])
    failed = len(results or []) - written
    state = "failed" if not written else ("partial" if failed else "succeeded")
    db.execute(
        conn,
        "UPDATE memory_ingests SET state = %s, results_json = %s, written = %s, failed = %s, detail = %s, "
        "       items_json = NULL, completed_at = now() WHERE id = %s",
        (state, Jsonb(results) if results is not None else None, written, failed, detail, ingest["id"]),
    )
    if written:
        db.execute(conn, "UPDATE memory_spaces SET item_count = item_count + %s WHERE id = %s",
                   (written, ingest["space_id"]))


def writer_dsn(ingest: dict, *, password: str, port: int) -> str:
    names = provisioning.TenantNames.for_ref(ingest["project_ref"])
    return make_conninfo(host=ingest["internal_host"], port=port, dbname=names.database,
                         user=names.memwriter, password=password)


def run_once(*, key_ring: crypto.KeyRing, writer_connect=None, port: int | None = None) -> bool:
    """Claim and write one ingest. False when there was nothing to do.

    `writer_connect(ingest, password)` opens the writer's connection; the default
    reaches the node's internal host. Tests pass one that reaches the local node.
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
        with db.connection() as conn:
            password = provisioning.load_credential(conn, project_id=ingest["project_id"],
                                                    credential_type="db_memwriter", key_ring=key_ring)
        connect = writer_connect or (lambda row, pw: psycopg.connect(writer_dsn(row, password=pw, port=port),
                                                                     autocommit=True))
        with connect(ingest, password) as writer:
            results = write_items(writer, ingest["schema_name"], ingest["items_json"])
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


__all__ = ["claim", "finish", "run_once", "write_items", "writer_dsn"]


def main() -> int:
    cfg = config_module.load()
    cp_logging.configure()
    db.init_pool(cfg.database_url)
    key_ring = crypto.KeyRing(cfg.kek)
    with db.connection() as conn:
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

