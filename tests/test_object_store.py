"""The S3 surface `storage-api` depends on, against the pinned object store.

Slice 0's bake-off, re-run. That matters rather than being ceremony: slice 0
measured **SeaweedFS 4.44** and recorded in `specs/storage-server-model.md` that
4.44 "was released the same day it was tested; slice 3 should pin a release with
some age on it rather than tracking latest". Slice 3 pins **4.41**, three
releases back — so the evidence for the pin has to move with it, or the pin
would rest on a measurement of a different build.

Not a general S3 conformance run. These are the operations upstream's own code
paths depend on, taken from its `.env.sample` and the Supabase S3-compatibility
documentation, all exercised with SigV4 and path-style addressing. The four in
bold in ADR-055 — multipart, presigned GET, presigned PUT, presign expiry — are
the ones the provider decision was called a risk on, so a failure in any of them
is a provider question rather than a workaround.

Skipped without a store. `scripts/storage-test-cluster.sh` builds one on a data
address; `MALUDB_REQUIRE_OBJECT_STORE=1` turns an absent one into a failed run,
which CI sets.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from tests.conftest import object_store_configured, requires_object_store

pytestmark = [requires_object_store]

BUCKET = os.environ.get("MALUDB_STORAGE_S3_BUCKET", "maludb").strip() or "maludb"

# 5 MiB is S3's minimum part size for every part but the last, so a multipart
# test below it proves nothing about the path `storage-api` actually takes.
PART_SIZE = 5 * 1024 * 1024


@pytest.fixture(scope="module")
def s3():
    if not object_store_configured():
        pytest.skip("no object store configured")
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["MALUDB_STORAGE_S3_ENDPOINT"].strip(),
        aws_access_key_id=os.environ["MALUDB_STORAGE_S3_ACCESS_KEY"].strip(),
        aws_secret_access_key=os.environ["MALUDB_STORAGE_S3_SECRET_KEY"].strip(),
        region_name=os.environ.get("MALUDB_STORAGE_S3_REGION", "us-east-1").strip(),
        # Path style, not virtual-host style. `storage-api` sets
        # STORAGE_S3_FORCE_PATH_STYLE for the same reason: a self-hosted store
        # reached by IP address has no per-bucket DNS to resolve.
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )
    try:
        client.head_bucket(Bucket=BUCKET)
    except Exception:  # noqa: BLE001 - a missing bucket is the normal first run
        client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture
def key() -> str:
    """A key under a prefix no other test uses, so a failure leaves no trap."""
    return f"bakeoff/{uuid.uuid4().hex}/object.bin"


# -- the basics ------------------------------------------------------------


def test_a_bucket_can_be_created_and_listed(s3):
    assert BUCKET in {b["Name"] for b in s3.list_buckets()["Buckets"]}


def test_put_get_and_head_agree_and_the_etag_is_the_md5(s3, key):
    body = b"the quick brown fox" * 100
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)

    got = s3.get_object(Bucket=BUCKET, Key=key)
    assert got["Body"].read() == body

    head = s3.head_object(Bucket=BUCKET, Key=key)
    assert head["ContentLength"] == len(body)
    # `storage-api` compares ETags to detect a changed object, so an ETag that
    # is not the MD5 of a single-part upload is a compatibility difference
    # rather than a cosmetic one.
    assert head["ETag"].strip('"') == hashlib.md5(body).hexdigest()  # noqa: S324


def test_a_conditional_get_returns_not_modified(s3, key):
    """`If-None-Match` is how a cached object is revalidated without moving the
    bytes. On the egress ceiling ADR-056 adds, that is the difference between
    serving a file and serving 200 bytes of headers."""
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"cache me")
    etag = s3.head_object(Bucket=BUCKET, Key=key)["ETag"]

    from botocore.exceptions import ClientError

    with pytest.raises(ClientError) as raised:
        s3.get_object(Bucket=BUCKET, Key=key, IfNoneMatch=etag)
    assert raised.value.response["ResponseMetadata"]["HTTPStatusCode"] == 304


def test_a_range_get_returns_only_the_range(s3, key):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"0123456789")
    got = s3.get_object(Bucket=BUCKET, Key=key, Range="bytes=2-5")
    assert got["Body"].read() == b"2345"


def test_copy_object_duplicates_without_a_round_trip(s3, key):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"original")
    destination = key + ".copy"
    s3.copy_object(Bucket=BUCKET, Key=destination, CopySource={"Bucket": BUCKET, "Key": key})
    assert s3.get_object(Bucket=BUCKET, Key=destination)["Body"].read() == b"original"


def test_list_objects_v2_paginates_by_prefix(s3):
    """The call the platform's own measurement and cleanup are built on, so its
    paging behaviour is not incidental here."""
    prefix = f"bakeoff/{uuid.uuid4().hex}/"
    for i in range(5):
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}{i}", Body=b"x")

    seen = []
    token = None
    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 2}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        seen.extend(item["Key"] for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            break
        token = page["NextContinuationToken"]

    assert sorted(seen) == sorted(f"{prefix}{i}" for i in range(5))


def test_object_tagging_round_trips(s3, key):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"tagged")
    s3.put_object_tagging(
        Bucket=BUCKET, Key=key, Tagging={"TagSet": [{"Key": "tenant", "Value": "probe"}]}
    )
    tags = s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]
    assert {"Key": "tenant", "Value": "probe"} in tags


def test_delete_removes_the_object(s3, key):
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"transient")
    s3.delete_object(Bucket=BUCKET, Key=key)

    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        s3.head_object(Bucket=BUCKET, Key=key)


def test_delete_objects_removes_a_batch(s3):
    """The call `jobs.cleanup` uses to reclaim a deleted project's files. One
    request per object would make deleting a large project a long loop that can
    fail halfway."""
    prefix = f"bakeoff/{uuid.uuid4().hex}/"
    keys = [f"{prefix}{i}" for i in range(10)]
    for k in keys:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"x")

    result = s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True}
    )
    assert not result.get("Errors")
    assert s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("KeyCount", 0) == 0


# -- multipart, which ADR-055 named as the risk ----------------------------


def test_multipart_create_upload_and_complete(s3, key):
    """Bold in ADR-055's risk list. `storage-api` uploads anything large this
    way, so a gap here changes the provider rather than the code."""
    created = s3.create_multipart_upload(Bucket=BUCKET, Key=key)
    upload_id = created["UploadId"]

    first = b"a" * PART_SIZE
    tail = b"b" * 1024
    parts = []
    for number, chunk in ((1, first), (2, tail)):
        uploaded = s3.upload_part(
            Bucket=BUCKET, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk
        )
        parts.append({"ETag": uploaded["ETag"], "PartNumber": number})

    s3.complete_multipart_upload(
        Bucket=BUCKET, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
    )

    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == first + tail


def test_multipart_can_be_listed_and_aborted(s3, key):
    """Abort is what stops an interrupted upload becoming bytes nobody can see
    and nobody is counting — the orphan problem in its smallest form."""
    upload_id = s3.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]
    s3.upload_part(
        Bucket=BUCKET, Key=key, UploadId=upload_id, PartNumber=1, Body=b"c" * PART_SIZE
    )

    listed = s3.list_multipart_uploads(Bucket=BUCKET, Prefix=key)
    assert upload_id in {u["UploadId"] for u in listed.get("Uploads", [])}

    s3.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload_id)

    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        s3.head_object(Bucket=BUCKET, Key=key)


# -- presigning, the other half of ADR-055's risk --------------------------


def test_a_presigned_get_serves_the_object_without_credentials(s3, key):
    import httpx

    s3.put_object(Bucket=BUCKET, Key=key, Body=b"presigned body")
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=60
    )
    response = httpx.get(url, timeout=10)
    assert response.status_code == 200
    assert response.content == b"presigned body"


def test_a_presigned_put_accepts_an_upload_without_credentials(s3, key):
    import httpx

    url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=60
    )
    assert httpx.put(url, content=b"uploaded by url", timeout=10).status_code == 200
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"uploaded by url"


def test_an_expired_presigned_url_is_refused(s3, key):
    """The one that matters most of the four.

    A presigned URL whose expiry is decorative is a permanent public link to a
    customer's private object, handed out by a feature whose whole promise is
    that it stops working. Slice 0 checked it against 4.44; this checks the
    build that is actually pinned.
    """
    import time

    import httpx

    s3.put_object(Bucket=BUCKET, Key=key, Body=b"briefly available")
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=1
    )
    time.sleep(2)
    assert httpx.get(url, timeout=10).status_code == 403
