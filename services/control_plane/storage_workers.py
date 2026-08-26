"""The shared `storage-api` instance, and the tenants registered with it.

ADR-058: **one instance per node**, in `MULTI_TENANT=true` mode. That is the
opposite of ADR-034's answer for Realtime and is not in tension with it. ADR-034
was forced by a specific fact — `SLOT_NAME_SUFFIX` is server-level and
PostgreSQL replication slot names are cluster-unique, so one Realtime server
serves one tenant per cluster — and `storage-api` has no equivalent. Measured:
105.8 MB for a dedicated instance against **~0.7 MB marginal per tenant**
shared, which would have made Storage the most expensive thing a project could
enable for a capability ADR-056 puts on the free tier.

So the per-project work here is **registering a tenant**, not starting a
container. That is the difference to hold on to while reading this module: it
looks like `realtime_workers` and its unit of work is a row in somebody else's
database rather than a process.

## What holds the tenants apart

Two independent boundaries, both measured in slice 0.

1. **The host selects the tenant.** `REQUEST_X_FORWARDED_HOST_REGEXP` extracts
   a project ref from `X-Forwarded-Host` and the worker looks its configuration
   up in the multitenant database.
2. **The tenant's own JWT secret must verify.** A token signed for one tenant
   presented against another's host is refused — `403 signature verification
   failed`, measured — so a forged host header alone reaches nothing.

**The first is not a control the platform may lean on.** `X-Forwarded-Host` is
a client-supplied header on the way in, and slice 4 must set it authoritatively
and strip whatever arrived. Choosing which tenant's configuration and connection
pool a request is evaluated against is a denial-of-service and
information-disclosure surface even where the JWT check holds. Slice 0 said to
review that path as though the second boundary did not exist, and this module
does not weaken the instruction by mentioning it.

## Containment

ADR-035, unchanged and unexcepted: rootless Podman with `--network=slirp4netns`
and **no** `allow_host_loopback`. The container reaches exactly two things —
this node's PostgreSQL on the storage data address, and the object store on its
own — and cannot reach the node's loopback, where every tenant's PostgREST
answers anonymous reads to anything that can open its port.

That containment is also what makes ADR-055's exit to dedicated storage
hardware cheap rather than merely intended: the object store is addressed as
though remote because it cannot be addressed any other way, so moving it is an
endpoint change.

## What this module deliberately does not turn on

`S3_PROTOCOL_ENABLED` stays false. Upstream's S3 protocol endpoint hands a
customer an access key and a reachable endpoint, which is ADR-039's paid line
almost word for word, and Phase 10 defers it to a decision of its own rather
than letting it arrive as a default.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg import sql

from services.control_plane import crypto, db, entitlements, models, provisioning, workers

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("MALUDB_STORAGE_CONFIG_DIR", "/etc/maludb/storage"))
SERVICE_UNIT = "maludb-storage.service"
CONTAINER_NAME = "maludb-storage"

# The port inside the container. `SERVER_PORT` sets it and the unit publishes
# the node-side port onto loopback, where only the gateway reaches it.
CONTAINER_PORT = 5000
CONTAINER_ADMIN_PORT = 5001

# Node-level state, so the names carry no project ref. Not in the `mldb_`
# family: those names mean "a customer's database", and anything that sweeps a
# node by prefix should not find this.
METADATA_DATABASE = "maludb_storage_meta"
METADATA_ROLE = "maludb_storage_meta"

# The three shared Supabase role names, which the worker switches into per
# request. `DB_INSTALL_ROLES` is false, so the platform has already created them
# (ADR-016) and bootstrap 012 has already granted them what they need.
ANON_ROLE = "anon"
AUTHENTICATED_ROLE = "authenticated"
SERVICE_ROLE = "service_role"

# Boots, connects to the multitenant database, runs its own migrations. Slower
# than PostgREST and faster than Realtime's BEAM.
READINESS_TIMEOUT_SECONDS = 60.0
READINESS_POLL_SECONDS = 0.5
ADMIN_TIMEOUT_SECONDS = 15.0

# The per-object ceiling handed to the worker when a tenant is registered.
#
# **Not zero.** `fileSizeLimit: 0` reads like "no limit" and means a limit of
# zero bytes: every upload answers `413 EntityTooLarge`, which was the first
# thing the end-to-end spike found. There is no sentinel for unlimited, so this
# is a real number and it is deliberately generous -- 50 GiB, above anything a
# plan will allow.
#
# The ceiling a customer actually meets is their plan's `object_storage_bytes`
# (ADR-056), enforced by the gateway in slice 4. This one exists so that a
# request which somehow reached the worker without passing the gateway is still
# bounded by something, in the same spirit as Realtime's `MAX_CONNECTIONS`.
TENANT_FILE_SIZE_LIMIT = 50 * 1024 * 1024 * 1024


class StorageWorkerError(RuntimeError):
    """The storage worker could not be configured, started, or registered."""


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedSecrets:
    """The three secrets one node's instance needs, from one stored root.

    Derived rather than stored, on `realtime_workers.DerivedSecrets`' reasoning:
    one credential carries an instance, and separate HKDF info strings mean a
    leak of the admin key does not also hand over the key that decrypts every
    tenant's connection settings.
    """

    admin_api_key: str
    auth_encryption_key: str
    metadata_password: str


def derived_secrets(root: str) -> DerivedSecrets:
    material = root.encode()
    return DerivedSecrets(
        admin_api_key=crypto.derive_key(material, info=b"maludb-storage-admin-api-key-v1").hex(),
        # Upstream uses these characters as key material for encrypting each
        # tenant's stored settings, so base64 rather than hex for
        # `realtime_workers`' reason: the same number of characters carries half
        # again as much entropy.
        auth_encryption_key=base64.urlsafe_b64encode(
            crypto.derive_key(material, info=b"maludb-storage-auth-enc-key-v1")
        ).decode()[:32],
        metadata_password=crypto.derive_key(
            material, info=b"maludb-storage-metadata-password-v1"
        ).hex(),
    )


def node_secret(
    conn: psycopg.Connection, *, node_id: int, key_ring: crypto.KeyRing
) -> str | None:
    """This node's storage root secret, or None if it has never had one.

    The read half of `ensure_node_secret`, separated so that a caller which is
    *inspecting* a node cannot accidentally provision one. Generating a root
    from a reconciliation pass would seal a secret the running container does
    not hold, and every tenant it has already encrypted would stop being
    readable -- a repair that breaks the thing it was sent to check.
    """
    row = db.one(
        conn,
        "SELECT storage_secret_ciphertext, storage_secret_nonce, storage_secret_key_version "
        "  FROM nodes WHERE id = %s",
        (node_id,),
    )
    if row is None:
        raise StorageWorkerError(f"no node with id {node_id}")
    if row["storage_secret_ciphertext"] is None:
        return None
    return key_ring.open(
        crypto.SealedValue(
            ciphertext=bytes(row["storage_secret_ciphertext"]),
            nonce=bytes(row["storage_secret_nonce"]),
            key_version=row["storage_secret_key_version"],
        ),
        aad=crypto.aad_for("nodes", "storage_secret_ciphertext", str(node_id)),
    ).decode()


def ensure_node_secret(
    conn: psycopg.Connection, *, node_id: int, key_ring: crypto.KeyRing
) -> str:
    """This node's storage root secret, generated once and reused.

    **Reused matters more here than it does for Realtime.**
    `AUTH_ENCRYPTION_KEY` is derived from this and decrypts every registered
    tenant's connection settings in the multitenant database. Regenerating it
    would not break one project; it would leave every tenant on the node
    unreadable at once, and the failure would surface as `TenantNotFound` on
    traffic rather than as an error here.
    """
    existing = node_secret(conn, node_id=node_id, key_ring=key_ring)
    if existing is not None:
        return existing

    aad = crypto.aad_for("nodes", "storage_secret_ciphertext", str(node_id))
    root = secrets.token_hex(32)
    sealed = key_ring.seal(root.encode(), aad=aad)
    db.execute(
        conn,
        "UPDATE nodes SET storage_secret_ciphertext = %s, storage_secret_nonce = %s, "
        "storage_secret_key_version = %s WHERE id = %s",
        (sealed.ciphertext, sealed.nonce, sealed.key_version, node_id),
    )
    conn.commit()
    return root


# --------------------------------------------------------------------------
# The multitenant database
# --------------------------------------------------------------------------


def ensure_metadata_database(
    admin_conn: psycopg.Connection,
    *,
    password: str,
    metadata_connect,
) -> None:
    """Create the node's multitenant database, role and lockdown. Idempotent.

    Platform state, and node-level: one database per node holding every
    registered tenant's DSN and JWT secret. That concentration is the blast
    radius ADR-058 accepted explicitly, so the lockdown below is not boilerplate
    — it is the only thing standing between this database and any other role on
    the cluster.

    The role owns one database and nothing else. Emphatically not the platform
    owner: the container holds this password, and a container holding a
    credential that reaches other databases is what the whole arrangement exists
    to prevent.
    """
    role = sql.Identifier(METADATA_ROLE)
    exists = provisioning.role_exists(admin_conn, METADATA_ROLE)
    admin_conn.execute(
        sql.SQL(
            "{verb} {role} WITH LOGIN PASSWORD {password} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(
            verb=sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE"),
            role=role,
            password=sql.Literal(password),
        )
    )

    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (METADATA_DATABASE,))
        missing = cur.fetchone() is None
    if missing:
        # CREATE DATABASE cannot run inside a transaction block.
        admin_conn.commit()
        previous = admin_conn.autocommit
        admin_conn.autocommit = True
        try:
            admin_conn.execute(
                sql.SQL("CREATE DATABASE {db} OWNER {role}").format(
                    db=sql.Identifier(METADATA_DATABASE), role=role
                )
            )
        finally:
            admin_conn.autocommit = previous

    # ADR-014's lockdown applied to platform state. PostgreSQL grants CONNECT to
    # PUBLIC on every new database, so without this every tenant role on the
    # node could open the one database that names every tenant's DSN.
    with metadata_connect(METADATA_DATABASE) as meta_conn:
        meta_conn.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {db} FROM PUBLIC").format(
                db=sql.Identifier(METADATA_DATABASE)
            )
        )
        meta_conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=sql.Identifier(METADATA_DATABASE), role=role
            )
        )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageSettings:
    """Everything needed to render one node's storage worker environment."""

    secrets: DerivedSecrets
    # Where the container reaches this node's PostgreSQL. Never loopback --
    # validated in `render_env` rather than trusted.
    db_host: str
    db_port: int
    # The loopback ports the container publishes on the node.
    port: int
    admin_port: int
    image: str
    memory_max: str
    # The object store (ADR-055). An endpoint and a credential, never a driver.
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    # What a project ref looks like, so the worker can extract one from the
    # forwarded host. Built from the gateway domain rather than configured
    # separately, so the two cannot disagree about what a tenant is called.
    gateway_domain: str


def _reject_unusable(pairs: list[tuple[str, str]]) -> None:
    """Refuse a value that could forge a second entry in the file.

    A newline is the only character that can, since both readers take the rest
    of the line literally. Generated values cannot contain one; a configured
    endpoint, image or domain could, and this is the boundary they cross.
    """
    for name, value in pairs:
        if "\n" in value or "\r" in value:
            raise StorageWorkerError(
                f"{name} contains a newline and cannot be written to the environment file"
            )


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host.strip().lower() in {"localhost", "localhost.localdomain"}


def forwarded_host_regexp(gateway_domain: str) -> str:
    """What the worker matches `X-Forwarded-Host` against to name a tenant.

    Anchored at both ends, and the alphabet and the length are taken from
    `models` rather than written out here -- so the set of hosts that can name a
    tenant is exactly the set of strings that can be a project ref, and stays
    that way if the ref format ever changes. Written by hand first as
    `[a-z0-9]{4,16}`, which was wider than the validator and would have let the
    worker resolve a tenant name no project could ever have.

    An unanchored pattern would let `evil.com/?x=abcd0001.maludb.local` name a
    tenant. The domain is escaped because it is configuration: a dot is a
    metacharacter, so `maludb.local` unescaped also matches `maludbxlocal`.
    """
    alphabet = "".join(sorted(set(models.PROJECT_REF_ALPHABET)))
    return (
        rf"^([{re.escape(alphabet)}]{{{models.PROJECT_REF_LENGTH}}})"
        rf"\.{re.escape(gateway_domain)}$"
    )


def render_env(settings: StorageSettings) -> str:
    """Render the node's storage worker environment file.

    A pure function so the exact bytes reaching disk are testable, matching the
    other three workers. **Unquoted**: read by both systemd and
    `podman --env-file`, and the second keeps quotes as characters.
    """
    if _is_loopback(settings.db_host):
        raise StorageWorkerError(
            f"the storage data address is {settings.db_host!r}, which is loopback. The container "
            "has no access to the node's loopback by design -- with it, it reaches every other "
            "worker on the node. Prepare the node with a dedicated address and set "
            "MALUDB_STORAGE_DB_HOST to it."
        )

    endpoint_host = urlsplit(settings.s3_endpoint).hostname or ""
    if _is_loopback(endpoint_host):
        raise StorageWorkerError(
            f"the object store endpoint is {settings.s3_endpoint!r}, which is loopback. The "
            "container cannot reach it, and an endpoint that only works from the node is one "
            "that has assumed the store is co-located -- which is exactly what ADR-055's exit "
            "to dedicated hardware depends on nobody having assumed."
        )

    pairs = [
        # -- the server -------------------------------------------------
        ("SERVER_PORT", str(CONTAINER_PORT)),
        ("SERVER_HOST", "0.0.0.0"),  # noqa: S104 - inside the container's own namespace
        ("SERVER_ADMIN_PORT", str(CONTAINER_ADMIN_PORT)),
        ("LOG_LEVEL", "info"),
        # -- multi-tenancy (ADR-058) ------------------------------------
        ("MULTI_TENANT", "true"),
        ("REQUEST_X_FORWARDED_HOST_REGEXP", forwarded_host_regexp(settings.gateway_domain)),
        (
            "DATABASE_MULTITENANT_URL",
            f"postgresql://{METADATA_ROLE}:{settings.secrets.metadata_password}"
            f"@{settings.db_host}:{settings.db_port}/{METADATA_DATABASE}",
        ),
        # The admin API's credential, presented as an `apikey` header rather
        # than `Authorization` -- which answers 401 and costs an hour to
        # discover, so slice 0 wrote it down and this comment keeps it.
        ("SERVER_ADMIN_API_KEYS", settings.secrets.admin_api_key),
        # Encrypts each tenant's stored settings, including its database URL.
        ("AUTH_ENCRYPTION_KEY", settings.secrets.auth_encryption_key),
        # Off. The admin API can read back a tenant's secrets with it on, and
        # nothing the platform does needs that.
        ("ADMIN_RETURN_TENANT_SENSITIVE_DATA", "false"),
        # -- the tenant databases ---------------------------------------
        # ADR-004 and ADR-016. True would have this container create `anon`,
        # `authenticated`, `service_role` and a superuser -- names shared with
        # every other tenant on the cluster. Bootstrap 012 does the half the
        # platform owes in exchange.
        ("DB_INSTALL_ROLES", "false"),
        ("DB_ANON_ROLE", ANON_ROLE),
        ("DB_AUTHENTICATED_ROLE", AUTHENTICATED_ROLE),
        ("DB_SERVICE_ROLE", SERVICE_ROLE),
        ("DB_ALLOW_MIGRATION_REFRESH", "false"),
        ("DB_SEARCH_PATH", "storage"),
        # -- the object store (ADR-055, ADR-057) -------------------------
        ("STORAGE_BACKEND", "s3"),
        ("GLOBAL_S3_ENDPOINT", settings.s3_endpoint),
        ("GLOBAL_S3_BUCKET", settings.s3_bucket),
        ("GLOBAL_S3_FORCE_PATH_STYLE", "true"),
        ("REGION", settings.s3_region),
        ("AWS_ACCESS_KEY_ID", settings.s3_access_key),
        ("AWS_SECRET_ACCESS_KEY", settings.s3_secret_key),
        ("AWS_DEFAULT_REGION", settings.s3_region),
        # -- deferred, and off rather than defaulted ---------------------
        # A credential plus a reachable endpoint is ADR-039's paid line almost
        # word for word. Phase 10 defers it to its own decision; leaving it to
        # a default would be that decision made by omission.
        ("S3_PROTOCOL_ENABLED", "false"),
        # Needs imgproxy, which slice 0 explicitly did not clear on this CPU
        # profile -- no AVX2, which is what killed the newer Realtime image.
        ("IMAGE_TRANSFORMATION_ENABLED", "false"),
        # -- read by the unit, not by the server -------------------------
        ("MALUDB_STORAGE_HOST_PORT", str(settings.port)),
        ("MALUDB_STORAGE_ADMIN_HOST_PORT", str(settings.admin_port)),
        ("MALUDB_STORAGE_IMAGE", settings.image),
        ("MALUDB_STORAGE_MEMORY_MAX", settings.memory_max),
        ("MALUDB_STORAGE_CONTAINER", CONTAINER_NAME),
    ]
    _reject_unusable(pairs)
    header = [
        "# MaluDB storage worker, one per node (ADR-058). Generated -- do not edit.",
        "# Contains live credentials; mode 0600, never committed, never logged.",
        "# Unquoted on purpose: podman --env-file keeps quotes as characters.",
        "",
    ]
    return "\n".join(header + [f"{k}={v}" for k, v in pairs] + [""])


def write_env(settings: StorageSettings, *, config_dir: Path | None = None) -> Path:
    """Write the environment file 0600, before it has any content.

    Created private and populated second, matching the other workers: writing
    then chmod'ing leaves a window in which the object-store credential and
    every tenant's decryption key are world readable.
    """
    config_dir = config_dir or CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "storage.env"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(render_env(settings))
    return path


def podman_args(settings: StorageSettings, *, config_dir: Path | None = None) -> list[str]:
    """The container invocation, as the unit runs it.

    Kept here as well as in `deploy/maludb-storage.service` because tests need
    to start a real instance on a host with no unit installed, and an operator
    debugging a node needs to run the same thing by hand. The two are asserted
    to agree in `tests/test_storage_workers.py`.

    `--network=slirp4netns` **without** `allow_host_loopback` (ADR-035). Both
    ports are published on loopback only: the data port is the gateway's to
    reach, and the admin port can reconfigure any tenant including its database
    URL, so ADR-037 puts it firmly on the internal side.
    """
    config_dir = config_dir or CONFIG_DIR
    return [
        "podman", "run", "--rm",
        "--name", CONTAINER_NAME,
        "--network=slirp4netns",
        "--publish", f"127.0.0.1:{settings.port}:{CONTAINER_PORT}",
        "--publish", f"127.0.0.1:{settings.admin_port}:{CONTAINER_ADMIN_PORT}",
        "--env-file", str(config_dir / "storage.env"),
        "--memory", settings.memory_max,
        # Everything dropped. Unlike the Realtime image this one needs nothing
        # back: it is Node running as a non-root user with no sudo step in its
        # entrypoint, so `--cap-drop ALL` and `no-new-privileges` both hold --
        # which is a stronger containment than ADR-033 could get for Realtime,
        # and worth taking where it is available rather than matching the weaker
        # neighbour for symmetry.
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        settings.image,
    ]


def settings_for(config, secrets_: DerivedSecrets) -> StorageSettings:
    """Build the settings from a node's configuration, refusing an unprepared node.

    Refused rather than defaulted: a node with no storage data address or no
    object store endpoint cannot run a contained worker, and starting a badly
    contained one is worse than not starting one.
    """
    missing = [
        name
        for name, value in (
            ("MALUDB_STORAGE_DB_HOST", config.storage_db_host),
            ("MALUDB_STORAGE_S3_ENDPOINT", config.storage_s3_endpoint),
            ("MALUDB_STORAGE_S3_ACCESS_KEY", config.storage_s3_access_key),
            ("MALUDB_STORAGE_S3_SECRET_KEY", config.storage_s3_secret_key),
        )
        if not value
    ]
    if missing:
        raise StorageWorkerError(
            f"this node is not prepared for object storage: {', '.join(missing)} unset. "
            "See scripts/storage-test-cluster.sh and docs/STORAGE.md."
        )

    return StorageSettings(
        secrets=secrets_,
        db_host=config.storage_db_host,
        db_port=config.storage_db_port,
        port=config.storage_port,
        admin_port=config.storage_admin_port,
        image=config.storage_image,
        memory_max=config.storage_memory_max,
        s3_endpoint=config.storage_s3_endpoint,
        s3_bucket=config.storage_s3_bucket,
        s3_region=config.storage_s3_region,
        s3_access_key=config.storage_s3_access_key,
        s3_secret_key=config.storage_s3_secret_key,
        gateway_domain=config.gateway_domain,
    )


# --------------------------------------------------------------------------
# The admin API
# --------------------------------------------------------------------------


def _admin(
    method: str,
    path: str,
    *,
    admin_port: int,
    api_key: str,
    payload: dict | None = None,
    expect_missing_ok: bool = False,
) -> dict | None:
    """One call to the worker's admin API, on loopback.

    The credential goes in an **`apikey`** header. `Authorization` answers 401,
    which slice 0 recorded because it costs an hour to discover and reads like a
    wrong key rather than a wrong header.
    """
    url = f"http://127.0.0.1:{admin_port}{path}"
    try:
        response = httpx.request(
            method,
            url,
            headers={"apikey": api_key, "content-type": "application/json"},
            content=json.dumps(payload) if payload is not None else None,
            timeout=ADMIN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        # Never the exception's text: a transport error can echo the URL, and
        # the URL is harmless but the habit is worth keeping consistent.
        raise StorageWorkerError(
            f"the storage worker's admin API did not answer ({type(exc).__name__})"
        ) from None

    if expect_missing_ok and response.status_code == 404:
        return None
    if response.status_code >= 400:
        # The body can carry a tenant's configuration; the status alone is what
        # a caller needs and what a log should hold.
        raise StorageWorkerError(
            f"the storage worker's admin API answered {response.status_code} to {method} {path}"
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def tenant_payload(
    *,
    project_ref: str,
    tenant_dsn: str,
    jwt_secret: str,
    database_pool_size: int,
) -> dict:
    """What the admin API is told about one tenant.

    The DSN and the JWT secret are the whole of it, and both are the platform's
    own: the customer never supplies either. `jwtSecret` is the project's own
    signing secret, shared with PostgREST and GoTrue -- a second secret here
    would make every token the gateway accepts fail at the Storage API.
    """
    return {
        "anonKey": "",
        "serviceKey": "",
        "jwtSecret": jwt_secret,
        "databaseUrl": tenant_dsn,
        "databasePoolUrl": tenant_dsn,
        "maxConnections": database_pool_size,
        # See TENANT_FILE_SIZE_LIMIT: a backstop, not the product's limit. Zero
        # here does not mean unlimited, it means nothing can be uploaded.
        "fileSizeLimit": TENANT_FILE_SIZE_LIMIT,
        "features": {
            # Both deferred by Phase 10 and named rather than defaulted, for the
            # reason `render_env` gives: a deferral that arrives by omission is
            # a decision nobody made.
            "imageTransformation": {"enabled": False},
            "s3Protocol": {"enabled": False},
        },
    }


def register_tenant(
    *,
    admin_port: int,
    api_key: str,
    project_ref: str,
    tenant_dsn: str,
    jwt_secret: str,
    database_pool_size: int = 5,
) -> None:
    """Tell the shared worker about one tenant. Idempotent.

    **PUT, not POST, and the difference is not stylistic.** Upstream offers
    both on `/tenants/{id}`: `POST` inserts and answers `500` with a primary key
    violation if the tenant is already there, while `PUT` calls its
    `upsertTenantAndGenerateJwk` and rewrites the configuration. Measured, by
    writing `POST` here first and watching the second provisioning run of the
    same project fail.

    That makes the whole retry story work: this is safe on a provisioning
    retry, safe to re-run when a plan changes the pool size, and safe to run for
    every project on a node when a worker is rebuilt.
    """
    if not models.is_valid_project_ref(project_ref):
        raise StorageWorkerError(f"invalid project ref {project_ref!r}")
    _admin(
        "PUT",
        f"/tenants/{project_ref}",
        admin_port=admin_port,
        api_key=api_key,
        payload=tenant_payload(
            project_ref=project_ref,
            tenant_dsn=tenant_dsn,
            jwt_secret=jwt_secret,
            database_pool_size=database_pool_size,
        ),
    )


def deregister_tenant(*, admin_port: int, api_key: str, project_ref: str) -> None:
    """Remove a tenant from the shared worker.

    A 404 is success: the goal is that the worker does not serve this tenant,
    and a worker that has never heard of it already meets that.
    """
    if not models.is_valid_project_ref(project_ref):
        raise StorageWorkerError(f"invalid project ref {project_ref!r}")
    _admin(
        "DELETE",
        f"/tenants/{project_ref}",
        admin_port=admin_port,
        api_key=api_key,
        expect_missing_ok=True,
    )


def tenant_known(*, admin_port: int, api_key: str, project_ref: str) -> bool:
    """Whether the worker currently holds a configuration for this tenant.

    Presence, and nothing else. The admin API answers this with the tenant's
    **whole** configuration -- its database URL, which carries a live password,
    and its JWT signing secret -- so the body is discarded here rather than
    returned. A caller that never receives it cannot log it, which is the same
    rule `_admin` follows for error bodies and for the same reason.

    A 404 is the answer this exists to get: it is what a worker whose
    multitenant database was rebuilt says about a tenant the control plane
    believes it serves.
    """
    if not models.is_valid_project_ref(project_ref):
        raise StorageWorkerError(f"invalid project ref {project_ref!r}")
    found = _admin(
        "GET",
        f"/tenants/{project_ref}",
        admin_port=admin_port,
        api_key=api_key,
        expect_missing_ok=True,
    )
    return found is not None


def is_ready(*, admin_port: int, api_key: str, timeout: float = 2.0) -> bool:
    """Whether the worker is up and has migrated its multitenant database.

    Asks a question only a migrated instance can answer, rather than checking
    that a port is open: the container accepts connections before it has run its
    own migrations, and a readiness check that a half-started worker passes is
    how a tenant gets registered into a database that has no table for it.
    """
    try:
        response = httpx.get(
            f"http://127.0.0.1:{admin_port}/tenants",
            headers={"apikey": api_key},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def supervisor(**kwargs) -> workers.SystemdSupervisor:
    """The node's single storage unit.

    Not a template unit: `maludb-storage.service`, one per node. The supervisor
    takes a project ref for the other three workers and this one has none, so
    the template is a constant and the ref is ignored.
    """
    kwargs.setdefault("template", SERVICE_UNIT)
    return workers.SystemdSupervisor(**kwargs)


# --------------------------------------------------------------------------
# Registration bookkeeping
# --------------------------------------------------------------------------


def mark_registered(conn: psycopg.Connection, project_id: uuid.UUID) -> None:
    db.execute(
        conn,
        "UPDATE projects SET storage_registered_at = now() WHERE id = %s",
        (project_id,),
    )


def mark_unregistered(conn: psycopg.Connection, project_id: uuid.UUID) -> None:
    db.execute(
        conn,
        "UPDATE projects SET storage_registered_at = NULL WHERE id = %s",
        (project_id,),
    )


def registered_projects(conn: psycopg.Connection, *, node_id: int) -> list[dict]:
    """Every project this node's worker is expected to serve.

    What a restarted worker owes registration to. The multitenant database
    survives a restart, so this is a repair path rather than the normal one --
    and it exists because "the container has the truth" is exactly the
    assumption that fails after the one restart nobody watched.
    """
    return db.query(
        conn,
        "SELECT id, project_ref FROM projects "
        " WHERE node_id = %s AND deleted_at IS NULL AND storage_registered_at IS NOT NULL "
        " ORDER BY project_ref",
        (node_id,),
    )


def ensure_registered(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    project_ref: str,
    node_id: int,
    config,
    key_ring: crypto.KeyRing,
) -> bool:
    """Register one project with its node's shared worker, and record it.

    The per-project half of a shared instance, in one place because it has two
    callers with very different failure appetites. Provisioning calls it once
    and treats a failure as a delay rather than a failed project (ADR-058);
    the gateway calls it on the request that needs it, which is what migration
    0025 meant by "a project that is not registered is simply one whose next
    Storage request registers it". Two copies of this would be two chances for
    the DSN, the pool size or the secret derivation to drift apart.

    Returns False where the node is not prepared for object storage. That is a
    deployment without Storage rather than a broken one, and the caller turns
    it into a 404 for the surface rather than a 503 for the node.

    Raises `StorageWorkerError` if the worker refuses or cannot be reached --
    never with the driver's message attached, because the DSN built here
    carries a live password and a connection error can echo the statement it
    came from.
    """
    if config is None or not config.storage_s3_endpoint:
        return False

    names = provisioning.TenantNames.for_ref(project_ref)
    try:
        root = ensure_node_secret(conn, node_id=node_id, key_ring=key_ring)
        secrets_ = derived_secrets(root)
        storage_password = provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_storage", key_ring=key_ring,
        )
        jwt_secret = provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring,
        )
        allowed = entitlements.for_project(conn, project_id)
        dsn = (
            f"postgresql://{names.storage}:{storage_password}"
            f"@{config.storage_db_host}:{config.storage_db_port}/{names.database}"
        )
        register_tenant(
            admin_port=config.storage_admin_port,
            api_key=secrets_.admin_api_key,
            project_ref=project_ref,
            tenant_dsn=dsn,
            jwt_secret=jwt_secret,
            database_pool_size=allowed.postgrest_pool_size,
        )
    except StorageWorkerError:
        raise
    except Exception as exc:  # noqa: BLE001 - see the docstring on the message
        raise StorageWorkerError(
            f"could not register {project_ref} with the storage worker ({type(exc).__name__})"
        ) from None
    mark_registered(conn, project_id)
    return True
