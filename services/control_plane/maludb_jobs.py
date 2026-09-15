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

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements, memory_ingest, model_providers

KIND_ENABLE = "enable"
KIND_REFRESH = "refresh"
KIND_DISABLE = "disable"
# ADR-077 compartments slice 2b. Vector compartments' own opt-in (decision 6).
KIND_VECTORS_ENABLE = "vectors_enable"
KIND_VECTORS_DISABLE = "vectors_disable"
# ADR-079 memory slice 2a. One job reconciles every space of a project -- builds
# the pending ones and, since slice 2c, deletes the ones marked `deleting`; the
# spaces themselves are rows in `memory_spaces`, where the name is reserved.
KIND_MEMORY_SPACES = "memory_spaces"
# Kinds that draw on no hourly budget: the disables, which build nothing. Memory
# space jobs were unmetered while a space could only be created, because
# `memory_max_spaces` bounded that; deletion (slice 2c) made a create-delete cycle
# possible, and each turn is node-superuser work, so they count now. Only creation
# is ever refused for it: deleting one's own data is not rationed.
_UNMETERED_KINDS = (KIND_DISABLE, KIND_VECTORS_DISABLE)

# A space name is customer text that becomes part of an SQL identifier
# (`mem_<name>`), so it is held to a fixed pattern here and by a CHECK on the
# table. Short enough that the schema name stays far inside PostgreSQL's 63.
SPACE_NAME_RE = r"\A[a-z][a-z0-9_]{0,39}\Z"
SPACE_SCHEMA_PREFIX = "mem_"


def space_schema(name: str) -> str:
    """The tenant schema a space lives in. Only ever derived, never supplied."""
    if not re.match(SPACE_NAME_RE, name or ""):
        raise JobRefused(
            422, "a memory space name is 1 to 40 characters: a lower-case letter, then lower-case "
                 "letters, digits or underscores"
        )
    return SPACE_SCHEMA_PREFIX + name

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
    "SELECT pr.id, pr.status, pr.node_id, pr.maludb_datamodel_enabled, pr.maludb_vectors_enabled, "
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

    Enables and refreshes -- of the data-model graph and, since slice 2b, of
    vector compartments too. A vectors enablement does no copy, but it is
    superuser work a customer can ask for, and alternating enable and disable
    would otherwise make that work free to repeat without bound. So the budget is
    the project's MaluDB node work per hour, of which refreshes are most. Not
    disables, which build nothing.
    """
    return [
        r["requested_at"] for r in db.query(
            conn,
            "SELECT requested_at FROM maludb_jobs "
            " WHERE project_id = %s AND requested_at > %s AND NOT kind = ANY(%s) "
            "   AND (state = ANY(%s) OR (state = 'failed' AND refused)) "
            " ORDER BY requested_at",
            (project_id, now - LIMIT_WINDOW, list(_UNMETERED_KINDS), list(COUNTED_STATES)),
        )
    ]


def _within_limit(conn, project_id: uuid.UUID, *, allowed, now: datetime,
                  what: str = "data-model refresh(es)") -> None:
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
        f"this project's plan allows {limit} {what} an hour, and "
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


def request_vectors_enable(
    conn: psycopg.Connection, *, project_id: uuid.UUID, requested_by: uuid.UUID | None,
    now: datetime | None = None,
) -> Queued | None:
    """Queue turning vector compartments on. None when they are already on."""
    project = _project(conn, project_id, lock=True)
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    if not allowed.maludb_vectors:
        raise JobRefused(403, "this project's plan does not include MaluDB vector compartments")
    if project["status"] not in SERVING_STATUSES or project["node_id"] is None:
        raise JobRefused(409, "the project is not ready; try again once it is active")
    if project["maludb_vectors_enabled"]:
        return None
    pending = _pending(conn, project_id, KIND_VECTORS_ENABLE)
    if pending is not None:
        return Queued(pending["id"], KIND_VECTORS_ENABLE, pending["state"], pending["requested_at"],
                      coalesced=True)
    _within_limit(conn, project_id, allowed=allowed, now=now or datetime.now(UTC))
    return _insert(conn, project_id, KIND_VECTORS_ENABLE, requested_by)


def request_vectors_disable(
    conn: psycopg.Connection, *, project_id: uuid.UUID, requested_by: uuid.UUID | None
) -> Queued | None:
    """Queue turning vector compartments off. No entitlement check and no budget,
    for `request_disable`'s reasons; nothing is dropped."""
    project = _project(conn, project_id, lock=True)
    if project["status"] not in DISABLEABLE_STATUSES or project["node_id"] is None:
        raise JobRefused(409, "the project is not in a state that can be changed; try again shortly")
    pending = _pending(conn, project_id, KIND_VECTORS_DISABLE)
    if pending is not None:
        return Queued(pending["id"], KIND_VECTORS_DISABLE, pending["state"], pending["requested_at"],
                      coalesced=True)
    if not project["maludb_vectors_enabled"] and _pending(conn, project_id, KIND_VECTORS_ENABLE) is None:
        return None
    return _insert(conn, project_id, KIND_VECTORS_DISABLE, requested_by)


def request_memory_space(
    conn: psycopg.Connection, *, project_id: uuid.UUID, name: str, requested_by: uuid.UUID | None
) -> tuple[dict, Queued | None]:
    """Reserve a memory space's name and queue its build (ADR-079 decision 1).

    Returns the space row and the job, or `None` for the job when the space is
    already active. Under the project's row lock, so two requests cannot both take
    the plan's last space. A space that failed to build may be asked for again
    under the same name, and is set back to pending rather than duplicated.
    """
    schema = space_schema(name)
    project = _project(conn, project_id, lock=True)
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    if not allowed.maludb_memory:
        raise JobRefused(403, "this project's plan does not include MaluDB memory spaces")
    if project["status"] not in SERVING_STATUSES or project["node_id"] is None:
        raise JobRefused(409, "the project is not ready; try again once it is active")

    existing = db.one(conn, "SELECT id, state FROM memory_spaces WHERE project_id = %s AND name = %s",
                      (project_id, name))
    if existing is not None and existing["state"] == "active":
        return _space(conn, existing["id"]), None
    if existing is not None and existing["state"] == "deleting":
        raise JobRefused(409, f"memory space {name!r} is being deleted; ask again once it is gone")
    # Asked before anything is reserved, so a refusal leaves no row behind whatever
    # the caller does with the transaction.
    if _pending(conn, project_id, KIND_MEMORY_SPACES) is None:
        _within_limit(conn, project_id, allowed=allowed, now=datetime.now(UTC), what=_SPACE_WORK)
    if existing is None:
        # Pending and active spaces hold a slot; a failed one does not, since a
        # build the platform could not finish is not the customer's to pay for.
        held = db.one(conn, "SELECT count(*) AS n FROM memory_spaces WHERE project_id = %s "
                            "AND state IN ('pending', 'active', 'deleting')", (project_id,))["n"]
        if held >= allowed.memory_max_spaces:
            # Without the number, as the project cap does: naming the ceiling
            # tells a caller which plan would raise it.
            raise JobRefused(409, "this project has reached its plan's memory space limit")
        space_id = db.one(
            conn,
            "INSERT INTO memory_spaces (project_id, name, schema_name, requested_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (project_id, name, schema, requested_by),
        )["id"]
    else:
        held = db.one(conn, "SELECT count(*) AS n FROM memory_spaces WHERE project_id = %s "
                            "AND state IN ('pending', 'active', 'deleting')", (project_id,))["n"]
        if existing["state"] == "failed" and held >= allowed.memory_max_spaces:
            raise JobRefused(409, "this project has reached its plan's memory space limit")
        db.execute(conn, "UPDATE memory_spaces SET state = 'pending', detail = NULL, requested_at = now(), "
                         "requested_by = %s WHERE id = %s", (requested_by, existing["id"]))
        space_id = existing["id"]

    return _space(conn, space_id), _space_job(conn, project_id, allowed, requested_by)


_SPACE_WORK = "MaluDB node operations (data-model refreshes and memory space changes)"


def _space_job(conn, project_id: uuid.UUID, allowed, requested_by) -> Queued:
    """The project's pending space job, or a new one within the hourly budget."""
    pending = _pending(conn, project_id, KIND_MEMORY_SPACES)
    if pending is not None:
        return Queued(pending["id"], KIND_MEMORY_SPACES, pending["state"], pending["requested_at"], coalesced=True)
    _within_limit(conn, project_id, allowed=allowed, now=datetime.now(UTC), what=_SPACE_WORK)
    return _insert(conn, project_id, KIND_MEMORY_SPACES, requested_by)


def request_memory_space_deletion(
    conn: psycopg.Connection, *, project_id: uuid.UUID, name: str, requested_by: uuid.UUID | None
) -> tuple[dict | None, Queued | None]:
    """Mark a space `deleting` and queue the job that deletes it (ADR-079 memory slice 2c).

    Returns (space, job). A space whose build failed has nothing on the node -- its
    tenant transaction rolled back -- so its row is removed here and both are None.

    **Writes stop first, under admission's lock** (`memory_ingest.enqueue`), so no
    ingest is admitted between marking the space and failing what it had queued.
    Running ingests finish or fail on their own; the job's `DROP SCHEMA` waits for
    their transactions.

    No entitlement check and **no budget refusal**, as for disabling: a project whose
    plan lost memory, or which has spent its hour, must still be able to delete what
    it holds. The job still counts against the budget; the cycle a customer could
    repeat is bounded because every turn of it needs a creation, which is refused.
    """
    space_schema(name)
    project = _project(conn, project_id, lock=True)
    if project["status"] not in DISABLEABLE_STATUSES or project["node_id"] is None:
        raise JobRefused(409, "the project is not in a state that can be changed; try again shortly")
    db.execute(conn, "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
               (memory_ingest.INGEST_LOCK_NAMESPACE, str(project_id)))
    space = db.one(conn, "SELECT id, state FROM memory_spaces WHERE project_id = %s AND name = %s FOR UPDATE",
                   (project_id, name))
    if space is None:
        raise JobRefused(404, f"no memory space {name!r}")
    if space["state"] == "failed":
        db.execute(conn, "DELETE FROM memory_spaces WHERE id = %s", (space["id"],))
        return None, None
    if space["state"] != "deleting":
        db.execute(conn, "UPDATE memory_spaces SET state = 'deleting', detail = NULL WHERE id = %s", (space["id"],))
        db.execute(
            conn,
            "UPDATE memory_ingests SET state = 'failed', items_json = NULL, completed_at = now(), "
            "       detail = 'the memory space was deleted before this ingest ran' "
            " WHERE space_id = %s AND state = 'pending'",
            (space["id"],),
        )
    pending = _pending(conn, project_id, KIND_MEMORY_SPACES)
    if pending is not None:
        return _space(conn, space["id"]), Queued(pending["id"], KIND_MEMORY_SPACES, pending["state"],
                                                 pending["requested_at"], coalesced=True)
    return _space(conn, space["id"]), _insert(conn, project_id, KIND_MEMORY_SPACES, requested_by)


def _space(conn: psycopg.Connection, space_id: int) -> dict:
    return db.one(
        conn,
        "SELECT id, name, schema_name, state, requested_at, active_at, memory_schema_version, detail "
        "  FROM memory_spaces WHERE id = %s",
        (space_id,),
    )


def memory_spaces(conn: psycopg.Connection, *, project_id: uuid.UUID) -> dict:
    """A project's spaces and the plan's ceilings, from the control plane alone."""
    project = db.one(
        conn,
        "SELECT pl.code AS plan_code, pl.config_json FROM projects pr "
        "  LEFT JOIN plans pl ON pl.id = pr.plan_id WHERE pr.id = %s",
        (project_id,),
    )
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    return {
        "entitled": allowed.maludb_memory,
        "max_spaces": allowed.memory_max_spaces,
        "max_items": allowed.memory_max_items,
        "ingests_per_hour": allowed.memory_ingests_per_hour,
        "spaces": db.query(
            conn,
            "SELECT id, name, schema_name, state, requested_at, active_at, memory_schema_version, detail, "
            "       extraction_provider, extraction_model, embedding_provider, embedding_model, item_count "
            "  FROM memory_spaces WHERE project_id = %s ORDER BY name",
            (project_id,),
        ),
    }


AUDIT_SPACE_MODELS_SET = "maludb.memory.space_models_set"


def set_memory_models(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    name: str,
    extraction_provider: str,
    extraction_model: str | None,
    embedding_provider: str,
    embedding_model: str | None,
    actor_user_id: uuid.UUID | None,
) -> dict:
    """Name the models a space extracts and embeds text with (ADR-079, memory slice 5b).

    The model names are free-form, shape-checked, and only ever sent in a request
    body to a fixed provider host; there is no endpoint to set. **The embedding
    model is fixed once the space holds memories or has any ingest queued**, because
    search compares only vectors of one dimension and a space that mixed two would
    silently stop finding half of what it holds. The caller commits.
    """
    if extraction_provider not in model_providers.EXTRACTION_PROVIDERS:
        raise JobRefused(422, f"extraction_provider must be one of {', '.join(model_providers.EXTRACTION_PROVIDERS)}")
    if embedding_provider not in model_providers.EMBEDDING_PROVIDERS:
        raise JobRefused(422, f"embedding_provider must be one of {', '.join(model_providers.EMBEDDING_PROVIDERS)}")
    extraction_model = extraction_model or model_providers.DEFAULT_EXTRACTION_MODELS[extraction_provider]
    embedding_model = embedding_model or model_providers.DEFAULT_EMBEDDING_MODELS[embedding_provider]
    for field_name, value in (("extraction_model", extraction_model), ("embedding_model", embedding_model)):
        try:
            model_providers.checked_model(value)
        except ValueError as exc:
            raise JobRefused(422, f"{field_name}: {exc}") from None

    # Admission's lock (`memory_ingest.enqueue`): without it an ingest admitted between
    # the queue check below and this commit would be embedded with a model the space's
    # other memories were not.
    db.execute(conn, "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
               (memory_ingest.INGEST_LOCK_NAMESPACE, str(project_id)))
    space = db.one(
        conn,
        "SELECT id, state, item_count, embedding_provider, embedding_model FROM memory_spaces "
        " WHERE project_id = %s AND name = %s FOR UPDATE",
        (project_id, name),
    )
    if space is None:
        raise JobRefused(404, f"no memory space {name!r}")
    if space["state"] == "deleting":
        raise JobRefused(409, f"memory space {name!r} is being deleted")
    changing = (space["embedding_provider"], space["embedding_model"]) != (embedding_provider, embedding_model)
    if changing:
        busy = db.one(conn, "SELECT count(*) AS n FROM memory_ingests WHERE space_id = %s "
                            "   AND state IN ('pending', 'running')", (space["id"],))["n"]
        if space["item_count"] or busy:
            raise JobRefused(409, "the embedding model cannot change once a memory space holds memories or has "
                                  "ingests queued: search compares only vectors from one model")
    row = db.one(
        conn,
        "UPDATE memory_spaces SET extraction_provider = %s, extraction_model = %s, embedding_provider = %s, "
        "       embedding_model = %s WHERE id = %s "
        "RETURNING name, extraction_provider, extraction_model, embedding_provider, embedding_model",
        (extraction_provider, extraction_model, embedding_provider, embedding_model, space["id"]),
    )
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, detail_json) "
        "VALUES (%s, %s, %s, %s, %s)",
        (project_id, "user" if actor_user_id else "system", actor_user_id, AUDIT_SPACE_MODELS_SET,
         Jsonb({"space": name, "extraction_provider": extraction_provider, "extraction_model": extraction_model,
                "embedding_provider": embedding_provider, "embedding_model": embedding_model})),
    )
    return row

def vectors_status(conn: psycopg.Connection, *, project_id: uuid.UUID) -> dict:
    """What a customer can know about vector compartments without reaching the node.

    The limits are the plan's; how many vectors are stored lives in the tenant
    database, which this application cannot reach (ADR-038) -- `vector_compartments()`
    answers that from the project's own Data API.
    """
    project = db.one(
        conn,
        "SELECT pr.maludb_vectors_enabled, pr.maludb_vectors_enabled_at, pl.code AS plan_code, "
        "       pl.config_json FROM projects pr LEFT JOIN plans pl ON pl.id = pr.plan_id WHERE pr.id = %s",
        (project_id,),
    )
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    latest = {
        r["kind"]: r for r in db.query(
            conn,
            "SELECT DISTINCT ON (kind) id, kind, state, detail, result_json, requested_at, "
            "       started_at, completed_at "
            "  FROM maludb_jobs WHERE project_id = %s AND kind = ANY(%s) ORDER BY kind, requested_at DESC",
            (project_id, [KIND_VECTORS_ENABLE, KIND_VECTORS_DISABLE]),
        )
    }
    return {
        "entitled": allowed.maludb_vectors,
        "enabled": bool(project["maludb_vectors_enabled"]),
        "enabled_at": project["maludb_vectors_enabled_at"],
        "max_vectors": allowed.vector_max_count,
        "max_dimensions": allowed.vector_max_dimension,
        "max_compartments": allowed.vector_max_compartments,
        "latest_enable": latest.get(KIND_VECTORS_ENABLE),
        "latest_disable": latest.get(KIND_VECTORS_DISABLE),
    }


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
    "KIND_MEMORY_SPACES",
    "SPACE_SCHEMA_PREFIX",
    "memory_spaces",
    "request_memory_space",
    "space_schema",
    "KIND_DISABLE",
    "KIND_ENABLE",
    "KIND_REFRESH",
    "KIND_VECTORS_DISABLE",
    "KIND_VECTORS_ENABLE",
    "JobRefused",
    "Queued",
    "claim",
    "finish",
    "refreshes_counted",
    "request_disable",
    "request_enable",
    "request_refresh",
    "request_vectors_disable",
    "request_vectors_enable",
    "status",
    "vectors_status",
]
