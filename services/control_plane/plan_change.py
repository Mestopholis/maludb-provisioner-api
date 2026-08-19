"""Moving a project between plans (Phase 09 slice 1).

Slice 0 made a plan true on its node. This is the operation that changes which
plan that is: recorded, resumable, mutually exclusive, and asserting the one
thing ADR-006 promises -- that the database does not move.

**What this is not.** It takes no money and exposes no route. Nothing here can
be reached by a customer; `cp-manage project set-plan` is the only caller until
slice 4 gives a billing provider one. That ordering is deliberate: the only
consumer a plan-change route will ever have is a webhook handler, and building
the route now means designing its authentication twice.

Three decisions worth reading before changing this.

**The node is written before the plan row.** A failed apply then leaves the
project entirely on its old plan rather than half on a new one. The alternative
fails in the direction that matters: a downgrade that updated the row first and
then failed would leave `direct_database_access` live on a node while the plan
says the project no longer has it, indefinitely and silently.

**The project's `status` is not touched.** Migration 0018 records why at
length: three separate gates serve only `("PROVISIONED", "ACTIVE")`, so parking
a project in `UPGRADING` would take its data API, its SQL console and its
workers offline for the duration of a purchase.

**Identity is asserted, not assumed.** Acceptance criterion 1 says an upgrade
retains the database. The correct implementation is to write no code that moves
one -- but "we did not write that code" is a claim about the present, so the
identity is read before and after and compared. If it ever differs, something
else moved it and this operation is the one that will say so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from services.control_plane import db, entitlements, models, plan_apply, provisioning

CHANGED = "project.plan.changed"


class PlanChangeError(RuntimeError):
    """A refusal that is safe to show an operator."""


@dataclass(frozen=True)
class Identity:
    """The things a plan change must leave exactly as it found them.

    API keys are included because a customer's application authenticates with
    them: a change that rotated a key would be an upgrade that took the
    customer's site down, which is the same failure as moving the database and
    less obvious.
    """

    project_ref: str
    database_name: str | None
    node_id: int | None
    api_key_ids: tuple[uuid.UUID, ...]


@dataclass
class Change:
    project_ref: str
    from_plan: str
    to_plan: str
    change_id: uuid.UUID | None = None
    corrected: list[plan_apply.Divergence] | None = None
    closed_request: uuid.UUID | None = None
    unchanged: bool = False


def identity(conn: psycopg.Connection, project_id: uuid.UUID) -> Identity:
    row = db.one(
        conn,
        "SELECT project_ref, database_name, node_id FROM projects WHERE id = %s",
        (project_id,),
    )
    if row is None:
        raise PlanChangeError("project does not exist")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM api_keys WHERE project_id = %s AND revoked_at IS NULL ORDER BY id",
            (project_id,),
        )
        keys = tuple(r[0] for r in cur.fetchall())
    return Identity(
        project_ref=row["project_ref"], database_name=row["database_name"],
        node_id=row["node_id"], api_key_ids=keys,
    )


def change_plan(
    conn: psycopg.Connection,
    admin_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    to_plan_code: str,
    requested_by: uuid.UUID | None = None,
) -> Change:
    """Move a project to `to_plan_code`, and make it true on the node.

    Raises `PlanChangeError` for anything an operator can act on: an unknown or
    retired plan, a project being deleted, a change already running. Everything
    else is left to propagate, because a node that cannot be written to is not
    a condition this function can summarise honestly.
    """
    project = db.one(
        conn,
        "SELECT pr.id, pr.project_ref, pr.status, pr.deleted_at, pl.code AS plan_code "
        "  FROM projects pr JOIN plans pl ON pl.id = pr.plan_id "
        " WHERE pr.id = %s",
        (project_id,),
    )
    if project is None:
        raise PlanChangeError("project does not exist")
    if project["deleted_at"] is not None or project["status"] in ("DELETING", "DELETED"):
        raise PlanChangeError("project is being deleted")

    target = models.plan_by_code(conn, to_plan_code)
    if target is None:
        # `plan_by_code` filters on is_active, so a retired plan and a
        # misspelled one answer the same. Both are the operator's to fix and
        # neither should half-run.
        raise PlanChangeError(f"no active plan with code {to_plan_code!r}")

    if project["plan_code"] == target.code:
        return Change(
            project_ref=project["project_ref"], from_plan=project["plan_code"],
            to_plan=target.code, unchanged=True,
        )

    before = identity(conn, project_id)
    change_id = uuid.uuid4()
    try:
        db.execute(
            conn,
            "INSERT INTO plan_changes (id, project_id, from_plan_code, to_plan_code, requested_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (change_id, project_id, project["plan_code"], target.code, requested_by),
        )
        conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        conn.rollback()
        raise PlanChangeError(
            "a plan change is already running for this project; "
            "finish or fail it before starting another"
        ) from exc

    try:
        names = provisioning.TenantNames.for_ref(project["project_ref"])
        allowed = entitlements.resolve(target.code, target.config)
        # The node first. A failure here leaves the project entirely on its old
        # plan rather than half on the new one.
        applied = plan_apply.apply(admin_conn, names, allowed)
        if applied.missing_roles:
            raise PlanChangeError(
                f"{project['project_ref']}: role(s) absent on the node "
                f"({', '.join(applied.missing_roles)}); the project needs provisioning "
                "attention before its plan can move"
            )

        db.execute(
            conn,
            "UPDATE projects SET plan_id = %s, upgraded_at = now() WHERE id = %s",
            (target.id, project_id),
        )

        after = identity(conn, project_id)
        if after != before:
            # Never expected, and therefore worth raising rather than logging.
            # ADR-006's promise is that this cannot happen; an assertion is what
            # makes that a fact about the running system rather than about the
            # code somebody read.
            raise PlanChangeError(
                "the project's identity changed during a plan change; "
                f"before={before} after={after}"
            )

        closed = _close_matching_request(conn, project_id, target.code)
        db.execute(
            conn,
            "INSERT INTO audit_events (project_id, actor_type, actor_user_id, event_type, "
            "                          detail_json) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                project_id,
                "user" if requested_by else "system",
                requested_by,
                CHANGED,
                psycopg.types.json.Jsonb(
                    {
                        "from_plan": project["plan_code"],
                        "to_plan": target.code,
                        "settings_corrected": len(applied.corrected),
                        "database_retained": True,
                    }
                ),
            ),
        )
        db.execute(
            conn,
            "UPDATE plan_changes SET state = 'APPLIED', completed_at = now() WHERE id = %s",
            (change_id,),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        _record_failure(conn, change_id, exc)
        raise

    return Change(
        project_ref=project["project_ref"], from_plan=project["plan_code"],
        to_plan=target.code, change_id=change_id, corrected=applied.corrected,
        closed_request=closed,
    )


def _failure_text(exc: BaseException) -> str:
    """What is safe to store about a failure, which is not `str(exc)`.

    The node work here runs on a connection opened from a decrypted superuser
    DSN, and **a psycopg connection error can echo the DSN it failed on** --
    the reason `sql_console.ConsoleError` refuses driver text for connection
    failures, and the finding the slice-0 review turned up in `plans drift`.
    This row is read back by `cp-manage project plan-history`, so storing the
    driver's own message would print a node credential to whoever runs it.

    A `PlanChangeError` is this module's own text and is stored in full. Any
    other exception contributes its type, plus the sqlstate when PostgreSQL
    gave one -- which is the part an operator can act on anyway.
    """
    if isinstance(exc, PlanChangeError):
        return f"PlanChangeError: {exc}"[:2000]
    if isinstance(exc, psycopg.Error):
        return f"{type(exc).__name__}: sqlstate {exc.sqlstate or 'none'}"
    return type(exc).__name__


def _record_failure(conn: psycopg.Connection, change_id: uuid.UUID, exc: BaseException) -> None:
    """Mark the row FAILED, and never let that write hide the original error."""
    try:
        db.execute(
            conn,
            "UPDATE plan_changes SET state = 'FAILED', completed_at = now(), error = %s "
            " WHERE id = %s",
            (_failure_text(exc), change_id),
        )
        conn.commit()
    except psycopg.Error:
        conn.rollback()


def _close_matching_request(
    conn: psycopg.Connection, project_id: uuid.UUID, plan_code: str
) -> uuid.UUID | None:
    """Close the open upgrade request this change answers, if it answers one.

    Only when the plan matches what was asked for. A project moved to a
    different plan than the one requested has not had its question answered,
    and closing the row would drop it out of the queue an operator works.
    """
    row = db.one(
        conn,
        "UPDATE upgrade_requests SET state = 'CLOSED', updated_at = now() "
        " WHERE project_id = %s AND state <> 'CLOSED' AND requested_plan_code = %s "
        "RETURNING id",
        (project_id, plan_code),
    )
    return row["id"] if row else None


def running(conn: psycopg.Connection, project_id: uuid.UUID) -> dict | None:
    return db.one(
        conn,
        "SELECT id, from_plan_code, to_plan_code, started_at FROM plan_changes "
        " WHERE project_id = %s AND state = 'RUNNING'",
        (project_id,),
    )


def history(conn: psycopg.Connection, project_id: uuid.UUID, *, limit: int = 20) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT id, from_plan_code, to_plan_code, state, started_at, completed_at, error "
            "  FROM plan_changes WHERE project_id = %s ORDER BY started_at DESC LIMIT %s",
            (project_id, limit),
        )
        return cur.fetchall()
