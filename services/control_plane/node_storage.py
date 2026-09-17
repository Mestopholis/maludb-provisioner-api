"""The node's storage reconciler: nothing registered for a project the node no longer serves.

Free slice 10c, found deploying project deletion (10b). `jobs.delete_project` drops the tenant's
database, roles and objects, but it cannot deregister the shared `storage-api` worker: that worker's
admin API listens on the **node's** loopback and the control plane is another machine -- ADR-085 put
it there deliberately, because that API can rewrite any tenant's database URL. So a deleted project
left a registration behind carrying its database URL and its JWT signing secret, naming a database
that no longer exists.

This closes it from the node, where that port is reachable:

    python -m services.control_plane.node_storage reconcile

**No KEK and no new database role.** The worker's admin credential is already on the node, in the
environment file the worker itself reads, and systemd hands this pass the two values it needs from
that file. *Which projects this node still serves* is a question the gateway's own role can answer
on its own, because ADR-072's row policies show that role exactly its own node's projects: a project
that was deleted, or moved to another node, simply stops being visible -- which is the same answer
as "this node should not be serving it". Like `node_maintenance`, this imports nothing that can
reach the key ring, and `tests/test_node_storage.py` walks the closure to keep it so.

**It fails closed.** If the control plane cannot be asked, or the worker's listing cannot be read,
the pass deregisters nothing and exits non-zero. The risk worth being careful about is not a stale
registration -- it is this pass concluding, from a bad answer, that a live tenant should stop being
served, which would take that project's Storage API down until someone noticed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg
from psycopg.rows import tuple_row

from services.control_plane import logging as cp_logging
from services.control_plane import storage_admin

log = logging.getLogger(__name__)

# A bound on how much one pass may undo. Deregistering is cheap to repeat and expensive to get wrong
# in bulk, so a pass wanting to remove more than this says so and removes nothing. The shape it
# guards against is not "many deletions on a busy day" -- it is the gateway role losing its node
# mapping, in which case ADR-072's policies correctly answer "no projects" and every tenant on the
# node looks stale at once. Fifty is far above a day's deletions and far below a node's population.
MAX_DEREGISTRATIONS = 50


class ReconcileRefused(RuntimeError):
    """The pass will not act on the answer it got."""


def live_refs(conn: psycopg.Connection, candidates: tuple[str, ...]) -> set[str]:
    """Which of these refs are projects this node still serves, as the database will say.

    The connection is the gateway's own role, so ADR-072's row policies answer for this node and no
    other. `delete_project` clears the placement, so a deleted project is not visible here even
    while its row remains -- which is what makes a deleted tenant reconcilable at all.
    """
    if not candidates:
        return set()
    # An explicit row factory rather than the connection's: this is handed a connection someone
    # else opened, and a `dict_row` one would make `row[0]` a KeyError at the worst moment.
    with conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            "SELECT project_ref FROM projects WHERE project_ref = ANY(%s)",
            (list(candidates),),
        )
        return {row[0] for row in cursor.fetchall()}


def reconcile(
    *,
    database_url: str,
    admin_port: int,
    api_key: str,
    connect=psycopg.connect,
    admin=storage_admin,
) -> int:
    """Deregister every tenant this node no longer serves. Returns how many were removed."""
    registered = admin.known_tenants(admin_port=admin_port, api_key=api_key)
    if not registered:
        log.info("the storage worker holds no tenants; nothing to reconcile")
        return 0

    # A short connection, opened for this question and closed before anything is changed: the same
    # shape `node_backup` uses, so a slow deregistration does not hold a control-plane connection.
    with connect(database_url, connect_timeout=10) as conn:
        live = live_refs(conn, registered)

    stale = sorted(set(registered) - live)
    if not stale:
        log.info("every registered tenant is a project this node serves (%d)", len(registered))
        return 0
    if len(stale) > MAX_DEREGISTRATIONS:
        raise ReconcileRefused(
            f"{len(stale)} of {len(registered)} registered tenants look stale, over the limit of "
            f"{MAX_DEREGISTRATIONS}. That is the shape of a wrong answer rather than that many "
            f"deletions -- check that this node's gateway role is still mapped to this node. "
            f"Nothing was deregistered"
        )

    removed = 0
    for ref in stale:
        try:
            admin.deregister_tenant(admin_port=admin_port, api_key=api_key, project_ref=ref)
        except storage_admin.StorageWorkerError as exc:
            # One tenant's failure is not the next one's: the pass is idempotent and runs again.
            log.warning("could not deregister %s (%s)", ref, exc)
            continue
        removed += 1
        log.info("deregistered %s: this node no longer serves it", ref)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_storage", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("reconcile",))
    args = parser.parse_args(argv)
    cp_logging.configure()
    assert args.command == "reconcile"  # noqa: S101 - argparse has already refused anything else

    database_url = os.environ.get("MALUDB_GATEWAY_DATABASE_URL", "").strip()
    # `SERVER_ADMIN_API_KEYS` and `MALUDB_STORAGE_ADMIN_HOST_PORT` are the worker's own names, read
    # from the worker's own environment file by systemd. Naming them again here would be a second
    # copy of a credential, and a second copy is a thing that drifts.
    api_key = os.environ.get("SERVER_ADMIN_API_KEYS", "").strip()
    port = os.environ.get("MALUDB_STORAGE_ADMIN_HOST_PORT", "").strip()
    missing = [
        name for name, value in (
            ("MALUDB_GATEWAY_DATABASE_URL", database_url),
            ("SERVER_ADMIN_API_KEYS", api_key),
            ("MALUDB_STORAGE_ADMIN_HOST_PORT", port),
        ) if not value
    ]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}: this pass reads the gateway's database URL from "
            f"gateway.env and the worker's admin address from the worker's own storage.env"
        )
    if not port.isdigit():
        raise SystemExit(f"MALUDB_STORAGE_ADMIN_HOST_PORT is not a port number: {port!r}")

    try:
        removed = reconcile(database_url=database_url, admin_port=int(port), api_key=api_key)
    except (storage_admin.StorageWorkerError, ReconcileRefused) as exc:
        log.error("reconcile: %s", exc)
        return 1
    except psycopg.Error as exc:
        # The server's message, never the exception's text: a connection error echoes the DSN.
        primary = (exc.diag.message_primary or "").strip() if exc.diag else ""
        log.error("reconcile: could not ask the control plane (%s%s)",
                  type(exc).__name__, f": {primary}" if primary else "")
        return 1
    log.info("reconcile finished: %d tenant(s) deregistered", removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
