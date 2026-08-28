"""Objects versus metadata (Phase 11 slice 4, ADR-069).

The comparison this file tests is a set difference, and the whole difficulty is
in what counts as a member. Three things had to be measured before it could be
written, and each one would have produced a wrong pass if assumed:

1. **An overwrite replaces the key.** Upstream deletes the previous version's
   bytes and writes a new version UUID, so the bucket holds no population of
   superseded versions. A pass that assumed it did -- comparing on
   `<ref>/<bucket>/<name>` rather than on the full key -- would have reported
   every overwritten object on the platform as orphaned.
2. **An incomplete multipart upload holds bytes `ListObjectsV2` does not
   return.** So a reconciliation built only on the object listing calls such a
   store clean, and so does the quota.
3. **This store returns no `Initiated` on a multipart listing.** The S3 API
   specifies one. Without it an abandoned upload cannot be told from a live one,
   which is why they are reported and never aborted.

The drift in the end-to-end tests below is injected against a **real** object
store rather than a stub, because every one of those three facts is a property
of the store rather than of this code.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import uuid

import pytest

from services.control_plane import object_storage, reconcile
from tests.conftest import (
    object_store_configured,
    requires_db,
    storage_env_config,
)

requires_object_store = pytest.mark.skipif(
    not object_store_configured(),
    reason="MALUDB_STORAGE_S3_ENDPOINT and credentials are needed "
    "(scripts/storage-test-cluster.sh builds one)",
)

REF = "rcn00001"


def _row(bucket="b", name="f.txt", version=None, size=10) -> reconcile.ObjectRow:
    return reconcile.ObjectRow(
        bucket_id=bucket, name=name, version=version or str(uuid.uuid4()), size=size
    )


def _key(row: reconcile.ObjectRow, ref: str = REF, size=None, age_s=3600) -> reconcile.StoredKey:
    return reconcile.StoredKey(
        key=row.key(ref),
        size=size if size is not None else row.size,
        last_modified=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_s),
    )


class _FakeConn:
    """A tenant connection that answers the two queries `read_rows` makes."""

    def __init__(self, rows, has_table=True):
        self._rows = rows
        self._has_table = has_table

    def cursor(self):
        return _FakeCursor(self._rows, self._has_table)


class _FakeCursor:
    def __init__(self, rows, has_table):
        self._rows = rows
        self._has_table = has_table
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        return ("storage.objects" if self._has_table else None,)

    def fetchall(self):
        return [(r.bucket_id, r.name, r.version, r.size) for r in self._rows]


def _reconcile(rows, keys, *, in_flight=(), min_age_s=reconcile.ORPHAN_MIN_AGE_S, monkeypatch):
    monkeypatch.setattr(reconcile, "read_keys", lambda *a, **k: (list(keys), True, ""))
    monkeypatch.setattr(reconcile, "read_in_flight", lambda *a, **k: list(in_flight))
    return reconcile.reconcile(
        None, _FakeConn(rows), project_ref=REF, min_age_s=min_age_s
    )


# --------------------------------------------------------------------------
# The join
# --------------------------------------------------------------------------


def test_the_key_is_the_row_plus_its_version():
    """Measured layout, asserted so a schema change cannot quietly move it."""
    row = _row(bucket="files", name="note.txt", version="f03a350e-0000-0000-0000-000000000000")
    assert row.key("swk00003") == (
        "swk00003/files/note.txt/f03a350e-0000-0000-0000-000000000000"
    )


def test_a_matching_row_and_key_is_clean(monkeypatch):
    row = _row()
    report = _reconcile([row], [_key(row)], monkeypatch=monkeypatch)
    assert report.clean
    assert report.rows == 1 and report.keys == 1
    assert report.problems() == []


def test_two_versions_of_a_name_do_not_match_each_other(monkeypatch):
    """The comparison is on the full key, including the version.

    This is the assertion that would fail if someone 'simplified' the join to
    `<ref>/<bucket>/<name>`. It would then pass while reporting nothing -- and
    it would also stop finding a lost object that had been replaced by a stale
    one, which is the case this precision exists for.
    """
    row = _row(version="11111111-1111-1111-1111-111111111111")
    stale = dataclasses.replace(row, version="22222222-2222-2222-2222-222222222222")
    report = _reconcile([row], [_key(stale)], monkeypatch=monkeypatch)
    assert len(report.dangling) == 1
    assert len(report.orphaned) == 1


# --------------------------------------------------------------------------
# Dangling rows
# --------------------------------------------------------------------------


def test_a_row_with_no_key_is_dangling(monkeypatch):
    row = _row(size=4096)
    report = _reconcile([row], [], monkeypatch=monkeypatch)
    assert report.dangling == [row]
    assert report.dangling_bytes == 4096
    assert not report.clean
    assert "cannot be downloaded" in report.problems()[0]


def test_a_row_with_no_version_names_no_key_and_is_dangling(monkeypatch):
    """Upstream's `version` is nullable, so this row is reachable.

    It is reported rather than skipped: a row the project lists and cannot
    download is exactly what dangling means, whatever the reason.
    """
    row = _row(version="")
    report = _reconcile([row], [], monkeypatch=monkeypatch)
    assert report.dangling == [row]


# --------------------------------------------------------------------------
# Orphaned keys, and the ageing that keeps them honest
# --------------------------------------------------------------------------


def test_an_old_key_with_no_row_is_orphaned(monkeypatch):
    stray = _key(_row(), size=900, age_s=86400)
    report = _reconcile([], [stray], monkeypatch=monkeypatch)
    assert report.orphaned == [stray]
    assert report.orphaned_bytes == 900
    assert "nobody is billed" in report.problems()[0]


def test_a_recent_key_with_no_row_is_an_upload_in_progress(monkeypatch):
    """The false positive this threshold exists to prevent.

    An upload writes its bytes and then commits its row, so a pass running
    during one sees a key with no row and is looking at a healthy upload.
    """
    report = _reconcile([], [_key(_row(), age_s=5)], monkeypatch=monkeypatch)
    assert report.orphaned == []
    assert report.unmatched_recent == 1
    assert report.clean
    assert any("upload in progress" in n for n in report.notes())


def test_a_key_the_store_gave_no_timestamp_for_is_not_called_an_orphan(monkeypatch):
    """Conservative direction: this is the population an operator might delete."""
    stray = reconcile.StoredKey(key=f"{REF}/b/x/1", size=1, last_modified=None)
    report = _reconcile([], [stray], monkeypatch=monkeypatch)
    assert report.orphaned == []
    assert report.unmatched_recent == 1


# --------------------------------------------------------------------------
# In-flight multipart uploads
# --------------------------------------------------------------------------


def test_in_flight_uploads_are_reported_and_are_not_a_disagreement(monkeypatch):
    """A busy project is not a broken one."""
    report = _reconcile([], [], in_flight=[f"{REF}/b/big.bin/v1"], monkeypatch=monkeypatch)
    assert report.clean, "an upload in progress must not make a project look inconsistent"
    assert report.problems() == []
    note = " ".join(report.notes())
    assert "multipart" in note
    assert "the object listing does not show" in note
    assert "never aborted here" in note


def test_there_is_no_delete_path_in_this_module():
    """The property, asserted against the parsed source rather than by grepping.

    Slice 2 learned that lesson the hard way: the first version of its
    equivalent test failed on the docstring explaining the rule. Reconciliation
    reports; collecting orphans is an operator's act with its own decision
    behind it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(reconcile))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("delete_object", "delete_objects", "abort_multipart_upload"):
        assert forbidden not in called, f"{forbidden} is reachable from the reconciliation pass"


# --------------------------------------------------------------------------
# An unreadable store is unread, not empty
# --------------------------------------------------------------------------


def test_an_unreadable_store_is_not_reported_as_an_empty_one(monkeypatch):
    """The failure that would otherwise say every object has been lost.

    An empty listing and a refused request produce the same set difference. One
    of them means the project has no objects; the other means the platform
    could not look.
    """
    monkeypatch.setattr(reconcile, "read_keys", lambda *a, **k: ([], False, "EndpointConnectionError"))
    monkeypatch.setattr(reconcile, "read_in_flight", lambda *a, **k: [])
    report = reconcile.reconcile(None, _FakeConn([_row()]), project_ref=REF)
    assert not report.store_readable
    assert report.dangling == [], "nothing may be called dangling on a store nobody read"
    assert "NOT reconciled" in report.problems()[0]


def test_a_tenant_without_storage_reconciles_as_empty(monkeypatch):
    monkeypatch.setattr(reconcile, "read_keys", lambda *a, **k: ([], True, ""))
    monkeypatch.setattr(reconcile, "read_in_flight", lambda *a, **k: [])
    report = reconcile.reconcile(None, _FakeConn([], has_table=False), project_ref=REF)
    assert report.clean and report.rows == 0


def test_an_invalid_project_ref_is_refused():
    """The prefix selects what a report is about; a ref is untrusted input."""
    with pytest.raises(reconcile.ReconcileError):
        reconcile.reconcile(None, _FakeConn([]), project_ref="../../etc")


# --------------------------------------------------------------------------
# Durability
# --------------------------------------------------------------------------


def test_a_single_copy_store_fails_production_and_warns_elsewhere():
    single = object_storage.StoreDurability(
        reachable=True, detail="", replication="000", replicated=False, production=True
    )
    assert not single.ok
    assert "ONE copy" in single.failures[0]

    dev = dataclasses.replace(single, production=False)
    assert dev.ok
    assert any("not enforced outside production" in w for w in dev.warnings)


def test_a_replicated_store_passes():
    two = object_storage.StoreDurability(
        reachable=True, detail="", replication="001", replicated=True, production=True
    )
    assert two.ok and two.warnings == []


def test_an_undeclared_store_is_not_a_failure():
    """A managed S3 service exposes no such endpoint. Silence is not a fault.

    Nor is it a pass: the warning says the check did not run, which is the same
    three-outcome shape ADR-068 uses for backup retention.
    """
    unknown = object_storage.StoreDurability(
        reachable=False, detail="no endpoint configured", production=True
    )
    assert unknown.ok
    assert any("undeclared" in w for w in unknown.warnings)


def test_a_store_with_no_master_endpoint_is_undeclared_rather_than_replicated():
    """Fails toward 'we do not know', never toward 'it is fine'."""
    cfg = dataclasses.replace(storage_env_config(), storage_master_endpoint=None)
    durability = object_storage.inspect_durability(cfg)
    assert durability.replicated is None
    assert durability.ok, "an unaskable store is not a failing one"


# --------------------------------------------------------------------------
# Against a real object store
# --------------------------------------------------------------------------


@requires_object_store
def test_a_real_store_reports_a_replication_factor():
    """Read from the store rather than from a fixture.

    The development store is built at SeaweedFS's default of `000` on purpose,
    so this asserts the shape rather than the value -- and asserts that the
    value was actually obtained, which is the part that would silently stop
    working if the master API moved.
    """
    cfg = dataclasses.replace(
        storage_env_config(),
        storage_master_endpoint=os.environ.get("MALUDB_STORAGE_MASTER_ENDPOINT") or None,
    )
    if not cfg.storage_master_endpoint:
        pytest.skip("MALUDB_STORAGE_MASTER_ENDPOINT is unset")
    durability = object_storage.inspect_durability(cfg)
    assert durability.reachable, durability.detail
    assert durability.replication is not None
    assert durability.replicated is not None


@requires_object_store
@requires_db
def test_real_orphaned_bytes_are_found_and_nothing_is_deleted():
    """Drift injected into a real bucket, then found, then still there.

    The last clause is the point: a reconciliation that tidied up as it went
    would pass the first two assertions.
    """
    cfg = storage_env_config()
    client = object_storage._client(cfg)
    prefix = object_storage.project_prefix(REF)
    stray = f"{prefix}b/orphan.bin/{uuid.uuid4()}"
    client.put_object(Bucket=cfg.storage_s3_bucket, Key=stray, Body=b"x" * 128)
    try:
        # Age is what separates an orphan from an upload in progress, and this
        # key is seconds old -- so a zero threshold is what makes the assertion
        # about the comparison rather than about the clock.
        report = reconcile.reconcile(
            cfg, _FakeConn([]), project_ref=REF, min_age_s=0
        )
        assert report.store_readable, report.detail
        assert [item.key for item in report.orphaned] == [stray]
        assert report.orphaned_bytes == 128

        still = client.list_objects_v2(Bucket=cfg.storage_s3_bucket, Prefix=stray)
        assert still.get("KeyCount") == 1, "the reconciliation deleted what it found"
    finally:
        client.delete_object(Bucket=cfg.storage_s3_bucket, Key=stray)


@requires_object_store
@requires_db
def test_a_real_incomplete_multipart_upload_is_invisible_to_the_object_listing():
    """The finding the in-flight category exists for, measured here rather than
    asserted from upstream's documentation.

    The bytes are real: a part is uploaded and never completed. `ListObjectsV2`
    returns nothing, so the quota counts nothing and a reconciliation built on
    that listing alone would call this store clean.
    """
    cfg = storage_env_config()
    client = object_storage._client(cfg)
    prefix = object_storage.project_prefix(REF)
    key = f"{prefix}b/abandoned.bin/{uuid.uuid4()}"
    started = client.create_multipart_upload(Bucket=cfg.storage_s3_bucket, Key=key)
    try:
        client.upload_part(
            Bucket=cfg.storage_s3_bucket, Key=key,
            UploadId=started["UploadId"], PartNumber=1, Body=b"y" * (5 * 1024 * 1024),
        )
        listed = client.list_objects_v2(Bucket=cfg.storage_s3_bucket, Prefix=key)
        assert listed.get("KeyCount", 0) == 0, (
            "the object listing showed an incomplete upload; the premise of the "
            "in-flight category no longer holds and the pass should be revisited"
        )

        report = reconcile.reconcile(cfg, _FakeConn([]), project_ref=REF, min_age_s=0)
        assert key in report.in_flight
        assert report.clean, "an upload in progress is not a disagreement"
        assert any("multipart" in note for note in report.notes())
    finally:
        client.abort_multipart_upload(
            Bucket=cfg.storage_s3_bucket, Key=key, UploadId=started["UploadId"]
        )


@requires_object_store
def test_this_store_gives_no_initiated_timestamp_to_age_an_upload_by():
    """Recorded as a test because it is why in-flight uploads are never aborted.

    The S3 API specifies `Initiated` on a `ListMultipartUploads` entry. If this
    store ever starts returning one, ageing them becomes possible and the
    decision not to abort should be revisited -- so this asserts the limitation
    rather than leaving it in a comment that nothing checks.
    """
    cfg = storage_env_config()
    client = object_storage._client(cfg)
    key = f"{object_storage.project_prefix(REF)}b/aging.bin/{uuid.uuid4()}"
    started = client.create_multipart_upload(Bucket=cfg.storage_s3_bucket, Key=key)
    try:
        listing = client.list_multipart_uploads(
            Bucket=cfg.storage_s3_bucket, Prefix=key
        )
        uploads = listing.get("Uploads", [])
        assert uploads, "the store did not list an upload it had just accepted"
        assert "Initiated" not in uploads[0], (
            "this store now returns Initiated; in-flight uploads can be aged, and "
            "reconcile.py's reason for never aborting them should be revisited"
        )
    finally:
        client.abort_multipart_upload(
            Bucket=cfg.storage_s3_bucket, Key=key, UploadId=started["UploadId"]
        )


# --------------------------------------------------------------------------
# What reaches a terminal, and what stays inside one project
# --------------------------------------------------------------------------


def test_a_customer_authored_name_cannot_rewrite_a_terminal():
    """Object names and bucket ids are customer-authored and this report prints them.

    The escaping is `repr`, so an ESC becomes the four characters `\\x1b` rather
    than a control sequence. Asserted on the class that matters -- control
    characters and newlines -- rather than on one example.
    """
    hostile = "\x1b[31mred\x1b[0m\nSecond line\r\x07"
    shown = reconcile.safe_label(hostile)
    for raw in ("\x1b", "\n", "\r", "\x07"):
        assert raw not in shown, f"{raw!r} survived into a line bound for a terminal"


def test_a_very_long_name_is_truncated():
    """A name can be as long as the store allows; a report is not where to find out."""
    shown = reconcile.safe_label("x" * 5000)
    assert len(shown) <= reconcile.LABEL_LIMIT + len("...(truncated)")
    assert shown.endswith("...(truncated)")


def test_an_ordinary_name_survives_readably():
    """Escaping that made every report unreadable would get removed."""
    assert "invoices/2026-01.pdf" in reconcile.safe_label("invoices/2026-01.pdf")


@requires_object_store
@requires_db
def test_one_projects_reconciliation_cannot_see_another_projects_objects():
    """The isolation property, asserted as a denial against a real store.

    `AGENTS.md` puts cross-tenant data access first in the review list, and this
    module reads a shared bucket that holds every tenant's files. What keeps one
    project's report to its own objects is the key prefix and nothing else, so
    it is tested rather than argued: a neighbour's object is written, and the
    first project's reconciliation must not mention it in any category.
    """
    cfg = storage_env_config()
    client = object_storage._client(cfg)
    neighbour = "rcn00002"
    stray = f"{object_storage.project_prefix(neighbour)}b/theirs.bin/{uuid.uuid4()}"
    client.put_object(Bucket=cfg.storage_s3_bucket, Key=stray, Body=b"not yours")
    try:
        report = reconcile.reconcile(cfg, _FakeConn([]), project_ref=REF, min_age_s=0)
        assert report.keys == 0, "the listing reached outside this project's prefix"
        assert report.orphaned == []
        assert all(stray != item.key for item in report.orphaned)
        assert stray not in report.in_flight
    finally:
        client.delete_object(Bucket=cfg.storage_s3_bucket, Key=stray)
