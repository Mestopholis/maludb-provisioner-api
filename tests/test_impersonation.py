"""Running a statement as the customer's own users would meet it (slice 3).

The failure this exists for is not an error. Phase 00 finding 7 and ADR-018
established that a Supabase-shaped tenant answers a blocked read with an *empty
result set* rather than `42501`, because `anon` holds the grant and RLS is what
filters -- so a customer whose policy is wrong sees no rows and no message, and
reading the policy again does not tell them which rows it would have hidden.
Impersonation is how they ask the database instead.

The security question is narrower than it looks, and slice 1 answered half of it
already: `RESET ROLE` is reachable from submitted text. So impersonation cannot
be a nested `SET ROLE` from the admin role -- the reset would walk straight back
out of it. It is a different *login role* instead: `mldb_<ref>_authenticator`,
which is a member of the three shared names and of nothing else. That is
negative test P, and it is what makes "cannot escape back to the admin role"
true by construction rather than by hoping the submitted text does not try.

What impersonation is *not* is a sandbox. The customer can reach the admin role
by sending the next request without a role, which is their own database and
their own right.
"""

from __future__ import annotations

import uuid

import pytest

from services.control_plane import db, provisioning, sql_console
from services.control_plane.api import tenant_access
from tests.conftest import TEST_CREDENTIAL, node_host_and_port, requires_db
from tests.test_provisioning import ADMIN_DSN

pytestmark = requires_db
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

USER_A = str(uuid.UUID(int=0xA))
USER_B = str(uuid.UUID(int=0xB))


# -- the pure parts, which need no node ------------------------------------


def test_the_impersonatable_roles_are_exactly_the_shared_three():
    """Not a list to keep in step with another list. The authenticator is a
    member of precisely these, so asking for anything else would be asking for
    a role the impersonating connection could not enter anyway."""
    assert tenant_access.IMPERSONATABLE_ROLES == provisioning.SHARED_ROLES
    assert "postgres" not in tenant_access.IMPERSONATABLE_ROLES
    assert not any(name.startswith("mldb_") for name in tenant_access.IMPERSONATABLE_ROLES)


# -- the request contract --------------------------------------------------


def _account(client) -> str:
    client.post("/v1/auth/signup", json={"email": "imp@example.com", "password": TEST_CREDENTIAL})
    return client.post(
        "/v1/auth/signin", json={"email": "imp@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]


def _post(client, token: str, **body):
    return client.post(
        "/v1/projects/anyref/sql", json={"statement": "SELECT 1", **body},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_role_outside_the_three_is_refused_before_any_project_is_looked_up(client, db_pool):  # noqa: ARG001
    """422 from the contract rather than 404 from the project lookup: the
    request is malformed, and answering 404 would make a typo look like a
    missing project."""
    token = _account(client)
    assert _post(client, token, role="postgres").status_code == 422
    assert _post(client, token, role="mldb_anyref_admin").status_code == 422


def test_claims_without_a_role_are_refused(client, db_pool):  # noqa: ARG001
    """`auth.uid()` read while running as the admin role answers whatever was
    set and means nothing, because no policy is being applied to that role."""
    answered = _post(client, token=_account(client), claims={"sub": USER_A})
    assert answered.status_code == 422
    assert "role" in answered.text


def test_an_oversized_claim_set_is_refused(client, db_pool):  # noqa: ARG001
    """A GUC value is memory in the backend, and this one is caller-supplied."""
    answered = _post(
        client, token=_account(client), role="authenticated", claims={"sub": "x" * 9_000}
    )
    assert answered.status_code == 422


# -- against a real tenant -------------------------------------------------


def _authenticator_dsn(project_id, names, key_ring) -> str:
    host, port = node_host_and_port()
    with db.connection() as conn:
        password = provisioning.load_credential(
            conn, project_id=project_id, credential_type="db_authenticator", key_ring=key_ring
        )
    return sql_console.executor_dsn(
        host=host, port=port, database=names.database,
        role=names.authenticator, password=password,
    )


def _seed(dsn: str, names) -> None:
    """A table with the policy every migrated Supabase project has."""
    sql_console.execute(
        dsn,
        f"""
        CREATE TABLE public.notes (id int PRIMARY KEY, owner uuid, body text);
        INSERT INTO public.notes VALUES (1, '{USER_A}', 'a''s note'),
                                        (2, '{USER_B}', 'b''s note');
        ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY;
        CREATE POLICY own_rows ON public.notes FOR SELECT TO authenticated
            USING (owner = auth.uid());
        """,  # noqa: S608 - USER_A and USER_B are constants in this file
        run_as=names.admin, row_limit=100, timeout_ms=10_000,
    )


def _as(dsn: str, role: str, statement: str, claims: dict | None = None):
    return sql_console.execute(
        dsn, statement, run_as=role, row_limit=100, timeout_ms=10_000, claims=claims
    )


@requires_node
def test_P_the_authenticator_cannot_reach_the_admin_role(tenant, admin_conn):
    """Negative test P, and the whole of slice 3's containment.

    Impersonation runs on this role's connection precisely because `RESET ROLE`
    lands here. If it were ever granted the admin role, every impersonated
    statement would be one reset away from DDL as the table owner.
    """
    _, names, _ = tenant("impp0001")
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m "
            "  JOIN pg_roles r ON r.oid = m.roleid "
            "  JOIN pg_roles e ON e.oid = m.member "
            " WHERE e.rolname = %s",
            (names.authenticator,),
        )
        memberships = {row["rolname"] for row in cur.fetchall()}
    assert memberships == set(provisioning.SHARED_ROLES)
    assert names.admin not in memberships


@requires_node
def test_a_reset_role_from_an_impersonated_session_cannot_climb_to_admin(tenant, key_ring):
    """The same shape as slice 1's `RESET ROLE` test, and the opposite outcome.

    There the reset was allowed to reach the admin role, because that was the
    customer's intended ceiling. Here it must not, because the caller asked to
    be someone smaller -- so the assertion is that the attempt fails rather than
    that the reset is blocked.
    """
    project_id, names, _ = tenant("impr0001")
    dsn = _authenticator_dsn(project_id, names, key_ring)

    results = _as(dsn, "anon", "RESET ROLE; SELECT current_user AS who;")
    assert results[-1].rows == [{"who": names.authenticator}]

    with pytest.raises(sql_console.ConsoleError, match="42501|permission denied"):
        _as(dsn, "anon", f'RESET ROLE; SET ROLE "{names.admin}";')


@requires_node
def test_a_policy_filters_to_the_impersonated_user(tenant, key_ring):
    """The feature. Two claim sets, two answers, one policy -- which is the
    question a customer cannot answer by reading the policy."""
    project_id, names, executor_dsn = tenant("impf0001")
    _seed(executor_dsn, names)
    dsn = _authenticator_dsn(project_id, names, key_ring)

    seen_by_a = _as(dsn, "authenticated", "SELECT id FROM public.notes", {"sub": USER_A})
    seen_by_b = _as(dsn, "authenticated", "SELECT id FROM public.notes", {"sub": USER_B})

    assert seen_by_a[-1].rows == [{"id": 1}]
    assert seen_by_b[-1].rows == [{"id": 2}]


@requires_node
def test_anon_sees_an_empty_set_rather_than_a_permission_error(tenant, key_ring):
    """Phase 00 finding 7 and ADR-018, demonstrable from a route for the first
    time. The grant exists so a denial looks like an empty result, which is what
    migrated applications expect -- and is exactly why a customer needs this to
    tell "no rows match" from "no rows are permitted"."""
    project_id, names, executor_dsn = tenant("impa0001")
    _seed(executor_dsn, names)
    dsn = _authenticator_dsn(project_id, names, key_ring)

    results = _as(dsn, "anon", "SELECT id FROM public.notes")
    assert results[-1].rows == []
    assert results[-1].columns == ["id"]


@requires_node
def test_service_role_bypasses_rls_as_supabase_does(tenant, key_ring):
    """`service_role` carries BYPASSRLS to match Supabase, and provisioning's
    comment records that this is safe only because it is NOLOGIN and reachable
    solely by `SET ROLE` from a tenant's own authenticator. This route is that
    path, so the property is asserted here rather than assumed."""
    project_id, names, executor_dsn = tenant("imps0001")
    _seed(executor_dsn, names)
    dsn = _authenticator_dsn(project_id, names, key_ring)

    results = _as(dsn, "service_role", "SELECT count(*) AS n FROM public.notes")
    assert results[-1].rows == [{"n": 2}]


@requires_node
def test_an_impersonated_session_cannot_run_ddl(tenant, key_ring):
    """The ceiling is lower than the console's, which is the point of asking for
    it. `anon` holds table grants, not schema ownership."""
    project_id, names, _ = tenant("impd0001")
    dsn = _authenticator_dsn(project_id, names, key_ring)

    with pytest.raises(sql_console.ConsoleError, match="42501|permission denied"):
        _as(dsn, "anon", "CREATE TABLE public.mine (id int)")


@requires_node
def test_the_role_claim_defaults_to_the_role_being_impersonated(tenant, key_ring):
    """Supabase's own tokens always carry it and `auth.role()` reads it, so a
    claim set without one would answer NULL for a session that is demonstrably
    `authenticated` -- a difference from production created by the console."""
    project_id, names, _ = tenant("impc0001")
    dsn = _authenticator_dsn(project_id, names, key_ring)

    from services.control_plane.api import sql as sql_route

    body = sql_route.StatementIn(statement="SELECT 1", role="authenticated", claims={"sub": USER_A})
    claims = sql_route._claims_for(body)
    assert claims == {"sub": USER_A, "role": "authenticated"}

    results = _as(dsn, "authenticated", "SELECT auth.role() AS r, auth.uid() AS u", claims)
    assert results[-1].rows == [{"r": "authenticated", "u": uuid.UUID(USER_A)}]

    # An explicit role claim is left alone: a customer testing what a mismatched
    # token does is asking a real question.
    mismatched = sql_route._claims_for(
        sql_route.StatementIn(statement="SELECT 1", role="authenticated", claims={"role": "anon"})
    )
    assert mismatched == {"role": "anon"}


# -- end to end, through the route ----------------------------------------


def _token(client, ref: str) -> str:
    return client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]


def _allow_several_statements(ref: str) -> None:
    """Raise this project's console allowance off the fallback tier's.

    Free resolves one statement per eight-second window, so a test that sends
    two requests to make a comparison gets a 429 for the second and an assertion
    failure that looks like a bug in the thing being compared. Overriding
    `plans.config_json` is how a deployment does this for real (`AGENTS.md`:
    never hard-code plan limits), so the test uses the same lever.
    """
    import psycopg

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = "
            "  (SELECT plan_id FROM projects WHERE project_ref = %s)",
            (psycopg.types.json.Jsonb({"limits": {"sql_console_concurrent": 5}}), ref),
        )
        conn.commit()


@requires_node
def test_the_route_runs_as_the_requested_role_and_says_which(client, tenant):
    """End to end: the credential swap, the `SET ROLE`, the claims, and the
    `requested_role` a dashboard needs to label the result with."""
    ref = "impe0001"
    _, names, executor_dsn = tenant(ref)
    _seed(executor_dsn, names)
    _allow_several_statements(ref)
    headers = {"Authorization": f"Bearer {_token(client, ref)}"}

    answered = client.post(
        f"/v1/projects/{ref}/sql",
        json={
            "statement": "SELECT id FROM public.notes",
            "role": "authenticated",
            "claims": {"sub": USER_A},
        },
        headers=headers,
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["requested_role"] == "authenticated"
    assert body["results"][-1]["rows"] == [{"id": 1}]

    # Without a role, the same statement is the admin role and sees everything:
    # impersonation is a lower ceiling for the request, not for the customer.
    plain = client.post(
        f"/v1/projects/{ref}/sql",
        json={"statement": "SELECT id FROM public.notes ORDER BY id"},
        headers=headers,
    )
    assert plain.json()["requested_role"] == names.admin
    assert plain.json()["results"][-1]["rows"] == [{"id": 1}, {"id": 2}]


@requires_node
def test_a_session_asked_for_anon_can_still_become_service_role(tenant, key_ring, admin_conn):
    """Written as the fact it is, because a control was once built on its
    opposite.

    `SET ROLE` is authorized against the **session user**, not the current role.
    The session user here is the authenticator, a member of all three shared
    names -- so a request that asks for `anon` reaches `service_role` in one
    statement, with no `RESET ROLE` and no grant of its own. Measured, then
    asserted, because the first version of this slice refused *the request* to
    impersonate `service_role` while a project was over quota and that check was
    bypassable by exactly this line.

    Nothing above the three is reachable: test P covers the admin role.
    """
    project_id, names, _ = tenant("impx0001")
    dsn = _authenticator_dsn(project_id, names, key_ring)

    results = _as(dsn, "anon", "SET ROLE service_role; SELECT current_user AS who;")
    assert results[-1].rows == [{"who": "service_role"}]


@requires_node
def test_a_restricted_project_cannot_write_however_it_asks(tenant, key_ring):
    """ADR-041, and the reason it is grants rather than a check on the request.

    Phase 05 exempted `service_role` from the storage revoke because it "is
    reachable only from the project's own backend", whose route is the gateway
    -- and the gateway refuses writes at quota. Impersonation is a second route
    the gateway never sees. The fix is that the revoke now covers `service_role`
    too, so it binds whichever of the three the session ends up in, including the
    one it climbed to itself.
    """
    project_id, names, executor_dsn = tenant("impq0001")
    _seed(executor_dsn, names)
    dsn = _authenticator_dsn(project_id, names, key_ring)

    import psycopg

    from services.control_plane import storage
    from tests.test_provisioning import _tenant_admin_dsn

    with psycopg.connect(_tenant_admin_dsn(names.database)) as tenant_conn:
        storage.restrict(tenant_conn)

    write = "INSERT INTO public.notes VALUES (3, NULL, 'x')"
    for asked_for, statement in (
        ("service_role", write),
        ("anon", write),
        # The bypass itself: ask for the restricted role, climb to the exempt
        # one, write. This is the line the first fix could not stop.
        ("anon", f"SET ROLE service_role; {write}"),
    ):
        with pytest.raises(sql_console.ConsoleError, match="42501|permission denied"):
            _as(dsn, asked_for, statement)

    # And shrinking still works, which is what keeps the restriction
    # recoverable rather than terminal.
    _as(dsn, "service_role", "DELETE FROM public.notes WHERE id = 2")
    assert _as(dsn, "service_role", "SELECT count(*) AS n FROM public.notes")[-1].rows == [{"n": 1}]


@requires_node
def test_the_trail_records_the_role_and_the_claim_keys_but_never_the_claims(client, tenant):
    """A claim set is where an end user's id and email live. The trail answers
    "who ran what as whom", which needs the role and the shape of the claims and
    not the identities of the customer's own users."""
    ref = "impg0001"
    project_id, _, _ = tenant(ref)
    headers = {"Authorization": f"Bearer {_token(client, ref)}"}

    assert client.post(
        f"/v1/projects/{ref}/sql",
        json={
            "statement": "SELECT 1",
            "role": "authenticated",
            "claims": {"sub": USER_A, "email": "someone@example.com"},
        },
        headers=headers,
    ).status_code == 200

    with db.connection() as conn:
        event = db.one(
            conn,
            "SELECT detail_json FROM audit_events WHERE project_id = %s "
            "  AND event_type = 'project.sql.executed'",
            (project_id,),
        )
    detail = event["detail_json"]
    assert detail["requested_role"] == "authenticated"
    assert detail["claim_keys"] == ["email", "role", "sub"]
    assert USER_A not in repr(detail)
    assert "someone@example.com" not in repr(detail)

    # And the customer-visible audit route passes both through, which it only
    # does for keys somebody put on the allowlist.
    listed = client.get(f"/v1/projects/{ref}/audit-events", headers=headers).json()
    executed = next(e for e in listed if e["event_type"] == "project.sql.executed")
    assert executed["detail"]["requested_role"] == "authenticated"
    assert executed["detail"]["claim_keys"] == ["email", "role", "sub"]
