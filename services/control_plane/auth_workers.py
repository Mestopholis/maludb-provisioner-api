"""Per-project GoTrue workers.

Mirrors `workers.py` for the Auth surface and reuses its machinery -- the
systemd supervisor, port allocation, and the project's JWT signing secret --
rather than growing a parallel copy. What differs is genuinely different:

- GoTrue is configured by environment variables, not a config file, so the unit
  uses `EnvironmentFile=` and this module renders that file.
- It has a migration step. PostgREST reads a schema; GoTrue owns one and must
  migrate it before it can serve, which makes "started" and "usable" two
  separate conditions.
- It is started on demand rather than for every project. ADR-022 measured Auth
  at 17.6 MB PSS of the 31.8 MB a warm project costs -- the single largest
  per-project allocation -- and requires that projects which do not use Auth
  not pay for it.

The JWT secret is **reused**, never minted. A project whose Auth signs with a
different key than its Data API verifies is a project whose own tokens it
rejects, and that failure looks like a compatibility bug rather than a
configuration one.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from services.control_plane import crypto, db, provisioning, workers

log = logging.getLogger(__name__)

CONFIG_DIR = Path("/etc/maludb/gotrue")

SERVICE_TEMPLATE = "maludb-gotrue@{ref}.service"

# Resolved at start time, like MALUDB_POSTGREST_BIN for the API worker.
DEFAULT_BINARY = "gotrue"

READINESS_TIMEOUT_SECONDS = 30.0
READINESS_POLL_SECONDS = 0.1

# GoTrue's migration set is larger than PostgREST's startup work -- 70
# migrations against an empty tenant -- and runs once per project.
MIGRATION_TIMEOUT_SECONDS = 120.0


class AuthWorkerError(RuntimeError):
    """A GoTrue worker could not be configured, migrated, started or readied."""


@dataclass(frozen=True)
class AuthSettings:
    """Everything needed to render one project's GoTrue environment."""

    project_ref: str
    database: str
    auth_role: str
    auth_password: str
    jwt_secret: str
    port: int
    site_url: str
    external_url: str
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    # ADR-019: confirmation is on by default and MAILER_AUTOCONFIRM=true is not
    # a production posture. It is permitted here only so slices 1-3 can be built
    # and tested before the relay integration lands; slice 4 removes it.
    autoconfirm: bool = False
    disable_signup: bool = False
    jwt_exp_seconds: int = 3600


def _quote(value: str) -> str:
    """Quote a value for a systemd EnvironmentFile.

    systemd parses these itself rather than through a shell, and an unquoted
    value ending at whitespace would silently truncate a password. Backslashes
    and double quotes are escaped because the quoted form honours both.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_env(settings: AuthSettings) -> str:
    """Render one project's GoTrue environment file.

    A pure function, so the exact bytes reaching disk are testable rather than
    inferred from a running process -- the same reason `workers.render_config`
    is one.
    """
    dsn = (
        f"postgres://{settings.auth_role}:{settings.auth_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.database}"
    )
    pairs = [
        ("GOTRUE_DB_DRIVER", "postgres"),
        ("GOTRUE_DB_DATABASE_URL", dsn),
        # Bookkeeping stays in `auth`. Bootstrap 007 also sets the role's
        # search_path, which is what stops GoTrue reaching for `public` -- where
        # on PostgreSQL 15+ it has no CREATE and fails outright.
        ("GOTRUE_DB_NAMESPACE", "auth"),
        # Reused, never minted. See the module docstring.
        ("GOTRUE_JWT_SECRET", settings.jwt_secret),
        # Both must match what PostgREST expects of a token, or a valid session
        # is refused by the Data API.
        ("GOTRUE_JWT_AUD", "authenticated"),
        ("GOTRUE_JWT_DEFAULT_GROUP_NAME", "authenticated"),
        ("GOTRUE_JWT_EXP", str(settings.jwt_exp_seconds)),
        # Loopback only. docs/API-GATEWAY.md requires internal worker endpoints
        # to be unreachable from the internet, and binding the socket is a
        # stronger guarantee than a firewall rule.
        ("GOTRUE_API_HOST", "127.0.0.1"),
        ("GOTRUE_API_PORT", str(settings.port)),
        # What the outside world calls this project. Used in the links GoTrue
        # emails, so it must be the gateway hostname and never the loopback
        # address the worker actually binds.
        ("API_EXTERNAL_URL", settings.external_url),
        ("GOTRUE_SITE_URL", settings.site_url),
        ("GOTRUE_MAILER_AUTOCONFIRM", "true" if settings.autoconfirm else "false"),
        ("GOTRUE_DISABLE_SIGNUP", "true" if settings.disable_signup else "false"),
        ("GOTRUE_LOG_LEVEL", "info"),
    ]
    header = [
        f"# MaluDB project {settings.project_ref}. Generated -- do not edit.",
        "# Contains live credentials; mode 0600, never committed, never logged.",
        "",
    ]
    return "\n".join(header + [f"{k}={_quote(v)}" for k, v in pairs] + [""])


def write_env(settings: AuthSettings, *, config_dir: Path = CONFIG_DIR) -> Path:
    """Write the environment file with private permissions, before it has content.

    Created 0600 first and populated second, matching `workers.write_config`:
    writing then chmod'ing leaves a window in which the auth role's password and
    the JWT signing secret are world readable, and on a shared node that window
    is enough.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{settings.project_ref}.env"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(render_env(settings))
    return path


def settings_for(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    gateway_domain: str,
    scheme: str = "https",
) -> AuthSettings:
    """Assemble a project's Auth configuration from stored state.

    Credentials are read back through the key ring; nothing here accepts a
    plaintext secret from a caller, and nothing returns one.
    """
    project = db.one(
        conn,
        "SELECT project_ref, database_name, auth_port, status FROM projects WHERE id = %s",
        (project_id,),
    )
    if project is None:
        raise AuthWorkerError("project does not exist")
    if project["database_name"] is None:
        raise AuthWorkerError("project has no database; provision it before starting Auth")

    names = provisioning.TenantNames.for_ref(project["project_ref"])
    port = project["auth_port"] or workers.allocate_port(
        conn, project_id=project_id, column="auth_port"
    )
    conn.commit()

    password = provisioning.load_credential(
        conn, project_id=project_id, credential_type="db_auth", key_ring=key_ring
    )
    jwt_secret = workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)

    host = f"{project['project_ref']}.{gateway_domain}"
    return AuthSettings(
        project_ref=project["project_ref"],
        database=project["database_name"],
        auth_role=names.auth,
        auth_password=password,
        jwt_secret=jwt_secret,
        port=port,
        site_url=f"{scheme}://{host}",
        external_url=f"{scheme}://{host}/auth/v1",
    )


def _binary() -> str:
    configured = os.environ.get("MALUDB_GOTRUE_BIN", "").strip()
    if configured:
        return configured
    found = shutil.which(DEFAULT_BINARY) or shutil.which("auth")
    if not found:
        raise AuthWorkerError(
            "no GoTrue binary; set MALUDB_GOTRUE_BIN or put `gotrue` on the path"
        )
    return found


def migrate(settings: AuthSettings, *, binary: str | None = None) -> None:
    """Run GoTrue's migrations against this tenant.

    Separate from starting the worker because they fail differently and mean
    different things: a failed migration is a tenant that is not ready, while a
    failed start is a node problem. Running it explicitly also keeps the
    ordering visible -- bootstrap 007 must have handed the schema over first, or
    this fails on ownership.
    """
    env = {}
    for line in render_env(settings).splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.partition("=")
        env[key] = raw.strip('"').replace('\\"', '"').replace("\\\\", "\\")
    # GoTrue reads DATABASE_URL for the migrate subcommand.
    env["DATABASE_URL"] = env["GOTRUE_DB_DATABASE_URL"]
    env["PATH"] = "/usr/bin:/bin"

    # ruff S603: fixed executable, arguments as a list, no shell.
    result = subprocess.run(  # noqa: S603
        [binary or _binary(), "migrate"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=MIGRATION_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        # GoTrue's migration output echoes the failing statement but not the
        # DSN; even so, only the last line is surfaced and the environment --
        # which holds the password and the signing secret -- never is.
        last = (result.stderr or result.stdout).strip().splitlines()
        detail = last[-1] if last else "no output"
        log.error("gotrue migrate failed for project %s", settings.project_ref)
        raise AuthWorkerError(f"GoTrue migration failed: {detail}")


def is_ready(port: int, *, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Whether GoTrue will actually serve a request.

    ADR-022 requires waking to wait for readiness rather than for the port to
    open. GoTrue answers /health once it is serving, so this asks it rather than
    inferring from a successful connect.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed scheme, loopback host
            f"http://{host}:{port}/health", timeout=timeout
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_until_ready(
    port: int, *, timeout: float = READINESS_TIMEOUT_SECONDS, host: str = "127.0.0.1"
) -> float:
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        if is_ready(port, host=host):
            return time.monotonic() - started
        time.sleep(READINESS_POLL_SECONDS)
    raise AuthWorkerError(f"GoTrue on port {port} did not become ready within {timeout:.0f}s")


def supervisor(**kwargs) -> workers.SystemdSupervisor:
    """A supervisor bound to the GoTrue unit template."""
    return workers.SystemdSupervisor(template=SERVICE_TEMPLATE, **kwargs)


def _set_state(conn: psycopg.Connection, project_id: uuid.UUID, state: str) -> None:
    db.execute(
        conn, "UPDATE projects SET auth_worker_state = %s WHERE id = %s", (state, project_id)
    )
    conn.commit()


def enable_auth(conn: psycopg.Connection, *, project_id: uuid.UUID) -> None:
    """Mark a project as using Auth.

    ADR-022's demand-driven requirement made structural: a project gets an Auth
    worker because something asked for one, not because it exists.
    """
    db.execute(conn, "UPDATE projects SET auth_enabled = TRUE WHERE id = %s", (project_id,))
    conn.commit()


def start_worker(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring: crypto.KeyRing,
    gateway_domain: str,
    supervisor: workers.Supervisor,
    config_dir: Path = CONFIG_DIR,
    binary: str | None = None,
    scheme: str = "https",
) -> float:
    """Configure, migrate if needed, and start a project's Auth worker.

    Returns the time taken to reach readiness. Safe to call on a project whose
    worker is already running -- it re-renders the environment and waits, rather
    than restarting, so two concurrent wakes do not bounce a serving worker.
    """
    project = db.one(
        conn,
        "SELECT project_ref, auth_enabled, auth_migrated_at FROM projects WHERE id = %s",
        (project_id,),
    )
    if project is None:
        raise AuthWorkerError("project does not exist")
    if not project["auth_enabled"]:
        raise AuthWorkerError(
            "project does not have Auth enabled; ADR-022 requires the worker be started "
            "on demand rather than for every project"
        )

    settings = settings_for(
        conn,
        project_id=project_id,
        key_ring=key_ring,
        gateway_domain=gateway_domain,
        scheme=scheme,
    )
    _set_state(conn, project_id, "STARTING")

    try:
        if project["auth_migrated_at"] is None:
            migrate(settings, binary=binary)
            db.execute(
                conn, "UPDATE projects SET auth_migrated_at = now() WHERE id = %s", (project_id,)
            )
            conn.commit()

        write_env(settings, config_dir=config_dir)
        supervisor.start(settings.project_ref)
        elapsed = wait_until_ready(settings.port)
    except Exception:
        _set_state(conn, project_id, "FAILED")
        raise

    _set_state(conn, project_id, "RUNNING")
    return elapsed


def stop_worker(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    supervisor: workers.Supervisor,
) -> None:
    """Sleep an Auth worker. The tenant's `auth` schema is untouched.

    ADR-022: a slept project must remain a project, and Auth is the largest
    single allocation a sleeping project gives back.
    """
    project = db.one(conn, "SELECT project_ref FROM projects WHERE id = %s", (project_id,))
    if project is None:
        raise AuthWorkerError("project does not exist")
    supervisor.stop(project["project_ref"])
    _set_state(conn, project_id, "STOPPED")


def idle_auth_workers(conn: psycopg.Connection, *, idle_minutes: int) -> list[dict]:
    """Running Auth workers with no recent traffic -- candidates for sleep."""
    return db.query(
        conn,
        """
        SELECT id, project_ref, node_id, auth_worker_last_active_at
          FROM projects
         WHERE auth_worker_state = 'RUNNING' AND deleted_at IS NULL
           AND (auth_worker_last_active_at IS NULL
                OR auth_worker_last_active_at < now() - make_interval(mins => %s))
         ORDER BY auth_worker_last_active_at NULLS FIRST
        """,
        (idle_minutes,),
    )
