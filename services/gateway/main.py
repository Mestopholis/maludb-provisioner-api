"""Entry point for the gateway process.

Separate from the control-plane application on purpose. They sit on different
sides of a trust boundary: the control plane authenticates platform users and
holds the KEK, while this listens to the public internet and proxies tenant
traffic. Running them in one process would mean one bug in request handling
reaches provisioning credentials.
"""

from __future__ import annotations

import logging
import sys

import psycopg

from services.control_plane import (
    auth_workers,
    crypto,
    db,
    gateway_grants,
    realtime_workers,
)
from services.control_plane import config as cp_config
from services.control_plane import logging as cp_logging
from services.gateway.app import Gateway, create_app

log = logging.getLogger("maludb.gateway")


def assert_narrowed(conn: psycopg.Connection, *, environment: str) -> None:
    """Refuse to serve if this role can recover another node's superuser DSN.

    **The check is the privilege, not the configuration.** A gateway pointed at
    the control plane's DSN works perfectly and is fully exposed, so asserting
    that `MALUDB_GATEWAY_DATABASE_URL` is set would pass precisely the
    deployment that has to fail. This asks the database what the connected role
    can actually read.

    Outside production it warns instead of refusing. Development and the test
    suite run one role against one database, and a local gateway that refused to
    start would most likely be "fixed" by pasting in the production DSN — which
    is the opposite of what this exists to encourage. Same shape as the
    `plans sync` warning, for the same reason.
    """
    try:
        conn.execute(gateway_grants.probe_sql())
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        return  # correctly narrowed: the column is unreadable
    except psycopg.Error:
        conn.rollback()
        raise
    conn.rollback()

    message = (
        "this gateway's database role can read nodes.admin_ciphertext, so a compromise of "
        "this internet-facing process yields the PostgreSQL superuser DSN of every node on "
        "the platform (ADR-072). Give it its own role: create a LOGIN role, run "
        "`cp-manage gateway grant --role <name>`, and point "
        "MALUDB_GATEWAY_DATABASE_URL at it"
    )
    if environment == "production":
        raise RuntimeError(message)
    log.warning("%s -- this is refused in production; allowed here because MALUDB_ENV=%s",
                message, environment)


def _assert_node_identity(conn: psycopg.Connection, *, environment: str) -> None:
    """Refuse to serve if this role is not mapped to a node (ADR-072 point 2).

    The row policies resolve `current_user` through `nodes.gateway_role`, and an
    unmapped role resolves to NULL, which matches no row. That is the direction
    a mistake here must fail in -- a gateway that sees nothing is safe and a
    gateway that sees the fleet is the finding ADR-072 exists for -- but from
    outside it looks like every tenant on the machine has vanished, with a
    healthy process and no error anywhere.

    So it is asked once, at startup, where it can be said plainly.

    Called from `build` rather than from `assert_narrowed`, because that
    function *returns early* on the correctly-narrowed path -- which is exactly
    the deployment this check is for. Folding it in there would have run it only
    for gateways that had already failed the more serious test.
    """
    node_id = gateway_grants.node_identity(conn)
    conn.rollback()
    if node_id is not None:
        log.info("gateway serves node id %s (ADR-072)", node_id)
        return

    message = (
        "this gateway's database role is not mapped to any node, so its row policies match "
        "nothing and every project on this machine will answer 404 (ADR-072). Run "
        "`cp-manage gateway grant --role <name> --node <node>`"
    )
    if environment == "production":
        raise RuntimeError(message)
    log.warning("%s -- this is refused in production; allowed here because MALUDB_ENV=%s",
                message, environment)


def build() -> object:
    """Factory for `uvicorn --factory services.gateway.main:build`."""
    settings = cp_config.load()
    cp_logging.configure()
    # The gateway's own role (ADR-072). Empty falls back to the control plane's,
    # which `assert_narrowed` then refuses in production.
    db.init_pool(settings.gateway_database_url or settings.database_url)

    key_ring = crypto.KeyRing(settings.kek)
    with db.connection() as conn:
        assert_narrowed(conn, environment=settings.environment)
        _assert_node_identity(conn, environment=settings.environment)
        key_ring.load(conn)

    from services.control_plane.workers import SystemdSupervisor

    return create_app(
        Gateway(
            config=settings,
            key_ring=key_ring,
            supervisor=SystemdSupervisor(),
            # A second supervisor, bound to the GoTrue unit template. The two
            # drive different units and must not be shared.
            auth_supervisor=auth_workers.supervisor(),
            # A third, bound to the Realtime unit, which runs a container
            # (ADR-033). Waking one is the most expensive thing this process
            # does and the only one that needs a per-project port looked up.
            realtime_supervisor=realtime_workers.supervisor(),
        )
    )


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else 8110
    uvicorn.run(build(), host="0.0.0.0", port=port)  # noqa: S104 - the public listener
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
