"""Per-project PostgREST workers (ADR-007, ADR-022, ADR-027).

Three things this module is careful about, each for a reason that was measured
rather than assumed:

- **The worker connects as the project's authenticator, never as a superuser.**
  `docs/ARCHITECTURE.md` forbids putting database superuser credentials in the
  gateway, and the same reasoning applies with more force here: PostgREST
  executes SQL composed from HTTP requests. Its database role is the last line
  of defence, and it is the tenant's constrained login role.

- **Readiness is not port-open.** ADR-022: PostgREST answers `503 PGRST002`
  until its schema cache has loaded, so a request routed the moment the socket
  accepts connections fails. Waking a slept project has to wait for the service
  to answer.

- **The config file holds two live secrets** -- the authenticator password and
  the project's JWT secret. It is written with a private umask, to a directory
  outside the repository, and never logged.

Supervision is systemd (ADR-027), reached through a narrow protocol so tests
can substitute a recorder and so a future change of supervisor does not reach
into the rest of the control plane.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg import sql

from services.control_plane import crypto, db, entitlements, models, provisioning

log = logging.getLogger(__name__)

# Where per-project worker configuration lives. Outside the repository, and
# readable only by the account that runs the workers.
CONFIG_DIR = Path("/etc/maludb/postgrest")

SERVICE_TEMPLATE = "maludb-postgrest@{ref}.service"

# ADR-022 measured 4 PostgreSQL backends per warm project at a pool size of 3,
# and identified connections rather than memory as the binding constraint on
# density. Raising this raises the cost of every warm project.
#
# The floor for a project with no resolvable plan. The real value comes from the
# entitlement, so a plan change moves it without a release.
DEFAULT_POOL_SIZE = 3

# Ports for tenant workers, allocated per node. Bound to loopback only.
PORT_RANGE = range(20000, 30000)

# HS256 keys shorter than the 32-byte digest weaken the MAC.
JWT_SECRET_BYTES = 48

READINESS_TIMEOUT_SECONDS = 15.0
READINESS_POLL_SECONDS = 0.1


class WorkerError(RuntimeError):
    """A worker could not be configured, started, or made ready."""


@dataclass(frozen=True)
class WorkerSettings:
    """Everything needed to render one project's PostgREST configuration."""

    project_ref: str
    database: str
    authenticator_role: str
    authenticator_password: str
    jwt_secret: str
    port: int
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    pool_size: int = DEFAULT_POOL_SIZE
    exposed_schema: str = "public"


# --------------------------------------------------------------------------
# The JWT secret
# --------------------------------------------------------------------------


def ensure_jwt_secret(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
) -> str:
    """The project's JWT signing secret, generated once and reused.

    Class B under ADR-023: PostgREST needs the literal value at every start, so
    it must be recoverable, which means encrypted rather than hashed.

    Phase 02 deferred this to "Auth configuration in Phase 04". It lands here
    instead because PostgREST needs it first -- and it is the *same* secret
    GoTrue will need, since a token GoTrue signs has to verify in PostgREST.
    Generating a second one in Phase 04 would produce a project whose own Auth
    tokens its own Data API rejects.
    """
    try:
        return provisioning.load_credential(
            conn, project_id=project_id, credential_type="jwt_signing", key_ring=key_ring
        )
    except provisioning.ProvisioningError:
        pass

    # Plain high-entropy random, not one of the prefixed platform tokens: this
    # value is an HMAC key for PostgREST and GoTrue, never presented as a
    # credential, so a recognisable prefix would leak which bytes are the key.
    secret = secrets.token_urlsafe(JWT_SECRET_BYTES)
    provisioning.store_credential(
        conn,
        project_id=project_id,
        credential_type="jwt_signing",
        role_name=None,
        secret=secret,
        key_ring=key_ring,
    )
    return secret


# --------------------------------------------------------------------------
# Port allocation
# --------------------------------------------------------------------------


# The port columns a worker may be allocated from. A fixed set, because the
# name is interpolated into SQL as an identifier: composed with sql.Identifier
# so it cannot inject, but an unexpected value would still read or write the
# wrong column, which AGENTS.md treats as the real risk behind generated
# identifiers.
PORT_COLUMNS = ("api_port", "auth_port", "realtime_port")


def allocate_port(
    conn: psycopg.Connection, *, project_id: uuid.UUID, column: str = "api_port"
) -> int:
    """Reserve a loopback port for this project on its node.

    Allocated inside a transaction that locks the node row, so two projects
    being started concurrently cannot be handed the same port -- the same
    approach slice 1 of Phase 02 used for placement, and for the same reason.

    `column` selects which worker's port is being allocated. Both are taken from
    the same range and checked against each other, so an API worker and an Auth
    worker on one node can never collide.
    """
    if column not in PORT_COLUMNS:
        raise WorkerError(f"unknown port column {column!r}")

    project = db.one(
        conn,
        sql.SQL("SELECT node_id, {col} AS port FROM projects WHERE id = %s").format(
            col=sql.Identifier(column)
        ),
        (project_id,),
    )
    if project is None:
        raise WorkerError("project does not exist")
    if project["port"] is not None:
        return project["port"]
    if project["node_id"] is None:
        raise WorkerError("project has no node placement, so no node to allocate a port on")

    db.one(conn, "SELECT id FROM nodes WHERE id = %s FOR UPDATE", (project["node_id"],))
    # Every port on the node, whichever worker holds it.
    taken = {
        row["port"]
        for row in db.query(
            conn,
            "SELECT unnest(array[api_port, auth_port, realtime_port]) AS port "
            "  FROM projects WHERE node_id = %s",
            (project["node_id"],),
        )
        if row["port"] is not None
    }
    for candidate in PORT_RANGE:
        if candidate not in taken:
            db.execute(
                conn,
                sql.SQL("UPDATE projects SET {col} = %s WHERE id = %s").format(
                    col=sql.Identifier(column)
                ),
                (candidate, project_id),
            )
            return candidate
    raise WorkerError(f"no free API port on node {project['node_id']}")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def render_config(settings: WorkerSettings) -> str:
    """Render one project's PostgREST configuration.

    Kept a pure function so the exact bytes that reach disk can be asserted in a
    test rather than inferred from a running process.
    """
    dsn = (
        f"postgres://{settings.authenticator_role}:{settings.authenticator_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.database}"
    )
    return "\n".join(
        [
            f"# MaluDB project {settings.project_ref}. Generated -- do not edit.",
            "# Contains live credentials; mode 0600, never committed, never logged.",
            "",
            f'db-uri = "{dsn}"',
            f'db-schemas = "{settings.exposed_schema}"',
            # Requests arriving without a JWT act as anon. Phase 00 finding 7:
            # anon must hold a grant so RLS answers with an empty set rather
            # than 42501, which migrated applications distinguish.
            'db-anon-role = "anon"',
            f"db-pool = {settings.pool_size}",
            f'jwt-secret = "{settings.jwt_secret}"',
            # Loopback only. docs/API-GATEWAY.md requires internal worker
            # endpoints to be unreachable from the internet; binding the socket
            # is a stronger guarantee than a firewall rule.
            'server-host = "127.0.0.1"',
            f"server-port = {settings.port}",
            # The event trigger in bootstrap 006 sends this; without the
            # listener the notification goes nowhere and Phase 00 finding 3
            # returns.
            'db-channel = "pgrst"',
            "db-channel-enabled = true",
            "",
        ]
    )


def write_config(settings: WorkerSettings, *, config_dir: Path = CONFIG_DIR) -> Path:
    """Write the config with private permissions, before it has any content.

    Created 0600 first and populated second: writing then chmod'ing leaves a
    window in which the authenticator password and JWT secret are world
    readable, and on a shared node that window is enough.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{settings.project_ref}.conf"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(render_config(settings))
    return path


# --------------------------------------------------------------------------
# Supervision
# --------------------------------------------------------------------------


class Supervisor(Protocol):
    """Start and stop one project's worker. Narrow on purpose."""

    def start(self, project_ref: str) -> None: ...
    def stop(self, project_ref: str) -> None: ...
    def is_active(self, project_ref: str) -> bool: ...


class SystemdSupervisor:
    """ADR-027: workers are `maludb-postgrest@<ref>.service` template units.

    The control plane asks systemd rather than spawning children, so a control
    plane restart does not orphan every tenant's worker and an operator can
    inspect one with tools that predate this codebase.
    """

    def __init__(
        self,
        *,
        systemctl: str = "systemctl",
        use_sudo: bool = False,
        template: str = SERVICE_TEMPLATE,
    ) -> None:
        self._prefix = ["sudo", "-n", systemctl] if use_sudo else [systemctl]
        self._template = template

    def unit_for(self, project_ref: str) -> str:
        """The unit name for a project, refusing an invalid ref.

        `AGENTS.md` requires identifiers generated from project metadata to be
        validated, and a systemd unit name is one: an unchecked ref could name a
        different unit entirely. Nothing here runs through a shell and arguments
        are passed as a list, so this is not command injection -- it is the
        weaker but real risk of acting on the wrong target.
        """
        if not models.is_valid_project_ref(project_ref):
            raise WorkerError(f"invalid project ref {project_ref!r}")
        return self._template.format(ref=project_ref)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # ruff S603: the executable is fixed, arguments are a list rather than a
        # shell string, and every project ref is validated by unit_for above.
        return subprocess.run(  # noqa: S603
            [*self._prefix, *args], capture_output=True, text=True, check=False
        )

    def start(self, project_ref: str) -> None:
        unit = self.unit_for(project_ref)
        result = self._run("start", unit)
        if result.returncode != 0:
            # systemd's stderr names the unit and the failure, and carries no
            # credential -- the secrets are in the config file, not the command.
            raise WorkerError(f"could not start {unit}: {result.stderr.strip()}")

    def stop(self, project_ref: str) -> None:
        unit = self.unit_for(project_ref)
        result = self._run("stop", unit)
        if result.returncode != 0:
            raise WorkerError(f"could not stop {unit}: {result.stderr.strip()}")

    def is_active(self, project_ref: str) -> bool:
        return self._run("is-active", self.unit_for(project_ref)).returncode == 0


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


def is_ready(port: int, *, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Whether the worker will actually serve a request.

    ADR-022: PostgREST answers `503 PGRST002` while its schema cache loads, so
    a check that only proves the port accepts connections routes traffic into a
    service that is not yet serving. This asks for a response and requires it
    not to be that.
    """
    import http.client

    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        connection.close()
    except OSError:
        return False
    return response.status != 503


def wait_until_ready(
    port: int,
    *,
    host: str = "127.0.0.1",
    timeout: float = READINESS_TIMEOUT_SECONDS,
) -> float:
    """Block until the worker serves, returning how long it took.

    The elapsed time is returned rather than discarded because ADR-022's
    sub-second cold start is what makes an aggressive free-tier sleep policy
    correct; if it regresses, the number is what shows it.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        if is_ready(port, host=host):
            return time.monotonic() - started
        time.sleep(READINESS_POLL_SECONDS)
    raise WorkerError(f"worker on port {port} did not become ready within {timeout}s")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def _set_worker_state(conn: psycopg.Connection, project_id: uuid.UUID, state: str) -> None:
    db.execute(conn, "UPDATE projects SET worker_state = %s WHERE id = %s", (state, project_id))
    conn.commit()


def start_worker(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    supervisor: Supervisor,
    config_dir: Path = CONFIG_DIR,
) -> int:
    """Configure and start a project's worker, returning its readiness time.

    Safe to call on a project whose worker is already running: it re-renders the
    config and waits for readiness rather than restarting, so a wake triggered
    by two concurrent requests does not bounce a serving worker.
    """
    project = db.one(
        conn,
        "SELECT project_ref, database_name, status, api_port FROM projects WHERE id = %s",
        (project_id,),
    )
    if project is None:
        raise WorkerError("project does not exist")
    if project["database_name"] is None:
        raise WorkerError("project has no database; it has not been provisioned")
    if project["status"] not in ("PROVISIONED", "ACTIVE"):
        raise WorkerError(f"refusing to start a worker for a project in {project['status']}")

    names = provisioning.TenantNames.for_ref(project["project_ref"])
    port = allocate_port(conn, project_id=project_id)
    conn.commit()

    settings = WorkerSettings(
        project_ref=project["project_ref"],
        database=project["database_name"],
        authenticator_role=names.authenticator,
        authenticator_password=provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_authenticator", key_ring=key_ring
        ),
        jwt_secret=ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring),
        port=port,
        # From the plan, not a constant. ADR-022 makes the pool size the single
        # biggest lever on how many warm projects a node holds, so it belongs
        # where a plan change moves it rather than where a release does.
        pool_size=entitlements.for_project(conn, project_id).postgrest_pool_size,
    )
    conn.commit()

    write_config(settings, config_dir=config_dir)
    _set_worker_state(conn, project_id, "STARTING")
    try:
        supervisor.start(settings.project_ref)
        elapsed = wait_until_ready(port)
    except WorkerError:
        _set_worker_state(conn, project_id, "FAILED")
        log.error("worker for project %s did not become ready", project_id)
        raise

    db.execute(
        conn,
        "UPDATE projects SET worker_state = 'RUNNING', worker_last_active_at = now() WHERE id = %s",
        (project_id,),
    )
    conn.commit()
    log.info("worker for project %s ready in %.3fs on port %s", project_id, elapsed, port)
    return port


def stop_worker(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    supervisor: Supervisor,
) -> None:
    """Sleep a worker. The database and its data are untouched.

    ADR-005 and ADR-022: free-tier economics rest on sleeping workers, and a
    slept project must remain a project. This never touches `database_name`,
    never drops anything, and leaves the project's status alone -- only the
    worker stops.
    """
    project = db.one(conn, "SELECT project_ref FROM projects WHERE id = %s", (project_id,))
    if project is None:
        raise WorkerError("project does not exist")
    supervisor.stop(project["project_ref"])
    _set_worker_state(conn, project_id, "STOPPED")


def idle_workers(conn: psycopg.Connection, *, idle_minutes: int) -> list[dict]:
    """Running workers with no recent traffic -- candidates for sleep."""
    return db.query(
        conn,
        """
        SELECT id, project_ref, node_id, worker_last_active_at
          FROM projects
         WHERE worker_state = 'RUNNING' AND deleted_at IS NULL
           AND (worker_last_active_at IS NULL
                OR worker_last_active_at < now() - make_interval(mins => %s))
         ORDER BY worker_last_active_at NULLS FIRST
        """,
        (idle_minutes,),
    )
