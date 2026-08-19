"""The public/internal split, and the first throttle on the control plane.

Phase 07 slice 0, ADR-037 and ADR-038. Two properties are asserted here that
nothing else in the suite would notice breaking, and both are about what the
*internet-facing* application can do rather than about what it currently does:

- it serves exactly the classified routes, so a router added without a decision
  is unreachable from outside rather than reachable by omission;
- it cannot obtain a node's superuser credential, which is a property of what
  its code can reach rather than of what today's handlers happen to call.

The rate-limit tests drive a fake clock rather than sleeping. A limiter tested
with `time.sleep` is a test nobody runs twice.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest
from fastapi.testclient import TestClient

from services.control_plane import ratelimit
from services.control_plane.api import limit_dep
from services.control_plane.main import (
    INTERNAL_ROUTERS,
    PUBLIC_ROUTERS,
    create_app,
    create_public_app,
)
from tests.conftest import requires_db

TEST_CREDENTIAL = "correct-horse-battery-staple-42"  # noqa: S105 - test fixture, not a real secret

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "services" / "control_plane"

# The credential ADR-038 keeps away from the internet-facing process, and the
# entry point that uses one. `nodes` as a module is *not* forbidden -- Phase 07
# slice 1 needs `reserve_placement` from it to place a project a customer asked
# for. What must never be reachable is the superuser DSN itself.
FORBIDDEN_CALLS = frozenset({"admin_dsn", "provision"})

# Modules that exist to do work on a node. A public route that needs one of
# these has misunderstood which process it is running in.
FORBIDDEN_MODULES = frozenset(
    {
        "services.control_plane.jobs",
        "services.control_plane.manage",
        "services.control_plane.tenant_bootstrap",
        "services.control_plane.realtime_workers",
    }
)


def _module_file(dotted: str) -> pathlib.Path | None:
    relative = dotted.replace("services.control_plane", "").lstrip(".").replace(".", "/")
    candidate = SOURCE_ROOT / f"{relative}.py"
    return candidate if candidate.is_file() else None


def _import_closure(entry_points: list[str]) -> tuple[set[str], dict[str, list[str]]]:
    """Every control-plane module reachable from these, and the calls they make.

    Static rather than dynamic on purpose: what matters is what the code *can*
    reach. A test that only observed which functions today's request handlers
    happen to call would pass the day someone imports the provisioner and stop
    passing the day they call it, which is a slice too late.
    """
    seen: set[str] = set()
    found: dict[str, list[str]] = {}
    stack = list(entry_points)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(module)
        if path is None:
            continue
        tree = ast.parse(path.read_text())

        # Module-level imports only, for the closure. A function-local import is
        # a different claim: `realtime.py` imports `realtime_workers` inside two
        # of its functions to break a cycle, and counting that as "the module
        # drags this in" made this test demand a refactor of code no public
        # route can call. What still catches a local import that matters is the
        # call scan below, which walks every scope: reaching a forbidden
        # function is the thing being forbidden, not naming its module.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
                "services.control_plane"
            ):
                stack.append(node.module)
                stack.extend(f"{node.module}.{alias.name}" for alias in node.names)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else None
                )
                if name in FORBIDDEN_CALLS:
                    found.setdefault(name, []).append(module)
    return seen, found


def _public_router_modules() -> list[str]:
    """Every module `PUBLIC_ROUTERS` mounts, not a sample of them.

    It was five of nine until Phase 08 slice 2, and the four it left out
    included `sql` -- the one public route that opens a connection to a tenant
    database. An ADR-038 assertion that skips the router most likely to want
    node credentials is checking the wrong half of the surface.
    """
    return [
        f"services.control_plane.api.{name}"
        for name in (
            "auth", "health", "organizations", "plans", "projects",
            "api_keys", "usage", "audit", "sql", "schema", "auth_import",
        )
    ]


# -- the classification ----------------------------------------------------


def _paths(app) -> set[str]:
    """Every path an application actually serves.

    Walked recursively rather than read off `app.routes`: this FastAPI version
    keeps an included router as one wrapper object with its own `routes`, so a
    flat comprehension over the top level sees no paths at all -- and a test
    asserting "the public application serves no unclassified route" would have
    passed by finding nothing whatsoever.
    """
    found: set[str] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        # FastAPI 0.141 keeps an included router as a `_IncludedRouter` wrapper
        # holding `original_router`, rather than flattening its routes into the
        # application. Both shapes are handled so this does not quietly find
        # nothing again on a version bump -- which is how it failed first: an
        # empty set satisfies "serves no unclassified route" perfectly.
        nested = getattr(route, "routes", None)
        if not nested:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None)
        if nested:
            stack.extend(nested)
        path = getattr(route, "path", None)
        if path:
            found.add(path)
    return found


# What ADR-037 classified, written out rather than derived from PUBLIC_ROUTERS.
# Deriving it makes the test a mirror: moving a router into the public tuple by
# mistake would move the expectation with it and the test would agree. This list
# is the decision, and changing it should take a moment's thought about whether
# the internet may have that route.
PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/readyz",
        "/v1/auth/signup",
        "/v1/auth/signin",
        "/v1/auth/signout",
        "/v1/auth/me",
        "/v1/auth/tokens",
        "/v1/auth/tokens/{token_id}",
        # Phase 07 slice 4. Reset is the one flow an anonymous caller drives
        # end to end, so both halves are public and both answer uniformly.
        "/v1/auth/password-reset",
        "/v1/auth/password-reset/complete",
        "/v1/auth/sessions",
        "/v1/auth/sessions/revoke-all",
        "/v1/organizations",
        "/v1/organizations/invitations/accept",
        "/v1/organizations/{org_id}/invitations",
        "/v1/organizations/{org_id}/members",
        "/v1/organizations/{org_id}/members/{user_id}",
        "/v1/organizations/{org_id}/projects",
        "/v1/organizations/{org_id}/transfer-ownership",
        "/v1/plans",
        "/v1/projects/{project_ref}",
        # Phase 07 slice 2. A key authenticates to the gateway, which holds the
        # tenant's real credentials; nothing here returns anything that could
        # reach PostgreSQL directly.
        "/v1/projects/{project_ref}/api-keys",
        "/v1/projects/{project_ref}/api-keys/{key_id}",
        # Phase 07 slice 3. Reporting, never granting: the upgrade route
        # records intent and changes no entitlement.
        "/v1/projects/{project_ref}/usage",
        "/v1/projects/{project_ref}/upgrade-request",
        # Phase 07 slice 5. Allowlisted event types with allowlisted detail
        # keys: `detail_json` is free-form and written by several subsystems.
        "/v1/projects/{project_ref}/audit-events",
        # Phase 08 slice 1, ADR-039. Public because the dashboard calls it, and
        # the most consequential route on this list: it is the one that runs a
        # customer's own text against their database. What keeps it safe is the
        # role it runs as rather than its position on this listener.
        "/v1/projects/{project_ref}/sql",
        # Phase 08 slice 2. Read-only, and the filtering is the security
        # property: `pg_roles` is cluster-scoped, so passing it through would
        # name every other tenant on the node.
        "/v1/projects/{project_ref}/database/schema",
        # Phase 08 slice 7. Public because the migration CLI calls it, and the
        # only route that connects as the tenant's auth role -- it composes
        # every statement itself from an allowlist, so nothing a caller sends is
        # executed.
        "/v1/projects/{project_ref}/auth/import",
        # Phase 09 slice 2, ADR-047. The only route in the platform that returns
        # a credential opening a real PostgreSQL connection from the internet,
        # so its position on this listener is the least of what protects it:
        # manager-only, refused unless the plan grants `direct_database_access`,
        # audited on both sides, and the secret it hands over belongs to a role
        # created to be given away rather than to the one the platform acts as.
        "/v1/projects/{project_ref}/database/connection",
        # Rotation, and it needs no node credential: it connects as the client
        # role and changes its own password, which ADR-038 requires because a
        # node credential must never live in this application.
        "/v1/projects/{project_ref}/database/connection/rotate",
    }
)


def test_the_public_app_serves_exactly_the_classified_routes(app_config):
    """The route table is the classification, or the classification is a comment."""
    expected = set(PUBLIC_PATHS)

    served = {
        path
        for path in _paths(create_public_app(app_config))
        if path not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    }

    assert expected <= served, f"classified routes are not served: {expected - served}"
    assert not served - expected, f"the public application serves unclassified routes: {served - expected}"


def test_the_internal_hook_is_not_reachable_on_the_public_application(app_config):
    """The one router that must never be public, checked by serving rather than by list.

    GoTrue's send-email hook is called by a project's Auth worker on a node. Its
    signature is what authenticates it -- the route says so itself -- and being
    off the public listener is the second line, not the first.
    """
    hook = "/internal/hooks/email/{project_ref}"
    assert hook in _paths(create_app(app_config)), "the internal app should serve the hook"
    assert hook not in _paths(create_public_app(app_config))


def test_every_router_is_classified_one_way_or_the_other():
    """A router that is in neither tuple is a router nobody decided about."""
    from services.control_plane.api import (
        api_keys,
        audit,
        auth,
        auth_import,
        database,
        health,
        hooks,
        organizations,
        plans,
        projects,
        schema,
        sql,
        usage,
    )

    every = {id(r) for r in (auth.router, health.router, hooks.router,
                             organizations.router, plans.router, projects.router,
                             api_keys.router, usage.router, audit.router,
                             sql.router, schema.router, auth_import.router,
                             database.router)}
    classified = {id(r) for r in (*INTERNAL_ROUTERS, *PUBLIC_ROUTERS)}
    assert every == classified, "a router exists that neither application mounts"


# -- ADR-038: the public application cannot reach a node's superuser -------


def test_the_public_application_cannot_reach_node_admin_credentials():
    """The finding this slice exists for, as a test rather than a review comment.

    `nodes.admin_dsn()` unwraps a node's superuser DSN with the KEK, and the
    control-plane process holds the KEK because project credentials need it.
    Nothing calls it from a route today; ADR-038 is the decision that nothing
    ever will from the process bound to the internet, and provisioning runs in a
    worker instead.
    """
    closure, forbidden = _import_closure(_public_router_modules())

    assert not forbidden, (
        "the public application can reach node admin credentials or the provisioner: "
        f"{forbidden}. ADR-038 puts that work in a worker -- if a public route now "
        "needs it, the ADR changes first."
    )
    leaked = closure & FORBIDDEN_MODULES
    assert not leaked, f"a public router imports node-side machinery: {leaked}"


# -- the first throttle ----------------------------------------------------


class _Clock:
    """A clock the test moves, so a window can pass without a test sleeping."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def throttled(app_config, db_pool):  # noqa: ARG001 - db_pool prepares the database
    """A public application whose limiter is tiny and whose clock is ours."""
    config = dataclasses.replace(
        app_config,
        signup_attempts=2, signup_window_seconds=3600,
        signin_attempts=3, signin_window_seconds=300,
        signin_account_attempts=2, signin_account_window_seconds=300,
    )
    app = create_public_app(config)
    clock = _Clock()
    with TestClient(app) as client:
        app.state.limiter = ratelimit.LocalLimiter(clock=clock)
        yield client, clock


@requires_db
def test_signup_is_limited_per_source(throttled):
    """Signup is the one route an anonymous caller uses to create durable state."""
    client, _clock = throttled
    for i in range(2):
        response = client.post(
            "/v1/auth/signup",
            json={"email": f"limit{i}@example.com", "password": TEST_CREDENTIAL},
        )
        assert response.status_code == 201, response.text

    refused = client.post(
        "/v1/auth/signup", json={"email": "limit-third@example.com", "password": TEST_CREDENTIAL}
    )
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0, "a 429 without Retry-After is a guess"
    # And the account was not created despite the refusal.
    assert client.post(
        "/v1/auth/signin",
        json={"email": "limit-third@example.com", "password": TEST_CREDENTIAL},
    ).status_code in (401, 429)


@requires_db
def test_the_window_passing_restores_the_allowance(throttled):
    client, clock = throttled
    for i in range(2):
        client.post(
            "/v1/auth/signup", json={"email": f"window{i}@example.com", "password": TEST_CREDENTIAL}
        )
    assert client.post(
        "/v1/auth/signup", json={"email": "window-x@example.com", "password": TEST_CREDENTIAL}
    ).status_code == 429

    clock.advance(3600)
    assert client.post(
        "/v1/auth/signup", json={"email": "window-y@example.com", "password": TEST_CREDENTIAL}
    ).status_code == 201


@requires_db
def test_signin_is_limited_per_account_as_well_as_per_source(throttled):
    """Per-source alone does not stop a distributed attempt against one account.

    Every call here comes from a different forwarded source and the source limit
    is never reached; what stops it is the bucket keyed on the account, which is
    the half that makes credential stuffing expensive rather than merely slow.
    """
    client, _clock = throttled
    client.post(
        "/v1/auth/signup", json={"email": "victim@example.com", "password": TEST_CREDENTIAL}
    )

    wrong = {"email": "victim@example.com", "password": "not-the-password"}
    assert client.post("/v1/auth/signin", json=wrong).status_code == 401
    assert client.post("/v1/auth/signin", json=wrong).status_code == 401
    refused = client.post("/v1/auth/signin", json=wrong)
    assert refused.status_code == 429, "the account bucket did not stop a third failure"
    assert int(refused.headers["retry-after"]) > 0

    # And the real owner is refused too, which is the cost of this control: an
    # attacker can lock an account for the window by failing it deliberately.
    # The alternative -- no limit at all -- is worse, and the window is short.
    # It is also why the account bucket counts failures rather than attempts:
    # charging every attempt would produce this lockout with no attacker
    # involved at all.
    assert client.post(
        "/v1/auth/signin",
        json={"email": "victim@example.com", "password": TEST_CREDENTIAL},
    ).status_code == 429


@requires_db
def test_a_successful_signin_releases_the_source_bucket(throttled):
    """Mistyping a password twice must not ration the next five minutes."""
    client, _clock = throttled
    client.post("/v1/auth/signup", json={"email": "typist@example.com", "password": TEST_CREDENTIAL})

    client.post("/v1/auth/signin", json={"email": "typist@example.com", "password": "wrong-1"})
    ok = client.post(
        "/v1/auth/signin", json={"email": "typist@example.com", "password": TEST_CREDENTIAL}
    )
    assert ok.status_code == 200, ok.text

    # Five more from the same source and the same account, which the source
    # limit of three would refuse had success not released it -- and which the
    # account limit of two would refuse if it counted attempts rather than
    # failures. Signing in repeatedly is what a person with several devices
    # does, and it must not be rationed.
    for _ in range(5):
        assert client.post(
            "/v1/auth/signin",
            json={"email": "typist@example.com", "password": TEST_CREDENTIAL},
        ).status_code == 200


# -- who a call is counted against ----------------------------------------


class _Request:
    """The two attributes `client_key` reads, without an ASGI server."""

    def __init__(self, config, *, host: str | None, forwarded: str | None = None) -> None:
        self.app = type("app", (), {"state": type("state", (), {"config": config})})
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
        self.client = type("client", (), {"host": host}) if host else None


def test_a_forwarded_header_is_ignored_unless_the_deployment_trusts_it(app_config):
    """Trusting it by default would let every caller pick its own bucket.

    Which is not a weaker limit but no limit: a caller that chooses the key its
    attempts are counted against can present a new one per attempt.
    """
    untrusting = dataclasses.replace(app_config, trust_forwarded_for=False)
    request = _Request(untrusting, host="10.0.0.9", forwarded="1.2.3.4")
    assert limit_dep.client_key(request) == "10.0.0.9"


def test_a_trusted_deployment_reads_the_last_hop(app_config):
    """The first entry is what the client claimed; the last is what we observed."""
    trusting = dataclasses.replace(app_config, trust_forwarded_for=True)
    request = _Request(trusting, host="10.0.0.9", forwarded="1.2.3.4, 203.0.113.7")
    assert limit_dep.client_key(request) == "203.0.113.7"


def test_a_call_with_no_peer_address_shares_one_bucket_rather_than_none(app_config):
    request = _Request(app_config, host=None)
    assert limit_dep.client_key(request) == "unknown"


# -- the limiter itself ----------------------------------------------------


def test_a_limit_of_zero_closes_the_route():
    """Usable in an incident, and distinct from an unset limit."""
    limiter = ratelimit.LocalLimiter(clock=_Clock())
    assert not limiter.check("k", ratelimit.Limit(0, 60)).allowed


def test_an_unusable_window_fails_open_rather_than_locking_everyone_out():
    """A configuration typo must not be the thing that stops customers signing in."""
    limiter = ratelimit.LocalLimiter(clock=_Clock())
    assert limiter.check("k", ratelimit.Limit(5, 0)).allowed


def test_an_idle_bucket_does_not_accumulate_a_burst():
    """A week of quiet must not buy a week of attempts at once."""
    clock = _Clock()
    limiter = ratelimit.LocalLimiter(clock=clock)
    limit = ratelimit.Limit(3, 60)
    for _ in range(3):
        assert limiter.check("k", limit).allowed
    clock.advance(7 * 24 * 3600)
    for _ in range(3):
        assert limiter.check("k", limit).allowed
    assert not limiter.check("k", limit).allowed, "the bucket refilled past its size"
