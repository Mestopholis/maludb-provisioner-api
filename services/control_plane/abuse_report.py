"""Which projects are pressing on their ceilings, newest accounts first among equals.

Launch slice 3. Signup is public (decided 2026-08-16), so the free tier is where
mining, spam and farmed accounts land, on nodes shared with paying tenants. The
controls that bound what one project can do already exist -- hard ceilings
(ADR-050), a challenge, the per-organization project cap. What did not exist is
anything for the person reviewing abuse to look at. This is that, and only that:
it **reports and never acts**. Suspending a project is an explicit state
transition with somebody's name on it, as moving one is (ADR-066).

Built only from what the control plane already records -- database and object
bytes from the maintenance pass, egress counted this month (ADR-056), email sent
in the last day -- so it needs no node credential and runs anywhere `cp-manage`
does. **What it cannot see:** CPU and live connections, which are node-side
facts; the gateway already enforces the connection and request ceilings, and a
node-side view is a later addition rather than a guess made here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import psycopg

from services.control_plane import db, entitlements, models


@dataclass
class ProjectPressure:
    project_ref: str
    plan_code: str
    org_id: uuid.UUID
    account_age_days: int
    # name -> used / limit, 0.0 upward; None when never measured.
    ratios: dict[str, float | None] = field(default_factory=dict)

    @property
    def peak(self) -> float:
        measured = [r for r in self.ratios.values() if r is not None]
        return max(measured) if measured else 0.0

    @property
    def peak_name(self) -> str | None:
        measured = {k: v for k, v in self.ratios.items() if v}
        return max(measured, key=measured.get) if measured else None


def _ratio(used: int | None, limit: int) -> float | None:
    if used is None:
        return None
    if limit <= 0:
        # A ceiling of zero that something is using is the loudest signal there is.
        return float("inf") if used > 0 else 0.0
    return used / limit


def report(conn: psycopg.Connection, *, plan_code: str | None = None, now: datetime | None = None
           ) -> list[ProjectPressure]:
    """Live projects on `plan_code` (the default plan when None), highest pressure first.

    Ties -- most projects have used nearly nothing -- are broken by the youngest
    organization, since a farmed account is new by construction.
    """
    now = now or datetime.now(UTC)
    if plan_code is None:
        default = models.default_plan(conn)
        if default is None:
            return []
        plan_code = default.code
    month = date(now.year, now.month, 1)
    rows = db.query(
        conn,
        """
        SELECT pr.id, pr.project_ref, pr.org_id, p.code, p.config_json,
               pr.database_bytes, pr.object_bytes, o.created_at AS org_created_at,
               coalesce(e.bytes, 0) AS egress_bytes,
               (SELECT count(*) FROM email_events ev
                 WHERE ev.project_id = pr.id AND ev.event_type = 'sent'
                   AND ev.occurred_at > %s - interval '1 day') AS emails_day
          FROM projects pr
          JOIN plans p ON p.id = pr.plan_id
          JOIN organizations o ON o.id = pr.org_id
          LEFT JOIN project_egress e ON e.project_id = pr.id AND e.period_start = %s
         WHERE pr.deleted_at IS NULL AND p.code = %s
        """,
        (now, month, plan_code),
    )
    out = []
    for row in rows:
        allowed = entitlements.resolve(row["code"], row["config_json"])
        out.append(ProjectPressure(
            project_ref=row["project_ref"],
            plan_code=row["code"],
            org_id=row["org_id"],
            account_age_days=max(0, (now - row["org_created_at"]).days),
            ratios={
                "database": _ratio(row["database_bytes"], allowed.database_storage_bytes),
                "objects": _ratio(row["object_bytes"], allowed.object_storage_bytes),
                "egress": _ratio(row["egress_bytes"], allowed.egress_bytes_per_month),
                "email/day": _ratio(row["emails_day"], allowed.emails_per_day),
            },
        ))
    out.sort(key=lambda p: (-p.peak, p.account_age_days, p.project_ref))
    return out


__all__ = ["ProjectPressure", "report"]
