"""Per-node extension pins, the tested-versions list, and the node check (ADR-075).

Pinning slice 1. `vector` and `maludb_core` are pinned exactly, per node, to a
version `specs/extension-versions.yaml` lists as tested. A node whose installed
packages disagree with its pin -- or that has no pin, or has not been checked
since it was pinned -- takes no new projects, restores or moves, and keeps
serving the tenants it has (decision 4).

## What pinning slice 0 measured, and what it changed here

- **The package is the upgrade.** Every `vector` script from 0.8.0 to 0.8.6 is
  empty, so what a node runs is decided by its package, and `default_version` --
  from the control file that package installed -- is what the check compares.
- **An installed package does not reach sessions already open.** A backend that
  loaded `vector.so` before `dpkg` replaced it keeps the old file mapped, marked
  `(deleted)`, until it disconnects; pooled workers hold connections as long as
  they run. So the check also counts those backends, read from
  `/proc/<pid>/maps`, and a node with any is not at its pin.
- **Moves and restores install the receiving node's version**, because a dump's
  `CREATE EXTENSION` carries none. So a move between nodes whose pins differ is
  refused, not only a move onto a mismatched node (`move_refusal`).
- **A pin may not move down on a node with tenants**: after a package downgrade
  their catalogues say the newer version and `ALTER EXTENSION` has no path back.

## Where each half lives

The pin is a row (`node_extension_pins`) because it is an operator's decision;
the check's result is `capacity_json.extension_check` because it is a
measurement a re-run replaces. `nodes.capacity_of` reads both and
`NodeCapacity.rejection_reason` compares them, which is the one place placement,
moves and the maintenance capacity report already consult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from services.control_plane import db

log = logging.getLogger("maludb.extension_pins")

PINNED = ("vector", "maludb_core")
VERSIONS_SPEC = Path(__file__).resolve().parent.parent.parent / "specs" / "extension-versions.yaml"
AUDIT_PIN_SET = "node.extension_pin.set"

# The library whose replacement a running backend can outlive. maludb_core is
# built from source into a library too, but it is replaced by an operator's
# `make install`, not by an unattended `apt upgrade`; vector is the one a routine
# package upgrade moves.
_LIBRARY = "vector.so"


class PinError(ValueError):
    """A pin was refused."""


# --------------------------------------------------------------------------
# The tested list


def tested_versions(spec_path: Path | None = None) -> dict[str, list[str]]:
    """Versions CI has tested, per pinned extension, from the spec. Read each time.

    Read rather than cached, like `tenant_bootstrap.allowlisted_extensions`: the
    file is the authority, and adding a version is a review and a merge.
    """
    spec = yaml.safe_load((spec_path or VERSIONS_SPEC).read_text()) or {}
    listed = spec.get("extensions") or {}
    result: dict[str, list[str]] = {}
    for extension in PINNED:
        entries = listed.get(extension) or []
        versions = [str(entry["version"]) for entry in entries if entry.get("version")]
        if not versions:
            raise PinError(f"{VERSIONS_SPEC.name} lists no tested version of {extension}")
        result[extension] = versions
    unknown = sorted(set(listed) - set(PINNED))
    if unknown:
        raise PinError(
            f"{VERSIONS_SPEC.name} lists {unknown}, which ADR-075 does not pin; contrib "
            "extensions follow the PostgreSQL minor version"
        )
    return result


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


# --------------------------------------------------------------------------
# Pins


def pins(conn: psycopg.Connection, node_id: int) -> dict[str, dict]:
    return {
        row["extension"]: row
        for row in db.query(
            conn,
            "SELECT extension, version, set_by, set_at FROM node_extension_pins WHERE node_id = %s",
            (node_id,),
        )
    }


def set_pin(
    conn: psycopg.Connection,
    *,
    node_name: str,
    extension: str,
    version: str,
    actor: str,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    """Pin one extension on one node. Returns {previous, version}.

    Refused, before anything is written, when the extension is not one ADR-075
    pins, the version is not on the tested list, or the pin would move down on a
    node that has tenants.
    """
    if extension not in PINNED:
        raise PinError(f"{extension!r} is not pinned; only {', '.join(PINNED)} are (ADR-075)")
    tested = tested_versions(spec_path)[extension]
    if version not in tested:
        raise PinError(
            f"{extension} {version} is not in {VERSIONS_SPEC.name} (tested: {', '.join(tested)}); "
            "a version is listed once CI has run against it"
        )
    node = db.one(conn, "SELECT id FROM nodes WHERE name = %s FOR UPDATE", (node_name,))
    if node is None:
        raise PinError(f"no node named {node_name!r}")

    current = pins(conn, node["id"]).get(extension)
    previous = current["version"] if current else None
    if previous is not None and version_key(version) < version_key(previous):
        tenants = db.one(
            conn,
            "SELECT count(*) AS n FROM projects WHERE node_id = %s AND deleted_at IS NULL",
            (node["id"],),
        )["n"]
        if tenants:
            raise PinError(
                f"{extension} is pinned at {previous} on {node_name}, which has {tenants} "
                f"tenant(s); moving the pin down to {version} is refused -- their catalogues "
                "cannot follow a package downgrade (pinning slice 0, finding 6)"
            )

    db.execute(
        conn,
        """
        INSERT INTO node_extension_pins (node_id, extension, version, set_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (node_id, extension)
        DO UPDATE SET version = EXCLUDED.version, set_by = EXCLUDED.set_by, set_at = now()
        """,
        (node["id"], extension, version, actor),
    )
    db.execute(
        conn,
        "INSERT INTO audit_events (actor_type, actor_id, event_type, detail_json) "
        "VALUES ('staff', %s, %s, %s)",
        (actor, AUDIT_PIN_SET, Jsonb({"node": node_name, "extension": extension,
                                      "from": previous, "to": version})),
    )
    conn.commit()
    return {"previous": previous, "version": version}


# --------------------------------------------------------------------------
# The node check


@dataclass
class ExtensionCheck:
    provided: dict[str, str | None] = field(default_factory=dict)
    server_version: str | None = None
    contrib: dict[str, str] = field(default_factory=dict)
    stale_backends: int = 0
    unreadable_backends: int = 0

    def as_capacity(self) -> dict[str, Any]:
        return {
            "provided": self.provided,
            "server_version": self.server_version,
            "contrib": self.contrib,
            "stale_backends": self.stale_backends,
            "unreadable_backends": self.unreadable_backends,
        }


def maps_have_replaced_library(maps: str) -> bool:
    """Whether a `/proc/<pid>/maps` shows `vector.so` mapped from a replaced file.

    The kernel marks a mapping whose file was unlinked with ` (deleted)`. Pinning
    slice 0 saw exactly this on a backend opened before `apt-get install`:
    `/usr/lib/postgresql/17/lib/vector.so (deleted)`.
    """
    return any(
        line.rstrip().endswith(f"/{_LIBRARY} (deleted)") for line in maps.splitlines()
    )


def inspect_node(admin_conn: psycopg.Connection) -> ExtensionCheck:
    """What the node provides, as PostgreSQL reports it. Reads only.

    `admin_conn` must be a superuser connection: `pg_read_file` on another
    backend's `/proc/<pid>/maps` needs it, and so does the cluster-wide view of
    `pg_stat_activity`.
    """
    check = ExtensionCheck()
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT name, default_version FROM pg_available_extensions WHERE name = ANY(%s)",
            (list(PINNED),),
        )
        found = dict(cur.fetchall())
        check.provided = {extension: found.get(extension) for extension in PINNED}
        cur.execute("SHOW server_version")
        check.server_version = cur.fetchone()[0]
        # Recorded, never pinned (decision 2): what the PostgreSQL minor brought.
        cur.execute(
            "SELECT name, default_version FROM pg_available_extensions "
            "WHERE name <> ALL(%s) ORDER BY name",
            (list(PINNED),),
        )
        check.contrib = dict(cur.fetchall())

        cur.execute("SELECT pid FROM pg_stat_activity WHERE pid <> pg_backend_pid()")
        for (pid,) in cur.fetchall():
            try:
                cur.execute("SELECT pg_read_file(%s)", (f"/proc/{int(pid)}/maps",))
                maps = cur.fetchone()[0] or ""
            except psycopg.Error:
                # A backend that exited between the two queries, or one this
                # role cannot read. Counted rather than ignored: an unreadable
                # backend is not evidence of a current library.
                admin_conn.rollback()
                check.unreadable_backends += 1
                continue
            if maps_have_replaced_library(maps):
                check.stale_backends += 1
    return check


def record_check(conn: psycopg.Connection, *, node_name: str, check: ExtensionCheck) -> None:
    updated = db.execute(
        conn,
        "UPDATE nodes SET capacity_json = capacity_json || jsonb_build_object("
        "    'extension_check', %s::jsonb || jsonb_build_object('checked_at', now())) "
        " WHERE name = %s",
        (Jsonb(check.as_capacity()), node_name),
    )
    if updated == 0:
        raise PinError(f"no node named {node_name!r}")
    conn.commit()


# --------------------------------------------------------------------------
# Refusals


def rejection_reason(node_pins: dict[str, dict], check: dict | None) -> str | None:
    """Why a node's extensions disagree with its pins, or None when they agree.

    Pure, over what `nodes.capacity_of` already read, so placement opens no
    connection to the node and a test needs no cluster.
    """
    missing = [extension for extension in PINNED if extension not in node_pins]
    if missing:
        return (
            f"no extension pin for {', '.join(missing)}; run `cp-manage node pin set` with a "
            "version from specs/extension-versions.yaml (ADR-075)"
        )
    if not isinstance(check, dict) or not check.get("checked_at"):
        return "extensions never checked against the pins; run `cp-manage node extension-check`"
    try:
        checked_at = datetime.fromisoformat(str(check["checked_at"]))
    except ValueError:
        return "the last extension check is unreadable; run `cp-manage node extension-check`"
    if any(checked_at < pin["set_at"] for pin in node_pins.values()):
        return "extensions not checked since the pin changed; run `cp-manage node extension-check`"
    provided = check.get("provided") or {}
    for extension in PINNED:
        pinned = node_pins[extension]["version"]
        if provided.get(extension) != pinned:
            return (
                f"{extension} is pinned at {pinned} but the node provides "
                f"{provided.get(extension) or 'nothing'}; install the pinned package, or change "
                "the pin, then run `cp-manage node extension-check`"
            )
    stale = check.get("stale_backends")
    if not isinstance(stale, int) or stale:
        return (
            f"{stale} backend(s) still run a replaced {_LIBRARY}; restart the node's workers, then "
            "run `cp-manage node extension-check` (an installed package does not reach open sessions)"
        )
    return None


def move_refusal(conn: psycopg.Connection, *, source_node_id: int, target_node_id: int) -> str | None:
    """Why a tenant cannot move between these two nodes' pins, or None.

    A dump's `CREATE EXTENSION` carries no version, so a tenant arrives at the
    target's: lower silently downgrades it, higher lands it ahead of the upgrade
    run. Equal pins are the only safe move.
    """
    source, target = pins(conn, source_node_id), pins(conn, target_node_id)
    for extension in PINNED:
        s, t = source.get(extension), target.get(extension)
        if s is None or t is None or s["version"] != t["version"]:
            return (
                f"{extension} is pinned at {s['version'] if s else 'nothing'} on the source and "
                f"{t['version'] if t else 'nothing'} on the target; a move installs the target's "
                "version, so pins must match (ADR-075)"
            )
    return None


__all__ = [
    "AUDIT_PIN_SET",
    "PINNED",
    "ExtensionCheck",
    "PinError",
    "inspect_node",
    "maps_have_replaced_library",
    "move_refusal",
    "pins",
    "record_check",
    "rejection_reason",
    "set_pin",
    "tested_versions",
]
