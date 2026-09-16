"""Read-only reports for the operator console (ADR-082 slices 3a sales and customers, 3b usage).

**Its own queries, importing nothing but `db`.** The same questions are answered for
`cp-manage` by `billing`, `subscriptions` and friends, but those modules import the
Stripe client, plan changes and provisioning; the console's import graph must reach
none of them (`tests/test_admin_app.py`). A read here is a few lines of SQL, and
duplicating a SELECT is cheaper than widening what an internet-adjacent process can
call.

**Platform records, never customer content** (ADR-082 decision 6). Names, owner emails,
plan codes, states, byte counts and billing event outcomes. Nothing from inside a tenant
database, no key material, and no amounts (ADR-052: amounts are Stripe's).

Every query here runs as `cp_admin_console`; `tests/test_admin_reports.py` runs each
report as that role, so a query reaching a column the role was not granted fails there.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import psycopg
from psycopg.types.json import Jsonb

from services.control_plane import db, entitlements

# Statuses a customer would call "set up and working" and "on its way", as the dashboard
# groups them (frontend STATUS table); anything else is shown by its raw value.
SERVING = ("PROVISIONED", "ACTIVE")
ENDED = ("DELETING", "DELETED")


def overview(conn: psycopg.Connection, *, grace_days: int) -> dict:
    """The numbers on the console's first page."""
    counts = db.one(
        conn,
        """
        SELECT (SELECT count(*) FROM organizations WHERE deleted_at IS NULL) AS organizations,
               (SELECT count(*) FROM users WHERE deleted_at IS NULL) AS users,
               (SELECT count(*) FROM users WHERE deleted_at IS NULL
                  AND created_at > now() - interval '7 days') AS users_last_7_days,
               (SELECT count(*) FROM projects WHERE deleted_at IS NULL AND status <> ALL(%s)) AS projects,
               (SELECT count(*) FROM projects WHERE deleted_at IS NULL AND status = ANY(%s)) AS projects_serving,
               (SELECT count(*) FROM projects WHERE deleted_at IS NULL AND status = 'FAILED') AS projects_failed,
               (SELECT count(*) FROM billing_events WHERE received_at > now() - interval '7 days') AS events_7d,
               (SELECT count(*) FROM billing_events WHERE outcome IN ('failed', 'received')
                  AND received_at > now() - interval '7 days') AS events_7d_unhandled
        """,
        (list(ENDED), list(SERVING)),
    )
    by_plan = db.query(
        conn,
        """
        SELECT pl.code AS plan_code, count(*) AS projects
          FROM projects pr JOIN plans pl ON pl.id = pr.plan_id
         WHERE pr.deleted_at IS NULL AND pr.status <> ALL(%s)
         GROUP BY pl.code ORDER BY count(*) DESC, pl.code
        """,
        (list(ENDED),),
    )
    by_state = db.query(
        conn,
        "SELECT state, count(*) AS subscriptions FROM subscriptions GROUP BY state ORDER BY state",
    )
    return {
        **counts,
        "projects_by_plan": by_plan,
        "subscriptions_by_state": by_state,
        "in_grace": len(in_grace(conn, grace_days=grace_days)),
        "pending_reconciliation": len(pending_reconciliation(conn)),
    }


def subscriptions(conn: psycopg.Connection, *, state: str | None = None, limit: int = 200) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT s.id, s.plan_code, s.state, s.state_since, s.period_start, s.period_end, s.created_at,
               s.provider_subscription_id, s.provider_customer_id,
               pr.project_ref, pr.display_name AS project_name, o.id AS org_id, o.display_name AS org_name
          FROM subscriptions s
          JOIN projects pr ON pr.id = s.project_id
          JOIN organizations o ON o.id = s.org_id
         WHERE (%s::text IS NULL OR s.state = %s)
         ORDER BY s.state_since DESC NULLS LAST, s.created_at DESC
         LIMIT %s
        """,
        (state, state, limit),
    )


def in_grace(conn: psycopg.Connection, *, grace_days: int) -> list[dict]:
    """Past-due subscriptions still inside the grace period (ADR-051), soonest to expire first."""
    return db.query(
        conn,
        """
        SELECT s.plan_code, s.state_since, pr.project_ref, o.id AS org_id, o.display_name AS org_name,
               s.state_since + (%s * interval '1 day') AS expires_at
          FROM subscriptions s
          JOIN projects pr ON pr.id = s.project_id
          JOIN organizations o ON o.id = s.org_id
         WHERE s.state = 'past_due' AND pr.deleted_at IS NULL AND pr.status <> ALL(%s)
         ORDER BY s.state_since
        """,
        (grace_days, list(ENDED)),
    )


def pending_reconciliation(conn: psycopg.Connection) -> list[dict]:
    """Paid-for changes the maintenance pass has not applied yet (ADR-053)."""
    return db.query(
        conn,
        """
        SELECT s.plan_code, s.state, s.state_as_of, pr.project_ref
          FROM subscriptions s
          JOIN projects pr ON pr.id = s.project_id
         WHERE (s.reconciled_state, s.reconciled_plan_code) IS DISTINCT FROM (s.state, s.plan_code)
           AND pr.deleted_at IS NULL AND pr.status <> ALL(%s)
         ORDER BY s.state_as_of
        """,
        (list(ENDED),),
    )


def billing_events(conn: psycopg.Connection, *, outcome: str | None = None, limit: int = 100) -> list[dict]:
    return db.query(
        conn,
        """
        SELECT e.event_id, e.event_type, e.livemode, e.event_at, e.received_at, e.outcome, e.note,
               pr.project_ref
          FROM billing_events e
          LEFT JOIN projects pr ON pr.id = e.project_id
         WHERE (%s::text IS NULL OR e.outcome = %s)
         ORDER BY e.received_at DESC
         LIMIT %s
        """,
        (outcome, outcome, limit),
    )


def customers(conn: psycopg.Connection, *, search: str | None = None, limit: int = 100) -> list[dict]:
    """Organizations, newest first, with their owners and what they hold."""
    pattern = f"%{search.strip().lower()}%" if search and search.strip() else None
    return db.query(
        conn,
        """
        SELECT o.id, o.display_name, o.slug, o.is_personal, o.created_at,
               (SELECT array_agg(u.email ORDER BY u.email) FROM org_members m JOIN users u ON u.id = m.user_id
                 WHERE m.org_id = o.id AND m.role = 'owner') AS owners,
               (SELECT count(*) FROM org_members m WHERE m.org_id = o.id) AS members,
               (SELECT count(*) FROM projects pr WHERE pr.org_id = o.id AND pr.deleted_at IS NULL
                  AND pr.status <> ALL(%s)) AS projects,
               (SELECT count(*) FROM subscriptions s WHERE s.org_id = o.id
                  AND s.state IN ('active', 'trialing', 'past_due')) AS paying_subscriptions
          FROM organizations o
         WHERE o.deleted_at IS NULL
           AND (%s::text IS NULL
                OR lower(o.display_name) LIKE %s OR lower(o.slug) LIKE %s
                OR EXISTS (SELECT 1 FROM org_members m JOIN users u ON u.id = m.user_id
                            WHERE m.org_id = o.id AND lower(u.email) LIKE %s))
         ORDER BY o.created_at DESC
         LIMIT %s
        """,
        (list(ENDED), pattern, pattern, pattern, pattern, limit),
    )


def customer(conn: psycopg.Connection, org_id: uuid.UUID) -> dict | None:
    """One organization's platform records: members, projects, subscriptions, billing events."""
    org = db.one(
        conn,
        "SELECT id, display_name, slug, is_personal, created_at FROM organizations "
        " WHERE id = %s AND deleted_at IS NULL",
        (org_id,),
    )
    if org is None:
        return None
    members = db.query(
        conn,
        """
        SELECT u.email, u.display_name, u.status, u.created_at, u.last_login_at, u.email_verified_at,
               m.role, m.created_at AS joined_at
          FROM org_members m JOIN users u ON u.id = m.user_id
         WHERE m.org_id = %s
         ORDER BY m.role = 'owner' DESC, u.email
        """,
        (org_id,),
    )
    projects = db.query(
        conn,
        """
        SELECT pr.project_ref, pr.display_name, pr.status, pl.code AS plan_code, pr.created_at,
               pr.database_bytes, pr.object_bytes, pr.storage_state, pr.object_storage_state
          FROM projects pr JOIN plans pl ON pl.id = pr.plan_id
         WHERE pr.org_id = %s AND pr.deleted_at IS NULL
         ORDER BY pr.created_at DESC
        """,
        (org_id,),
    )
    subs = db.query(
        conn,
        """
        SELECT s.plan_code, s.state, s.state_since, s.period_end, s.provider_subscription_id,
               s.provider_customer_id, pr.project_ref
          FROM subscriptions s JOIN projects pr ON pr.id = s.project_id
         WHERE s.org_id = %s
         ORDER BY s.created_at DESC
        """,
        (org_id,),
    )
    events = db.query(
        conn,
        """
        SELECT e.event_type, e.livemode, e.received_at, e.outcome, e.note, pr.project_ref
          FROM billing_events e JOIN projects pr ON pr.id = e.project_id
         WHERE pr.org_id = %s
         ORDER BY e.received_at DESC
         LIMIT 50
        """,
        (org_id,),
    )
    return {**org, "members": members, "projects": projects, "subscriptions": subs, "billing_events": events}


def record_view(conn: psycopg.Connection, *, staff_id: uuid.UUID, org_id: uuid.UUID, page: str) -> None:
    """`staff.view`: a staff member looked at one organization's records (ADR-082 decision 6)."""
    db.execute(
        conn,
        "INSERT INTO audit_events (actor_type, actor_id, org_id, event_type, detail_json) "
        "VALUES ('staff', %s, %s, 'staff.view', %s)",
        (f"staff:{staff_id}", org_id, Jsonb({"page": page})),
    )


# -- usage (slice 3b) ----------------------------------------------------------------

# The same proportion `storage` and `object_storage` warn at. Restated rather than
# imported: those modules reach the object store client and the node's database.
WARNING_FRACTION = 0.8


def _ratio(used: int | None, limit: int) -> float | None:
    """`abuse_report._ratio`: None when never measured; a used zero ceiling is infinite pressure."""
    if used is None:
        return None
    if limit <= 0:
        return float("inf") if used > 0 else 0.0
    return used / limit


def _state(used: int | None, limit: int) -> str | None:
    """`object_storage.classify`'s rule, for counters that have no stored state."""
    if used is None:
        return None
    if limit <= 0 or used >= limit:
        return "exceeded"
    return "warning" if used >= limit * WARNING_FRACTION else "ok"


@dataclass
class ProjectUsage:
    project_ref: str
    display_name: str
    status: str
    plan_code: str
    org_id: uuid.UUID
    org_name: str
    account_age_days: int
    database: dict = field(default_factory=dict)
    objects: dict = field(default_factory=dict)
    egress: dict = field(default_factory=dict)
    email_day: dict = field(default_factory=dict)

    @property
    def meters(self) -> dict[str, dict]:
        return {"database": self.database, "objects": self.objects, "egress": self.egress,
                "email_day": self.email_day}

    @property
    def peak(self) -> float:
        measured = [m["ratio"] for m in self.meters.values() if m["ratio"] is not None]
        return max(measured) if measured else 0.0

    @property
    def peak_meter(self) -> str | None:
        measured = {name: m["ratio"] for name, m in self.meters.items() if m["ratio"]}
        return max(measured, key=measured.get) if measured else None


def project_usage(conn: psycopg.Connection, *, plan_code: str | None = None, now: datetime | None = None
                  ) -> list[ProjectUsage]:
    """Every live project against its plan's ceilings, highest pressure first.

    What `abuse_report.report` measures -- stored database and object bytes from the
    maintenance pass, egress this UTC month (ADR-056), email sent in the last day -- for
    every plan, or one. Ties break toward the youngest organization, since a farmed
    account is new by construction. CPU and live connections are node-side and absent.
    """
    now = now or datetime.now(UTC)
    month = date(now.year, now.month, 1)
    rows = db.query(
        conn,
        """
        SELECT pr.project_ref, pr.display_name, pr.status, pr.org_id, o.display_name AS org_name,
               o.created_at AS org_created_at, pl.code AS plan_code, pl.config_json,
               pr.database_bytes, pr.database_measured_at, pr.storage_state,
               pr.object_bytes, pr.object_measured_at, pr.object_storage_state,
               coalesce(e.bytes, 0) AS egress_bytes,
               (SELECT count(*) FROM email_events ev
                 WHERE ev.project_id = pr.id AND ev.event_type = 'sent'
                   AND ev.occurred_at > %s - interval '1 day') AS emails_day
          FROM projects pr
          JOIN plans pl ON pl.id = pr.plan_id
          JOIN organizations o ON o.id = pr.org_id
          LEFT JOIN project_egress e ON e.project_id = pr.id AND e.period_start = %s
         WHERE pr.deleted_at IS NULL AND pr.status <> ALL(%s)
           AND (%s::text IS NULL OR pl.code = %s)
        """,
        (now, month, list(ENDED), plan_code, plan_code),
    )
    out = []
    for row in rows:
        allowed = entitlements.resolve(row["plan_code"], row["config_json"])
        out.append(ProjectUsage(
            project_ref=row["project_ref"],
            display_name=row["display_name"],
            status=row["status"],
            plan_code=row["plan_code"],
            org_id=row["org_id"],
            org_name=row["org_name"],
            account_age_days=max(0, (now - row["org_created_at"]).days),
            database={"used": row["database_bytes"], "limit": allowed.database_storage_bytes,
                      "ratio": _ratio(row["database_bytes"], allowed.database_storage_bytes),
                      "state": row["storage_state"], "measured_at": row["database_measured_at"]},
            objects={"used": row["object_bytes"], "limit": allowed.object_storage_bytes,
                     "ratio": _ratio(row["object_bytes"], allowed.object_storage_bytes),
                     "state": row["object_storage_state"], "measured_at": row["object_measured_at"]},
            egress={"used": row["egress_bytes"], "limit": allowed.egress_bytes_per_month,
                    "ratio": _ratio(row["egress_bytes"], allowed.egress_bytes_per_month),
                    "state": _state(row["egress_bytes"], allowed.egress_bytes_per_month), "measured_at": None},
            email_day={"used": row["emails_day"], "limit": allowed.emails_per_day,
                       "ratio": _ratio(row["emails_day"], allowed.emails_per_day),
                       "state": _state(row["emails_day"], allowed.emails_per_day), "measured_at": None},
        ))
    out.sort(key=lambda p: (-p.peak, p.account_age_days, p.project_ref))
    return out
