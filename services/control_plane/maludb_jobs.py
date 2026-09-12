"""The queue between a customer's request and the data-model graph (ADR-074).

Phase 12 slice 4. A customer asks to enable or refresh the graph; the work runs
as the node superuser, which ADR-038 keeps out of the internet-facing
application. So asking writes a row here, and the provisioner claims it.

**This module reads and writes the control plane and nothing else**, because the
public routes import it and `tests/test_control_plane_surfaces.py` walks what
they can reach. The node work is `maludb.enable` and `maludb.refresh`, called by
the provisioner; nothing here imports them.

## What a request can cost, decided before it is queued

- **The plan's limit is enforced here, at enqueue**, as a refusal naming the
  limit and when the next request is allowed. Not as a queued request that
  silently never runs: a customer who is refused should know it now. **Enabling
  draws on the same budget as refreshing** -- it does all of a refresh's work
  and more.
- **What counts.** Jobs waiting, running or done, and jobs the platform
  *refused*. Not jobs that broke: charging a customer for the platform's failure
  makes it their problem. But a refusal -- a squatted schema, say -- is something
  a customer can cause on purpose, after superuser work has started, and
  exempting it would make that work free to repeat without limit.
- **Coalescing.** A request for a project that already has a *pending* job of the
  same kind joins it and is not counted. A job already *running* does not absorb
  a new request, because it may have read the catalogue before the migration the
  customer is refreshing for.
- **Enabling an enabled project queues nothing.** Enablement takes a full copy,
  so re-enabling on request would be an unmetered refresh.

The count and the insert happen under a row lock on the project, so two requests
racing for a project's last refresh of the hour cannot both have it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements

KIND_ENABLE = "enable"
KIND_REFRESH = "refresh"
KIND_DISABLE = "disable"

# Disabling needs the database to exist, and is allowed in more states than
# enabling: a paused or suspended project is exactly one whose structure an
# operator may want off the Data API. Mirrors `maludb.DISABLEABLE_STATUSES`,
# which this module cannot import (the public routes import this one).
DISABLEABLE_STATUSES = ("PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED")

# States a project must be in for its database to be worked on.
SERVING_STATUSES = ("PROVISIONED", "ACTIVE")

# How far back the per-hour limit looks.
LIMIT_WINDOW = timedelta(hours=1)

# States that count against the limit, plus any failed job the platform
# *refused* (`refused` is true). A job that broke does not count: charging the
# customer for the platform's failure would make it their problem.
COUNTED_STATES = ("pending", "running", "succeeded")

# A job the provisioner started and never finished -- a crash, a restart --
# is failed after this long, so it stops appearing to run forever. Generous: a
# first copy of a 300-table schema measured ~3.5 s.
ABANDONED_AFTER = timedelta(minutes=15)


class JobRefused(RuntimeError):
    """A request the platform will not queue. `status` is the HTTP answer."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class Queued:
    job_id: int
    kind: str
    state: str
    requested_at: datetime
    coalesced: bool


_PROJECT_SQL = (
    "SELECT pr.id, pr.status, pr.node_id, pr.maludb_datamodel_enabled, "
    "       pl.code AS plan_code, pl.config_json "
    "  FROM projects pr LEFT JOIN plans pl ON pl.id = pr.plan_id "
    " WHERE pr.id = %s AND pr.deleted_at IS NULL"
)
_PROJECT_FOR_UPDATE_SQL = _PROJECT_SQL + " FOR UPDATE OF pr"


def _project(conn: psycopg.Connection, project_id: uuid.UUID, *, lock: bool) -> dict:
    row = db.one(conn, _PROJECT_FOR_UPDATE_SQL if lock else _PROJECT_SQL, (project_id,))
    if row is None:
        raise JobRefused(404, "project not found")
    return row


def _entitled(project: dict) -> entitlements.Entitlements:
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    if not allowed.maludb_datamodel:
        raise JobRefused(403, "this project's plan does not include the MaluDB data-model graph")
    if project["status"] not in SERVING_STATUSES or project["node_id"] is None:
        raise JobRefused(409, "the project is not ready; try again once it is active")
    return allowed


def _pending(conn: psycopg.Connection, project_id: uuid.UUID, kind: str) -> dict | None:
    return db.one(
        conn,
        "SELECT id, state, requested_at FROM maludb_jobs "
        "WHERE project_id = %s AND kind = %s AND state = 'pending'",
        (project_id, kind),
    )


def _insert(conn, project_id: uuid.UUID, kind: str, requested_by: uuid.UUID | None) -> Queued:
    row = db.one(
        conn,
        "INSERT INTO maludb_jobs (project_id, kind, requested_by) VALUES (%s, %s, %s) "
        "RETURNING id, state, requested_at",
        (project_id, kind, requested_by),
    )
    return Queued(row["id"], kind, row["state"], row["requested_at"], coalesced=False)


def request_enable(
    conn: psycopg.Connection, *, project_id: uuid.UUID, requested_by: uuid.UUID | None
) -> Queued | None:
    """Queue enablement. None when the project is already enabled."""
    project = _project(conn, project_id, lock=True)
    allowed = _entitled(project)
    if project["maludb_datamodel_enabled"]:
        return None
    pending = _pending(conn, project_id, KIND_ENABLE)
    if pending is not None:
        return Queued(pending["id"], KIND_ENABLE, pending["state"], pending["requested_at"],
                      coalesced=True)
    _within_limit(conn, project_id, allowed=allowed, now=datetime.now(UTC))
    return _insert(conn, project_id, KIND_ENABLE, requested_by)


def request_disable(
    conn: psycopg.Connection, *, project_id: uuid.UUID, requested_by: uuid.UUID | None
) -> Queued | None:
    """Queue turning the surface off. None when it is already off and nothing waits.

    **No entitlement check and no budget.** A project whose plan lost the feature
    must still be able to switch it off -- refusing would leave its structure
    published because of a billing change. And withdrawing is cheap, while
    switching on and off repeatedly is already bounded: enabling draws on the
    budget.
    """
    project = _project(conn, project_id, lock=True)
    if project["status"] not in DISABLEABLE_STATUSES or project["node_id"] is None:
        raise JobRefused(409, "the project is not in a state that can be changed; try again shortly")
    pending = _pending(conn, project_id, KIND_DISABLE)
    if pending is not None:
        return Queued(pending["id"], KIND_DISABLE, pending["state"], pending["requested_at"],
                      coalesced=True)
    # Off, and no enablement waiting to turn it back on: nothing to do. A pending
    # enable means the customer changed their mind, so the disable is queued
    # behind it and the later request wins.
    if not project["maludb_datamodel_enabled"] and _pending(conn, project_id, KIND_ENABLE) is None:
        return None
    return _insert(conn, project_id, KIND_DISABLE, requested_by)


def refreshes_counted(conn: psycopg.Connection, project_id: uuid.UUID, *, now: datetime) -> list:
    """The jobs in the trailing window that count against the limit, oldest first.

    Enables and refreshes. Not disables, which do no copy work.
    """
    return [
        r["requested_at"] for r in db.query(
            conn,
            "SELECT requested_at FROM maludb_jobs "
            " WHERE project_id = %s AND requested_at > %s AND kind <> 'disable' "
            "   AND (state = ANY(%s) OR (state = 'failed' AND refused)) "
            " ORDER BY requested_at",
            (project_id, now - LIMIT_WINDOW, list(COUNTED_STATES)),
        )
    ]


def _within_limit(conn, project_id: uuid.UUID, *, allowed, now: datetime) -> None:
    limit = allowed.datamodel_refreshes_per_hour
    counted = refreshes_counted(conn, project_id, now=now)
    if len(counted) < limit:
        return
    # When the oldest counted job leaves the window, one is available. A limit
    # of zero never frees one, and "in 0 seconds" would be a lie, so that case
    # names the plan instead of a time.
    if limit <= 0:
        raise JobRefused(429, "this project's plan allows no requested data-model refreshes")
    opens_at = counted[len(counted) - limit] + LIMIT_WINDOW
    retry_after = max(1, int((opens_at - now).total_seconds()) + 1)
    raise JobRefused(
        429,
        f"this project's plan allows {limit} data-model refresh(es) an hour, and "
        f"{len(counted)} have been requested in the last hour; the next is allowed in "
        f"{retry_after} seconds",
        retry_after=retry_after,
    )


def request_refresh(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    now: datetime | None = None,
) -> Queued:
    """Queue a refresh, join a pending one, or refuse with the plan's limit."""
    now = now or datetime.now(UTC)
    project = _project(conn, project_id, lock=True)
    allowed = _entitled(project)
    if not project["maludb_datamodel_enabled"]:
        raise JobRefused(409, "the MaluDB data-model graph is not enabled for this project; "
                              "enable it first")

    pending = _pending(conn, project_id, KIND_REFRESH)
    if pending is not None:
        return Queued(pending["id"], KIND_REFRESH, pending["state"], pending["requested_at"],
                      coalesced=True)

    _within_limit(conn, project_id, allowed=allowed, now=now)
    return _insert(conn, project_id, KIND_REFRESH, requested_by)


# --------------------------------------------------------------------------
# The worker's side, still control-plane only


def claim(conn: psycopg.Connection) -> dict | None:
    """Take the job that has waited longest, marking it running.

    `FOR UPDATE SKIP LOCKED`, the provisioner's own rule for a second worker. A
    job left running by a worker that died is failed first, so it stops looking
    live -- and, being failed, stops counting against the customer's limit.
    """
    db.execute(
        conn,
        "UPDATE maludb_jobs SET state = 'failed', completed_at = now(), "
        "       detail = 'the platform stopped before finishing this request; ask again' "
        " WHERE state = 'running' AND started_at < now() - %s",
        (ABANDONED_AFTER,),
    )
    row = db.one(
        conn,
        """
        SELECT j.id, j.project_id, j.kind, p.node_id, p.project_ref
          FROM maludb_jobs j JOIN projects p ON p.id = j.project_id
         WHERE j.state = 'pending' AND p.deleted_at IS NULL AND p.node_id IS NOT NULL
         ORDER BY j.requested_at
         LIMIT 1
           FOR UPDATE OF j SKIP LOCKED
        """,
    )
    if row is None:
        return None
    db.execute(conn, "UPDATE maludb_jobs SET state = 'running', started_at = now() WHERE id = %s",
               (row["id"],))
    return row


def finish(conn: psycopg.Connection, job_id: int, *, succeeded: bool, detail: str | None = None,
           result: dict | None = None, refused: bool = False) -> None:
    db.execute(
        conn,
        "UPDATE maludb_jobs SET state = %s, detail = %s, result_json = %s, refused = %s, "
        "       completed_at = now() WHERE id = %s",
        ("succeeded" if succeeded else "failed", detail, Jsonb(result or {}),
         refused and not succeeded, job_id),
    )


# --------------------------------------------------------------------------
# What a customer can see


def status(conn: psycopg.Connection, *, project_id: uuid.UUID, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    project = db.one(
        conn,
        "SELECT pr.maludb_datamodel_enabled, pr.maludb_datamodel_enabled_at, "
        "       pr.maludb_memory_schema_version, pl.code AS plan_code, pl.config_json "
        "  FROM projects pr LEFT JOIN plans pl ON pl.id = pr.plan_id WHERE pr.id = %s",
        (project_id,),
    )
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    latest = {
        r["kind"]: r for r in db.query(
            conn,
            "SELECT DISTINCT ON (kind) id, kind, state, detail, result_json, requested_at, "
            "       started_at, completed_at "
            "  FROM maludb_jobs WHERE project_id = %s ORDER BY kind, requested_at DESC",
            (project_id,),
        )
    }
    return {
        "entitled": allowed.maludb_datamodel,
        "enabled": bool(project["maludb_datamodel_enabled"]),
        "enabled_at": project["maludb_datamodel_enabled_at"],
        "memory_schema_version": project["maludb_memory_schema_version"],
        "refreshes_per_hour": allowed.datamodel_refreshes_per_hour,
        "refreshes_in_last_hour": len(refreshes_counted(conn, project_id, now=now)),
        "latest_enable": latest.get(KIND_ENABLE),
        "latest_refresh": latest.get(KIND_REFRESH),
        "latest_disable": latest.get(KIND_DISABLE),
    }


__all__ = [
    "KIND_DISABLE",
    "KIND_ENABLE",
    "KIND_REFRESH",
    "JobRefused",
    "Queued",
    "claim",
    "finish",
    "refreshes_counted",
    "request_disable",
    "request_enable",
    "request_refresh",
    "status",
]
