"""Storage, with the official client, through the gateway (Phase 10 slice 5).

Phase 10's compatibility milestone, and the only test in the repository that
proves it: `@supabase/supabase-js` creates a bucket, uploads, downloads, lists,
signs and removes over `http://<ref>.maludb.local/storage/v1`, against a real
`storage-api` serving a real provisioned tenant. Everything in the path is the
platform's own code -- provisioning, bootstrap 012, the shared worker of
ADR-058, the gateway's key check and its two ADR-056 ceilings -- and the
hostname resolves, because the hostname *is* the routing key (ADR-008).

Two projects rather than one, and that is the acceptance criterion rather than
thoroughness: `stcp0001` and `stcp0002` hold a bucket of the same name holding
an object of the same name, and each reads back its own bytes. They also have
their own `/etc/hosts` entries, since a project ref that did not resolve would
be a test that bypassed the only thing routing a request to a tenant.

**What driving the real client found**, both now ADR-062 and both invisible to
a hand-written client because a hand-written client sends what its author
assumed:

- `supabase-js` presents the publishable key as `Authorization: Bearer <key>`.
  MaluDB keys are opaque (ADR-028) and the gateway dropped the header, which is
  right for PostgREST -- the absence of a token selects `db-anon-role` -- and
  wrong here: `storage-api` reads the bearer and refuses an empty one before
  consulting any policy. Every anonymous Storage call answered 403.
- `createSignedUrl` returns a link carrying a `token` and no `apikey`, and the
  gateway answered 401 to it. A signed URL that needs an API key to follow is
  not a signed URL.

The RLS policy this suite depends on is created by the *platform*, which is
ADR-061 rather than a shortcut: no customer-reachable role can author one, so
the platform authoring it is the arrangement under test.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
import uvicorn

from services.control_plane import api_keys, db
from services.control_plane import config as cp_config
from services.control_plane import storage_workers as sw
from services.gateway import limits
from services.gateway.app import Gateway, create_app
from tests.conftest import (
    STORAGE_IMAGE,
    TEST_CREDENTIAL,
    TEST_KEK,
    TEST_PEPPER,
    object_store_configured,
    requires_db,
    storage_env_config,
    storage_image_available,
)
from tests.test_provisioning import ADMIN_DSN, _provision_core, _tenant_admin_dsn, requires_maludb_core

# Fixed so the hosts entries are stable and can be made once during environment
# setup rather than mutated by a running test.
COMPAT_REF = "stcp0001"
OTHER_REF = "stcp0002"
GATEWAY_PORT = 28112

COMPAT_DIR = Path(__file__).parent / "compat"
DATA_HOST = os.environ.get("MALUDB_STORAGE_DB_HOST", "").strip()

# Shared with tests/compat/storage.mjs, which creates them. The policy below
# names one, so the two files have to agree.
GATED_BUCKET = "gated"


def _resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return True


requires_hostnames = pytest.mark.skipif(
    not (_resolves(f"{COMPAT_REF}.maludb.local") and _resolves(f"{OTHER_REF}.maludb.local")),
    reason=(
        f"add '127.0.0.1 {COMPAT_REF}.maludb.local' and "
        f"'127.0.0.1 {OTHER_REF}.maludb.local' to /etc/hosts"
    ),
)

pytestmark = [
    requires_db,
    pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset"),
    requires_maludb_core,
    pytest.mark.skipif(
        not (storage_image_available() and object_store_configured() and DATA_HOST),
        reason=(
            f"needs {STORAGE_IMAGE}, an object store and MALUDB_STORAGE_DB_HOST "
            "(scripts/storage-test-cluster.sh)"
        ),
    ),
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
    pytest.mark.skipif(
        not (COMPAT_DIR / "node_modules").exists(),
        reason="run `npm install` in tests/compat",
    ),
    requires_hostnames,
]


# What the platform authors on the customer's behalf. `CREATE POLICY` requires
# ownership of the table and `storage.objects` is owned by `mldb_<ref>_storage`,
# so nothing a customer reaches can write this -- which is the deferral recorded
# as `storage_policy_authoring` (ADR-061). Enforcement is what this suite
# claims, and enforcement is what exists.
#
# Deliberately two conditions. The bucket name is what makes the *negative* case
# meaningful -- a policy admitting `authenticated` to everything would pass a
# suite that only checked the happy path and would be a project with no
# isolation between its own buckets. `auth.uid()` is what makes it a claim
# rather than only a role: `storage-api` sets `request.jwt.claims` on the
# connection it switches role on, so a token whose claims were lost on the way
# through the gateway fails this even though its role survived.
GATED_POLICY = f"""
CREATE POLICY compat_gated_read ON storage.objects
    FOR SELECT TO authenticated
    USING (bucket_id = '{GATED_BUCKET}' AND auth.uid() IS NOT NULL)
"""


@pytest.fixture(scope="module")
def _module_db(migrated_database):
    """One pool for the whole module, and no truncation inside it.

    `db_pool` is function-scoped and truncates, so a module-scoped stack that
    depended on it would have its projects deleted by the first test that ran.
    tests/test_compatibility.py takes the same route for the same reason.
    """
    from services.control_plane import db as database

    database.close_pool()
    database.init_pool(migrated_database)
    yield
    database.close_pool()


def _reset_tenant(names) -> None:
    """Drop whatever a previous run left behind.

    Refs are fixed here, so a re-run meets its own leftovers rather than a clean
    cluster -- and a half-provisioned tenant fails in a way that reads like a
    provisioning bug.
    """
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{names.database}" WITH (FORCE)')
        for role in (names.authenticator, names.auth, names.admin, names.storage):
            admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def _provision(project_id: uuid.UUID, ref: str, key_ring, plan_id) -> dict:
    """A tenant the storage worker can serve, built from the platform's own code.

    Not a hand-rolled database: bootstrap 012 is what hands `storage` to the
    constrained owner and turns RLS on for every table upstream creates, and a
    fixture that built the schema itself would be a fixture asserting against
    its own idea of a tenant.
    """
    from services.control_plane import provisioning, tenant_bootstrap, workers

    names = provisioning.TenantNames.for_ref(ref)
    _reset_tenant(names)

    with db.connection() as conn:
        # Children first. Nothing truncates in this module -- it cannot, see
        # `_module_db` -- so a second run meets the first run's project row, and
        # the foreign keys exist precisely to stop it vanishing while its keys
        # and credentials still point at it.
        for statement in (
            "DELETE FROM api_keys WHERE project_id IN "
            "(SELECT id FROM projects WHERE project_ref = %s)",
            "DELETE FROM project_credentials WHERE project_id IN "
            "(SELECT id FROM projects WHERE project_ref = %s)",
            "DELETE FROM provisioning_jobs WHERE project_id IN "
            "(SELECT id FROM projects WHERE project_ref = %s)",
            "DELETE FROM projects WHERE project_ref = %s",
        ):
            db.execute(conn, statement, (ref,))
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
            "VALUES (%s, %s, %s, %s, %s, 'PLACEMENT_RESERVED')",
            (project_id, _org_for(conn, ref), ref, ref, plan_id),
        )
        conn.commit()

    admin_conn = psycopg.connect(ADMIN_DSN, row_factory=psycopg.rows.dict_row)
    try:
        _, passwords = _provision_core(project_id, admin_conn, key_ring, ref)

        with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
            tenant_conn.execute("CREATE EXTENSION IF NOT EXISTS maludb_core CASCADE")
            tenant_conn.commit()
            with db.connection() as conn:
                tenant_bootstrap.bootstrap_project(conn, tenant_conn, project_id=project_id)

        with db.connection() as conn:
            # The two credentials `storage_workers.ensure_registered` reads to
            # build the tenant's DSN and to tell the worker what a token for
            # this project is signed with. Stored the same way provisioning
            # stores them, so the gateway's registration path is the real one.
            provisioning.store_credential(
                conn, project_id=project_id, credential_type="db_storage",
                role_name=names.storage, secret=passwords["storage"], key_ring=key_ring,
            )
            workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
            db.execute(
                conn,
                "UPDATE projects SET status = 'ACTIVE', database_name = %s WHERE id = %s",
                (names.database, project_id),
            )
            conn.commit()
    finally:
        admin_conn.close()

    return {"names": names, "passwords": passwords}


def _org_for(conn, ref: str):
    """Reuse the platform account behind a fixed ref rather than insisting on a
    new one, so a re-run does not fail on the last run's user."""
    from services.control_plane import identity

    existing = db.one(
        conn,
        "SELECT m.org_id FROM users u JOIN org_members m ON m.user_id = u.id "
        " WHERE u.email = %s LIMIT 1",
        (f"{ref}@example.com",),
    )
    if existing is not None:
        return existing["org_id"]
    _, org = identity.create_user_with_personal_org(
        conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
    )
    return org


def _user_token(project_id: uuid.UUID, key_ring) -> str:
    """A signed-in user's token, minted with the tenant's own signing secret.

    Not issued by GoTrue, and the difference is worth naming: what this suite
    tests is whether a *role and its claims* reach `storage.objects` through the
    gateway, and Phase 04 already proves that GoTrue's tokens come back through
    the official client. Standing up an Auth worker here would add a second
    thing that can fail and would answer a question already answered.
    """
    import jwt as pyjwt

    from services.control_plane import provisioning

    with db.connection() as conn:
        secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    now = int(time.time())
    return pyjwt.encode(
        {
            "role": "authenticated",
            "sub": str(uuid.uuid4()),
            "iss": "maludb-compat",
            "iat": now,
            "exp": now + 3600,
        },
        secret,
        algorithm="HS256",
    )


@pytest.fixture(scope="module")
def storage_compat_stack(_module_db, tmp_path_factory):
    """Two tenants, one shared worker, and the gateway in front of both."""
    from services.control_plane import crypto

    key_ring = crypto.KeyRing(TEST_KEK)
    with db.connection() as conn:
        key_ring.load(conn)
        plan_id = db.one(
            conn,
            "INSERT INTO plans (code, name, config_json) VALUES ('storage-compat','Storage Compat',%s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
            (
                psycopg.types.json.Jsonb(
                    # Generous on purpose. ADR-056's ceilings have their own
                    # tests; here they would only turn a compatibility failure
                    # into a quota failure that reads the same from the client.
                    {"limits": {"egress_bytes_per_month": 1 << 30, "object_storage_bytes": 1 << 30}}
                ),
            ),
        )["id"]
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES ('storage-compat-node','sc.example','sc.internal','shared','active', now()) "
            "ON CONFLICT (name) DO UPDATE SET status = 'active' RETURNING id",
        )["id"]
        conn.commit()

    projects = {}
    for ref in (COMPAT_REF, OTHER_REF):
        project_id = uuid.uuid4()
        projects[ref] = _provision(project_id, ref, key_ring, plan_id)
        projects[ref]["id"] = project_id

    with db.connection() as conn:
        for entry in projects.values():
            db.execute(
                conn, "UPDATE projects SET node_id = %s WHERE id = %s", (node_id, entry["id"])
            )
        # The node's storage root secret, generated by the platform and read
        # back so the worker below is started from the same derivation the
        # gateway will register tenants with. Inventing one here and hoping they
        # matched is how this fails as `TenantNotFound` on traffic.
        root = sw.ensure_node_secret(conn, node_id=node_id, key_ring=key_ring)
        conn.commit()

    # The policy the customer cannot write (ADR-061), on the first project only:
    # the second is the cross-project half and needs no policy.
    with psycopg.connect(_tenant_admin_dsn(projects[COMPAT_REF]["names"].database)) as tenant_conn:
        tenant_conn.execute(GATED_POLICY)
        tenant_conn.commit()

    config = storage_env_config()
    secrets_ = sw.derived_secrets(root)
    settings = sw.settings_for(config, secrets_)
    config_dir = tmp_path_factory.mktemp("storage-compat")

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
        sw.ensure_metadata_database(
            admin_conn,
            password=secrets_.metadata_password,
            metadata_connect=lambda name: psycopg.connect(_tenant_admin_dsn(name), autocommit=True),
        )

    sw.write_env(settings, config_dir=config_dir)
    subprocess.run(["podman", "rm", "-f", sw.CONTAINER_NAME], capture_output=True, check=False)  # noqa: S603, S607
    log_path = config_dir / "worker.log"
    log = log_path.open("wb")
    worker = subprocess.Popen(  # noqa: S603 - generated argv, fixed binary
        sw.podman_args(settings, config_dir=config_dir), stdout=log, stderr=subprocess.STDOUT
    )

    deadline = time.time() + sw.READINESS_TIMEOUT_SECONDS
    while time.time() < deadline:
        if sw.is_ready(admin_port=settings.admin_port, api_key=secrets_.admin_api_key):
            break
        time.sleep(sw.READINESS_POLL_SECONDS)
    else:
        worker.kill()
        log.flush()
        pytest.fail(
            f"the storage worker never became ready in {sw.READINESS_TIMEOUT_SECONDS:.0f}s "
            f"(db {settings.db_host}:{settings.db_port}, s3 {settings.s3_endpoint}). "
            f"Container output:\n{log_path.read_text(errors='replace')[-4000:] or '<nothing>'}"
        )

    keys = {}
    with db.connection() as conn:
        for ref, entry in projects.items():
            keys[ref] = {
                "publishable": api_keys.create(
                    conn, project_id=entry["id"], key_type=api_keys.PUBLISHABLE,
                    pepper=TEST_PEPPER, key_ring=key_ring,
                ).plaintext,
                "secret": api_keys.create(
                    conn, project_id=entry["id"], key_type=api_keys.SECRET,
                    pepper=TEST_PEPPER, key_ring=key_ring,
                ).plaintext,
            }
        conn.commit()

    gateway_config = cp_config.Config(
        environment="test",
        database_url=os.environ["MALUDB_CONTROL_PLANE_DATABASE_URL"],
        gateway_domain="maludb.local",
        database_domain="db.maludb.local",
        docs_enabled=False,
        kek=TEST_KEK,
        token_pepper=TEST_PEPPER,
        storage_port=settings.port,
        storage_admin_port=settings.admin_port,
        storage_db_host=settings.db_host,
        storage_db_port=settings.db_port,
        storage_s3_endpoint=settings.s3_endpoint,
        storage_s3_bucket=settings.s3_bucket,
        storage_s3_access_key=settings.s3_access_key,
        storage_s3_secret_key=settings.s3_secret_key,
    )
    gateway = Gateway(
        config=gateway_config,
        key_ring=key_ring,
        wake_sleeping=False,
        # Flushed per request so a failing run's egress row is readable rather
        # than still sitting in a process that is about to exit.
        egress=limits.EgressMeter(flush_seconds=0.0),
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(gateway), host="127.0.0.1", port=GATEWAY_PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    yield {
        "projects": projects,
        "keys": keys,
        "url": f"http://{COMPAT_REF}.maludb.local:{GATEWAY_PORT}",
        "other_url": f"http://{OTHER_REF}.maludb.local:{GATEWAY_PORT}",
        "user_token": _user_token(projects[COMPAT_REF]["id"], key_ring),
        "log_path": log_path,
    }

    server.should_exit = True
    subprocess.run(["podman", "rm", "-f", sw.CONTAINER_NAME], capture_output=True, check=False)  # noqa: S603, S607
    worker.wait(timeout=30)
    log.close()
    for entry in projects.values():
        _reset_tenant(entry["names"])


@pytest.fixture(scope="module")
def compat_results(storage_compat_stack):
    """Run the official client once; every test reads one case from the output."""
    stack = storage_compat_stack
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [shutil.which("node") or "node", str(COMPAT_DIR / "storage.mjs")],  # noqa: S607
        cwd=COMPAT_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            **os.environ,
            "MALUDB_URL": stack["url"],
            "MALUDB_KEY": stack["keys"][COMPAT_REF]["publishable"],
            "MALUDB_SECRET_KEY": stack["keys"][COMPAT_REF]["secret"],
            "MALUDB_USER_TOKEN": stack["user_token"],
            "MALUDB_OTHER_URL": stack["other_url"],
            "MALUDB_OTHER_SECRET_KEY": stack["keys"][OTHER_REF]["secret"],
        },
        check=False,
    )
    cases = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            cases[json.loads(line)["name"]] = json.loads(line)
    if not cases:
        # The worker's own account of it, for the same reason the Realtime
        # compatibility test reads a container log: every server-side refusal
        # reaches this client as a message about HTTP, and the reason it was
        # refused is in a file this process can read.
        worker_log = stack["log_path"].read_text(errors="replace")[-3000:]
        pytest.fail(
            f"the client suite produced no results.\nstdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}\n--- the worker's log ---\n{worker_log}"
        )
    return cases


# One test per behaviour rather than one for the whole suite: a single failing
# operation should name itself, and `specs/compatibility-matrix.yaml` is
# promoted per feature, so the evidence has to be per feature too.
@pytest.mark.parametrize(
    "case",
    [
        "storage create bucket",
        "storage list buckets",
        "storage get bucket",
        "storage upload",
        "storage upload rejects a duplicate without upsert",
        "storage upsert",
        "storage download",
        "storage list objects",
        "storage remove",
        # ADR-062. Both are URLs the platform hands out to a caller that will
        # never hold an API key, and both were refused by the gateway before
        # this slice drove the official client at them.
        "storage public url",
        "storage signed url",
        "storage a signed url expires",
        "storage a signed url is refused by another project",
        # The phase's acceptance criteria, and the reason this file exists.
        "storage rls hides an object from an anonymous caller",
        "storage rls admits a signed-in user",
        "storage rls does not admit a signed-in user elsewhere",
        "storage rls hides objects from an anonymous list",
        "storage a project cannot reach another project's objects",
        "storage a key for another project reaches nothing",
    ],
)
def test_official_client(compat_results, case):
    result = compat_results.get(case)
    assert result is not None, f"the client suite never ran {case!r}"
    assert result["ok"], f"{case}: {result.get('error')}"


def test_the_worker_registered_both_projects_on_demand(compat_results, storage_compat_stack):
    """Migration 0025's design, exercised rather than asserted in isolation.

    Neither project was registered with the worker when the client started;
    each was registered by its own first Storage request. A test that only
    checked the operations would pass with registration done in the fixture,
    which is not the path a real first request takes.
    """
    with db.connection() as conn:
        for entry in storage_compat_stack["projects"].values():
            row = db.one(
                conn, "SELECT storage_registered_at FROM projects WHERE id = %s", (entry["id"],)
            )
            assert row["storage_registered_at"] is not None


def test_the_bytes_served_were_counted(compat_results, storage_compat_stack):
    """ADR-056's meter, on traffic that came from the official client.

    Including the anonymous ones: the public URL and the signed URL are both
    fetched without a key, and bytes served from them are the project's. A meter
    that only counted authenticated responses would leave the free tier's
    largest exposure uncounted.
    """
    from services.control_plane import object_storage

    with db.connection() as conn:
        used = object_storage.egress_used(
            conn, project_id=storage_compat_stack["projects"][COMPAT_REF]["id"]
        )
    assert used > 0, "the compatibility run served objects and recorded no egress"
