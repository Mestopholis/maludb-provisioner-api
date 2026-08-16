"""Entry point for the gateway process.

Separate from the control-plane application on purpose. They sit on different
sides of a trust boundary: the control plane authenticates platform users and
holds the KEK, while this listens to the public internet and proxies tenant
traffic. Running them in one process would mean one bug in request handling
reaches provisioning credentials.
"""

from __future__ import annotations

import sys

from services.control_plane import auth_workers, crypto, db, realtime_workers
from services.control_plane import config as cp_config
from services.control_plane import logging as cp_logging
from services.gateway.app import Gateway, create_app


def build() -> object:
    """Factory for `uvicorn --factory services.gateway.main:build`."""
    settings = cp_config.load()
    cp_logging.configure()
    db.init_pool(settings.database_url)

    key_ring = crypto.KeyRing(settings.kek)
    with db.connection() as conn:
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
