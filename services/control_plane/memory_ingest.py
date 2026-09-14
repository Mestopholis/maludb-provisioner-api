"""Queued memory ingest: what a request may carry, what it may cost, and its result.

ADR-079 decision 3, memory slice 5a. A customer's agent posts embedded edges to
`https://<ref>.<domain>/memory/v1/spaces/{space}/ingest` with the project's secret
key -- the gateway authenticates it, calls `enqueue`, and answers 202 -- and the
memory worker writes each item into the space as the project's writer, recording
per item what happened.

**This module reads and writes the control plane and nothing else**, because the
gateway imports it: no tenant connection, no node credential, no key ring.

## What a request may carry

One of two kinds, never mixed in one request:

- **edges** (slice 5a) -- at most `MAX_ITEMS`, each an edge the customer's own
  models produced: `subject` and `verb` (1 to 200 characters), `text` (the
  source, up to `MAX_TEXT` characters), `embedding` (1 to `MAX_DIMENSIONS` finite
  numbers), and an optional `embedding_model` label;
- **text** (slice 5b) -- at most `MAX_TEXT_ITEMS`, each a `text` and an optional
  `title`. The memory worker extracts edges from it and embeds them with the
  space's models and the project's own provider keys, so the space must name its
  models first. Fewer items than edges, because each costs seconds at a provider.

## What it may cost, decided before it is queued

Under a transaction-scoped advisory lock per project, so two requests cannot both
take the last of an allowance:

- **`memory_ingests_per_hour`** -- requests, counted over the trailing hour, a
  429 with `Retry-After` when exhausted.
- **`memory_max_items`** -- what the project's spaces hold (`item_count`, kept by
  the worker) plus what is already queued plus this request. A 409 that does not
  name the ceiling, as the project cap does not.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements

MAX_ITEMS = 100
MAX_TEXT_ITEMS = 20
MAX_TEXT = 8_000
MAX_LABEL = 200
MAX_DIMENSIONS = 4_096
LIMIT_WINDOW = timedelta(hours=1)

# The advisory-lock namespace for "one ingest admission per project at a time".
INGEST_LOCK_NAMESPACE = 0x4D494E47  # "MING"


class IngestRefused(ValueError):
    """A request the platform will not queue. `status` is the HTTP answer."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class Queued:
    ingest_id: uuid.UUID
    space: str
    kind: str
    item_count: int
    requested_at: datetime


def _text(item: dict, key: str, index: int, *, limit: int, required: bool = True) -> str | None:
    value = item.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IngestRefused(422, f"items[{index}].{key} must be text of 1 to {limit} characters")
    return value


def validate(payload: object) -> tuple[str, list[dict]]:
    """The kind and items of a request body, normalised, or a refusal naming the first problem."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise IngestRefused(422, 'the body must be {"items": [...]}')
    items = payload["items"]
    if not 1 <= len(items) <= MAX_ITEMS:
        raise IngestRefused(422, f"items must hold 1 to {MAX_ITEMS} entries")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise IngestRefused(422, f"items[{index}] must be an object")
    kinds = {"edges" if "embedding" in item else "text" for item in items}
    if len(kinds) > 1:
        raise IngestRefused(422, "items must be all edges (with an embedding) or all text (without one), not both")
    if kinds == {"text"}:
        return "text", _text_items(items)
    return "edges", _edge_items(items)


def _text_items(items: list) -> list[dict]:
    if len(items) > MAX_TEXT_ITEMS:
        raise IngestRefused(422, f"a text ingest holds 1 to {MAX_TEXT_ITEMS} items")
    return [{"text": _text(item, "text", index, limit=MAX_TEXT),
             "title": _text(item, "title", index, limit=MAX_LABEL, required=False)}
            for index, item in enumerate(items)]


def _edge_items(items: list) -> list[dict]:
    clean = []
    for index, item in enumerate(items):
        embedding = item.get("embedding")
        if (not isinstance(embedding, list) or not 1 <= len(embedding) <= MAX_DIMENSIONS
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                           for x in embedding)):
            raise IngestRefused(422, f"items[{index}].embedding must be 1 to {MAX_DIMENSIONS} finite numbers")
        clean.append({
            "subject": _text(item, "subject", index, limit=MAX_LABEL),
            "verb": _text(item, "verb", index, limit=MAX_LABEL),
            "text": _text(item, "text", index, limit=MAX_TEXT),
            "embedding": [float(x) for x in embedding],
            "embedding_model": _text(item, "embedding_model", index, limit=MAX_LABEL, required=False),
        })
    return clean


def enqueue(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    space: str,
    items: list[dict],
    allowed: entitlements.Entitlements,
    kind: str = "edges",
    now: datetime | None = None,
) -> Queued:
    """Admit and queue a validated request, or refuse it. The caller commits."""
    now = now or datetime.now(UTC)
    if not allowed.maludb_memory:
        raise IngestRefused(403, "this project's plan does not include MaluDB memory spaces")
    db.execute(conn, "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (INGEST_LOCK_NAMESPACE, str(project_id)))

    target = db.one(conn, "SELECT id, extraction_provider, embedding_provider FROM memory_spaces "
                          " WHERE project_id = %s AND name = %s AND state = 'active'", (project_id, space))
    if target is None:
        raise IngestRefused(404, f"no memory space {space!r}")
    if kind == "text" and not (target["extraction_provider"] and target["embedding_provider"]):
        raise IngestRefused(409, f"memory space {space!r} has no models to extract and embed text with; a manager "
                                 "can set them with PUT /v1/projects/{ref}/maludb/memory/spaces/{name}/models")

    recent = [r["requested_at"] for r in db.query(
        conn, "SELECT requested_at FROM memory_ingests WHERE project_id = %s AND requested_at > %s "
              "ORDER BY requested_at", (project_id, now - LIMIT_WINDOW))]
    limit = allowed.memory_ingests_per_hour
    if len(recent) >= limit:
        if limit <= 0:
            raise IngestRefused(429, "this project's plan allows no memory ingest requests")
        opens_at = recent[len(recent) - limit] + LIMIT_WINDOW
        retry_after = max(1, int((opens_at - now).total_seconds()) + 1)
        raise IngestRefused(429, f"this project's plan allows {limit} memory ingest requests an hour; "
                                 f"the next is allowed in {retry_after} seconds", retry_after=retry_after)

    held = db.one(
        conn,
        "SELECT (SELECT coalesce(sum(item_count), 0) FROM memory_spaces WHERE project_id = %s) "
        "     + (SELECT coalesce(sum(item_count), 0) FROM memory_ingests "
        "         WHERE project_id = %s AND state IN ('pending', 'running')) AS n",
        (project_id, project_id),
    )["n"]
    if held + len(items) > allowed.memory_max_items:
        raise IngestRefused(409, "this project has reached its plan's stored memory limit")

    ingest_id = uuid.uuid4()
    row = db.one(
        conn,
        "INSERT INTO memory_ingests (id, project_id, space_id, kind, item_count, items_json, requested_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING requested_at",
        # The same instant the hourly window above was measured against.
        (ingest_id, project_id, target["id"], kind, len(items), Jsonb(items), now),
    )
    return Queued(ingest_id=ingest_id, space=space, kind=kind, item_count=len(items), requested_at=row["requested_at"])


def status(conn: psycopg.Connection, *, project_id: uuid.UUID, ingest_id: str) -> dict | None:
    """One request's state and per-item results, for its own project only. None when unknown."""
    try:
        parsed = uuid.UUID(ingest_id)
    except ValueError:
        return None
    row = db.one(
        conn,
        "SELECT i.id, s.name AS space, i.kind, i.state, i.item_count, i.written, i.failed, i.results_json, i.detail, "
        "       i.requested_at, i.started_at, i.completed_at "
        "  FROM memory_ingests i JOIN memory_spaces s ON s.id = i.space_id "
        " WHERE i.id = %s AND i.project_id = %s",
        (parsed, project_id),
    )
    if row is None:
        return None
    return {
        "id": str(row["id"]), "space": row["space"], "kind": row["kind"], "state": row["state"],
        "items": row["item_count"],
        "written": row["written"], "failed": row["failed"], "results": row["results_json"],
        "detail": row["detail"],
        **{k: row[k].isoformat() if row[k] else None for k in ("requested_at", "started_at", "completed_at")},
    }


__all__ = ["MAX_ITEMS", "MAX_TEXT_ITEMS", "IngestRefused", "Queued", "enqueue", "status", "validate"]
