"""Per-project Realtime workers (ADR-027, ADR-033, ADR-034).

Phase 06 slice 5: what the slice 4 spike drove by hand, as code. It follows
`auth_workers.py` wherever it can -- the systemd supervisor, port allocation,
an environment file written 0600 -- and departs from it in four places, each of
which was measured against a running instance rather than assumed.

**The worker is a container, and the container is not on the node's network.**
Upstream ships an image only (ADR-033), so the unit runs `podman run` rather
than a binary. Rootless Podman can reach a host service two ways, and the
difference is the whole of this module's security posture: with
`slirp4netns`'s `allow_host_loopback` on, the instance reached `127.0.0.1:5432`
-- a *different* PostgreSQL cluster, carrying tenants this project has nothing
to do with -- and, by the same route, every loopback-bound worker on the node.
A tenant's PostgREST answers anonymous requests through `db-anon-role` to
anything that can open its port, so that arrangement would hand a compromised
Realtime container the anon-visible data of every project on the node, past the
gateway and past ADR-028's keys entirely.

So the container gets no host loopback, and PostgreSQL is reached at a
**dedicated non-loopback address on the node** (`realtime_db_host`). Measured:
without the flag the container reaches no loopback service at all, and still
reaches an address on the node's own interface. That address is node
preparation, and ADR-031's `pg_hba.conf` reject has to name it too -- an address
added without one re-opens the physical-replication hole ADR-031 closed.

**The metadata database is per project.** `CLUSTER_STRATEGIES` defaults to
`POSTGRES` in a production release and discovers peers *through the metadata
database*, so instances sharing one would form a single distributed Erlang
cluster spanning every tenant on the node. One database each, and
`CLUSTER_STRATEGIES=NONE` besides.

**One host port per instance, not two.** Slice 4 recorded two because it ran the
container on the host's network. With a network namespace per instance, gen_rpc
binds the same port inside every one of them and never leaves.

**The environment file is unquoted**, unlike the GoTrue one. `podman --env-file`
takes the rest of the line literally and keeps quotes as characters where
systemd strips them, so a quoted password would be wrong by two bytes and would
fail as an authentication error rather than as a syntax one. Values are checked
for newlines instead, which is the only character that could forge an entry.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path

import jwt
import psycopg
from psycopg import sql

from services.control_plane import crypto, db, entitlements, models, provisioning, workers

log = logging.getLogger(__name__)

CONFIG_DIR = Path("/etc/maludb/realtime")

SERVICE_TEMPLATE = "maludb-realtime@{ref}.service"

# The port inside the container. Fixed rather than allocated: the namespace is
# per instance, so every instance can use upstream's default and only the
# published loopback port differs.
CONTAINER_PORT = 4000

# The schema upstream keeps its tenant registry in, inside the metadata
# database. The platform creates it and the server migrates within it:
# DB_AFTER_CONNECT_QUERY sets search_path to this schema and the migrator does
# not create it, so a missing schema fails with `no schema has been selected to
# create in` before anything else happens.
METADATA_SCHEMA = "_realtime"

# One 32-byte root secret per instance, stored once; everything else the server
# needs is derived from it (see `derived_secrets`). Class B under ADR-023 --
# recoverable, because the server needs the literal values at every start.
CREDENTIAL_TYPE = "realtime_server"
# The metadata database role's password. Stored rather than derived because it
# is a database credential and must be rotatable without moving every other
# secret the instance holds.
METADATA_CREDENTIAL_TYPE = "db_realtime_meta"

# DB_ENC_KEY is used as raw AES-128 key material, so it is exactly 16 bytes.
DB_ENC_KEY_CHARS = 16

# Realtime boots the BEAM, connects to its metadata database and runs its own
# migrations before it serves, which is slower than either other worker.
READINESS_TIMEOUT_SECONDS = 90.0
READINESS_POLL_SECONDS = 0.5

# The admin API is on loopback and the token is minted for one call.
ADMIN_TOKEN_TTL_SECONDS = 60
ADMIN_TIMEOUT_SECONDS = 15.0


class RealtimeWorkerError(RuntimeError):
    """A Realtime worker could not be configured, started, or registered."""


@dataclass(frozen=True)
class RealtimeNames:
    """Identifiers derived from one project ref, validated at construction.

    The metadata database is deliberately **not** in the `mldb_` family. Those
    names mean "a customer's database"; this one is platform state that happens
    to be per project, and the distinction matters to anything that sweeps a
    node by prefix.
    """

    project_ref: str
    metadata_database: str
    metadata_role: str
    container: str
    app_name: str

    @classmethod
    def for_ref(cls, project_ref: str) -> RealtimeNames:
        # Raises on anything outside the strict alphabet, so every name below
        # is safe to quote as an identifier and safe to pass to systemd and
        # Podman as a unit and container name.
        ref = models.database_name_for(project_ref).removeprefix("mldb_")
        return cls(
            project_ref=project_ref,
            metadata_database=f"maludb_realtime_{ref}",
            metadata_role=f"maludb_realtime_{ref}",
            container=f"maludb-realtime-{ref}",
            app_name=f"realtime-{ref}",
        )

    @property
    def slot_suffix(self) -> str:
        """What upstream appends to its slot names (ADR-034).

        The same value `realtime.slot_names_for` predicts, and it must stay that
        way: the platform reserves capacity for slots it does not create, and a
        suffix that disagreed would leave the reservation pointing at names the
        server never uses.
        """
        return models.database_name_for(self.project_ref).removeprefix("mldb_")


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedSecrets:
    """The three secrets the server needs, derived from one stored root.

    Derived rather than stored so that one credential row carries an instance,
    and separated by HKDF info strings so that a leak of one -- the metrics
    token, say -- does not hand over the admin API. Deriving also means a
    rebuilt instance gets the same values without a second round trip.
    """

    api_jwt: str
    metrics_jwt: str
    secret_key_base: str
    db_enc_key: str


def derived_secrets(root: str) -> DerivedSecrets:
    material = root.encode()
    api = crypto.derive_key(material, info=b"maludb-realtime-api-jwt-v1").hex()
    return DerivedSecrets(
        api_jwt=api,
        metrics_jwt=crypto.derive_key(material, info=b"maludb-realtime-metrics-jwt-v1").hex(),
        # Phoenix wants at least 64 characters; 32 bytes of hex is exactly that.
        secret_key_base=crypto.derive_key(material, info=b"maludb-realtime-secret-key-base-v1").hex(),
        db_enc_key=crypto.derive_key(material, info=b"maludb-realtime-db-enc-key-v1").hex()[:DB_ENC_KEY_CHARS],
    )


def ensure_server_secret(
    conn: psycopg.Connection, *, project_id: uuid.UUID, key_ring: crypto.KeyRing
) -> str:
    """This instance's root secret, generated once and reused.

    Reused rather than regenerated at every start, because `DB_ENC_KEY` is
    derived from it and that key decrypts the tenant connection settings already
    written to the metadata database. A fresh root would leave a registered
    tenant the server can no longer read.
    """
    try:
        return provisioning.load_credential(
            conn, project_id=project_id, credential_type=CREDENTIAL_TYPE, key_ring=key_ring
        )
    except provisioning.ProvisioningError:
        pass

    root = secrets.token_hex(32)
    provisioning.store_credential(
        conn,
        project_id=project_id,
        credential_type=CREDENTIAL_TYPE,
        role_name=None,
        secret=root,
        key_ring=key_ring,
    )
    return root


# --------------------------------------------------------------------------
# The metadata database
# --------------------------------------------------------------------------


def ensure_metadata_database(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    names: RealtimeNames,
    key_ring: crypto.KeyRing,
    metadata_connect,
) -> str:
    """Create this instance's metadata database, role and schema. Idempotent.

    Platform state, not tenant state: `specs/realtime-server-model.md` requires
    it to live outside the tenant database, where it would otherwise be platform
    bookkeeping inside a customer's data and reachable from their Data API.

    The role is an ordinary login role owning one database. It is emphatically
    not the platform owner: the container holds this password, and a container
    holding a credential that can reach other databases is the thing the whole
    arrangement is built to avoid.

    Returns the role's password, reusing the stored one when there is one. Not
    regenerated on every call on purpose: enabling Realtime is safe to re-run,
    and a re-run that rotated this password would leave a *running* instance
    holding one that no longer works -- a failure that would surface later, on
    its next reconnect, rather than here.
    """
    try:
        password = provisioning.load_credential(
            conn, project_id=project_id, credential_type=METADATA_CREDENTIAL_TYPE, key_ring=key_ring
        )
    except provisioning.ProvisioningError:
        password = provisioning.generate_password()
    role = sql.Identifier(names.metadata_role)
    exists = provisioning.role_exists(admin_conn, names.metadata_role)
    admin_conn.execute(
        sql.SQL("{verb} {role} WITH LOGIN PASSWORD {password} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
            verb=sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE"),
            role=role,
            password=sql.Literal(password),
        )
    )
    provisioning.store_credential(
        conn,
        project_id=project_id,
        credential_type=METADATA_CREDENTIAL_TYPE,
        role_name=names.metadata_role,
        secret=password,
        key_ring=key_ring,
    )
    conn.commit()

    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (names.metadata_database,))
        if cur.fetchone() is None:
            # CREATE DATABASE cannot run inside a transaction block.
            admin_conn.commit()
            previous = admin_conn.autocommit
            admin_conn.autocommit = True
            try:
                admin_conn.execute(
                    sql.SQL("CREATE DATABASE {db} OWNER {role}").format(
                        db=sql.Identifier(names.metadata_database), role=role
                    )
                )
            finally:
                admin_conn.autocommit = previous

    # The schema the server migrates into. Owned by the role so the migrations
    # run as an ordinary user, which is the same principle slice 4 established
    # for the tenant migrations.
    with metadata_connect(names.metadata_database) as meta_conn:
        meta_conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {role}").format(
                schema=sql.Identifier(METADATA_SCHEMA), role=role
            )
        )
    return password


def drop_metadata_database(admin_conn: psycopg.Connection, names: RealtimeNames) -> None:
    """Remove the metadata database and its role, after the server has stopped.

    Called when Realtime is turned off, in the same spirit as dropping the
    replicator role: turning a capability off should reduce what exists, not
    only what is reachable. A metadata database left behind holds an encrypted
    copy of the tenant's replicator credential.
    """
    previous = admin_conn.autocommit
    admin_conn.commit()
    admin_conn.autocommit = True
    try:
        admin_conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {db} WITH (FORCE)").format(
                db=sql.Identifier(names.metadata_database)
            )
        )
        admin_conn.execute(
            sql.SQL("DROP ROLE IF EXISTS {role}").format(role=sql.Identifier(names.metadata_role))
        )
    finally:
        admin_conn.autocommit = previous


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RealtimeSettings:
    """Everything needed to render one project's Realtime environment."""

    project_ref: str
    names: RealtimeNames
    # The tenant database and the credential the server reads its WAL with.
    tenant_database: str
    replicator_role: str
    replicator_password: str
    # The project's own signing secret, shared with PostgREST and GoTrue. The
    # gateway mints socket tokens with it, so a second secret here would make
    # every connection the gateway authorised fail at the channel join.
    jwt_secret: str
    # The metadata database, which is this instance's own state.
    metadata_password: str
    secrets: DerivedSecrets
    # The loopback port the container publishes on the node.
    port: int
    # The node's Realtime data address. Never loopback -- see the module
    # docstring -- and validated in `render_env` rather than trusted.
    db_host: str
    db_port: int
    image: str
    memory_max: str
    # From the plan. The gateway counts sockets too (`limits.SocketLimiter`);
    # this is the same number enforced by the server itself, so a connection
    # that somehow reached it without passing the gateway is still bounded.
    max_concurrent_users: int


def _reject_unusable(pairs: list[tuple[str, str]]) -> None:
    """Refuse a value that could forge a second entry in the file.

    A newline is the only character that can, since both readers take the rest
    of the line literally. Generated values cannot contain one; a configured
    `db_host` or image could, and this is the boundary they cross.
    """
    for name, value in pairs:
        if "\n" in value or "\r" in value:
            raise RealtimeWorkerError(f"{name} contains a newline and cannot be written to the environment file")


def render_env(settings: RealtimeSettings) -> str:
    """Render one project's Realtime environment file.

    A pure function so the exact bytes reaching disk are testable, matching
    `workers.render_config` and `auth_workers.render_env`. **Unquoted**: this
    file is read by both systemd and `podman --env-file`, and the second keeps
    quotes as characters.
    """
    if _is_loopback(settings.db_host):
        raise RealtimeWorkerError(
            f"the Realtime data address is {settings.db_host!r}, which is loopback. The container "
            "has no access to the node's loopback by design -- with it, it reaches every other "
            "worker and every other cluster on the node. Prepare the node with a dedicated "
            "address and set MALUDB_REALTIME_DB_HOST to it."
        )

    pairs = [
        # -- the server itself ------------------------------------------
        ("PORT", str(CONTAINER_PORT)),
        ("APP_NAME", settings.names.app_name),
        # ADR-034: the slot names are server-level, and this is what makes two
        # tenants on one cluster possible at all.
        ("SLOT_NAME_SUFFIX", settings.names.slot_suffix),
        # Discovery defaults to POSTGRES, which would look for peers through the
        # metadata database. There is exactly one node here and it should not
        # look for others.
        ("CLUSTER_STRATEGIES", "NONE"),
        ("SEED_SELF_HOST", "false"),
        ("RUN_JANITOR", "true"),
        ("LOG_LEVEL", "info"),
        # IPv4 throughout. The image defaults to IPv6 for both Ecto and the
        # listener, and the container's namespace has no IPv6 route to the
        # node's data address.
        ("ECTO_IPV6", "false"),
        ("DB_IP_VERSION", "ipv4"),
        ("REALTIME_IP_VERSION", "ipv4"),
        ("ERL_AFLAGS", "-proto_dist inet_tcp"),
        # -- the metadata database --------------------------------------
        ("DB_HOST", settings.db_host),
        ("DB_PORT", str(settings.db_port)),
        ("DB_NAME", settings.names.metadata_database),
        ("DB_USER", settings.names.metadata_role),
        ("DB_PASSWORD", settings.metadata_password),
        ("DB_AFTER_CONNECT_QUERY", f"SET search_path TO {METADATA_SCHEMA}"),
        # -- secrets ----------------------------------------------------
        ("DB_ENC_KEY", settings.secrets.db_enc_key),
        ("API_JWT_SECRET", settings.secrets.api_jwt),
        # `fetch_env!` in a production release: without it the server does not
        # boot at all, which is not obvious from upstream's own compose file.
        ("METRICS_JWT_SECRET", settings.secrets.metrics_jwt),
        ("SECRET_KEY_BASE", settings.secrets.secret_key_base),
        # -- per-tenant ceilings, from the plan --------------------------
        ("MAX_CONNECTIONS", str(settings.max_concurrent_users)),
        ("TENANT_MAX_CONCURRENT_USERS", str(settings.max_concurrent_users)),
        # -- read by the unit, not by the server -------------------------
        # systemd expands these in ExecStart. They reach the container too,
        # since the file is also the --env-file; the server ignores what it does
        # not know, and keeping one file means the unit cannot drift from the
        # configuration the control plane wrote.
        ("MALUDB_REALTIME_HOST_PORT", str(settings.port)),
        ("MALUDB_REALTIME_IMAGE", settings.image),
        ("MALUDB_REALTIME_MEMORY_MAX", settings.memory_max),
        ("MALUDB_REALTIME_CONTAINER", settings.names.container),
    ]
    _reject_unusable(pairs)
    header = [
        f"# MaluDB project {settings.project_ref}. Generated -- do not edit.",
        "# Contains live credentials; mode 0600, never committed, never logged.",
        "# Unquoted on purpose: podman --env-file keeps quotes as characters.",
        "",
    ]
    return "\n".join(header + [f"{k}={v}" for k, v in pairs] + [""])


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        # A hostname rather than an address. `localhost` is the one worth
        # naming; anything else resolves on the node and cannot be judged here.
        return host.strip().lower() in {"localhost", "localhost.localdomain"}


def write_env(settings: RealtimeSettings, *, config_dir: Path = CONFIG_DIR) -> Path:
    """Write the environment file 0600, before it has any content.

    Created private and populated second, matching the other two workers:
    writing then chmod'ing leaves a window in which the replicator password --
    the highest-value credential the platform issues -- is world readable.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{settings.project_ref}.env"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(render_env(settings))
    return path


def podman_args(settings: RealtimeSettings, *, config_dir: Path = CONFIG_DIR) -> list[str]:
    """The container invocation, as the unit runs it.

    Kept here as well as in `deploy/maludb-realtime@.service` because tests need
    to start a real instance on a host that has no systemd unit installed, and
    an operator debugging a node needs to run the same thing by hand. The two
    are asserted to agree in `tests/test_realtime_workers.py`; if this list and
    the unit ever diverge, that test is what says so.

    `--network=slirp4netns` **without** `allow_host_loopback`, which is the
    whole point: see the module docstring.
    """
    return [
        "podman", "run", "--rm",
        "--name", settings.names.container,
        "--network=slirp4netns",
        "--publish", f"127.0.0.1:{settings.port}:{CONTAINER_PORT}",
        "--env-file", str(config_dir / f"{settings.project_ref}.env"),
        # ADR-034 measured ~146 MB. Well above it, so an ordinary instance never
        # meets the cap and a runaway one cannot take the node's other tenants
        # down with it -- the same reasoning as the other two units' MemoryMax.
        "--memory", settings.memory_max,
        # Everything dropped except the two the image genuinely needs. Its
        # entrypoint runs the migration step as `nobody` via sudo, so a bare
        # `--cap-drop ALL` fails at `setresuid` before the BEAM starts -- with
        # `no valid sudoers sources found`, which reads like a broken image
        # rather than a capability the platform removed.
        #
        # `no-new-privileges` is deliberately *not* set for the same reason: it
        # would defeat the same sudo. What contains this process is the user
        # namespace it runs in (rootless Podman, ADR-033) and the network
        # namespace with no route to the node's loopback.
        "--cap-drop", "ALL",
        "--cap-add", "SETUID",
        "--cap-add", "SETGID",
        settings.image,
    ]


def settings_for(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    config,
    metadata_password: str,
) -> RealtimeSettings:
    """Assemble one project's Realtime configuration from stored state.

    Every credential is read back through the key ring; nothing here accepts a
    plaintext secret from a caller except the metadata password, which its
    creator has just generated and which is written nowhere else.
    """
    project = db.one(
        conn,
        "SELECT project_ref, database_name, realtime_enabled, realtime_port "
        "  FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None:
        raise RealtimeWorkerError("project does not exist")
    if project["database_name"] is None:
        raise RealtimeWorkerError("project has no database; provision it before starting Realtime")
    if not project["realtime_enabled"]:
        raise RealtimeWorkerError(
            "Realtime is not enabled for this project. Enabling creates the replicator role and "
            "reserves the node's slots, and a worker without them has nothing to read."
        )

    names = RealtimeNames.for_ref(project["project_ref"])
    tenant = provisioning.TenantNames.for_ref(project["project_ref"])
    port = project["realtime_port"] or workers.allocate_port(
        conn, project_id=project_id, column="realtime_port"
    )
    conn.commit()

    allowed = entitlements.for_project(conn, project_id)
    return RealtimeSettings(
        project_ref=project["project_ref"],
        names=names,
        tenant_database=project["database_name"],
        replicator_role=tenant.replicator,
        replicator_password=provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_replicator", key_ring=key_ring
        ),
        jwt_secret=workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring),
        metadata_password=metadata_password,
        secrets=derived_secrets(
            ensure_server_secret(conn, project_id=project_id, key_ring=key_ring)
        ),
        port=port,
        db_host=config.realtime_db_host,
        db_port=config.realtime_db_port,
        image=config.realtime_image,
        memory_max=config.realtime_memory_max,
        max_concurrent_users=max(1, allowed.realtime_connections),
    )


# --------------------------------------------------------------------------
# The server's admin API
# --------------------------------------------------------------------------


def admin_token(api_secret: str) -> str:
    """A short-lived token for this instance's admin API.

    Minted per call and valid for a minute: it never leaves the node, and the
    only thing that holds it is the process that just made it.
    """
    now = int(time.time())
    return jwt.encode(
        {"role": "service_role", "iat": now, "exp": now + ADMIN_TOKEN_TTL_SECONDS},
        api_secret,
        algorithm="HS256",
    )


def _admin_call(
    *, port: int, path: str, method: str, api_secret: str, body: dict | None = None,
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {admin_token(api_secret)}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed scheme, loopback
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def tenant_payload(settings: RealtimeSettings) -> dict:
    """What the server is told about this project.

    Two things the spike found the hard way, recorded in
    `specs/realtime-server-model.md` and encoded here so they cannot be
    relearned: `external_id` is the **project ref alone** and never the
    hostname, because the server resolves a tenant from the first label of the
    `Host` header; and the connection settings name the *replicator*, whose
    password is the credential slice 2 stored and this is the only consumer of.
    """
    return {
        "tenant": {
            "name": settings.project_ref,
            "external_id": settings.project_ref,
            # The project's own signing secret. The gateway mints the socket's
            # token with the same one.
            "jwt_secret": settings.jwt_secret,
            "max_concurrent_users": settings.max_concurrent_users,
            "extensions": [
                {
                    "type": "postgres_cdc_rls",
                    "settings": {
                        "db_name": settings.tenant_database,
                        "db_host": settings.db_host,
                        "db_port": str(settings.db_port),
                        "db_user": settings.replicator_role,
                        "db_password": settings.replicator_password,
                        "region": "local",
                        "poll_interval_ms": 100,
                        "poll_max_record_bytes": 1_048_576,
                        "ssl_enforced": False,
                    },
                }
            ],
        }
    }


def register_tenant(settings: RealtimeSettings) -> None:
    """Register this project with its own Realtime server.

    Idempotent, and measured to be: the server answers 201 the first time and
    200 thereafter, so a retried enablement or a restarted worker re-registers
    without a conditional. `AGENTS.md` asks that of provisioning operations, and
    here it is upstream's behaviour rather than ours.
    """
    status, body = _admin_call(
        port=settings.port,
        path="/api/tenants",
        method="POST",
        api_secret=settings.secrets.api_jwt,
        body=tenant_payload(settings),
    )
    if status not in (200, 201):
        # The body echoes the request's settings on a validation error, and
        # those settings contain the replicator password. Only the status is
        # surfaced; the body goes nowhere.
        log.error("registering project %s with its Realtime server failed (%s)", settings.project_ref, status)
        raise RealtimeWorkerError(f"the Realtime server refused the tenant registration ({status})")
    log.info("project %s registered with its Realtime server", settings.project_ref)


def deregister_tenant(*, port: int, api_secret: str, project_ref: str) -> None:
    """Remove this project from its Realtime server. Also idempotent (204 twice)."""
    status, _ = _admin_call(
        port=port, path=f"/api/tenants/{project_ref}", method="DELETE", api_secret=api_secret
    )
    if status not in (200, 202, 204, 404):
        raise RealtimeWorkerError(f"the Realtime server refused to deregister the tenant ({status})")


def is_ready(port: int, *, api_secret: str, timeout: float = 2.0) -> bool:
    """Whether the server will actually answer.

    `GET /api/tenants` rather than a bare connect, for the same reason PostgREST
    is asked for a response rather than a socket: this request goes through the
    metadata database, so a 200 proves the server booted *and* migrated. The
    tenant-scoped endpoints cannot be used -- they resolve a tenant from the
    `Host` header and answer 401 until one is registered, which is after this.
    """
    try:
        status, _ = _admin_call(
            port=port, path="/api/tenants", method="GET", api_secret=api_secret, timeout=timeout
        )
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return status == 200


def wait_until_ready(
    port: int, *, api_secret: str, timeout: float = READINESS_TIMEOUT_SECONDS
) -> float:
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        if is_ready(port, api_secret=api_secret):
            return time.monotonic() - started
        time.sleep(READINESS_POLL_SECONDS)
    raise RealtimeWorkerError(f"Realtime on port {port} did not become ready within {timeout:.0f}s")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def supervisor(**kwargs) -> workers.SystemdSupervisor:
    """A supervisor bound to the Realtime unit template."""
    return workers.SystemdSupervisor(template=SERVICE_TEMPLATE, **kwargs)


def _set_state(conn: psycopg.Connection, project_id: uuid.UUID, state: str) -> None:
    db.execute(
        conn, "UPDATE projects SET realtime_worker_state = %s WHERE id = %s", (state, project_id)
    )
    conn.commit()


def start_worker(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    config,
    supervisor: workers.Supervisor,
    config_dir: Path = CONFIG_DIR,
) -> float:
    """Configure, start and register a project's Realtime instance.

    Returns how long it took to become ready. Safe to call on a project whose
    instance is already running: the environment is re-rendered and the tenant
    re-registered rather than the container restarted, so two concurrent wakes
    do not bounce a serving instance.

    **Takes no node-admin connection**, and that shapes the split between this
    and `ensure_metadata_database`. The gateway wakes slept workers, and
    `docs/ARCHITECTURE.md` forbids the gateway holding a credential that can
    create databases and roles. So enabling Realtime builds the metadata
    database once, with the admin connection an operator command has, and
    starting only reads the credential back.

    Registration happens **after** readiness and is not optional. An instance
    running with no tenant registered accepts a client's handshake and then
    refuses every channel -- the silent failure this phase keeps designing
    against -- so a worker that started but could not register is reported
    FAILED rather than left looking healthy.
    """
    try:
        metadata_password = provisioning.load_credential(
            conn, project_id=project_id, credential_type=METADATA_CREDENTIAL_TYPE, key_ring=key_ring
        )
    except provisioning.ProvisioningError as exc:
        raise RealtimeWorkerError(
            "this project has no Realtime metadata database. It is built when Realtime is "
            "enabled, so a project enabled before this existed needs `cp-manage project "
            "realtime --enable` run again."
        ) from exc

    settings = settings_for(
        conn, project_id=project_id, key_ring=key_ring, config=config,
        metadata_password=metadata_password,
    )

    write_env(settings, config_dir=config_dir)
    _set_state(conn, project_id, "STARTING")
    try:
        supervisor.start(settings.project_ref)
        elapsed = wait_until_ready(settings.port, api_secret=settings.secrets.api_jwt)
        register_tenant(settings)
    except Exception:
        _set_state(conn, project_id, "FAILED")
        log.error("Realtime worker for project %s did not come up", settings.project_ref)
        raise

    db.execute(
        conn,
        "UPDATE projects SET realtime_worker_state = 'RUNNING', realtime_registered_at = now(), "
        "       realtime_worker_last_active_at = now() WHERE id = %s",
        (project_id,),
    )
    conn.commit()
    log.info(
        "Realtime for project %s ready in %.1fs on port %s", settings.project_ref, elapsed, settings.port
    )
    return elapsed


def stop_worker(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    supervisor: workers.Supervisor,
) -> None:
    """Sleep a Realtime instance. Nothing about the project changes.

    The tenant stays registered and the slots stay reserved; only the ~146 MB
    goes back. That is the largest single allocation a node can reclaim, which
    is why the sleep policy matters more here than for either other worker.

    The server's slots become inactive rather than dropped, and ADR-032's bound
    is what keeps an asleep project from pinning WAL without limit -- the same
    protection a crashed consumer gets, arrived at deliberately.
    """
    project = db.one(conn, "SELECT project_ref FROM projects WHERE id = %s", (project_id,))
    if project is None:
        raise RealtimeWorkerError("project does not exist")
    supervisor.stop(project["project_ref"])
    _set_state(conn, project_id, "STOPPED")


def teardown(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    supervisor: workers.Supervisor,
) -> None:
    """Stop the instance and remove everything that was built for it.

    Ordered stop-then-remove, and the order is the point: the running server
    holds both replication slots open, and PostgreSQL will not drop an active
    slot. `realtime.disable` terminates the walsender if it has to, but doing
    that to a live server leaves it reconnecting in a loop against a tenant that
    is being dismantled.

    The metadata database goes too. It holds this project's replicator
    credential, encrypted with a key derived from a secret the platform still
    has, so leaving it behind leaves a copy of the most valuable credential the
    platform issues on a node that has no further use for it.
    """
    project = db.one(
        conn,
        "SELECT project_ref, realtime_port FROM projects WHERE id = %s",
        (project_id,),
    )
    if project is None:
        raise RealtimeWorkerError("project does not exist")
    names = RealtimeNames.for_ref(project["project_ref"])

    if project["realtime_port"]:
        try:
            root = provisioning.load_credential(
                conn, project_id=project_id, credential_type=CREDENTIAL_TYPE, key_ring=key_ring
            )
            deregister_tenant(
                port=project["realtime_port"],
                api_secret=derived_secrets(root).api_jwt,
                project_ref=project["project_ref"],
            )
        except (provisioning.ProvisioningError, RealtimeWorkerError, OSError) as exc:
            # Best effort, and deliberately not fatal: the instance is about to
            # be stopped and its metadata database dropped, so a tenant row left
            # in a database that will not exist is not a leak. Recorded because
            # a surprising failure here often means the server was already gone.
            log.info("could not deregister project %s before teardown (%s)",
                     project["project_ref"], type(exc).__name__)

    try:
        supervisor.stop(project["project_ref"])
    except workers.WorkerError as exc:
        log.info("Realtime unit for project %s was not running (%s)", project["project_ref"], exc)

    drop_metadata_database(admin_conn, names)
    db.execute(
        conn,
        "UPDATE projects SET realtime_worker_state = 'STOPPED', realtime_port = NULL, "
        "       realtime_registered_at = NULL WHERE id = %s",
        (project_id,),
    )
    db.execute(
        conn,
        "UPDATE project_credentials SET revoked_at = now() "
        " WHERE project_id = %s AND credential_type = ANY(%s) AND revoked_at IS NULL",
        (project_id, [CREDENTIAL_TYPE, METADATA_CREDENTIAL_TYPE]),
    )
    conn.commit()


def idle_realtime_workers(conn: psycopg.Connection, *, idle_minutes: int) -> list[dict]:
    """Running Realtime instances with no recent traffic -- candidates for sleep."""
    return db.query(
        conn,
        """
        SELECT id, project_ref, node_id, realtime_worker_last_active_at
          FROM projects
         WHERE realtime_worker_state = 'RUNNING' AND deleted_at IS NULL
           AND (realtime_worker_last_active_at IS NULL
                OR realtime_worker_last_active_at < now() - make_interval(mins => %s))
         ORDER BY realtime_worker_last_active_at NULLS FIRST
        """,
        (idle_minutes,),
    )
