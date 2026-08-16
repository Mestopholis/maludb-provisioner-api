"""Node preconditions for Realtime, and replication-slot safety.

Phase 06 slice 1. Nothing here enables Realtime for a project -- that is slice
2. This is the part that has to be true *before* a node may host its first
Realtime tenant, and the part that notices when a slot the platform committed to
has stopped working.

Two ADRs live in this file.

**ADR-031.** Logical decoding requires the `REPLICATION` role attribute and
there is no lesser privilege that buys it. A non-superuser holding only that
attribute took a 484 MB physical base backup of every database on a cluster it
held `CONNECT` on exactly one of -- the ADR-014 lockdown does not reach physical
replication, because physical replication names no database and never reaches the
check. The containment is a `pg_hba.conf` reject, whose `replication` keyword
matches physical connections only, so it closes base backups while leaving
logical decoding working and `CONNECT`-scoped. That is node configuration, so a
node missing it hands a cluster-wide reader to the first project that enables
Realtime.

**ADR-032.** An unconsumed slot pins WAL without limit: one idle slot grew
`pg_wal` from 17 MB to 225 MB during a single insert, and a `CHECKPOINT` did not
release it. A full disk stops writes for every tenant on the node, so one
project's *inactivity* is a cross-tenant outage. `max_slot_wal_keep_size`
inverts that into an invalidated slot -- contained, but silent, which is why
invalidation is detected and reported here rather than merely configured.

Evidence and reproduction for both: `specs/realtime-replication-model.md`.

## On checking `pg_hba.conf` two ways

Neither check is sufficient alone, which is why both are required to agree.

The empirical one opens a connection with libpq's `replication=true` -- a
physical replication connection and nothing else -- and sees whether the node
refuses it. That is the R6b test from the spec, run against the node rather than
reasoned about. Its limit is that it runs as *one* role, the platform's own, and
`pg_hba.conf` matches on the user as well as the address. A file that rejects
this role and admits another would answer the probe correctly while still
handing a tenant's replicator a readable copy of the cluster.

So the file is parsed too, which also lets the failure name the offending line
rather than only assert that one exists. First-match is modelled per (type,
address, netmask), and a reject only shadows the rules below it when it names
`all` users. CIDR containment between groups is not modelled, so a permissive
rule shadowed by a broader earlier reject is still reported -- deliberately the
conservative direction, and the fix is to delete a line that was already dead.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.control_plane import db, models

log = logging.getLogger(__name__)

# Slot states, mirroring the CHECK constraint in migration 0012.
NONE = "none"
PENDING = "pending"
ACTIVE = "active"
LOST = "lost"
MISSING = "missing"

# The lowest bound worth accepting for max_slot_wal_keep_size. Not a tuning
# recommendation -- a floor below which the setting is doing something other
# than what ADR-032 asked for. A bound smaller than a few WAL segments
# invalidates slots during ordinary traffic rather than during a stall.
MIN_SLOT_WAL_KEEP_MB = 64

# Slots the platform keeps for itself: backups, a future physical standby, an
# operator debugging with pg_recvlogical. Held back from the tenant pool for the
# same reason PLATFORM_CONNECTION_ALLOWANCE is -- a node full of tenants must
# still be operable.
PLATFORM_SLOT_ALLOWANCE = 2


class RealtimeError(RuntimeError):
    """A node could not be inspected, or a slot could not be reconciled."""


# --------------------------------------------------------------------------
# Node readiness
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeReadiness:
    """What a node's settings say about whether it may host Realtime.

    Every field is read from the node. Nothing here is defaulted from what a
    node is assumed to be: the spike found the interesting failures in exactly
    the settings an operator is most likely to have left alone.
    """

    wal_level: str
    max_replication_slots: int
    max_wal_senders: int
    # Megabytes, or -1 for unbounded, which is PostgreSQL's default and the
    # value ADR-032 exists to forbid.
    max_slot_wal_keep_mb: int
    # None when the probe could not be run at all, which is not the same as a
    # node that permits physical replication and must not be reported as though
    # it were.
    physical_replication_rejected: bool | None
    probe_detail: str
    # Rules that would let a physical replication connection through, in file
    # order, as `line 92: host replication all 127.0.0.1/32 scram-sha-256`.
    permissive_hba_rules: list[str] = field(default_factory=list)
    hba_detail: str = ""

    @property
    def failures(self) -> list[str]:
        """Why this node may not host Realtime. Empty means it may.

        Ordered by how expensive the fix is. `wal_level` needs a cluster
        restart, which is an outage for every tenant already on the node, so it
        is named first -- a node that is going to fail this should fail it before
        anyone starts editing `pg_hba.conf`.
        """
        problems: list[str] = []

        if self.wal_level != "logical":
            problems.append(
                f"wal_level is {self.wal_level!r}, not 'logical'; logical decoding cannot "
                "run at all below it, and changing it needs a cluster restart"
            )

        if self.physical_replication_rejected is not True:
            detail = (
                "the node accepted one"
                if self.physical_replication_rejected is False
                else f"the probe could not run ({self.probe_detail})"
            )
            problems.append(
                "physical replication is not rejected by pg_hba.conf -- "
                f"{detail}. ADR-031: without this, the first project to enable "
                "Realtime holds a readable copy of every tenant on the node"
            )

        # A passing probe is not sufficient on its own. It runs as the
        # platform's own role, and pg_hba matches on the user as well as the
        # address, so a file that rejects this role and admits another would
        # answer the probe correctly while still handing a tenant's replicator a
        # base backup of the cluster. The rules have to agree with the probe.
        if self.permissive_hba_rules:
            problems.append(
                "pg_hba.conf admits physical replication for some roles: "
                + "; ".join(self.permissive_hba_rules)
                + ". A rule naming a role the probe does not use is invisible to the probe, "
                "so the file has to say what the probe says"
            )

        if self.max_slot_wal_keep_mb < 0:
            problems.append(
                "max_slot_wal_keep_size is unbounded; ADR-032: one stalled consumer then "
                "fills the disk and stops writes for every tenant on the node"
            )
        elif self.max_slot_wal_keep_mb < MIN_SLOT_WAL_KEEP_MB:
            problems.append(
                f"max_slot_wal_keep_size is {self.max_slot_wal_keep_mb} MB, below the "
                f"{MIN_SLOT_WAL_KEEP_MB} MB floor; slots would be invalidated by ordinary "
                "traffic rather than by a stall"
            )

        if self.max_replication_slots <= PLATFORM_SLOT_ALLOWANCE:
            problems.append(
                f"max_replication_slots is {self.max_replication_slots}, leaving nothing for "
                f"tenants once the platform's {PLATFORM_SLOT_ALLOWANCE} are held back"
            )

        if self.max_wal_senders < self.max_replication_slots:
            problems.append(
                f"max_wal_senders ({self.max_wal_senders}) is below max_replication_slots "
                f"({self.max_replication_slots}); slots would exist that no consumer can attach to"
            )

        return problems

    @property
    def ready(self) -> bool:
        return not self.failures

    def as_capacity(self) -> dict[str, Any]:
        """The subset recorded on the node row, for placement to read later.

        Placement runs on a control-plane transaction and must not open a
        connection to a node to find out whether it can host Realtime.
        """
        return {
            "realtime_ready": self.ready,
            "wal_level": self.wal_level,
            "max_replication_slots": self.max_replication_slots,
            "max_wal_senders": self.max_wal_senders,
            "max_slot_wal_keep_mb": self.max_slot_wal_keep_mb,
            "physical_replication_rejected": self.physical_replication_rejected,
        }


def _settings(admin_conn: psycopg.Connection) -> dict[str, Any]:
    with admin_conn.cursor(row_factory=dict_row) as cur:
        # pg_settings rather than current_setting(), because max_slot_wal_keep_size
        # reports its own unit and the raw setting is the number of them. Reading
        # it as text gives '-1' on one node and '64MB' on the next.
        cur.execute(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s)",
            (["wal_level", "max_replication_slots", "max_wal_senders",
              "max_slot_wal_keep_size"],),
        )
        return {row["name"]: row for row in cur.fetchall()}


def _keep_size_mb(row: dict[str, Any] | None) -> int:
    """max_slot_wal_keep_size in megabytes, or -1 for unbounded."""
    if row is None:
        return -1
    value = int(row["setting"])
    if value < 0:
        return -1
    unit = (row["unit"] or "MB").strip()
    # PostgreSQL reports this GUC in MB by default; a node built with a
    # different block size reports kB. Treat anything unrecognised as the
    # smallest plausible interpretation, which fails the floor rather than
    # passing it.
    return value if unit == "MB" else value // 1024


def inspect_hba(admin_conn: psycopg.Connection) -> tuple[list[str], str]:
    """Rules that would admit a physical replication connection, in file order.

    `pg_hba.conf`'s `replication` keyword in the database column matches
    physical replication *only*; `all` does not match it, and logical
    replication names a real database and matches ordinary rules. So the rules
    that matter are exactly the ones mentioning `replication`.

    First-match is modelled per (type, address, netmask) group, which is enough
    to stop the default `host replication all 127.0.0.1/32 scram-sha-256` being
    reported when an operator has put a reject above it. It is not enough to
    model CIDR containment between groups, so a permissive rule shadowed by a
    broader earlier reject is still reported. That is the conservative
    direction, and the empirical probe is what decides readiness.
    """
    try:
        with admin_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT line_number, type, database, user_name, address, netmask, "
                "       auth_method, error "
                "  FROM pg_hba_file_rules ORDER BY line_number"
            )
            rows = cur.fetchall()
    except psycopg.Error as exc:
        # Reading it needs superuser or pg_read_server_files. Not fatal: the
        # probe is the check that decides, and this one only explains.
        admin_conn.rollback()
        return [], f"could not read pg_hba_file_rules ({type(exc).__name__})"

    permissive: list[str] = []
    # Groups where an earlier rule rejects *every* user, which makes anything
    # below it in the same group unreachable. Tracked per user set rather than
    # per address alone, because pg_hba matches on the user too: a reject
    # naming one role does not shadow a permissive rule naming another, and
    # treating it as though it did would report a node as contained when a
    # tenant's replicator is still admitted by the line underneath.
    shadowed: set[tuple[str | None, str | None, str | None]] = set()

    for row in rows:
        databases = row["database"] or []
        if "replication" not in databases:
            continue
        group = (row["type"], row["address"], row["netmask"])
        if group in shadowed:
            continue

        method = (row["auth_method"] or "").lower()
        users = row["user_name"] or ["all"]
        if method == "reject":
            if "all" in users:
                shadowed.add(group)
            continue
        # `local` entries are reached over the unix socket. `peer` there
        # requires the connecting OS user to be the database role, which a
        # Realtime process running as its own user cannot satisfy for
        # mldb_<ref>_replicator -- so it stays available for node operators
        # without being reachable by a tenant credential.
        if row["type"] == "local" and method == "peer":
            continue

        address = row["address"] or ""
        permissive.append(
            f"line {row['line_number']}: {row['type']} replication {','.join(users)} "
            f"{address} {method}".replace("  ", " ").strip()
        )

    if not rows:
        return permissive, "pg_hba_file_rules returned nothing"
    return permissive, f"{len(rows)} rules read"


def probe_physical_replication(dsn: str) -> tuple[bool, str]:
    """Try to open a physical replication connection. True means it was refused.

    libpq's `replication=true` opens a physical replication connection and
    nothing else, so this reaches exactly the `pg_hba.conf` path that
    `pg_basebackup` uses -- the R6b test from the spec, run rather than
    reasoned about.

    The DSN is a live credential and is never logged, including on failure:
    only the exception's class and PostgreSQL's own message are kept, and the
    message for this failure names the host and role, not the password.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    parsed["replication"] = "true"
    # A node that is up but slow must not hang a readiness check.
    parsed.setdefault("connect_timeout", "10")
    try:
        conn = psycopg.connect(psycopg.conninfo.make_conninfo(**parsed))
    except psycopg.OperationalError as exc:
        message = " ".join(str(exc).split())
        if "pg_hba.conf rejects replication connection" in message:
            return True, "pg_hba.conf rejects replication connections"
        # Anything else -- node down, wrong credential, TLS refusal -- is not
        # evidence that physical replication is closed, and must not be
        # recorded as though it were.
        return False, f"refused, but not by an hba reject: {message}"
    conn.close()
    return False, "the node accepted a physical replication connection"


def inspect_node(admin_conn: psycopg.Connection, *, dsn: str | None = None) -> NodeReadiness:
    """Read a node's Realtime preconditions.

    `dsn` is only used for the physical-replication probe, which needs its own
    connection. Without it the probe cannot run, and the readiness reports that
    rather than assuming either answer.
    """
    settings = _settings(admin_conn)
    permissive, hba_detail = inspect_hba(admin_conn)

    if dsn:
        rejected, probe_detail = probe_physical_replication(dsn)
    else:
        rejected, probe_detail = None, "no DSN supplied for the probe"

    def as_int(name: str, default: int) -> int:
        row = settings.get(name)
        return int(row["setting"]) if row else default

    return NodeReadiness(
        wal_level=(settings["wal_level"]["setting"] if "wal_level" in settings else "unknown"),
        max_replication_slots=as_int("max_replication_slots", 0),
        max_wal_senders=as_int("max_wal_senders", 0),
        max_slot_wal_keep_mb=_keep_size_mb(settings.get("max_slot_wal_keep_size")),
        physical_replication_rejected=rejected,
        probe_detail=probe_detail,
        permissive_hba_rules=permissive,
        hba_detail=hba_detail,
    )


def record_readiness(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    name: str,
    dsn: str | None = None,
) -> NodeReadiness:
    """Inspect a node and store the result on its row.

    Recorded rather than re-derived because placement must be able to refuse a
    Realtime project on an unprepared node without opening a connection to it,
    and because a node whose `wal_level` changed since it was checked should
    show as stale rather than silently pass.
    """
    readiness = inspect_node(admin_conn, dsn=dsn)
    updated = db.execute(
        conn,
        "UPDATE nodes "
        "   SET capacity_json = capacity_json || %s::jsonb, "
        # now() from the database rather than from this process: the check is
        # only meaningful relative to the clock everything else here is stamped
        # with.
        "       metrics_json = metrics_json || jsonb_build_object("
        "           'realtime_checked_at', now(), 'realtime_failures', %s::jsonb) "
        " WHERE name = %s",
        (
            psycopg.types.json.Jsonb(readiness.as_capacity()),
            psycopg.types.json.Jsonb(readiness.failures),
            name,
        ),
    )
    if updated == 0:
        raise RealtimeError(f"no node named {name!r}")
    conn.commit()
    return readiness


# --------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------


def slot_name_for(project_ref: str) -> str:
    """The logical slot name for a project.

    Derived from the same validated identifier as the database and roles, so a
    ref that would not make a safe database name cannot make a slot name either.
    Slot names allow a narrower alphabet than identifiers generally --
    [a-z0-9_] -- which `database_name_for` already satisfies.
    """
    return f"{models.database_name_for(project_ref)}_rt"


@dataclass(frozen=True)
class Slot:
    slot_name: str
    database: str | None
    slot_type: str
    active: bool
    wal_status: str | None
    # Bytes of WAL that may still be written before this slot is invalidated.
    # NULL once the slot is lost, and NULL for slots with no reserved WAL.
    safe_wal_size: int | None

    @property
    def invalidated(self) -> bool:
        return self.wal_status == "lost"


def slots_on_node(admin_conn: psycopg.Connection) -> list[Slot]:
    """Every replication slot the node holds, logical and physical.

    Physical slots are included deliberately. ADR-032 records that a role
    holding `REPLICATION` for legitimate reasons can create a WAL-reserving
    physical slot through ordinary SQL even with ADR-031's `pg_hba` reject in
    place, and PostgreSQL has no per-role slot quota. A physical slot nobody
    asked for is therefore a thing the platform has to be able to see.
    """
    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT slot_name, database, slot_type, active, wal_status, safe_wal_size "
            "  FROM pg_replication_slots ORDER BY slot_name"
        )
        return [
            Slot(
                slot_name=row["slot_name"],
                database=row["database"],
                slot_type=row["slot_type"],
                active=bool(row["active"]),
                wal_status=row["wal_status"],
                safe_wal_size=row["safe_wal_size"],
            )
            for row in cur.fetchall()
        ]


@dataclass
class SlotReport:
    """What one node's slots look like against what the platform believes."""

    node_name: str
    checked: int = 0
    invalidated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unaccounted: list[str] = field(default_factory=list)

    @property
    def incidents(self) -> int:
        return len(self.invalidated) + len(self.missing)


def _audit_slot(
    conn: psycopg.Connection,
    project_id: uuid.UUID,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    """Record an invalidation against the project it happened to.

    ADR-032: the one unacceptable outcome is silence. The customer-visible
    symptom of an invalidated slot is an application that stops receiving events
    with nothing anywhere to say so, and a contained failure nobody is told
    about is indistinguishable from data loss.

    Events about a slot that stopped working carry `replayed_on_recovery: false`
    explicitly. Recovery re-creates the slot, which resumes from the present; a
    report that does not say so leaves a customer assuming a backfill that never
    happened.
    """
    db.execute(
        conn,
        "INSERT INTO audit_events (project_id, actor_type, event_type, detail_json) "
        "VALUES (%s, 'system', %s, %s)",
        (project_id, event_type, psycopg.types.json.Jsonb(detail)),
    )


def reconcile_slots(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    node_id: int,
    node_name: str = "",
) -> SlotReport:
    """Compare a node's real slots against the projects that should hold them.

    Idempotent in the way `storage.evaluate` is: a project already recorded as
    lost is not re-audited on every pass, so a maintenance run every few minutes
    does not write an audit event every few minutes. The transition is the
    event, not the state.
    """
    report = SlotReport(node_name=node_name or str(node_id))
    by_name = {slot.slot_name: slot for slot in slots_on_node(admin_conn)}

    projects = db.query(
        conn,
        "SELECT id, project_ref, realtime_slot_name, realtime_slot_state "
        "  FROM projects "
        " WHERE node_id = %s AND realtime_enabled AND deleted_at IS NULL",
        (node_id,),
    )

    expected: set[str] = set()
    for project in projects:
        slot_name = project["realtime_slot_name"] or slot_name_for(project["project_ref"])
        expected.add(slot_name)
        previous = project["realtime_slot_state"]
        slot = by_name.get(slot_name)
        report.checked += 1

        if slot is None:
            state = MISSING
            detail = {
                "slot_name": slot_name,
                "reason": "the node does not have this slot",
                "replayed_on_recovery": False,
            }
        elif slot.invalidated:
            state = LOST
            detail = {
                "slot_name": slot_name,
                "reason": "the slot exceeded max_slot_wal_keep_size and was invalidated",
                "wal_status": slot.wal_status,
                "replayed_on_recovery": False,
            }
        else:
            state = ACTIVE
            detail = {}

        db.execute(
            conn,
            "UPDATE projects SET realtime_slot_state = %s, realtime_slot_checked_at = now(), "
            "       realtime_slot_lost_at = CASE WHEN %s IN ('lost','missing') "
            "                                    THEN COALESCE(realtime_slot_lost_at, now()) "
            "                                    ELSE NULL END "
            " WHERE id = %s",
            (state, state, project["id"]),
        )

        if state == previous:
            continue
        if state == LOST:
            report.invalidated.append(project["project_ref"])
            _audit_slot(conn, project["id"], "realtime.slot_invalidated", detail)
            log.warning(
                "project %s: replication slot invalidated, changes are not being delivered",
                project["project_ref"],
            )
        elif state == MISSING:
            report.missing.append(project["project_ref"])
            _audit_slot(conn, project["id"], "realtime.slot_missing", detail)
            log.warning("project %s: replication slot %s is absent from its node",
                        project["project_ref"], slot_name)
        elif previous in (LOST, MISSING):
            _audit_slot(conn, project["id"], "realtime.slot_restored",
                        {"slot_name": slot_name, "reason": "the slot is present again"})
            log.info("project %s: replication slot restored", project["project_ref"])

    # Slots on the node that no project claims. Includes the physical-slot path
    # from ADR-032, which the pg_hba reject does not close.
    for name, slot in by_name.items():
        if name in expected:
            continue
        report.unaccounted.append(f"{name} ({slot.slot_type}, wal_status={slot.wal_status})")

    conn.commit()
    return report


# --------------------------------------------------------------------------
# Per-project enablement (slice 2)
# --------------------------------------------------------------------------

PUBLICATION = "supabase_realtime"

# pgoutput is PostgreSQL's built-in logical decoding plugin and the one upstream
# Realtime consumes. Naming it here rather than making it configurable is
# deliberate: a different plugin is a different wire format, which is a
# compatibility decision and not a deployment setting.
OUTPUT_PLUGIN = "pgoutput"

CREDENTIAL_TYPE = "db_replicator"


@dataclass(frozen=True)
class Enablement:
    """What enabling or disabling actually did, for the operator and the audit."""

    project_ref: str
    slot_name: str
    changed: bool
    detail: str


def _project_for_realtime(conn: psycopg.Connection, project_id: uuid.UUID) -> dict:
    project = db.one(
        conn,
        "SELECT id, project_ref, node_id, database_name, status, realtime_enabled, "
        "       realtime_slot_name, realtime_slot_state, realtime_slot_lost_at "
        "  FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None:
        raise RealtimeError("project does not exist")
    if project["database_name"] is None or project["node_id"] is None:
        raise RealtimeError("project has no database yet; provision it before enabling Realtime")
    return project


def _claim_slot(conn: psycopg.Connection, project: dict, slot_name: str) -> None:
    """Take one of the node's slots for this project, or refuse.

    The check and the claim happen inside one transaction holding a row lock on
    the node, for the same reason `reserve_placement` does it that way: slots are
    a cluster-wide pool of ten, so two enablements racing for the last one is not
    a hypothetical. Checking capacity and then writing without the lock would let
    both win and leave the second to discover the truth from PostgreSQL, halfway
    through, on a project that had already been told it had Realtime.
    """
    from services.control_plane import nodes

    with conn.transaction():
        db.one(conn, "SELECT id FROM nodes WHERE id = %s FOR UPDATE", (project["node_id"],))
        capacity = nodes.capacity_of(conn, project["node_id"])
        reason = capacity.realtime_rejection_reason()
        if reason:
            raise RealtimeError(f"node {capacity.name} cannot take another Realtime project: {reason}")
        db.execute(
            conn,
            "UPDATE projects SET realtime_enabled = TRUE, realtime_slot_name = %s, "
            "       realtime_slot_state = %s WHERE id = %s",
            (slot_name, PENDING, project["id"]),
        )


def _release_claim(conn: psycopg.Connection, project_id: uuid.UUID) -> None:
    """Give the slot back after a failure, so a claim does not leak.

    A project left `realtime_enabled` with no slot would hold one of ten against
    a node forever, and would be reported as `missing` by every maintenance pass
    thereafter -- an incident raised about a capability nobody ever received.
    """
    db.execute(
        conn,
        "UPDATE projects SET realtime_enabled = FALSE, realtime_slot_name = NULL, "
        "       realtime_slot_state = %s, realtime_slot_lost_at = NULL WHERE id = %s",
        (NONE, project_id),
    )
    conn.commit()


def _slot_present(tenant_conn: psycopg.Connection, slot_name: str) -> bool:
    with tenant_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s AND database = current_database()",
            (slot_name,),
        )
        return cur.fetchone() is not None


def _create_slot(tenant_conn: psycopg.Connection, slot_name: str) -> None:
    """Create the project's logical slot, translating the ceiling into a refusal.

    R2: PostgreSQL fails loudly at `max_replication_slots`, on the operation that
    asked for it. That is the good shape and this preserves it -- the node's
    accounting is checked first, and this is the backstop for the case where the
    accounting and the node disagree.
    """
    try:
        with tenant_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_create_logical_replication_slot(%s, %s)", (slot_name, OUTPUT_PLUGIN)
            )
    except psycopg.errors.ConfigurationLimitExceeded as exc:
        raise RealtimeError(
            "the node is out of replication slots, so the slot was not created and Realtime "
            f"is not enabled: {exc}"
        ) from None
    except psycopg.errors.DuplicateObject:
        # Someone else created it between the check and here. Idempotent by
        # intent, so this is success rather than a race to report.
        tenant_conn.rollback()


def _drop_slot(tenant_conn: psycopg.Connection, slot_name: str) -> bool:
    """Drop a slot, disconnecting its consumer first if one is attached.

    Terminating the walsender is deliberate rather than incidental: PostgreSQL
    refuses to drop an active slot, and a slot left behind because its consumer
    was still attached is precisely the WAL-pinning failure ADR-032 exists to
    prevent. Turning Realtime off has to actually release the slot.
    """
    with tenant_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT active_pid FROM pg_replication_slots "
            " WHERE slot_name = %s AND database = current_database()",
            (slot_name,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if row["active_pid"] is not None:
            cur.execute("SELECT pg_terminate_backend(%s)", (row["active_pid"],))
        cur.execute("SELECT pg_drop_replication_slot(%s)", (slot_name,))
    return True


def publication_present(tenant_conn: psycopg.Connection) -> bool:
    with tenant_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (PUBLICATION,))
        return cur.fetchone() is not None


def enable(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    key_ring,
    tenant_connect,
) -> Enablement:
    """Turn Realtime on for one project.

    Entitlement first, then capacity, then the node. Ordered that way because
    each step is more expensive to undo than the one before it, and because the
    two refusals a customer can actually hit -- "your plan does not include
    this" and "this node is full" -- should not require touching the tenant
    database to discover.

    Safe to call repeatedly (AGENTS.md). A project that is already enabled and
    whose slot exists is left alone; one that is enabled with no slot gets the
    slot, which is also how a failed first attempt is resumed.
    """
    from services.control_plane import entitlements, provisioning

    project = _project_for_realtime(conn, project_id)
    names = provisioning.TenantNames.for_ref(project["project_ref"])
    slot_name = slot_name_for(project["project_ref"])

    allowed = entitlements.for_project(conn, project_id)
    if allowed.realtime_connections <= 0:
        raise RealtimeError(
            "this project's plan does not include Realtime (realtime_connections is 0). "
            "Change the plan rather than enabling it here -- the next provisioning run "
            "applies the plan and would turn it off again."
        )

    already = bool(project["realtime_enabled"])
    created = False
    if not already:
        _claim_slot(conn, project, slot_name)
        conn.commit()

    try:
        # The role is created before the slot, because a slot with no consumer
        # is the dangerous artefact and a role with no slot is inert.
        provisioning.create_replicator_role(
            admin_conn, names,
            password=(password := provisioning.generate_password()),
            connection_limit=max(1, min(allowed.realtime_connections, 10)),
        )
        provisioning.store_credential(
            conn, project_id=project_id, credential_type=CREDENTIAL_TYPE,
            role_name=names.replicator, secret=password, key_ring=key_ring,
        )
        conn.commit()

        with tenant_connect(project["database_name"]) as tenant_conn:
            if not publication_present(tenant_conn):
                raise RealtimeError(
                    f"the {PUBLICATION!r} publication is missing from this tenant; it is created "
                    "by bootstrap 009, so this project needs its bootstrap brought forward "
                    "before Realtime can work"
                )
            created = not _slot_present(tenant_conn, slot_name)
            if created:
                _create_slot(tenant_conn, slot_name)
    except Exception:
        if not already:
            _release_claim(conn, project_id)
        raise

    db.execute(
        conn,
        "UPDATE projects SET realtime_slot_state = %s, realtime_slot_checked_at = now(), "
        "       realtime_slot_lost_at = NULL WHERE id = %s",
        (ACTIVE, project_id),
    )
    changed = not already or created
    if changed:
        _audit_slot(conn, project_id, "realtime.enabled",
                    {"slot_name": slot_name, "publication": PUBLICATION})
    conn.commit()

    return Enablement(
        project_ref=project["project_ref"], slot_name=slot_name, changed=changed,
        detail="enabled" if changed else "already enabled",
    )


def disable(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> Enablement:
    """Turn Realtime off, and give the slot back.

    The slot is dropped first and the bookkeeping updated after, because the
    order that fails safely is the one where a crash leaves the platform still
    believing it owns a slot it might not have. The reverse leaves a slot on the
    node that nothing claims, pinning WAL with no project to attribute it to --
    which is the failure ADR-032 is about, arrived at by tidying up.
    """
    from services.control_plane import provisioning

    project = _project_for_realtime(conn, project_id)
    names = provisioning.TenantNames.for_ref(project["project_ref"])
    slot_name = project["realtime_slot_name"] or slot_name_for(project["project_ref"])

    dropped = False
    with tenant_connect(project["database_name"]) as tenant_conn:
        dropped = _drop_slot(tenant_conn, slot_name)

    provisioning.drop_replicator_role(admin_conn, names)
    db.execute(
        conn,
        "UPDATE project_credentials SET revoked_at = now() "
        " WHERE project_id = %s AND credential_type = %s AND revoked_at IS NULL",
        (project_id, CREDENTIAL_TYPE),
    )

    changed = bool(project["realtime_enabled"]) or dropped
    db.execute(
        conn,
        "UPDATE projects SET realtime_enabled = FALSE, realtime_slot_name = NULL, "
        "       realtime_slot_state = %s, realtime_slot_lost_at = NULL, "
        "       realtime_slot_checked_at = now() WHERE id = %s",
        (NONE, project_id),
    )
    if changed:
        _audit_slot(conn, project_id, "realtime.disabled",
                    {"slot_name": slot_name, "slot_dropped": dropped})
    conn.commit()

    return Enablement(
        project_ref=project["project_ref"], slot_name=slot_name, changed=changed,
        detail="disabled" if changed else "was not enabled",
    )


def recover_slot(
    conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> Enablement:
    """Re-create a slot that was invalidated or lost, and record what was missed.

    Slice 1 deliberately did not do this automatically, and it still does not:
    ADR-032 makes invalidation a project-visible incident, and a platform that
    silently repaired it would turn a reportable failure back into a silent one.
    This is the operator's deliberate act, and its audit event carries the size
    of the gap.

    **The gap is not replayed.** A new slot starts from the present, so every
    change written while the old one was invalid is gone from the stream. Saying
    so in the record is the point of the record.
    """
    project = _project_for_realtime(conn, project_id)
    if not project["realtime_enabled"]:
        raise RealtimeError("Realtime is not enabled for this project; enable it instead")
    if project["realtime_slot_state"] not in (LOST, MISSING):
        raise RealtimeError(
            f"this project's slot is {project['realtime_slot_state']!r}, so there is nothing to "
            "recover. Re-creating a working slot would lose changes for no reason."
        )

    slot_name = project["realtime_slot_name"] or slot_name_for(project["project_ref"])
    with tenant_connect(project["database_name"]) as tenant_conn:
        _drop_slot(tenant_conn, slot_name)
        _create_slot(tenant_conn, slot_name)

    lost_at = project["realtime_slot_lost_at"]
    db.execute(
        conn,
        "UPDATE projects SET realtime_slot_state = %s, realtime_slot_lost_at = NULL, "
        "       realtime_slot_checked_at = now() WHERE id = %s",
        (ACTIVE, project_id),
    )
    _audit_slot(
        conn, project_id, "realtime.slot_recreated",
        {
            "slot_name": slot_name,
            "gap_began_at": lost_at.isoformat() if lost_at else None,
            "reason": "the slot was re-created by an operator; changes written while it was "
                      "invalid were not delivered and cannot be recovered",
            "replayed_on_recovery": False,
        },
    )
    conn.commit()
    return Enablement(
        project_ref=project["project_ref"], slot_name=slot_name, changed=True,
        detail="slot re-created; the gap was not replayed",
    )


def apply_plan(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    tenant_connect,
) -> Enablement | None:
    """Make a project's Realtime match what its plan entitles it to.

    Only ever removes. A downgrade must take the capability away -- and take the
    slot back, since the node is holding one of ten for it -- but an *upgrade*
    does not silently start replicating a customer's tables, because enabling
    Realtime creates a role holding `REPLICATION` and that should be somebody's
    decision rather than a side effect of a billing change.

    Called from provisioning for the same reason `set_direct_sql_access` is: a
    plan that says no while the node says yes is a capability the customer is
    not paying for and the platform is still spending a slot on.

    Two things a reviewer should know, because they are the reason this is not
    more aggressive than it is.

    Disabling here is *destructive* in a way disabling direct SQL is not: it
    drops the replicator role and revokes its credential, where direct SQL only
    flips `LOGIN` and deliberately keeps the password so an upgrade does not
    hand the customer a different one. That asymmetry is correct, because the
    customer never holds the replicator password -- only the platform does -- so
    re-enabling mints a new one at no cost to anybody.

    And `entitlements.resolve` falls back to the **free** tier for any plan code
    it does not recognise, where `realtime_connections` is 0. So a project on a
    custom plan whose `config_json` omits the limit resolves to "not entitled"
    and will have Realtime removed the next time provisioning runs. That is the
    safe direction for a limit, and it is why this runs only from a provisioning
    run somebody triggered -- never from the maintenance pass, which would take
    a working capability away from a paying customer in the background over a
    missing key in a plan row.
    """
    from services.control_plane import entitlements

    project = db.one(
        conn,
        "SELECT realtime_enabled FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    if project is None or not project["realtime_enabled"]:
        return None
    if entitlements.for_project(conn, project_id).realtime_connections > 0:
        return None

    log.info("project %s: plan no longer includes Realtime, disabling", project_id)
    return disable(conn, admin_conn, project_id=project_id, tenant_connect=tenant_connect)


def committed_slots(conn: psycopg.Connection, node_id: int) -> int:
    """How many of a node's slots the platform has already promised to tenants.

    Counted from enablement rather than from what the node currently reports,
    because a slot that is missing or invalidated is still a slot the platform
    owes that project and must not hand to another one.
    """
    row = db.one(
        conn,
        "SELECT count(*) AS n FROM projects "
        " WHERE node_id = %s AND realtime_enabled AND deleted_at IS NULL",
        (node_id,),
    )
    return int(row["n"]) if row else 0


__all__ = [
    "ACTIVE",
    "CREDENTIAL_TYPE",
    "LOST",
    "MIN_SLOT_WAL_KEEP_MB",
    "MISSING",
    "NONE",
    "OUTPUT_PLUGIN",
    "PENDING",
    "PLATFORM_SLOT_ALLOWANCE",
    "PUBLICATION",
    "Enablement",
    "NodeReadiness",
    "RealtimeError",
    "Slot",
    "SlotReport",
    "apply_plan",
    "committed_slots",
    "disable",
    "enable",
    "publication_present",
    "recover_slot",
    "inspect_hba",
    "inspect_node",
    "probe_physical_replication",
    "reconcile_slots",
    "record_readiness",
    "slot_name_for",
    "slots_on_node",
]
