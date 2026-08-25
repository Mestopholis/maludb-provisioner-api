"""Migrating Supabase Storage (Phase 10 slice 6, ADR-063).

Two halves, and the split is the same one `tests/test_storage_workers.py` makes.

The **pure** half drives `migrate()` with injected transports, because what
matters here is the *decisions*: what is skipped and why, what a failure does to
the rest of the run, and whether a run that lost files can report success. Those
are the parts a customer's cutover depends on, and none of them need a Supabase
project to exercise.

The **database** half checks the reading, against a real `storage` schema built
to look like a source project's -- including one shaped like an *older* Supabase,
because `read_buckets` asks the catalogue which columns exist rather than
assuming, and a fixture that only ever built today's schema would never test
that.

What is deliberately asserted more than once: that a credential never reaches an
error message. Three of them are in play here -- the source DSN, the source's
service-role key and the destination's secret key -- and this is the one part of
the platform that holds all three at the same time.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from services.migrate import storage as storage_tools
from services.migrate.cli import EXIT_ERROR, build_parser, main
from tests.test_provisioning import ADMIN_DSN

requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

SOURCE_KEY = "sb-service-role-key-not-a-real-one"  # noqa: S105 - test fixture
PROJECT_KEY = "mldb_secret_not_a_real_one"  # noqa: S105 - test fixture


class _Response:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content

    def json(self) -> dict:
        return {}


def _source(handler=None, *, status: int = 200, body: bytes = b"bytes"):
    """A Supabase Storage that records what was asked of it."""
    calls: list[tuple[str, str, dict]] = []

    def transport(method, url, payload, headers):  # noqa: ARG001
        calls.append((method, url, headers))
        if handler is not None:
            return handler(method, url, headers)
        return _Response(status, body)

    client = storage_tools.SupabaseStorage(
        "https://abc.supabase.co", SOURCE_KEY, transport=transport
    )
    return client, calls


def _destination(handler=None, *, status: int = 200):
    """A MaluDB Storage endpoint that records what it was sent."""
    calls: list[dict] = []

    def transport(method, path, payload, headers, content=None):
        calls.append(
            {"method": method, "path": path, "payload": payload,
             "headers": headers, "content": content}
        )
        if handler is not None:
            return handler(method, path, payload, headers, content)
        return _Response(status)

    client = storage_tools.StorageDestination(
        "https://ref.maludb.com", PROJECT_KEY, transport=transport
    )
    return client, calls


def _object(bucket="files", name="a.txt", size=10, mimetype="text/plain", cache=None):
    return storage_tools.SourceObject(
        bucket_id=bucket, name=name, size=size, mimetype=mimetype, cache_control=cache
    )


BUCKET = storage_tools.Bucket(id="files", name="files", public=False)


# -- the object key, which is a URL and a path at the same time -------------


def test_a_key_keeps_its_slashes_and_escapes_everything_else():
    """Storage represents folders as slashes inside one object's name, so
    escaping them would ask for a different object -- one whose name really
    contains `%2F`. Everything else must be escaped: a key may legitimately hold
    a space, a `#` or a `?`, and each of those would otherwise end the path or
    start a query string."""
    assert storage_tools._encode_key("files", "a/b/c.txt") == "files/a/b/c.txt"
    assert storage_tools._encode_key("files", "holiday photo.jpg") == "files/holiday%20photo.jpg"
    assert storage_tools._encode_key("files", "a#b?c.txt") == "files/a%23b%3Fc.txt"


def test_a_key_cannot_walk_out_of_its_bucket():
    """The bucket prefix is what keeps one tenant's objects apart from another's
    at the destination. A leading slash on the name, or a trailing one on the
    bucket, must not produce a URL that addresses something else."""
    assert storage_tools._encode_key("files", "/a.txt") == "files/a.txt"
    assert storage_tools._encode_key("/files/", "a.txt") == "files/a.txt"


TRAVERSALS = [
    # Found in this slice's own security review, and the same shape as the
    # gateway's in slice 4: keeping `/` safe makes a key a path, and httpx
    # resolves dot segments when it builds the URL. This one would have been
    # uploaded to `/rest/v1/things` -- the Data API -- carrying the destination
    # project's secret key, which is service_role with no RLS in front of it.
    "../../../rest/v1/things",
    "a/../../../../rest/v1/things",
    # The same request written so a raw scan of the name does not see it. The
    # names come from a foreign system, so whatever Supabase's own key
    # validation allows today is not something this tool may assume.
    "%2e%2e/%2e%2e/rest/v1/things",
    "%252e%252e/rest/v1/things",
    ".",
]


@pytest.mark.parametrize("name", TRAVERSALS)
def test_a_dot_segment_is_refused_rather_than_normalised(name):
    """Refused, not rewritten: normalising here would mean this function and
    httpx having to agree forever about what normalising means."""
    with pytest.raises(storage_tools.StorageMigrationError, match="refused"):
        storage_tools._encode_key("files", name)


def test_a_traversing_object_is_reported_and_the_run_continues():
    """It reaches the report as a failed object rather than as a crash, so a
    migration meeting one file with a hostile name still moves the rest."""
    destination, calls = _destination()
    source, source_calls = _source()

    result = storage_tools.migrate(
        [BUCKET],
        iter([_object(name="../../../rest/v1/things"), _object(name="fine.txt")]),
        source, destination, max_object_bytes=1024,
    )

    assert result.objects_copied == 1
    assert len(result.failed) == 1
    assert not result.complete
    uploaded = [call["path"] for call in calls if "object" in call["path"]]
    assert uploaded == ["/storage/v1/object/files/fine.txt"]
    assert source_calls, "the safe object was not attempted"


# -- buckets ----------------------------------------------------------------


def test_a_bucket_carries_only_the_configuration_the_source_had():
    """`file_size_limit` and `allowed_mime_types` are optional on the source, so
    sending a null for one the source did not set would impose a limit the
    customer never had."""
    destination, calls = _destination()
    destination.create_bucket(storage_tools.Bucket(id="a", name="a", public=True))
    assert calls[0]["payload"] == {"id": "a", "name": "a", "public": True}

    destination.create_bucket(
        storage_tools.Bucket(
            id="b", name="b", public=False, file_size_limit=1024, allowed_mime_types=["image/png"]
        )
    )
    assert calls[1]["payload"] == {
        "id": "b", "name": "b", "public": False,
        "file_size_limit": 1024, "allowed_mime_types": ["image/png"],
    }


def test_an_existing_bucket_is_not_an_error():
    """A migration is restartable. The second run of one that failed halfway
    must not stop on the buckets the first run made."""
    destination, _ = _destination(status=409)
    assert destination.create_bucket(BUCKET) is False


def test_a_refused_bucket_stops_the_migration():
    """Unlike a single object: with no bucket there is nowhere for any of its
    objects to go, so continuing would report hundreds of identical failures."""
    destination, _ = _destination(status=403)
    with pytest.raises(storage_tools.StorageMigrationError, match="files"):
        destination.create_bucket(BUCKET)


def test_every_bucket_is_created_including_the_empty_ones():
    """An empty bucket is configuration the customer's application expects to
    find. The old scanner warned that these were not carried; slice 6 carries
    them, and this is the assertion that says so."""
    destination, calls = _destination()
    source, _ = _source()
    buckets = [
        storage_tools.Bucket(id="used", name="used"),
        storage_tools.Bucket(id="empty", name="empty"),
    ]

    result = storage_tools.migrate(
        buckets, iter([_object(bucket="used")]), source, destination,
        max_object_bytes=1024,
    )

    assert result.buckets_created == 2
    created = [call["payload"]["id"] for call in calls if call["path"] == "/storage/v1/bucket"]
    assert created == ["used", "empty"]


# -- objects ----------------------------------------------------------------


def test_an_object_carries_its_content_type_and_cache_control():
    """Both are what the source served the file with. A migration that dropped
    the content type would turn every image into an octet-stream download."""
    destination, calls = _destination()
    destination.upload(_object(mimetype="image/png", cache="max-age=3600"), b"png")

    headers = calls[0]["headers"]
    assert headers["content-type"] == "image/png"
    assert headers["cache-control"] == "max-age=3600"
    assert calls[0]["content"] == b"png"


def test_an_object_with_no_recorded_type_gets_a_safe_default():
    destination, calls = _destination()
    destination.upload(_object(mimetype=None), b"x")
    assert calls[0]["headers"]["content-type"] == "application/octet-stream"


def test_an_upload_overwrites_so_a_half_finished_run_can_be_repeated():
    """Without `x-upsert` the second run of an interrupted migration answers 409
    for every object the first run moved, and the customer's only way forward is
    deleting the bucket."""
    destination, calls = _destination()
    destination.upload(_object(), b"x")
    assert calls[0]["headers"]["x-upsert"] == "true"


def test_an_object_over_the_ceiling_is_skipped_before_it_is_downloaded():
    """The destination would answer 413 either way. Finding out there means
    having moved the bytes across the customer's connection for nothing -- which
    for the object most likely to hit this is the most expensive mistake the
    tool could make."""
    destination, _ = _destination()
    source, source_calls = _source()

    result = storage_tools.migrate(
        [BUCKET], iter([_object(size=10 * 1024 * 1024)]), source, destination,
        max_object_bytes=1024,
    )

    assert source_calls == [], "an oversize object was downloaded before being skipped"
    assert result.objects_copied == 0
    assert len(result.skipped) == 1
    key, reason = result.skipped[0]
    assert key == "files/a.txt"
    assert "ceiling" in reason


def test_an_object_whose_recorded_size_lied_is_still_refused():
    """`metadata->>'size'` is written by whatever uploaded the object, so the
    pre-download check is an optimisation and this is the actual ceiling.
    Without it a mis-recorded size becomes a 413 from the destination, reported
    as a failure rather than as the skip it is."""
    destination, calls = _destination()
    source, _ = _source(body=b"x" * 5000)

    result = storage_tools.migrate(
        [BUCKET], iter([_object(size=10)]), source, destination, max_object_bytes=1024
    )

    assert result.objects_copied == 0
    assert len(result.skipped) == 1
    assert not result.failed, "an oversize object was sent and reported as a failure"
    assert not [call for call in calls if "object" in call["path"]]


def test_an_object_of_unknown_size_is_attempted_rather_than_skipped():
    """`metadata->>'size'` is absent on some rows. Skipping those would silently
    drop files for a reason the customer cannot see; attempting one costs at
    worst a 413 that is reported."""
    destination, _ = _destination()
    source, _ = _source()

    result = storage_tools.migrate(
        [BUCKET], iter([_object(size=None)]), source, destination, max_object_bytes=1024
    )
    assert result.objects_copied == 1


def test_an_orphan_object_is_reported_rather_than_filtered_out():
    """A row whose bucket no longer exists is upstream's orphan. Filtering it in
    SQL would make the object count the customer sees not add up against the one
    the scan reported."""
    destination, _ = _destination()
    source, _ = _source()

    result = storage_tools.migrate(
        [BUCKET], iter([_object(bucket="deleted-bucket")]), source, destination,
        max_object_bytes=1024,
    )

    assert result.objects_copied == 0
    assert result.skipped[0][0] == "deleted-bucket/a.txt"
    assert "bucket" in result.skipped[0][1]


def test_one_failed_object_does_not_stop_the_rest():
    """Stopping on the first unreadable file means a customer discovering their
    broken objects one re-run at a time, inside a write freeze."""
    def handler(method, url, headers):  # noqa: ARG001
        return _Response(500 if "broken" in url else 200, b"ok")

    destination, _ = _destination()
    source, _ = _source(handler)

    result = storage_tools.migrate(
        [BUCKET],
        iter([_object(name="broken.txt"), _object(name="fine.txt")]),
        source, destination, max_object_bytes=1024,
    )

    assert result.objects_copied == 1
    assert len(result.failed) == 1
    assert result.failed[0][0] == "files/broken.txt"


def test_a_run_that_lost_files_is_not_complete():
    """The property the exit code is built on. A report that called this a
    success would be a script's permission to cut over."""
    destination, _ = _destination()
    source, _ = _source()

    lost = storage_tools.migrate(
        [BUCKET], iter([_object(size=10**9)]), source, destination, max_object_bytes=1024
    )
    assert not lost.complete

    clean = storage_tools.migrate(
        [BUCKET], iter([_object()]), source, destination, max_object_bytes=10**9
    )
    assert clean.complete
    assert clean.as_receipt() == {
        "buckets": 1, "objects": 1, "bytes": 5, "skipped": 0, "failed": 0
    }


# -- the three credentials, none of which may reach an error ----------------


def test_the_service_role_key_is_sent_as_a_header_and_never_in_an_error():
    """It is the entire source project: it reads every object in every bucket,
    past every policy."""
    def handler(method, url, headers):  # noqa: ARG001
        return _Response(403)

    source, calls = _source(handler)
    assert calls == []
    with pytest.raises(storage_tools.StorageMigrationError) as raised:
        source.download("files", "a.txt")

    assert SOURCE_KEY not in str(raised.value)
    _, url, headers = calls[0]
    assert headers["Authorization"] == f"Bearer {SOURCE_KEY}"
    assert SOURCE_KEY not in url, "the key was put in a URL, where it lands in logs"


def test_the_destination_secret_key_is_sent_as_a_header_and_never_in_an_error():
    """A secret key is the destination's data API with no row-level security in
    front of it."""
    destination, calls = _destination(status=500)
    with pytest.raises(storage_tools.StorageMigrationError) as raised:
        destination.upload(_object(), b"x")

    assert PROJECT_KEY not in str(raised.value)
    assert calls[0]["headers"]["apikey"] == PROJECT_KEY
    assert PROJECT_KEY not in calls[0]["path"]


def test_a_failure_names_the_object_so_a_customer_can_find_it():
    """The other half of not leaking the key: an error a customer cannot act on
    is one they will paste somewhere with the key in it to get help."""
    destination, _ = _destination(status=500)
    with pytest.raises(storage_tools.StorageMigrationError, match="files/a.txt"):
        destination.upload(_object(), b"x")


# -- the CLI ----------------------------------------------------------------


def test_with_storage_without_its_credentials_names_all_three(monkeypatch, capsys):
    """Checked before the schema is applied rather than where storage runs. A
    customer who forgot one should find out before their write freeze, not two
    thirds of the way through it."""
    for variable in (
        storage_tools.SOURCE_URL_ENV,
        storage_tools.SOURCE_KEY_ENV,
        storage_tools.DESTINATION_KEY_ENV,
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("MALUDB_SOURCE_DSN", "postgresql://x/y")
    monkeypatch.setenv("MALUDB_TOKEN", "a-token")

    code = main(["apply", "--project-ref", "abcd0001", "--with-storage"])

    assert code == EXIT_ERROR
    printed = capsys.readouterr().err
    for variable in (
        storage_tools.SOURCE_URL_ENV,
        storage_tools.SOURCE_KEY_ENV,
        storage_tools.DESTINATION_KEY_ENV,
    ):
        assert variable in printed


def test_the_storage_receipt_lists_every_object_that_did_not_arrive(tmp_path):
    """The terminal shows twenty. A customer with five hundred failures needs
    all of them, and needs them in something they can diff after a re-run."""
    from services.migrate.cli import _write_storage_receipt

    result = storage_tools.StorageReport(buckets_created=2, objects_copied=1, bytes_copied=5)
    result.skipped = [(f"files/{n}.bin", "over the ceiling") for n in range(50)]
    result.failed = [("files/broken.txt", "the source refused it (500)")]

    written = _write_storage_receipt(str(tmp_path / "run.json"), result, "abcd0001")

    import json
    payload = json.loads(open(written, encoding="utf-8").read())
    assert written.endswith("run.storage.json")
    assert payload["project_ref"] == "abcd0001"
    assert payload["objects"] == 1
    assert len(payload["skipped"]) == 50, "the receipt was truncated like the terminal output"
    assert payload["failed"][0]["object"] == "files/broken.txt"


def test_the_storage_receipt_carries_no_credential(tmp_path):
    """It is written to a path the customer chose, gets attached to change
    tickets, and is the sort of artefact that ends up in a repository."""
    from services.migrate.cli import _write_storage_receipt

    result = storage_tools.StorageReport()
    result.failed = [("files/a.txt", "the destination refused it (500)")]
    written = _write_storage_receipt(str(tmp_path / "run.json"), result, "abcd0001")

    body = open(written, encoding="utf-8").read()
    for secret in (SOURCE_KEY, PROJECT_KEY, "postgresql://"):
        assert secret not in body


def test_a_hostile_object_name_cannot_repaint_the_terminal():
    """The other half of the review. Both fields of a skip line carry text from
    the source: the key, and the reason -- which is an exception message that
    names the key again. Sanitising one and printing the other raw leaves the
    same string unescaped a field to the right."""
    hostile = "a\x1b[2J\x1b[1;32mStorage migrated cleanly\x1b[0m.txt"
    result = storage_tools.StorageReport()
    result.skipped = [(f"files/{hostile}", f"files/{hostile} is over the ceiling")]

    from services.migrate import report as report_tools

    for key, reason in result.skipped:
        line = f"  skipped {report_tools.sanitise(key)}: {report_tools.sanitise(reason)}"
        assert "\x1b" not in line
        assert "\r" not in line


def test_no_credential_can_be_passed_as_an_argument():
    """Unlike `--source-dsn`, which exists for scripting and says so. A
    service-role key or a secret key in `ps` output is a different class of
    mistake from a DSN, because neither is scoped to one database."""
    parser = build_parser()
    apply_options = {
        action.option_strings[0]
        for action in parser._subparsers._group_actions[0].choices["apply"]._actions
        if action.option_strings
    }
    assert "--with-storage" in apply_options
    for forbidden in ("--service-key", "--project-key", "--storage-key"):
        assert forbidden not in apply_options


# -- reading a source, which needs a database -------------------------------


@pytest.fixture
def supabase_storage_source(request):
    """A database shaped like a Supabase project's `storage` schema.

    `modern` builds today's columns; `legacy` leaves out `file_size_limit` and
    `allowed_mime_types`, which is what `read_buckets` asks the catalogue about.
    A fixture that only built today's would never exercise that path, and the
    failure it is there to prevent is a `42703` in the middle of a cutover.
    """
    shape = getattr(request, "param", "modern")
    name = f"mldb_storage_src_{uuid.uuid4().hex[:8]}"
    extra = (
        ", file_size_limit bigint, allowed_mime_types text[]" if shape == "modern" else ""
    )
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE SCHEMA storage")
            conn.execute(
                f"CREATE TABLE storage.buckets (id text primary key, name text, "
                f"public boolean{extra})"  # noqa: S608 - a constant in this file
            )
            conn.execute(
                "CREATE TABLE storage.objects ("
                "  id uuid primary key default gen_random_uuid(),"
                "  bucket_id text, name text, metadata jsonb)"
            )
            yield dsn, shape
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@requires_node
@pytest.mark.parametrize("supabase_storage_source", ["modern", "legacy"], indirect=True)
def test_buckets_are_read_from_whatever_columns_the_source_has(supabase_storage_source):
    dsn, shape = supabase_storage_source
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        if shape == "modern":
            conn.execute(
                "INSERT INTO storage.buckets VALUES ('avatars','avatars',true,1024,'{image/png}')"
            )
        else:
            conn.execute("INSERT INTO storage.buckets VALUES ('avatars','avatars',true)")
        conn.commit()

        buckets = storage_tools.read_buckets(conn)

    assert len(buckets) == 1
    assert buckets[0].id == "avatars"
    assert buckets[0].public is True
    if shape == "modern":
        assert buckets[0].file_size_limit == 1024
        assert buckets[0].allowed_mime_types == ["image/png"]
    else:
        # Not invented. A source with no such column had no such limit.
        assert buckets[0].file_size_limit is None
        assert buckets[0].allowed_mime_types is None


@requires_node
def test_objects_are_read_with_the_metadata_the_upload_needs(supabase_storage_source):
    dsn, _ = supabase_storage_source
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute(
            "INSERT INTO storage.objects (bucket_id, name, metadata) VALUES "
            """('files','a/b.txt','{"size": 12, "mimetype": "text/plain",
                                    "cacheControl": "max-age=60"}'),"""
            "('files','no-metadata.bin', '{}')"
        )
        conn.commit()

        objects = sorted(storage_tools.iter_objects(conn), key=lambda o: o.name)

    assert [o.name for o in objects] == ["a/b.txt", "no-metadata.bin"]
    assert objects[0].size == 12
    assert objects[0].mimetype == "text/plain"
    assert objects[0].cache_control == "max-age=60"
    # An object with no recorded size is not skipped for it -- see the pure test
    # above; this is where the None it relies on actually comes from.
    assert objects[1].size is None


@requires_node
def test_reading_the_source_never_writes_to_it(supabase_storage_source):
    """`tasks/PHASE-08-SUPABASE-MIGRATION.md` makes "source is not modified
    unexpectedly" an acceptance criterion, and for the object store half that
    means the read path works on a read-only connection."""
    dsn, _ = supabase_storage_source
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("INSERT INTO storage.buckets VALUES ('files','files',false)")
        conn.execute(
            "INSERT INTO storage.objects (bucket_id, name, metadata) "
            """VALUES ('files','a.txt','{"size": 1}')"""
        )
        conn.commit()

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        conn.read_only = True
        assert len(storage_tools.read_buckets(conn)) == 1
        assert len(list(storage_tools.iter_objects(conn))) == 1
