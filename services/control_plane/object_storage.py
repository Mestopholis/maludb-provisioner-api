"""Object-storage accounting: bytes held, bytes served, and the two ceilings.

**Object** storage. `services/control_plane/storage.py` is *database* storage —
`pg_database_size`, the quota state machine, and the ADR-040 write restriction.
The two share a word and nothing else, and Phase 10's plan split the modules
before either existed for exactly this reason. Nothing here is called `measure`,
`restrict` or `release` without a qualifier.

ADR-056 puts both resources on every tier including free, as **hard ceilings
under ADR-050**: refused at the point of use, never converted into a charge,
never reported to any payment provider. So this module measures and decides; it
never accumulates anything an invoice reads.

## Why there is no `restrict` here

`storage.py` enforces by revoking `INSERT` and `UPDATE` inside the tenant,
because a database grows through connections the platform does not mediate.
Object bytes are different: **every one of them arrives through the Storage
API**, which is the gateway. So the ceiling is enforced where the request is,
and the tenant database is untouched.

That makes this module's job smaller and its honesty requirement larger. It
publishes a state and a decision; slice 4's gateway is what refuses. A reader
should not be able to mistake a recorded `exceeded` for an applied control —
the state means "the next upload should be refused", not "uploads are now
impossible".

## The two resources are counted differently, on purpose

- **Bytes held** are *measured*, by a maintenance pass reading the tenant's own
  `storage.objects` metadata. Polling is right for a quantity that is a
  property of the world rather than of a request: it is self-correcting, a
  missed pass costs accuracy rather than truth, and it needs no hook on any hot
  path.
- **Bytes served** are *counted*, as they pass, because there is nowhere to
  read them back from afterwards. That puts them on ADR-026's measured gateway
  path, so `record_egress` takes a total rather than a single response: the
  caller accumulates in process and flushes, which is what ADR-030 already does
  for rate and concurrency limits.

## What "measured" means here, and what it does not

The figure comes from `storage.objects.metadata->>'size'` — the tenant's own
record of what it stored, which is also what upstream's
`storage.get_size_by_bucket()` reads. It is **not** a query against the object
store, and the two can drift: an upload that wrote bytes and then failed to
commit its row leaves an object nobody is billed for and nobody can reach.

That drift is a Phase 11 subject (reconciliation, orphan collection), and it is
named here rather than left for someone to discover in a support ticket. The
direction of the error is the tolerable one: the platform under-counts rather
than over-charging a customer for bytes their metadata does not show.

**The column is also customer-writable, and that is the sharper problem.**
`service_role` holds `ALL` on `storage.objects` and carries `BYPASSRLS`, and
`api/tenant_access.py` records that a request on an impersonating connection can
`SET ROLE service_role` in one line of its own SQL — a surface ADR-039 puts on
every tier. So one `UPDATE` sets every recorded size to zero and this module
measures zero. `anon` and `authenticated` cannot: same grants, no `BYPASSRLS`,
and RLS with no policy stops them.

Unlike ADR-040's equivalent admission this does **not** self-correct: that one
is a loop a customer has to keep running, and re-measuring here re-reads the
same forged column forever. The figure below is therefore the tenant's claim
about itself — good enough to bound an honest project, not a control against a
determined one. A measurement taken from the object store is what closes it, and
slice 3 is the first slice with an endpoint to ask.
`docs/RESOURCE-GOVERNANCE.md` carries the same warning where an operator will
find it.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from services.control_plane import db, entitlements, models

log = logging.getLogger(__name__)

# Where a project is told it is running out, while it can still do something
# about it. The same proportion for everyone, for `storage.WARNING_FRACTION`'s
# reason: a plan that set it to 100% would make the warning arrive with the
# refusal.
WARNING_FRACTION = 0.8

OK = "ok"
WARNING = "warning"
EXCEEDED = "exceeded"


class ObjectStorageError(RuntimeError):
    """Object storage could not be measured."""


@dataclass(frozen=True)
class Usage:
    """What a project is using of one ceiling, and what that means."""

    used_bytes: int
    quota_bytes: int
    state: str

    @property
    def fraction(self) -> float:
        if self.quota_bytes <= 0:
            return 0.0
        return self.used_bytes / self.quota_bytes

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.quota_bytes - self.used_bytes)


def classify(used_bytes: int, quota_bytes: int) -> Usage:
    """Turn a byte count into a state.

    A non-positive quota is treated as **exceeded**, not as unlimited. That is
    `entitlements`' rule applied one layer out: an operator-supplied plan whose
    limit is missing or nonsensical must not resolve to no limit. `UNLIMITED`
    exists in that module for the PostgreSQL timeouts, whose zero means
    something to PostgreSQL; it means nothing here, and a zero-byte ceiling
    read as infinite is the failure that costs money.
    """
    used = max(0, int(used_bytes))
    if quota_bytes <= 0:
        return Usage(used_bytes=used, quota_bytes=0, state=EXCEEDED)
    if used >= quota_bytes:
        state = EXCEEDED
    elif used >= quota_bytes * WARNING_FRACTION:
        state = WARNING
    else:
        state = OK
    return Usage(used_bytes=used, quota_bytes=int(quota_bytes), state=state)


# -- the object store itself -----------------------------------------------
#
# Two calls, and no more: list a project's objects, and delete them. ADR-055
# makes S3 the provider boundary, so this is a client rather than a driver --
# changing provider is changing an endpoint, not rewriting this section.


def _client(config):
    """An S3 client for the platform bucket, or None on an unprepared node.

    None rather than an exception: a deployment with no object store configured
    is a deployment where Storage is not in use, and neither measuring nor
    cleaning up should fail for the whole fleet because of it.
    """
    if not (config.storage_s3_endpoint and config.storage_s3_access_key):
        return None
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=config.storage_s3_endpoint,
        aws_access_key_id=config.storage_s3_access_key,
        aws_secret_access_key=config.storage_s3_secret_key,
        region_name=config.storage_s3_region,
        # Path style for the reason `storage-api` sets
        # STORAGE_S3_FORCE_PATH_STYLE: a self-hosted store reached by address
        # has no per-bucket DNS to resolve.
        config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def project_prefix(project_ref: str) -> str:
    """Where one project's objects live in the platform bucket (ADR-057).

    The trailing slash, and the validation, are both load-bearing and it is
    worth being precise about which does what. Project refs are a **fixed eight
    characters** (`models.PROJECT_REF_LENGTH`), so no valid ref can be a prefix
    of another and the slash is not what saves this from deleting a neighbour's
    objects -- the fixed length is. The slash is what keeps that true if the ref
    format ever gains a variable length, and the validation is what stops a
    caller passing something that is not a ref at all into a prefix that selects
    what gets deleted.
    """
    if not models.is_valid_project_ref(project_ref):
        raise ObjectStorageError(f"invalid project ref {project_ref!r}")
    return f"{project_ref}/"


def measure_store_bytes(config, project_ref: str) -> int | None:
    """What this project's objects weigh, according to the object store.

    **This is the figure a customer cannot write**, and that is the whole point
    of it. The metadata sum below is the tenant's own claim about itself, and
    slice 2 measured that a customer reaching `service_role` can rewrite it --
    one `UPDATE` takes a 900 MB project to a measured zero, and re-measuring
    re-reads the same forged column forever. The object store has no such
    surface: nothing a customer can reach writes these sizes.

    Returns None where the node has no object store configured, which is not an
    error -- see `_client`.
    """
    client = _client(config)
    if client is None:
        return None

    total = 0
    token = None
    prefix = project_prefix(project_ref)
    while True:
        kwargs = {"Bucket": config.storage_s3_bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        total += sum(int(item["Size"]) for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return total
        token = page["NextContinuationToken"]


def delete_project_objects(config, project_ref: str) -> int:
    """Remove every object belonging to one project. Returns how many.

    Called when a project is cleaned up. Objects live outside the tenant
    database and outside its roles, so dropping both leaves the bytes behind --
    a data-retention problem first and a cost problem second, and one that grows
    silently because nothing else looks.

    Batched, because one request per object makes deleting a large project a
    long loop that can fail halfway and leave no record of how far it got.
    """
    client = _client(config)
    if client is None:
        return 0

    prefix = project_prefix(project_ref)
    removed = 0
    while True:
        page = client.list_objects_v2(
            Bucket=config.storage_s3_bucket, Prefix=prefix, MaxKeys=1000
        )
        contents = page.get("Contents", [])
        if not contents:
            return removed
        result = client.delete_objects(
            Bucket=config.storage_s3_bucket,
            Delete={"Objects": [{"Key": item["Key"]} for item in contents], "Quiet": True},
        )
        errors = result.get("Errors") or []
        if errors:
            # Named without the keys: an object key is customer-authored and
            # this text reaches a log.
            raise ObjectStorageError(
                f"the object store refused to delete {len(errors)} of {len(contents)} objects"
            )
        removed += len(contents)
        # A page that was not truncated and deleted cleanly is the end. Looping
        # again would cost one empty listing; stopping on the flag risks leaving
        # objects behind if the store paginates differently, so this stops on
        # the listing coming back empty instead -- checked at the top.


# -- bytes held ------------------------------------------------------------


def measure_objects(tenant_conn: psycopg.Connection) -> int:
    """What the project's objects weigh, from its own metadata.

    Returns 0 for a tenant whose `storage.objects` does not exist yet. That is
    every project until the storage worker first serves it (slice 3), and it is
    not an error: a project that has never used Storage is using none of it.
    Raising instead would make the maintenance pass fail loudly for the entire
    fleet between this slice and the one that creates the table.

    The sum casts to **bigint**, and deliberately does not call upstream's
    `storage.get_size_by_bucket()`, which casts each row to `int` — a 4-byte
    integer. A single object over 2 GiB overflows that cast and takes the whole
    aggregate down with it, which would turn one large file into a fleet-wide
    measurement failure. Upstream reads the same column; only the cast differs.
    """
    with tenant_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT to_regclass('storage.objects') IS NOT NULL AS present")
        if not cur.fetchone()["present"]:
            return 0
        cur.execute(
            "SELECT coalesce(sum((metadata->>'size')::bigint), 0) AS bytes FROM storage.objects"
        )
        row = cur.fetchone()
    return int(row["bytes"] or 0)


def _audit(
    conn: psycopg.Connection, project_id: uuid.UUID, event_type: str, usage: Usage
) -> None:
    """A refused upload is customer-visible and needs an answer to "why".

    `docs/ACCOUNTS.md` requires actions to be attributable; this one has no
    human actor, so it is recorded as `system` with the numbers that produced
    the decision.
    """
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
        "VALUES (%s, 'system', %s, %s)",
        (
            project_id,
            event_type,
            psycopg.types.json.Jsonb(
                {
                    "used_bytes": usage.used_bytes,
                    "quota_bytes": usage.quota_bytes,
                    "fraction": round(usage.fraction, 4),
                }
            ),
        ),
    )


def evaluate(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
    config=None,
) -> Usage:
    """Measure one project's object bytes, record them, and classify.

    **Measured from the object store where one is configured, and from the
    tenant's metadata only as a fallback.** That is slice 2's finding closed:
    the metadata figure is the tenant's claim about itself, and a customer who
    can reach `service_role` can rewrite it -- one `UPDATE` takes a 900 MB
    project to a measured zero. Re-measuring does not correct that, because it
    re-reads the same forged column. The store's own accounting has no surface a
    customer can reach, so where the two disagree the store wins.

    The fallback is not a weakening. A node with no object store configured has
    no Storage traffic to account for, and the metadata sum is then both the
    only figure available and, on such a node, uncontested.

    Re-reads the entitlement every time rather than trusting a stored ceiling,
    which is the whole point of the pass and the lesson Phase 09 opened with: an
    entitlement applied once at provisioning is one a plan change never reaches.
    A project that upgrades stops being `exceeded` on the next pass with nothing
    else done to it, and `tests/test_object_storage_accounting.py` asserts that
    rather than assuming it.

    The audit event and the timestamp are written on the *transition* only, so a
    pass running every few minutes does not write one every few minutes. Nothing
    is applied to the tenant, so unlike `storage.evaluate` there is no revoke to
    re-issue on every pass — the enforcement point reads the state per request.
    """
    project = db.one(
        conn,
        "SELECT project_ref, database_name, object_storage_state FROM projects "
        " WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None:
        raise ObjectStorageError("project does not exist")
    if project["database_name"] is None:
        raise ObjectStorageError("project has no database to measure")

    quota = entitlements.for_project(conn, project_id).object_storage_bytes

    used = None
    if config is not None:
        try:
            used = measure_store_bytes(config, project["project_ref"])
        except Exception as exc:  # noqa: BLE001 - an unreachable store must not
            # stop the pass, and must not silently become a zero either: falling
            # through to the metadata figure is the honest answer, and it is the
            # figure this project had before an object store existed.
            log.warning(
                "project %s: could not measure the object store (%s); falling back to metadata",
                project["project_ref"], type(exc).__name__,
            )
            used = None

    if used is None:
        with tenant_connect(project["database_name"]) as tenant_conn:
            used = measure_objects(tenant_conn)
    usage = classify(used, quota)

    db.execute(
        conn,
        "UPDATE projects SET object_bytes = %s, object_measured_at = now(), "
        "object_storage_state = %s WHERE id = %s",
        (usage.used_bytes, usage.state, project_id),
    )

    previous = project["object_storage_state"]
    if usage.state != previous:
        if usage.state == EXCEEDED:
            db.execute(
                conn,
                "UPDATE projects SET object_exceeded_at = now() WHERE id = %s",
                (project_id,),
            )
            _audit(conn, project_id, "object_storage.exceeded", usage)
            log.warning(
                "project %s over its object storage quota: uploads will be refused",
                project["project_ref"],
            )
        else:
            if previous == EXCEEDED:
                db.execute(
                    conn,
                    "UPDATE projects SET object_exceeded_at = NULL WHERE id = %s",
                    (project_id,),
                )
                _audit(conn, project_id, "object_storage.released", usage)
                log.info(
                    "project %s back under its object storage quota", project["project_ref"]
                )
            elif usage.state == WARNING:
                _audit(conn, project_id, "object_storage.warning", usage)

    conn.commit()
    return usage


def due_for_measurement(conn: psycopg.Connection, *, limit: int = 50) -> list[dict]:
    """Projects an object-storage pass should look at, least recently first.

    Ordered by measurement age for `storage.due_for_measurement`'s reason: by
    project id, a pass with a limit would re-measure the same head of the list
    forever while the tail went years without being looked at.
    """
    return db.query(
        conn,
        """
        SELECT id, project_ref, node_id, database_name, object_measured_at
          FROM projects
         WHERE database_name IS NOT NULL AND deleted_at IS NULL
           AND status IN ('PROVISIONED', 'ACTIVE')
         ORDER BY object_measured_at NULLS FIRST
         LIMIT %s
        """,
        (limit,),
    )


# -- bytes served ----------------------------------------------------------


def period_start(moment: dt.datetime | None = None) -> dt.date:
    """The first day of the UTC month `moment` falls in.

    UTC calendar month rather than the subscription's billing period, and
    migration 0024 records why: a free project has no subscription and ADR-056
    puts this ceiling on free, so aligning to a billing period would mean
    inventing one. The ceiling is not a charge either (ADR-050), so there is
    nothing for it to line up with.

    Naive datetimes are treated as UTC rather than rejected. Every caller in
    this repository passes an aware one or nothing at all; a `ValueError` here
    would turn a mis-typed timestamp into a refused download.
    """
    now = moment or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    now = now.astimezone(dt.UTC)
    return dt.date(now.year, now.month, 1)


def next_period_start(moment: dt.datetime | None = None) -> dt.date:
    """The first day of the month after the one `moment` falls in.

    When an exhausted egress ceiling stops being exhausted. It lives beside
    `period_start` rather than in the gateway because the two have to agree
    about what a period is -- a `Retry-After` computed against a different
    calendar than the counter resets on is a client told to come back too early,
    forever.
    """
    start = period_start(moment)
    if start.month == 12:
        return dt.date(start.year + 1, 1, 1)
    return dt.date(start.year, start.month + 1, 1)


def seconds_until_next_period(moment: dt.datetime | None = None) -> int:
    """How long until the egress counter resets. At least one second.

    Never zero: a `Retry-After: 0` invites an immediate retry, and a client that
    takes it spins against the refusal until the clock moves.
    """
    now = moment or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    resets = dt.datetime.combine(next_period_start(now), dt.time.min, tzinfo=dt.UTC)
    return max(1, int((resets - now.astimezone(dt.UTC)).total_seconds()))


def record_egress(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    bytes_served: int,
    moment: dt.datetime | None = None,
) -> int:
    """Add to this project's egress for the current month. Returns the new total.

    **Additive, and takes a total rather than one response.** The caller is the
    gateway, on the path ADR-026 published a throughput number for, so a write
    per request is not available: slice 4 accumulates in process and flushes,
    the way ADR-030's limiters already work. This signature is what makes that
    possible, and a caller that passes one response's bytes is simply flushing a
    batch of one.

    A non-positive value is a no-op rather than a subtraction. Egress does not
    un-happen, and the `bytes >= 0` constraint on the table exists so a bug that
    tried could not hand a project unlimited egress for the rest of the month.
    """
    if bytes_served <= 0:
        return egress_used(conn, project_id=project_id, moment=moment)

    row = db.one(
        conn,
        """
        INSERT INTO project_egress (project_id, period_start, bytes)
             VALUES (%s, %s, %s)
        ON CONFLICT (project_id, period_start) DO UPDATE
                SET bytes = project_egress.bytes + EXCLUDED.bytes,
                    updated_at = now()
          RETURNING bytes
        """,
        (project_id, period_start(moment), int(bytes_served)),
    )
    return int(row["bytes"])


def egress_used(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    moment: dt.datetime | None = None,
) -> int:
    """Bytes served for this project in the current month. Zero if none."""
    row = db.one(
        conn,
        "SELECT bytes FROM project_egress WHERE project_id = %s AND period_start = %s",
        (project_id, period_start(moment)),
    )
    return int(row["bytes"]) if row else 0


def egress_usage(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    moment: dt.datetime | None = None,
) -> Usage:
    """This month's egress against the plan's ceiling.

    Reads the entitlement on every call for `evaluate`'s reason: the ceiling a
    request is judged against must be the plan the project is on now, not the
    plan it was on when the month started. A project that upgrades mid-month
    gets the larger ceiling immediately and keeps the bytes it has already
    served, which is the only arrangement that does not either punish an
    upgrade or reward a downgrade.
    """
    quota = entitlements.for_project(conn, project_id).egress_bytes_per_month
    return classify(egress_used(conn, project_id=project_id, moment=moment), quota)


def egress_history(
    conn: psycopg.Connection, *, project_id: uuid.UUID, months: int = 12
) -> list[dict]:
    """Recent months, newest first. What a per-period row buys over a counter
    that resets: an answer to "what did this project serve last month"."""
    return db.query(
        conn,
        "SELECT period_start, bytes, updated_at FROM project_egress "
        " WHERE project_id = %s ORDER BY period_start DESC LIMIT %s",
        (project_id, months),
    )
