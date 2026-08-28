"""Objects: what the metadata claims, what the store holds, and the gap.

Phase 11 slice 4. Two data sets make up a project — the tenant database and the
bytes in the shared platform bucket (ADR-057) — and until this module nothing
compared them. `docs/RESOURCE-GOVERNANCE.md` has booked the comparison as Phase
11 work since Phase 05, and slice 3 made it urgent rather than tidy: a
point-in-time restore returns `storage.objects` rows to a moment in the past
while the bucket stays present-day, so the two sets are *guaranteed* to disagree
after the one operation this phase exists to provide.

## The join, measured rather than assumed

An object is one row and exactly one key:

```
<project_ref>/<bucket_id>/<name>/<version>
```

`version` is the row's own column, so the mapping is one-to-one and the
comparison is a set difference on an exact key. That is not obvious and was
checked rather than read: an overwrite **replaces** the key -- upstream deletes
the previous version's bytes and writes a new version UUID -- and a delete
removes both sides. So there is no third population of superseded versions
sitting in the bucket, and a pass that assumed there was would have reported
every overwritten object on the platform as an orphan.

## Three populations, and only two of them are drift

**Dangling rows.** A row whose key is absent. The project lists a file that
cannot be downloaded. This is what a restore to a point in time produces for
every object uploaded after the target, and what a lost object produces at any
time.

**Orphaned keys.** A key with no row. Bytes nobody can reach and nobody is
billed for -- `object_storage.measure_store_bytes` reads the bucket, but the
quota a project is charged against reads its metadata, so an orphan is invisible
to the customer and to the invoice. Produced by an upload that wrote bytes and
failed to commit its row, by a project whose database was dropped without its
objects, and by a restore for every object deleted after the target.

**In-flight multipart uploads**, which are neither, and are the finding this
module exists to not get wrong. See below.

## Why an orphan is aged and an in-flight upload is not

An upload writes bytes before it commits its row, so a pass running during one
sees a key with no row and is looking at a healthy upload in progress. Orphans
are therefore reported only past an age threshold, taken from the object's
`LastModified`.

**Multipart uploads have no such age.** The S3 API specifies `Initiated` on a
`ListMultipartUploads` entry; the store this platform runs returns `Key` and
`UploadId` and nothing else. Measured. So the platform cannot tell an upload
abandoned last week from one three seconds old, and the safe reading is the
second: aborting a live multipart upload destroys a customer's file mid-write.

They are reported and never touched, and they are reported at all because the
bytes are otherwise **invisible**: an incomplete multipart upload holds real
storage that `ListObjectsV2` does not return. Measured with a 5 MiB part -- the
bucket listing showed zero keys while the upload held the space. A
reconciliation built only on the object listing would call that store clean, and
so does the quota.

## This module deletes nothing

There is no delete path here at all, which is a stronger property than a
confirmation prompt and is the same choice `restore.py` makes about a live
database. Both populations are consequences of a failure somewhere else, and
the first thing to do with a discrepancy of unknown cause is to look at it. What
collects orphans is an operator with this report, and whatever eventually
automates that is a decision with its own ADR -- not a default in a maintenance
pass that runs at three in the morning.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import psycopg

from services.control_plane import db, models, object_storage

log = logging.getLogger(__name__)

# How old a key with no row must be before it is called an orphan. An upload
# writes its bytes and then commits its row, so anything younger is more likely
# to be an upload in progress than a failure -- and a pass that reported it
# would produce a finding that resolves itself, which is the kind an operator
# learns to ignore.
#
# Fifteen minutes rather than one: upstream's own multipart path can leave a
# gap between the final part and the row, and the cost of waiting is a delay in
# noticing bytes that are already not being charged for.
ORPHAN_MIN_AGE_S = 15 * 60


# How much of a customer-authored name is printed before it is cut. Long enough
# to identify an object, short enough that one row cannot fill a terminal.
LABEL_LIMIT = 120


def safe_label(text: str) -> str:
    """A bucket id or object name, made safe to print.

    Object names and bucket ids are **customer-authored**, and every consumer of
    this report is a terminal or a log. `object_storage.delete_project_objects`
    already answers this by refusing to name keys at all, and that is right for
    an error nobody can act on -- but a dangling row is a finding an operator has
    to go and look at, so the name has to survive in some form.

    `repr` is the escaping, rather than a hand-written filter: it turns an ESC
    into the four characters `\x1b` and does the same for every other control
    character, which is the class that rewrites a terminal. Truncation is the
    second half -- a name can be as long as the store allows, and a report is
    not the place to find that out.
    """
    shown = repr(text)
    return shown if len(shown) <= LABEL_LIMIT else shown[:LABEL_LIMIT] + "...(truncated)"


class ReconcileError(RuntimeError):
    """A project's two data sets could not be compared."""


@dataclass(frozen=True)
class ObjectRow:
    """One row of `storage.objects`, and the key it claims."""

    bucket_id: str
    name: str
    version: str
    size: int

    def key(self, project_ref: str) -> str:
        return f"{object_storage.project_prefix(project_ref)}{self.bucket_id}/{self.name}/{self.version}"


@dataclass(frozen=True)
class StoredKey:
    """One key in the bucket, and when it was last written."""

    key: str
    size: int
    last_modified: dt.datetime | None

    def age_seconds(self, now: dt.datetime) -> float | None:
        if self.last_modified is None:
            return None
        return (now - self.last_modified).total_seconds()


@dataclass
class Reconciliation:
    """What the metadata and the store disagree about, for one project.

    Deliberately carries the counted totals *and* a bounded sample rather than
    every key. A project can hold millions of objects and this report is read by
    a human and written to a log; a finding that cannot be printed is one that
    gets printed anyway, truncated, by whoever is holding the incident.
    """

    project_ref: str
    rows: int = 0
    keys: int = 0
    # Rows whose key is not in the bucket. The project lists a file that cannot
    # be downloaded.
    dangling: list[ObjectRow] = field(default_factory=list)
    # Keys with no row, older than `ORPHAN_MIN_AGE_S`. Bytes nobody can reach
    # and nobody is billed for.
    orphaned: list[StoredKey] = field(default_factory=list)
    # Keys with no row that are too young to judge. Counted, not reported as a
    # problem: this is what a healthy upload in progress looks like.
    unmatched_recent: int = 0
    # In-flight multipart uploads. Reported, never touched, and never aged --
    # the store gives no `Initiated` to age them by.
    in_flight: list[str] = field(default_factory=list)
    # True when the object store could not be reached at all. Not the same as a
    # project with nothing in it, and must not be reported as though it were.
    store_readable: bool = True
    detail: str = ""

    @property
    def orphaned_bytes(self) -> int:
        return sum(item.size for item in self.orphaned)

    @property
    def dangling_bytes(self) -> int:
        """What the project is billed for and cannot download.

        From the metadata, because the bytes are by definition not there to
        measure -- so this is the tenant's own claim, with everything
        `object_storage`'s docstring says about that still true.
        """
        return sum(row.size for row in self.dangling)

    @property
    def clean(self) -> bool:
        """Whether the two data sets agree.

        In-flight uploads are not a disagreement. They are a normal state of a
        busy project, and folding them in here would make `clean` false for a
        project whose only fault is that somebody is uploading to it.
        """
        return not self.dangling and not self.orphaned

    def problems(self) -> list[str]:
        """What an operator should be told, in the order it matters."""
        issues: list[str] = []
        if not self.store_readable:
            issues.append(
                f"the object store could not be read ({self.detail}); this project was NOT "
                "reconciled, which is not the same as finding nothing wrong with it"
            )
            return issues

        if self.dangling:
            issues.append(
                f"{len(self.dangling)} row(s) name {self.dangling_bytes} bytes that are not in "
                "the object store; the project lists files that cannot be downloaded"
            )
        if self.orphaned:
            issues.append(
                f"{len(self.orphaned)} key(s) holding {self.orphaned_bytes} bytes have no row "
                f"and are older than {ORPHAN_MIN_AGE_S // 60}m; nobody can reach them and "
                "nobody is billed for them"
            )
        return issues

    def notes(self) -> list[str]:
        """True, worth saying, and not a disagreement."""
        notes: list[str] = []
        if self.unmatched_recent:
            notes.append(
                f"{self.unmatched_recent} key(s) have no row but are younger than "
                f"{ORPHAN_MIN_AGE_S // 60}m, which is what an upload in progress looks like; "
                "not counted as orphaned"
            )
        if self.in_flight:
            notes.append(
                f"{len(self.in_flight)} incomplete multipart upload(s) hold bytes that the "
                "object listing does not show and the quota does not count. This store returns "
                "no Initiated timestamp, so they cannot be aged and are never aborted here -- "
                "aborting a live upload destroys a customer's file mid-write"
            )
        return notes


def read_rows(tenant_conn: psycopg.Connection) -> list[ObjectRow]:
    """Every object the tenant's metadata claims.

    Returns an empty list for a tenant whose `storage.objects` does not exist,
    which is a project that has never used Storage rather than an error --
    `object_storage.measure_objects` draws the same distinction.
    """
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('storage.objects')")
        if cur.fetchone()[0] is None:
            return []
        # `version` is nullable in upstream's schema. A row without one names no
        # key at all, so it cannot be compared and is reported as dangling by
        # the comparison below rather than silently skipped.
        cur.execute(
            "SELECT bucket_id, name, coalesce(version, '') AS version, "
            "       coalesce((metadata->>'size')::bigint, 0) AS size "
            "  FROM storage.objects"
        )
        return [
            ObjectRow(bucket_id=r[0], name=r[1], version=r[2], size=int(r[3]))
            for r in cur.fetchall()
        ]


def read_keys(config, project_ref: str) -> tuple[list[StoredKey], bool, str]:
    """Every key in the bucket under this project's prefix.

    The third element of the tuple is why, when the second is False. An
    unreadable store has to be reported as unread rather than as empty: an empty
    listing and a refused request produce the same set difference, and one of
    them says every object this project owns has been lost.
    """
    client = object_storage._client(config)
    if client is None:
        return [], False, "no object store is configured on this node"

    prefix = object_storage.project_prefix(project_ref)
    keys: list[StoredKey] = []
    token = None
    try:
        while True:
            kwargs = {"Bucket": config.storage_s3_bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                keys.append(
                    StoredKey(
                        key=item["Key"],
                        size=int(item["Size"]),
                        last_modified=item.get("LastModified"),
                    )
                )
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
    except Exception as exc:  # noqa: BLE001 - reported, never raised past the report
        return [], False, f"{type(exc).__name__}"
    return keys, True, ""


def read_in_flight(config, project_ref: str) -> list[str]:
    """Incomplete multipart uploads under this project's prefix.

    Bytes that `ListObjectsV2` does not return and the quota does not count.
    Keys only: there is no size on a `ListMultipartUploads` entry, and asking
    the store for one per upload would turn a report into a crawl.
    """
    client = object_storage._client(config)
    if client is None:
        return []
    prefix = object_storage.project_prefix(project_ref)
    try:
        page = client.list_multipart_uploads(Bucket=config.storage_s3_bucket, Prefix=prefix)
    except Exception:  # noqa: BLE001 - an unsupported operation is not a finding
        return []
    return [item["Key"] for item in page.get("Uploads", [])]


def reconcile(
    config,
    tenant_conn: psycopg.Connection,
    *,
    project_ref: str,
    now: dt.datetime | None = None,
    min_age_s: int = ORPHAN_MIN_AGE_S,
) -> Reconciliation:
    """Compare one project's metadata with the bytes in the platform bucket.

    Reads both sides and subtracts. Writes nothing, to either side.

    The metadata is read first and the store second, and the order matters in
    the direction that produces false *dangling* rows rather than false orphans:
    a row committed between the two reads names a key the listing did not
    include. That is the safer error -- a dangling row sends an operator to look
    at an object that turns out to be fine, while a false orphan is a candidate
    for deletion. The age threshold covers the other direction.
    """
    if not models.is_valid_project_ref(project_ref):
        raise ReconcileError(f"invalid project ref {project_ref!r}")

    moment = now or dt.datetime.now(dt.UTC)
    rows = read_rows(tenant_conn)
    keys, readable, detail = read_keys(config, project_ref)

    report = Reconciliation(
        project_ref=project_ref,
        rows=len(rows),
        keys=len(keys),
        store_readable=readable,
        detail=detail,
    )
    if not readable:
        return report

    by_key = {key.key: key for key in keys}
    claimed: set[str] = set()
    for row in rows:
        # A row with no version names no key, so it can never match one. Left to
        # fall through to `dangling` rather than special-cased: it is a row the
        # project lists and cannot download, which is exactly what that means.
        key = row.key(project_ref) if row.version else None
        if key is not None and key in by_key:
            claimed.add(key)
        else:
            report.dangling.append(row)

    for key in keys:
        if key.key in claimed:
            continue
        age = key.age_seconds(moment)
        # No `LastModified` means the store did not say, and an object that
        # cannot be aged is treated as too young to judge. The conservative
        # direction: this population is the one an operator might delete.
        if age is None or age < min_age_s:
            report.unmatched_recent += 1
        else:
            report.orphaned.append(key)

    report.in_flight = read_in_flight(config, project_ref)
    return report


def due_for_reconciliation(conn: psycopg.Connection, *, limit: int = 20) -> list[dict]:
    """Projects a reconciliation pass should look at, least recently first.

    A smaller default limit than the measurement pass, and deliberately: this
    one lists a bucket prefix and reads a tenant's whole `storage.objects` for
    every project it touches, where measuring reads one number. A pass that
    tried to cover the fleet in one run would be a pass an operator turns off.
    """
    return db.query(
        conn,
        """
        SELECT id, project_ref, node_id, database_name, objects_reconciled_at
          FROM projects
         WHERE database_name IS NOT NULL AND node_id IS NOT NULL AND deleted_at IS NULL
           AND status IN ('PROVISIONED', 'ACTIVE')
         ORDER BY objects_reconciled_at NULLS FIRST
         LIMIT %s
        """,
        (limit,),
    )


def record(conn: psycopg.Connection, *, project_id, report: Reconciliation) -> None:
    """Store what the comparison found.

    **Only when it actually ran.** A store that could not be read leaves the
    columns exactly as they were, rather than writing zeroes -- a pass that
    recorded "0 dangling, 0 orphaned" on a store it never reached would be
    filing a clean bill of health it did not establish, and the row would then
    sort to the *back* of the queue and not be looked at again for as long as
    the fleet takes to cycle.
    """
    if not report.store_readable:
        return
    db.execute(
        conn,
        "UPDATE projects SET objects_reconciled_at = now(), "
        "       objects_dangling = %s, objects_orphaned = %s "
        " WHERE id = %s",
        (len(report.dangling), len(report.orphaned), project_id),
    )


__all__ = [
    "ORPHAN_MIN_AGE_S",
    "ObjectRow",
    "LABEL_LIMIT",
    "ReconcileError",
    "Reconciliation",
    "StoredKey",
    "due_for_reconciliation",
    "read_in_flight",
    "read_keys",
    "read_rows",
    "reconcile",
    "record",
    "safe_label",
]
