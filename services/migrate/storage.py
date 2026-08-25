"""Carrying Supabase Storage across (Phase 10 slice 6, ADR-063).

Three things move, and they come from two different places. Buckets and the
object *list* are rows in the source's database, which the scanner already
reads. The object **bytes** are not: they live in Supabase's object store, and
the only way to them is Supabase's own Storage API with the customer's
service-role key.

So this is the first part of the migrator that needs a second source credential,
and the first that writes to the destination somewhere other than the control
plane. ADR-042 already decided where both run -- on the customer's machine,
with the customer's own credentials -- and ADR-063 decides the rest: the
destination is written through its Storage API with the project's own secret
key, supplied by the customer, rather than one this tool mints for itself or a
control-plane route that would have to proxy every byte.

**Bytes flow through the customer's machine**, which is slower than a
server-side copy between two object stores and is the arrangement ADR-042
chose. Nothing here holds a whole bucket in memory: objects move one at a time,
and one that will not fit is skipped before it is downloaded rather than after.

**What is deliberately not carried**, each reported rather than silently
dropped:

- *Storage policies.* No customer-reachable role can author one here (ADR-061),
  so there is nowhere to put them. The scanner names them; a customer whose
  buckets were governed by policies has to have them applied by the platform.
  Note the failure mode is closed rather than open -- with no policy, RLS denies
  everything except `service_role` -- so what breaks is the application, not the
  privacy of the objects.
- *`owner_id` and the original timestamps.* The Storage API has no way to accept
  them; an uploaded object is owned by the token that uploaded it and is created
  now. A migration that wanted these would have to write `storage.objects`
  directly, which is the metadata the object store is kept consistent with, and
  ADR-061 is exactly the decision not to hand that out.
- *Objects larger than the destination's upload ceiling.* Named and skipped, and
  skipped **before** the download rather than after, because finding out at the
  destination means having already moved the bytes for nothing.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# One page of the object list. The list is read with a server-side cursor and
# never assembled whole: a bucket with a million objects is a list this tool
# streams, not one it holds.
OBJECT_PAGE = 500

# The destination's upload ceiling, which this tool cannot ask it for: nothing
# in the public API reports `storage_max_upload_bytes`, so this mirrors its
# default (ADR-060, and `upload_size_limit` in the compatibility matrix) and
# `--max-object-bytes` exists for a deployment that changed it. Used to skip an
# object *before* downloading it, so a wrong value here costs a needless skip
# rather than a needless transfer.
DEFAULT_MAX_OBJECT_BYTES = 50 * 1024 * 1024

# Where the customer's Supabase credentials come from. Environment variables by
# preference for the reason the source DSN is: an argument is visible in `ps`
# and lands in shell history, and a service-role key is the whole project.
SOURCE_URL_ENV = "MALUDB_SOURCE_STORAGE_URL"
SOURCE_KEY_ENV = "MALUDB_SOURCE_SERVICE_KEY"  # noqa: S105 - the variable's name
# And the destination's own key, which is a *different* credential from the
# platform token the rest of the CLI uses (ADR-063).
DESTINATION_KEY_ENV = "MALUDB_PROJECT_KEY"  # noqa: S105 - the variable's name


class StorageMigrationError(RuntimeError):
    """Storage could not be migrated. Never carries a key or a DSN."""


@dataclass
class Bucket:
    """A bucket as the source has it, and as the destination will be asked for."""

    id: str
    name: str
    public: bool = False
    file_size_limit: int | None = None
    allowed_mime_types: list[str] | None = None


@dataclass
class SourceObject:
    """One object's metadata, before its bytes are anywhere near this process."""

    bucket_id: str
    name: str
    size: int | None = None
    mimetype: str | None = None
    cache_control: str | None = None


@dataclass
class StorageReport:
    """What moved, what did not, and why -- for the receipt and for the console."""

    buckets_created: int = 0
    buckets_existing: int = 0
    objects_copied: int = 0
    bytes_copied: int = 0
    # Each entry is (object, reason). Kept whole rather than counted, because a
    # customer deciding whether to cut over needs to know *which* files did not
    # arrive.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.skipped and not self.failed

    def as_receipt(self) -> dict[str, Any]:
        return {
            "buckets": self.buckets_created + self.buckets_existing,
            "objects": self.objects_copied,
            "bytes": self.bytes_copied,
            "skipped": len(self.skipped),
            "failed": len(self.failed),
        }


# -- reading the source ----------------------------------------------------


def read_buckets(conn) -> list[Bucket]:
    """The source's buckets, with whatever configuration this source records.

    `file_size_limit` and `allowed_mime_types` are asked for by catalogue rather
    than assumed, on `auth._available`'s reasoning: Supabase and this platform
    pin different `storage-api` versions and a source may predate either column.
    Asking is the difference between a migration and a `42703`.
    """
    optional = _columns_present(conn, "buckets", ("file_size_limit", "allowed_mime_types"))
    # Interpolated, and safe for one reason worth stating rather than trusting a
    # `noqa` to imply: `optional` can only contain strings from the fixed tuple
    # passed to `_columns_present`, which matches them against `pg_attribute`
    # with a bound parameter. Nothing a source database contains reaches this
    # string -- a column *name* the customer invented is not in the tuple, so it
    # is not in the result.
    selected = ", ".join(["id", "name", "public", *optional])
    with conn.cursor() as cur:
        cur.execute(f"SELECT {selected} FROM storage.buckets ORDER BY id")  # noqa: S608
        return [
            Bucket(
                id=row["id"],
                name=row.get("name") or row["id"],
                public=bool(row.get("public")),
                file_size_limit=row.get("file_size_limit"),
                allowed_mime_types=row.get("allowed_mime_types"),
            )
            for row in cur.fetchall()
        ]


def iter_objects(conn, *, page: int = OBJECT_PAGE):
    """Every object in the source, streamed.

    A server-side cursor rather than one query returning everything: the object
    list is the one part of this migration whose size is set by the customer's
    usage rather than by their schema, and a bucket with a million objects would
    otherwise be a million rows in this process before the first byte moved.

    `name` is the key within the bucket. Rows with no `bucket_id` are upstream's
    orphans -- an object whose bucket was deleted -- and are skipped by the
    caller rather than filtered here, so they are reported rather than lost.
    """
    with conn.cursor(name="maludb_storage_objects") as cur:
        cur.itersize = page
        cur.execute(
            """
            SELECT bucket_id,
                   name,
                   (metadata->>'size')::bigint AS size,
                   metadata->>'mimetype'       AS mimetype,
                   metadata->>'cacheControl'   AS cache_control
              FROM storage.objects
             ORDER BY bucket_id, name
            """
        )
        for row in cur:
            yield SourceObject(
                bucket_id=row["bucket_id"],
                name=row["name"],
                size=row["size"],
                mimetype=row["mimetype"],
                cache_control=row["cache_control"],
            )


def _columns_present(conn, table: str, wanted: tuple[str, ...]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'storage' AND c.relname = %s
               AND a.attname = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (table, list(wanted)),
        )
        return [row["attname"] for row in cur.fetchall()]


# -- the two ends of the wire ----------------------------------------------


class SupabaseStorage:
    """The source's Storage API, read-only.

    Read-only is a property of what this calls rather than a mode it asks for:
    the one method here is a GET of an object. `tasks/PHASE-08-SUPABASE-MIGRATION.md`
    makes "source is not modified unexpectedly" an acceptance criterion, and for
    the object store that means never issuing anything but a download.
    """

    def __init__(self, url: str, service_key: str, *, transport: Callable | None = None) -> None:
        self.url = url.rstrip("/")
        self._key = service_key
        self._transport = transport

    def download(self, bucket: str, path: str) -> bytes:
        """One object's bytes.

        The service-role key goes in a header and nowhere else. A failure names
        the status and the object, never the key -- and never the response body,
        which on some Supabase errors echoes the request.
        """
        target = f"{self.url}/storage/v1/object/{_encode_key(bucket, path)}"
        headers = {"Authorization": f"Bearer {self._key}", "apikey": self._key}
        response = self._send("GET", target, headers)
        if response.status_code != 200:
            raise StorageMigrationError(
                f"the source refused {bucket}/{path} ({response.status_code})"
            )
        return response.content

    def _send(self, method: str, url: str, headers: dict[str, str]):
        if self._transport is not None:
            return self._transport(method, url, None, headers)
        import httpx

        return httpx.request(method, url, headers=headers, timeout=300)


class StorageDestination:
    """The destination project's Storage API, reached with its own secret key.

    **A different credential from the rest of the CLI** (ADR-063). Everything
    else here talks to the control plane with the customer's platform token;
    object bytes go to the project's own gateway hostname with the project's own
    secret key, which is what the Storage surface authenticates. The token
    cannot be used here and the key cannot be used there, and keeping them
    separate is what stops this tool from needing a privileged path.
    """

    def __init__(self, api_url: str, secret_key: str, *, transport: Callable | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self._key = secret_key
        self._transport = transport

    def create_bucket(self, bucket: Bucket) -> bool:
        """True if it was created, False if it was already there.

        Already-there is not an error: a migration is restartable, and the
        second run of one that failed halfway must not stop on the buckets it
        made the first time.
        """
        payload: dict[str, Any] = {"id": bucket.id, "name": bucket.name, "public": bucket.public}
        if bucket.file_size_limit is not None:
            payload["file_size_limit"] = bucket.file_size_limit
        if bucket.allowed_mime_types:
            payload["allowed_mime_types"] = bucket.allowed_mime_types

        response = self._send("POST", "/storage/v1/bucket", payload, None)
        if response.status_code == 200:
            return True
        if response.status_code == 409:
            return False
        raise StorageMigrationError(
            f"the destination refused bucket {bucket.id!r} ({response.status_code})"
        )

    def upload(self, obj: SourceObject, body: bytes) -> None:
        """One object, overwriting whatever is there.

        `x-upsert` for the same reason `create_bucket` tolerates 409: a
        half-finished migration is re-run, and the alternative is a customer
        deleting a bucket to retry.
        """
        headers = {
            "content-type": obj.mimetype or "application/octet-stream",
            "x-upsert": "true",
        }
        if obj.cache_control:
            headers["cache-control"] = obj.cache_control

        path = f"/storage/v1/object/{_encode_key(obj.bucket_id, obj.name)}"
        response = self._send("POST", path, None, headers, content=body)
        if response.status_code not in (200, 201):
            raise StorageMigrationError(
                f"the destination refused {obj.bucket_id}/{obj.name} ({response.status_code})"
            )

    def _send(self, method: str, path: str, payload, headers: dict | None, content: bytes | None = None):
        sent = {"apikey": self._key, "Authorization": f"Bearer {self._key}"}
        sent.update(headers or {})
        if self._transport is not None:
            return self._transport(method, path, payload, sent, content)
        import httpx

        return httpx.request(
            method, f"{self.api_url}{path}",
            json=payload, content=content, headers=sent, timeout=300,
        )


def _encode_key(bucket: str, path: str) -> str:
    """`<bucket>/<path>`, escaped for a URL but keeping the separators.

    `quote` with `safe="/"` rather than the default: an object key legitimately
    contains slashes -- that is how Storage represents folders -- and escaping
    them would ask for an object whose name contains `%2F`, which is a different
    object. Everything else is escaped, because a key may contain spaces, `#`
    or `?`, and those would otherwise end the path or start a query.

    **A dot segment is refused rather than escaped or normalised**, and this is
    the finding from this slice's own security review. Keeping `/` safe means a
    key is a path, and `httpx` resolves dot segments when it builds a request
    URL -- so an object named `../../../rest/v1/things` would have been uploaded
    to a URL that is not the Storage surface at all, carrying the destination
    project's **secret key** in its headers. That is service_role on the Data
    API, driven by a row in the source database.

    The names come from a foreign system: whatever Supabase's own key validation
    allows today is not something this tool can assume, and a customer whose
    application accepted user-supplied filenames is exactly who runs this. The
    same reasoning, and the same answer, as the gateway's `_has_dot_segment`:
    refuse, rather than normalise and hope two libraries agree forever about
    what normalising means.
    """
    from urllib.parse import quote, unquote

    combined = f"{bucket.strip('/')}/{path.lstrip('/')}"
    # Decoded first: `%2e%2e` is the same key written so a raw scan does not see
    # it, and the source stores whatever it was given.
    if any(segment in (".", "..") for segment in unquote(unquote(combined)).split("/")):
        raise StorageMigrationError(
            "the object key contains a path segment that would change the URL it is "
            "written to, and was refused rather than rewritten"
        )
    return quote(combined, safe="/")


# -- the migration ---------------------------------------------------------


def migrate(
    buckets: list[Bucket],
    objects,
    source: SupabaseStorage,
    destination: StorageDestination,
    *,
    max_object_bytes: int,
    progress: Callable[[str], None] = lambda _message: None,
) -> StorageReport:
    """Buckets, then objects, one at a time.

    Takes the two lists rather than a connection so the decisions here -- what
    is skipped, what is retried, what makes a run incomplete -- can be tested
    without a Supabase-shaped database. `objects` is consumed lazily and is
    expected to be `iter_objects`'s generator in real use, so a bucket with a
    million objects is still streamed.

    Buckets first because an object cannot be uploaded into a bucket that does
    not exist, and every bucket is created even if it holds nothing -- an empty
    bucket is configuration the customer's application expects to find, which is
    what the old `storage.empty_buckets` warning was about.

    A single object that fails does not stop the migration. It is recorded and
    the run continues, because the alternative -- stopping on the first
    unreadable file -- means a customer discovering their objects one failure
    per re-run. What must not happen is a report that calls that a success, so
    `StorageReport.complete` is false whenever anything was skipped or failed.
    """
    report = StorageReport()

    known = {bucket.id for bucket in buckets}
    for bucket in buckets:
        if destination.create_bucket(bucket):
            report.buckets_created += 1
        else:
            report.buckets_existing += 1
    progress(
        f"Buckets: {report.buckets_created} created, {report.buckets_existing} already present."
    )

    for obj in objects:
        key = f"{obj.bucket_id}/{obj.name}" if obj.bucket_id else obj.name

        if not obj.bucket_id or obj.bucket_id not in known:
            # Upstream's orphan: a row whose bucket is gone. Reported rather
            # than filtered out in SQL, so the count a customer sees adds up.
            report.skipped.append((key, "its bucket does not exist in the source"))
            continue
        if obj.size is not None and obj.size > max_object_bytes:
            # Checked before the download, not after: the destination would
            # answer 413 either way, and finding out there means having moved
            # the bytes for nothing.
            report.skipped.append(
                (key, f"{obj.size} bytes is over the destination's {max_object_bytes}-byte ceiling")
            )
            continue

        try:
            body = source.download(obj.bucket_id, obj.name)
            if len(body) > max_object_bytes:
                # The row said it would fit and it did not. `metadata->>'size'`
                # is written by whatever uploaded the object, so the check above
                # is an optimisation and this one is the ceiling: without it a
                # mis-recorded size means an upload the destination answers 413
                # to, reported as a failure rather than as the skip it is.
                report.skipped.append(
                    (key, f"{len(body)} bytes is over the destination's "
                          f"{max_object_bytes}-byte ceiling")
                )
                continue
            destination.upload(obj, body)
        except StorageMigrationError as exc:
            report.failed.append((key, str(exc)))
            continue

        report.objects_copied += 1
        report.bytes_copied += len(body)
        if report.objects_copied % 100 == 0:
            progress(f"  {report.objects_copied} objects, {report.bytes_copied / 1e6:.1f} MB")

    return report


def normalise_path(bucket: str, name: str) -> str:
    """The key as the destination will store it, for comparing the two sides.

    `posixpath.normpath` rather than string work: an object key is a POSIX-ish
    path and the two stores agree about what `a//b` means.
    """
    return posixpath.normpath(f"{bucket}/{name}")
