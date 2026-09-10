"""Rebuild a lost node from its backup, and reconnect the control plane to it.

Phase 11 slice 8. Slice 2 restores **one tenant** onto a scratch cluster and
extracts it; this restores the **whole cluster** onto fresh hardware and points
the control plane at the result. Same pgBackRest machinery, different question:
there, a customer wants a table back; here, a machine is gone.

**The slice exists for a number.** "We have backups" is a claim until somebody
has rebuilt from them and timed it, and a platform that sells recovery windows
(ADR-068) should not be guessing at its own. `RebuildOutcome.total_seconds` is
that number, and `docs/BACKUP-RECOVERY.md` records what it was measured against
-- an RTO true only of a 50 MB node is worse than no RTO.

**What this is not.** There is no failover here and no standby. A node outage is
an outage for its tenants until an operator runs this, and the runbook says so.
Inventing leader election in a slice would be the undocumented architectural
change `AGENTS.md` forbids.

## The order is the design, and each step is a way to lose data quietly

**Refuse a target that still has tenants.** Rebuilding onto a machine already
serving turns one outage into two. `restore.create_scratch_cluster` refuses to
build over a live data directory for the same reason; this is that guard at the
level of the control plane's own records.

**Repoint last.** ADR-059: a restore that cannot find a tenant's roles completes
with "errors ignored" and silently reassigns `auth` and `storage` to whoever ran
it. Repointing `projects.node_id` is what sends customer traffic at the result,
so it happens after ownership is checked, not before.

**Never delete the lost node's row.** It carries the encrypted admin DSN and the
backup stanza -- the only record of what was lost and where its backups are. It
moves to a terminal status instead.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from services.control_plane import db, models, nodes, provisioning, restore

log = logging.getLogger("maludb.node_rebuild")

# What a node becomes once its tenants live somewhere else. Not deleted: the row
# is the only record of the stanza its backups are under.
LOST_STATUS = "unhealthy"


class RebuildError(RuntimeError):
    """A node could not be rebuilt, or the result could not be trusted."""


@dataclass
class TenantCheck:
    project_ref: str
    database: str
    verified: bool
    detail: str


@dataclass
class RebuildOutcome:
    """What a rebuild did, and what it cost -- which is the point of the slice."""

    source_node: str
    target_node: str
    stanza: str
    restore_seconds: float = 0.0
    promote_seconds: float = 0.0
    total_seconds: float = 0.0
    databases_found: int = 0
    tenants: list[TenantCheck] = field(default_factory=list)
    repointed: int = 0
    status: str = "pending"
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "complete"

    @property
    def unverified(self) -> list[TenantCheck]:
        return [t for t in self.tenants if not t.verified]


def projects_on(conn: psycopg.Connection, *, node_name: str) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT p.id, p.project_ref, p.database_name, p.status
          FROM projects p JOIN nodes n ON n.id = p.node_id
         WHERE n.name = %s AND p.deleted_at IS NULL
         ORDER BY p.project_ref
        """,
        (node_name,),
    )


def preflight(
    conn: psycopg.Connection,
    *,
    source_node: str,
    target_node: str,
    stanza: str,
) -> list[str]:
    """Everything that should stop a rebuild before pgBackRest is invoked.

    A restore takes as long as it takes; discovering halfway through that the
    target already serves tenants is discovering it too late.
    """
    problems: list[str] = []

    source = db.one(conn, "SELECT id, name FROM nodes WHERE name = %s", (source_node,))
    if source is None:
        problems.append(f"no node named {source_node!r} to rebuild from")

    target = db.one(conn, "SELECT id, name FROM nodes WHERE name = %s", (target_node,))
    if target is None:
        problems.append(
            f"no node named {target_node!r}. Register it first: the rebuild "
            "reconnects the control plane to a node it knows about, rather than "
            "inventing one mid-recovery"
        )
    elif source is not None and source["id"] == target["id"]:
        problems.append(
            "the source and target are the same node. A rebuild restores onto "
            "fresh hardware; restoring over the machine you are recovering from "
            "destroys the evidence and the data together"
        )

    if target is not None:
        existing = projects_on(conn, node_name=target_node)
        if existing:
            problems.append(
                f"{target_node} already carries {len(existing)} project(s): "
                + ", ".join(p["project_ref"] for p in existing[:5])
                + ". Rebuilding onto a node that is already serving turns one "
                "outage into two"
            )

    if not stanza:
        problems.append("no pgBackRest stanza given, and none recorded for the source node")

    return problems


def _tenant_databases(admin_conn: psycopg.Connection) -> list[str]:
    """Every tenant database the restored cluster came up with."""
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'mldb\\_%' "
            "AND NOT datistemplate ORDER BY datname"
        )
        return [r[0] for r in cur.fetchall()]


def verify_tenants(
    target_admin: psycopg.Connection,
    databases: list[str],
    *,
    connect=None,
) -> list[TenantCheck]:
    """Check each restored tenant owns its own schemas (ADR-059).

    The finding this exists for: a restore that cannot find a tenant's roles
    completes with `pg_restore` exiting 1 and "errors ignored", every row
    present, and `auth` and `storage` silently owned by whoever ran it. A node
    rebuild hits that path for every tenant at once, so it is checked for every
    tenant rather than sampled.
    """
    open_ = connect or restore._connect_to  # noqa: SLF001
    out: list[TenantCheck] = []
    for database in databases:
        ref = restore.tenant_ref_of(database)
        if models.database_name_for(ref) != database:
            out.append(
                TenantCheck(ref, database, False, "not a name this platform generates")
            )
            continue
        names = provisioning.TenantNames.for_ref(ref)
        try:
            with open_(target_admin, database) as tenant_conn:
                report = restore.verify_ownership(
                    tenant_conn, target_admin, names, database=database
                )
        except Exception as exc:  # noqa: BLE001 - one bad tenant must not stop the rest
            out.append(TenantCheck(ref, database, False, f"{type(exc).__name__}: {exc}"))
            continue
        out.append(
            TenantCheck(
                ref,
                database,
                bool(report and report.verified),
                report.detail if report else "not checked",
            )
        )
    return out


def repoint(
    conn: psycopg.Connection, *, source_node: str, target_node: str, refs: list[str]
) -> int:
    """Move the named projects onto the target node. The last step, deliberately.

    Only the projects whose tenants verified: a project left pointing at the
    lost node is visibly broken, which is recoverable. A project pointing at a
    database whose `auth` schema belongs to the superuser is not visibly
    anything, and that is worse.
    """
    if not refs:
        return 0
    row = db.one(conn, "SELECT id FROM nodes WHERE name = %s", (target_node,))
    if row is None:
        raise RebuildError(f"no node named {target_node!r}")
    db.execute(
        conn,
        """
        UPDATE projects SET node_id = %s
         WHERE project_ref = ANY(%s)
           AND node_id = (SELECT id FROM nodes WHERE name = %s)
        """,
        (row["id"], refs, source_node),
    )
    return len(refs)


def rebuild(
    conn: psycopg.Connection,
    target_admin: psycopg.Connection,
    *,
    source_node: str,
    target_node: str,
    stanza: str,
    target_time: datetime | None = None,
    run_as: str = "postgres",
    connect=None,
) -> RebuildOutcome:
    """Restore a lost node's cluster onto the target, then reconnect the control plane.

    `target_admin` is a superuser connection to the **already-restored** target
    cluster. Restoring the data directory is a root-level operation on the
    target machine and is driven by `cp-manage node rebuild` on that host; this
    function owns the part that has to be right afterwards -- checking what came
    back, and repointing only what verified.
    """
    outcome = RebuildOutcome(source_node=source_node, target_node=target_node, stanza=stanza)
    started = time.monotonic()
    try:
        problems = preflight(
            conn, source_node=source_node, target_node=target_node, stanza=stanza
        )
        if problems:
            raise RebuildError("; ".join(problems))

        databases = _tenant_databases(target_admin)
        outcome.databases_found = len(databases)
        if not databases:
            raise RebuildError(
                "the restored cluster has no tenant databases. Either the stanza "
                "is not this node's, or the restore did not complete"
            )

        outcome.tenants = verify_tenants(target_admin, databases, connect=connect)
        verified = [t.project_ref for t in outcome.tenants if t.verified]

        # Repointing is what sends customer traffic at the result, so it comes
        # after the check and covers only what passed.
        outcome.repointed = repoint(
            conn, source_node=source_node, target_node=target_node, refs=verified
        )
        # The lost node keeps its row -- it holds the stanza and the encrypted
        # admin DSN -- and stops being placeable.
        nodes.set_status(conn, name=source_node, status=LOST_STATUS)
        conn.commit()

        if outcome.unverified:
            outcome.notes.append(
                f"{len(outcome.unverified)} tenant(s) did not verify and were left "
                "on " + source_node + ": "
                + ", ".join(t.project_ref for t in outcome.unverified)
            )
        outcome.status = "complete"
    except Exception as exc:  # noqa: BLE001 - reported, never half-applied
        conn.rollback()
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        log.warning("rebuild of %s onto %s failed: %s", source_node, target_node, outcome.error)
    finally:
        outcome.total_seconds = time.monotonic() - started
    return outcome
