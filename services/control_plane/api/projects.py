"""Project endpoints: ask for one, list them, read one's progress.

Phase 07 slice 1 added creation. Until it did, **nothing in the platform
created a project row** -- `cp-manage` could retry, clean up and inspect one,
and rows otherwise existed only in tests. So this is new code on the
provisioning path reachable by anyone who can sign up, which is why the request
is idempotent under a key and why the node work is not done here at all.

**Creating is asking.** ADR-038 keeps node superuser credentials out of the
internet-facing process, so this route allocates a reference, reserves a place
on a node and records the request; a provisioner worker does the rest and the
customer polls. The alternative -- provisioning inside the request -- would put
a credential that can create databases and roles on every node into the process
bound to the public interface, and would fail a customer's request outright for
a node that was briefly unreachable rather than retrying it.

Every route requires an authenticated principal and membership of the owning
organization. Non-membership answers 404 rather than 403, so the API cannot be
used to confirm which organizations or project refs exist.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from services.control_plane import db, entitlements, models, nodes
from services.control_plane.api.auth_dep import CurrentPrincipal, require_manager, require_member

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["projects"])


class ProjectOut(BaseModel):
    project_ref: str
    display_name: str
    status: str
    created_at: datetime
    # Where a client points. Derived from the ref and the gateway domain rather
    # than stored, because it is not a fact about the project: it is a fact
    # about how this deployment routes, and storing it would let a project keep
    # a URL the platform no longer serves. The hostname *is* the routing key
    # (ADR-008), which is also why it can be derived at all.
    api_url: str
    # Deliberately omits node_id and database_name. docs/API-GATEWAY.md
    # forbids exposing internal node and database names to clients -- and this
    # is also the phase's first acceptance criterion: no privileged database
    # credential reaches a customer, free or paid, through any response here.


class ProjectCreateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    # The plan by code rather than by id: ids are ours, codes are what a
    # customer sees on a pricing page. Absent means the default, so a dashboard
    # that has not implemented plan selection still works.
    plan_code: str | None = None


def _to_out(project: models.Project, *, gateway_domain: str) -> ProjectOut:
    return ProjectOut(
        project_ref=project.project_ref,
        display_name=project.display_name,
        status=project.status,
        created_at=project.created_at,
        api_url=f"https://{project.project_ref}.{gateway_domain}",
    )


@router.post(
    "/organizations/{org_id}/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a project; it is provisioned in the background",
)
def create_project(
    org_id: uuid.UUID,
    body: ProjectCreateIn,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectOut:
    """Allocate a reference, reserve a place on a node, and record the request.

    202 rather than 201, because the thing named in the response does not exist
    on a node yet. A dashboard polls `GET /v1/projects/{ref}` and watches
    `status`; there is nothing a customer can do with the project until it
    reaches ACTIVE, and pretending otherwise would be the API asserting
    something it has not done.

    Creating a project is a privilege of the organization's owners and admins
    rather than of every member: it commits the organization to a database, a
    set of roles and a slot on a node, and a member who cannot change a role
    should not be able to commit that either.
    """
    require_manager(principal, org_id)
    domain = request.app.state.config.gateway_domain
    key = (idempotency_key or "").strip() or None
    if key and len(key) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is too long"
        )

    with db.connection() as conn:
        # A replay returns what the first call created rather than making a
        # second project. Checked before anything is allocated, so a retried
        # request costs a lookup rather than a database on a node.
        if key:
            existing = models.project_by_idempotency_key(conn, org_id=org_id, key=key)
            if existing is not None:
                if existing.display_name != body.display_name:
                    # The same key naming a different project is a client bug,
                    # and answering with the first project would hide it.
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency-Key was already used for a different request",
                    )
                return _to_out(existing, gateway_domain=domain)

        if body.plan_code:
            # **Only the default plan may be chosen self-service.** `plan_code`
            # arrived in slice 1 as a convenience and was, until this was found
            # by the slice 5 security review, an entitlement the caller granted
            # themselves: `plan_by_code` accepts any active row, nothing checks
            # whether the organization is entitled to it, and `GET /v1/plans`
            # hands every authenticated user the list of codes.
            #
            # Naming a paid plan therefore granted paid entitlements to an
            # unbilled project -- 100 projects instead of 2, production resource
            # settings, and `direct_database_access: True`, which is the
            # "free projects are API-only" invariant in AGENTS.md and a named
            # item in its own review rules.
            #
            # Upgrades go through the queue slice 3 built, which is
            # operator-mediated on purpose. Phase 09 owns entitlements; until it
            # does, the safe reading of "which plan may this organization have"
            # is "the default one".
            #
            # The refusal is the same 404 an unknown code gets, so it is not a
            # probe for which plans exist and which are merely forbidden.
            default = models.default_plan(conn)
            if default is None or body.plan_code != default.code:
                log.info(
                    "refused a project on plan %r in org %s: not self-service",
                    body.plan_code, org_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="unknown plan"
                )
            plan = default
        else:
            plan = models.default_plan(conn)
            if plan is None:
                # The caller named nothing and the catalogue has no default, so
                # this is the platform's problem rather than theirs. Nothing
                # seeds `plans` -- entitlements resolve their own defaults by
                # code, so the table is a catalogue an operator populates -- and
                # a 404 here would send a customer looking for a plan name that
                # was never the issue.
                log.error("no default plan is configured; the plans catalogue is empty")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="no default plan is available; the platform is not accepting new projects",
                )

        # The account-farming cap (Phase 07 slice 5). A free tier open to the
        # public is farmed by creating projects, and each one is a database,
        # four roles and a slot on a node whether or not anybody connects to it.
        # Counted per organization because that is where projects live -- per
        # user would be defeated by an invitation -- and counted here rather
        # than in placement, so the refusal names the plan rather than looking
        # like the fleet is full.
        # **Not serialized, and that is a known gap rather than an oversight.**
        # Two concurrent requests can both read the same count, both find room
        # and both insert, so the cap is a cap against ordinary use and a soft
        # limit against somebody deliberately racing it. Closing it needs a lock
        # on the organization row held to the commit below; a first attempt at
        # that deadlocked against the test suite's own TRUNCATE and was removed
        # rather than shipped half-understood -- a lock whose failure mode is
        # unclear is worse than a documented soft limit.
        #
        # The over-creation it permits is bounded by how many requests fit in
        # the race, and every project created still costs the attacker an
        # account and a solved challenge.
        allowed = entitlements.resolve(plan.code, plan.config)
        existing = models.count_projects_for_org(conn, org_id)
        if existing >= allowed.max_projects:
            # Without the number: naming the ceiling tells a caller which plan
            # would raise it, which is a probe rather than an explanation.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this organization has reached its plan's project limit",
            )

        try:
            project_id = models.create_project(
                conn,
                org_id=org_id,
                display_name=body.display_name,
                plan_id=plan.id,
                requested_by=principal.user.id,
                idempotency_key=key,
            )
            # Placement in the same transaction as the row. A project that
            # exists with nowhere to go is a row the provisioner would pick up
            # forever, so if there is no capacity the whole request rolls back
            # and the customer is told rather than left with a project that
            # never becomes anything.
            # ADR-065. The pool comes from the plan's entitlement rather than
            # from this call's parameter default. For eleven phases it took that
            # default, so every project on the platform landed in `shared`
            # regardless of what it paid for -- the mechanism was plumbed and
            # the policy was missing. `allowed` is the same resolution the
            # project-limit check above already did.
            nodes.reserve_placement(
                conn, project_id=project_id, node_pool=allowed.node_pool
            )
            conn.commit()
        except nodes.PlacementError as exc:
            conn.rollback()
            log.warning("placement refused for a new project in org %s: %s", org_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no capacity for a new project right now; try again shortly",
            ) from exc

        created = models.get_project(conn, project_id)

    log.info(
        "project %s requested in org %s", created.project_ref, org_id,
        extra={"extra_fields": {"project_ref": created.project_ref}},
    )
    return _to_out(created, gateway_domain=domain)


@router.get(
    "/organizations/{org_id}/projects",
    response_model=list[ProjectOut],
    summary="List an organization's projects",
)
def list_projects(
    org_id: uuid.UUID, request: Request, principal: CurrentPrincipal
) -> list[ProjectOut]:
    require_member(principal, org_id)
    domain = request.app.state.config.gateway_domain
    with db.connection() as conn:
        return [
            _to_out(p, gateway_domain=domain)
            for p in models.list_projects_for_org(conn, org_id)
        ]


@router.get("/projects/{project_ref}", response_model=ProjectOut, summary="Get a project by reference")
def get_project(
    project_ref: str, request: Request, principal: CurrentPrincipal
) -> ProjectOut:
    domain = request.app.state.config.gateway_domain
    # Treat the ref as untrusted input; reject malformed values before lookup.
    if not models.is_valid_project_ref(project_ref):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    with db.connection() as conn:
        project = models.get_project_by_ref(conn, project_ref)

    # Not-a-member and does-not-exist are the same answer. Project refs appear
    # in public hostnames (ADR-008), so a distinguishable response would let
    # anyone confirm which refs are real.
    if project is None or not principal.is_member_of(project.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return _to_out(project, gateway_domain=domain)
