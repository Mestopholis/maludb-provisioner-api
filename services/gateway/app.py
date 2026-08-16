"""The public API gateway (ADR-026).

The request flow in `docs/API-GATEWAY.md`, in order, because the order is the
security property: the project comes from the hostname *first*, and the key is
then checked against that project. Resolving the project from the key instead
would make the hostname decorative and every key a key to every project.

Failures are uniform on purpose. A wrong domain, an unknown ref, a suspended
project, an unknown key and a key belonging to a different project all answer
401 with the same body. Distinguishing them hands out an oracle for which
project refs exist and which keys are live, and the client cannot act on the
difference anyway.

What this deliberately does not do: reach into the tenant database, interpret
the response body, or forward anything that identifies the node or database
serving the request.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

import httpx
import jwt
from psycopg import sql
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from services.control_plane import (
    auth_workers,
    crypto,
    db,
    entitlements,
    provisioning,
    realtime_workers,
    workers,
)
from services.gateway import keys, limits, routing, sockets

log = logging.getLogger(__name__)

# Serving statuses. A project that is provisioned but not yet active still
# answers, because Phase 03 is what carries it to ACTIVE.
SERVING_STATUSES = ("PROVISIONED", "ACTIVE")

# Headers that describe one hop and must never be forwarded (RFC 9110).
HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade",
    }
)

# Client-supplied forwarding headers are dropped rather than appended to.
# Accepting them lets a caller forge the origin recorded downstream, and
# nothing behind this gateway has a reason to believe them.
UNTRUSTED_INBOUND = frozenset({"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded"})

# docs/API-GATEWAY.md requires request-body limits. PostgREST will happily
# accept a very large insert; the ceiling belongs in front of it.
MAX_BODY_BYTES = 8 * 1024 * 1024

UPSTREAM_TIMEOUT_SECONDS = 30.0

# docs/API-GATEWAY.md routes each public surface to its own service, and the
# prefix belongs to the gateway rather than to the thing behind it: PostgREST
# serves at its own root and answers PGRST125 for anything else. Supabase does
# the same stripping, which is why a client written against Supabase works
# unchanged -- and why forwarding the path verbatim breaks every call.
REST_PREFIX = "/rest/v1"


@dataclass(frozen=True)
class Surface:
    """A public API prefix and the per-project worker behind it.

    Each surface has its own port, its own lifecycle state and its own activity
    clock, because ADR-022 requires them to sleep and wake independently: the
    Auth worker is 17.6 MB of the 31.8 MB a warm project costs, and a project
    using only the Data API must not pay for one.
    """

    prefix: str
    port_key: str
    state_key: str
    activity_column: str
    # Auth is opt-in per ADR-022; the Data API is what a project is for.
    enabled_key: str | None = None


REST = Surface(REST_PREFIX, "api_port", "worker_state", "worker_last_active_at")
AUTH = Surface("/auth/v1", "auth_port", "auth_worker_state", "auth_worker_last_active_at", "auth_enabled")
SURFACES = (REST, AUTH)

REALTIME_PREFIX = "/realtime/v1"

# Realtime *is* a surface after all, and slice 5 is where that changed. Slice 3
# left it out because ADR-031 made it one shared server per node: there was no
# per-project port to look up, nothing to wake and nothing to sleep, and forcing
# it into this shape would have meant three columns that exist to be ignored.
# ADR-034 gave it all three. It is still kept out of `SURFACES`, which is the
# list the *request* path routes over: Realtime has no HTTP handler, and adding
# it there would proxy a plain GET that should stay a 404.
REALTIME = Surface(
    REALTIME_PREFIX,
    "realtime_port",
    "realtime_worker_state",
    "realtime_worker_last_active_at",
    "realtime_enabled",
)

# Realtime does *not* serve at its own root, which every other surface here
# does. Upstream mounts the Phoenix socket at `/socket`, so the client's
# `/realtime/v1/websocket` becomes `/socket/websocket` -- Supabase's own edge
# makes exactly this substitution, which is why a client written against
# Supabase works unchanged.
#
# Slice 3 stripped the prefix and forwarded `/websocket`, because its stub
# upstream accepted any path and could not disagree. A real Realtime answers
# 404 to that, and 403 to the correct path with no token -- which is how the
# difference was found.
REALTIME_UPSTREAM_PREFIX = "/socket"

# Paths a user reaches by clicking a link in an email, where no API key can be
# presented: a browser navigating to a confirmation link sends an `apikey`
# header for nobody. Requiring one here 401s every confirmation and every
# password reset, which the compatibility suite found the moment it started
# running with confirmation on -- the Phase 04 end-to-end test drove GoTrue
# directly and never went through the gateway.
#
# Deliberately a short, exact list rather than a prefix. The credential on these
# requests is the single-use token in the query string, which GoTrue verifies;
# opening the wider Auth surface unauthenticated would expose signup and
# password endpoints to anyone who knows a project hostname.
PUBLIC_AUTH_PATHS = frozenset({"/auth/v1/verify"})

# Routed but not yet served. Answering 404 here rather than proxying means a
# client calling one against a project that has none gets a comprehensible
# answer instead of a confusing one from the wrong service.
#
# Realtime left this list in slice 3 -- but only over WebSocket. A plain HTTP
# GET to /realtime/v1 still lands here, which is correct: upstream serves that
# surface over a socket, and a client that did not upgrade has not asked for
# anything the platform can answer.
UNIMPLEMENTED_PREFIXES = ("/realtime/v1", "/storage/v1")

# How long a project's routing row and JWT secret may be reused. Both change
# rarely -- a port and a signing key are stable for the life of a worker -- and
# re-reading them per request cost more than everything else the gateway does
# put together. Bounded so a status change still takes effect promptly.
PROJECT_CACHE_TTL_SECONDS = 5.0

# One activity write per project per interval, tracked in memory rather than by
# asking the database whether it is time to write again. The rate-limiting
# UPDATE was still a round trip on the hot path, which is the cost it existed
# to avoid.
ACTIVITY_INTERVAL_SECONDS = 60.0

_UNAUTHORIZED = {"message": "invalid project or API key"}

# A Realtime token outlives a request because the socket it authorises does.
# Fifteen minutes rather than the request path's sixty seconds, and not longer:
# the socket survives its expiry (upstream checks the token when a channel is
# joined, not continuously), so this bounds how long a leaked one would be worth
# having rather than how long a connection may live. It never leaves the node --
# minted here, handed straight to a loopback connection.
REALTIME_TOKEN_TTL_SECONDS = 900

# How long after starting a wake the gateway refuses to start another for the
# same project. Comfortably longer than the nine seconds a start takes, so a
# client reconnecting on backoff finds a ready instance rather than causing a
# second one to be built.
WAKE_COOLDOWN_SECONDS = 20.0

# How long a client's socket is held open while its Realtime instance boots,
# and how often readiness is re-read while it waits.
#
# ADR-036 originally closed 1013 immediately and relied on the client
# reconnecting until the instance was up. That works only for clients that
# retry for longer than a boot takes, and the official client's patience turns
# out to depend on its runtime: measured against `supabase/realtime` v2.110.0
# with a 9.7s wake, @supabase/supabase-js 2.112.3 made four socket attempts on
# Node 24 and connected, and two on Node 22 and gave up. A platform cannot make
# correctness depend on that.
#
# So the socket is accepted first and the client's frames wait in the receive
# queue until the instance answers. The budget is generous because the cost of
# exceeding it is small: phoenix retries the *join* on a socket that is already
# open, which is upstream's own mechanism and does not depend on reconnecting.
WAKE_HOLD_SECONDS = 45.0
WAKE_POLL_SECONDS = 0.25


def _route(path: str) -> tuple[Surface, str] | None:
    """Match a request path to a surface and strip its prefix.

    The prefix belongs to the gateway, not to the service behind it: PostgREST
    and GoTrue both serve at their own roots. Phase 03 slice 4 found this the
    hard way -- forwarding `/rest/v1/...` verbatim made PostgREST answer
    PGRST125 for every call the gateway had just authorised.
    """
    for surface in SURFACES:
        if path == surface.prefix:
            return surface, "/"
        if path.startswith(surface.prefix + "/"):
            return surface, path[len(surface.prefix):]
    return None


def _deny(status: int = 401, message: str | None = None) -> Response:
    return JSONResponse(_UNAUTHORIZED if message is None else {"message": message}, status_code=status)


def _presented_key(request: Request) -> str | None:
    """The project key, per the Supabase client convention.

    supabase-js sends `apikey` on every request, and `Authorization: Bearer`
    carrying either the same key (anonymous) or an end-user JWT (signed in).
    So `apikey` is authoritative and Authorization is only a fallback.
    """
    header = request.headers.get("apikey")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return None


def _presented_socket_key(websocket: WebSocket) -> str | None:
    """The project key on a WebSocket handshake.

    The query string comes first, and that is not a stylistic choice: **a
    browser cannot set headers on a WebSocket handshake.** The browser
    WebSocket API takes a URL and an optional subprotocol list, and nothing
    else, which is why supabase-js connects to
    `.../realtime/v1/websocket?apikey=<key>&vsn=1.0.0`. A gateway that demanded
    a header here would work from Node and fail from every browser -- and the
    browser is the case Realtime exists for.

    The header forms are still accepted, because a server-side client can send
    them and there is no reason to refuse.

    The cost is real and worth naming: a key in a query string is a key in
    proxy logs, browser history and `Referer`. It is the protocol upstream
    defined and the one the official client speaks, so compatibility decides
    it (AGENTS.md), but it is an argument for keys that can be revoked -- which
    ADR-028's are.
    """
    query = websocket.query_params.get("apikey")
    if query and query.strip():
        return query.strip()
    header = websocket.headers.get("apikey")
    if header and header.strip():
        return header.strip()
    authorization = websocket.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def _upstream_query(websocket: WebSocket, *, token: str) -> str:
    """The query string upstream sees: the client's, minus its key, plus ours.

    The client's `apikey` must not be forwarded. It is a MaluDB key, opaque by
    ADR-028 and meaningless to Realtime, and forwarding it writes the platform's
    own credential into upstream's logs -- the request path already strips the
    same header for the same reason.

    In its place goes the minted JWT, because `?apikey=` is where upstream
    Realtime looks for one and the official client puts it there. It is the same
    token as the `Authorization` header carries; both are set so the connection
    does not depend on which of the two upstream happens to read.

    Every other parameter is preserved untouched -- `vsn` in particular, which
    selects the Phoenix serialiser version and would change the wire format if
    dropped.
    """
    preserved = [
        (name, value)
        for name, value in parse_qsl(str(websocket.url.query or ""), keep_blank_values=True)
        if name.lower() != "apikey"
    ]
    preserved.append(("apikey", token))
    return urlencode(preserved)


def _requested_subprotocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get("sec-websocket-protocol", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _held_subprotocol(websocket: WebSocket) -> str | None:
    """What to echo when accepting before upstream has negotiated.

    The ready path accepts only after upstream has chosen, and echoes that
    choice. A socket held through a wake cannot: it is accepted first, so the
    only honest answer is the client's own first preference, which is what a
    server picks when it has no opinion. The official client requests none at
    all -- phoenix carries everything in the query string and the frames -- so
    in practice this is None, and `_serve_socket` warns if upstream later
    disagrees with what was already promised.
    """
    requested = _requested_subprotocols(websocket)
    return requested[0] if requested else None


def _socket_upstream_headers(websocket: WebSocket, *, token: str) -> dict[str, str]:
    """What the Realtime server sees, beyond the handshake's own headers.

    An allowlist rather than the request path's denylist, and deliberately
    stricter: the handshake carries `Sec-WebSocket-Key`, `Sec-WebSocket-Version`
    and the rest, which the client library regenerates for its own connection
    and which would collide if forwarded. Starting from nothing also means a
    header nobody thought about does not reach upstream by default.

    `Host` is deliberately *not* here. It identifies the tenant upstream, and
    setting it as an extra header appends a second one rather than replacing the
    library's -- see `sockets.open_upstream`, which carries it in the URI
    instead.
    """
    headers = {"Authorization": f"Bearer {token}"}
    # The one client header worth carrying: upstream logs it, and losing it
    # makes every connection look like it came from nowhere.
    user_agent = websocket.headers.get("user-agent")
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


def _service_role_token(jwt_secret: str) -> str:
    """The token a secret key stands for.

    ADR-028's keys are opaque, but PostgREST decides a request's role from a
    JWT. The translation happens here rather than by giving customers a signed
    token directly: a JWT handed out is valid until it expires no matter what
    the platform later decides, while an opaque key can be revoked and is
    checked against the project on every request.
    """
    now = int(time.time())
    return jwt.encode(
        {"role": "service_role", "iss": "maludb-gateway", "iat": now, "exp": now + 60},
        jwt_secret,
        algorithm="HS256",
    )


def _forwarded_headers(request: Request, *, presented: str, authorization: str | None) -> dict[str, str]:
    headers = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in UNTRUSTED_INBOUND:
            continue
        # The platform key is ours, not PostgREST's. Left in place it is at
        # best noise and at worst something a future upstream tries to parse.
        if lowered == "apikey":
            continue
        if lowered == "authorization":
            continue
        if lowered == "host":
            continue
        headers[name] = value

    if authorization is not None:
        headers["Authorization"] = authorization
    return headers


def _upstream_authorization(identity, presented: str | None, request: Request, jwt_secret: str) -> str | None:
    """What Authorization the upstream should see, if any.

    Three cases, and the middle one is the one that bites:

    - a secret key becomes a short-lived service_role token;
    - a publishable key presented in Authorization is *not* a JWT, and passing
      it through makes PostgREST answer 401 for a request the gateway just
      authorised. It is dropped, and the absence of a token is what selects
      `db-anon-role`;
    - an end-user JWT (Phase 04) is forwarded untouched, because verifying it
      is PostgREST's job and re-signing it here would erase its claims.
    """
    if identity is None:
        # A link followed from an email. There is no caller identity to convey,
        # and the token in the query string is what GoTrue acts on.
        return None
    if identity.is_secret:
        return f"Bearer {_service_role_token(jwt_secret)}"

    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    if value.strip() == presented:
        return None
    return authorization


class Gateway:
    """Holds the pieces a request needs. One instance per process."""

    def __init__(
        self,
        *,
        config,
        key_ring: crypto.KeyRing,
        cache: keys.KeyCache | None = None,
        client: httpx.AsyncClient | None = None,
        supervisor: workers.Supervisor | None = None,
        auth_supervisor: workers.Supervisor | None = None,
        realtime_supervisor: workers.Supervisor | None = None,
        limiter: limits.Limiter | None = None,
        socket_limiter: limits.SocketLimiter | None = None,
        wake_sleeping: bool = True,
    ) -> None:
        self.config = config
        self.key_ring = key_ring
        self.cache = cache or keys.KeyCache()
        self.client = client or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
        self.supervisor = supervisor
        # Deliberately not defaulted to `supervisor`. The two drive different
        # systemd unit templates, and silently reusing the PostgREST one would
        # start the wrong unit -- a failure that looks like a worker that will
        # not come up rather than like a wiring mistake.
        self.auth_supervisor = auth_supervisor
        # A third, for the same reason there is a second: the Realtime unit runs
        # a container and starting the wrong template would look like a worker
        # that will not come up rather than like a wiring mistake.
        self.realtime_supervisor = realtime_supervisor
        self.limiter = limiter if limiter is not None else limits.LocalLimiter()
        # Its own limiter, because a socket is counted, not rated. See
        # `limits.SocketLimiter`.
        self.socket_limiter = socket_limiter or limits.SocketLimiter()
        self.wake_sleeping = wake_sleeping
        self._projects: dict[str, tuple[float, dict | None]] = {}
        self._secrets: dict[uuid.UUID, str] = {}
        self._activity: dict[uuid.UUID, float] = {}
        # Projects whose Realtime instance is being started right now, so a
        # reconnecting client does not ask for a second container start while
        # the first is still booting -- and when each was last tried, so a
        # failing one is not retried on every reconnect.
        self._waking: set[uuid.UUID] = set()
        self._waked: dict[uuid.UUID, float] = {}
        self._state_lock = threading.Lock()

    # -- resolution --------------------------------------------------------

    def _project(self, project_ref: str) -> dict | None:
        """The project's routing row, cached briefly.

        Negative results are cached too: without it, a stream of requests for
        refs that do not exist is a free way to drive control-plane queries.
        """
        now = time.monotonic()
        with self._state_lock:
            cached = self._projects.get(project_ref)
            if cached is not None and cached[0] > now:
                return cached[1]

        with db.connection() as conn:
            row = db.one(
                conn,
                "SELECT pr.id, pr.status, pr.api_port, pr.worker_state, pr.database_name, "
                "       pr.auth_port, pr.auth_worker_state, pr.auth_enabled, "
                "       pr.realtime_enabled, pr.realtime_port, pr.realtime_worker_state, "
                "       pl.code AS plan_code, pl.config_json "
                "  FROM projects pr LEFT JOIN plans pl ON pl.id = pr.plan_id "
                " WHERE pr.project_ref = %s AND pr.deleted_at IS NULL",
                (project_ref,),
            )
        with self._state_lock:
            self._projects[project_ref] = (now + PROJECT_CACHE_TTL_SECONDS, row)
        return row

    def _jwt_secret(self, project_id: uuid.UUID) -> str:
        """The project's signing secret, decrypted once per process.

        Read per request this was the single most expensive thing the gateway
        did -- a database round trip plus an AES-GCM open, to obtain a value
        that does not change. It is only needed to mint a service_role token,
        so the publishable path never asks for it at all.
        """
        with self._state_lock:
            cached = self._secrets.get(project_id)
        if cached is not None:
            return cached
        with db.connection() as conn:
            secret = provisioning.load_credential(
                conn, project_id=project_id, credential_type="jwt_signing", key_ring=self.key_ring
            )
        with self._state_lock:
            self._secrets[project_id] = secret
        return secret

    def forget(self, project_ref: str, project_id: uuid.UUID) -> None:
        """Drop cached state for a project. Used when it stops serving."""
        with self._state_lock:
            self._projects.pop(project_ref, None)
            self._secrets.pop(project_id, None)
        self.cache.invalidate_project(project_id)

    def _authenticate(self, presented: str, project_id: uuid.UUID):
        with db.connection() as conn:
            identity = self.cache.resolve(
                conn,
                presented=presented,
                project_id=project_id,
                pepper=self.config.token_pepper,
            )
            conn.commit()
        return identity

    def _wake(self, project: dict, surface: Surface) -> int | None:
        """Bring a slept worker up, returning the port it serves on.

        ADR-022: waking must wait for readiness rather than for the port to
        open, which the start_worker functions already do -- hence going through
        them rather than issuing a systemctl start from here.

        Returns None rather than raising when the surface is not enabled for
        this project. That is not an error: a project without Auth simply has no
        Auth worker, and the caller turns it into a 404 for the surface rather
        than a 503 for the node.
        """
        if not self.wake_sleeping:
            return None
        if surface is AUTH and self.auth_supervisor is None:
            return None
        if surface is REST and self.supervisor is None:
            return None
        if surface is REALTIME and self.realtime_supervisor is None:
            return None
        with db.connection() as conn:
            if surface is REALTIME:
                # No node-admin connection is passed, and none is available
                # here: this process must not hold a credential that can create
                # databases and roles (docs/ARCHITECTURE.md). Everything that
                # needs one was built when Realtime was enabled; waking only
                # renders the environment, starts the unit and re-registers the
                # tenant.
                realtime_workers.start_worker(
                    conn,
                    project_id=project["id"],
                    key_ring=self.key_ring,
                    config=self.config,
                    supervisor=self.realtime_supervisor,
                )
                row = db.one(
                    conn, "SELECT realtime_port FROM projects WHERE id = %s", (project["id"],)
                )
                return row["realtime_port"]
            if surface is AUTH:
                auth_workers.start_worker(
                    conn,
                    project_id=project["id"],
                    key_ring=self.key_ring,
                    gateway_domain=self.config.gateway_domain,
                    supervisor=self.auth_supervisor,
                )
                row = db.one(
                    conn, "SELECT auth_port FROM projects WHERE id = %s", (project["id"],)
                )
                return row["auth_port"]
            return workers.start_worker(
                conn,
                project_id=project["id"],
                key_ring=self.key_ring,
                supervisor=self.supervisor,
            )

    # -- the request -------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        try:
            project_ref = routing.project_ref_from_host(
                request.headers.get("host"), gateway_domain=self.config.gateway_domain
            )
        except routing.RoutingError as exc:
            log.info("rejected request: %s", exc)
            return _deny()

        presented = _presented_key(request)
        # A link followed from an email carries no key. Everything else does.
        link_followed = request.url.path in PUBLIC_AUTH_PATHS
        if not presented and not link_followed:
            return _deny()

        project = self._project(project_ref)
        if project is None or project["status"] not in SERVING_STATUSES:
            return _deny()

        identity = None
        if presented:
            identity = self._authenticate(presented, project["id"])
            if identity is None:
                return _deny()

        # Routed after authentication, deliberately: an unauthenticated caller
        # gets the same 401 whatever path it asks for, so the routing table is
        # not a probe for what a project exposes.
        route = _route(request.url.path)
        if route is None:
            if request.url.path.startswith(UNIMPLEMENTED_PREFIXES):
                return _deny(404, "this API surface is not available yet")
            return _deny(404, "not found")
        surface, upstream_path = route

        # Auth is opt-in (ADR-022). A project that has not enabled it has no
        # worker to reach, and saying so is more useful than a 503 -- and is
        # checked after authentication, so it does not reveal which projects use
        # Auth to an unauthenticated caller.
        if surface.enabled_key and not project[surface.enabled_key]:
            return _deny(404, "this API surface is not enabled for this project")

        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _deny(413, "request body too large")

        # Limited after authentication and routing, so an unauthenticated
        # caller cannot spend a project's allowance on its behalf -- which would
        # turn the limiter into the denial-of-service tool it exists to prevent.
        allowed = entitlements.resolve(project["plan_code"], project["config_json"])
        decision = self.limiter.acquire(
            project["id"],
            rate=allowed.api_requests_per_window,
            window_seconds=allowed.api_window_seconds,
            concurrency=allowed.concurrent_api_requests,
        )
        if not decision.allowed:
            log.info("project %s hit its %s limit", project_ref, decision.limit)
            headers = (
                {"Retry-After": str(decision.retry_after_seconds)}
                if decision.retry_after_seconds
                else None
            )
            return JSONResponse({"message": decision.message}, status_code=429, headers=headers)

        try:
            return await self._serve(
                request, project=project, project_ref=project_ref, surface=surface,
                upstream_path=upstream_path, body=body, identity=identity,
                presented=presented,
            )
        finally:
            # In a finally, always. A leaked concurrency slot never expires, so
            # a project that leaked its whole allowance can never serve again.
            self.limiter.release(project["id"])

    async def _serve(
        self, request: Request, *, project: dict, project_ref: str, surface: Surface,
        upstream_path: str, body: bytes, identity, presented: str | None,
    ) -> Response:

        # Optimistic: a project recorded as RUNNING is proxied to directly.
        # Probing readiness first would double the upstream round trips on every
        # single request to buy information that is almost always "yes", and the
        # rare "no" is caught below by the connection failing.
        port = project[surface.port_key] if project[surface.state_key] == "RUNNING" else None
        if port is None:
            try:
                port = self._wake(project, surface)
            except (workers.WorkerError, auth_workers.AuthWorkerError):
                log.error("could not bring up a worker for project %s", project_ref)
                return _deny(503, "project is temporarily unavailable")
        if port is None:
            return _deny(503, "project is temporarily unavailable")

        # Only a secret key needs the signing secret; asking for it on the
        # publishable path would pay for a token that is never minted.
        jwt_secret = self._jwt_secret(project["id"]) if (identity and identity.is_secret) else ""
        authorization = _upstream_authorization(identity, presented, request, jwt_secret)
        headers = _forwarded_headers(request, presented=presented or "", authorization=authorization)

        try:
            upstream = await self._proxy(request, port, body, headers, upstream_path)
        except httpx.HTTPError:
            # The recorded state said RUNNING and the socket disagreed -- a
            # worker that died, or a control plane that lost track of it. Wake
            # once and retry, because the alternative is serving 502 until
            # something else notices.
            try:
                woken = self._wake(project, surface)
            except (workers.WorkerError, auth_workers.AuthWorkerError):
                woken = None
            if woken is None:
                log.error("upstream request failed for project %s", project_ref)
                return _deny(502, "upstream request failed")
            try:
                upstream = await self._proxy(request, woken, body, headers, upstream_path)
            except httpx.HTTPError:
                # Never surface the driver's message: it names the internal host
                # and port, which docs/API-GATEWAY.md forbids exposing.
                log.error("upstream request failed for project %s after a wake", project_ref)
                return _deny(502, "upstream request failed")

        self._record_activity(project["id"], surface)
        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() not in HOP_BY_HOP and name.lower() != "content-length"
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=upstream.headers.get("content-type"),
        )

    async def _proxy(
        self, request: Request, port: int, body: bytes, headers: dict, path: str
    ) -> httpx.Response:
        return await self.client.request(
            request.method,
            f"http://127.0.0.1:{port}{path}",
            params=dict(request.query_params),
            content=body,
            headers=headers,
        )

    # -- the socket --------------------------------------------------------

    async def handle_websocket(self, websocket: WebSocket) -> None:
        """Authenticate a Realtime connection, then proxy it.

        The order is the same as the request path and for the same reason: the
        project comes from the hostname *first*, and the key is checked against
        that project. Resolving the project from the key would make the hostname
        decorative and every key a key to every project.

        Every refusal before the caller has proved it holds a key for this
        project closes with the same code and no body, exactly as the HTTP path
        answers a uniform 401 -- a distinguishable rejection is an oracle for
        which refs exist and which keys are live.

        Nothing is accepted before it is authorised. A denied connection is
        refused during the handshake, so a caller that fails here never holds an
        open socket.
        """
        try:
            project_ref = routing.project_ref_from_host(
                websocket.headers.get("host"), gateway_domain=self.config.gateway_domain
            )
        except routing.RoutingError as exc:
            log.info("rejected socket: %s", exc)
            await websocket.close(code=sockets.CLOSE_POLICY)
            return

        # Each refusal below is identical *on the wire* and distinct in the log.
        # The uniformity is what stops a caller learning which refs exist and
        # which keys are live; it was never meant to stop an operator finding
        # out why their client cannot connect, and for a while it did both.
        presented = _presented_socket_key(websocket)
        if not presented:
            log.info("rejected socket for %s: no key presented", project_ref)
            await websocket.close(code=sockets.CLOSE_POLICY)
            return

        project = self._project(project_ref)
        if project is None or project["status"] not in SERVING_STATUSES:
            log.info(
                "rejected socket for %s: project is %s",
                project_ref, "unknown" if project is None else project["status"],
            )
            await websocket.close(code=sockets.CLOSE_POLICY)
            return

        identity = self._authenticate(presented, project["id"])
        if identity is None:
            log.info("rejected socket for %s: the key is not valid for it", project_ref)
            await websocket.close(code=sockets.CLOSE_POLICY)
            return

        # Routed after authentication, so the path is not a probe for what a
        # project exposes.
        path = websocket.url.path
        if not (path == REALTIME_PREFIX or path.startswith(REALTIME_PREFIX + "/")):
            log.info("rejected socket for %s: %s is not a Realtime path", project_ref, path)
            await websocket.close(code=sockets.CLOSE_POLICY)
            return
        upstream_path = REALTIME_UPSTREAM_PREFIX + path[len(REALTIME_PREFIX):]

        # From here the caller has proved it holds a key for this project, so a
        # named reason is help rather than an oracle.
        if not project["realtime_enabled"]:
            log.info("project %s has no Realtime enabled", project_ref)
            await websocket.close(code=sockets.CLOSE_NOT_ENABLED)
            return

        # The project's own instance (ADR-034), woken if it is asleep. Realtime
        # is the most expensive thing a project can have running -- ~146 MB
        # against 31.8 MB for an entire warm project -- so it is slept
        # aggressively and a first connection after that pays the wake.
        #
        # Checked *before* the connection is counted, and that ordering is the
        # bug this originally had: a refusal that returns while holding a socket
        # slot leaks one per attempt, and a client reconnecting through a wake
        # reconnects several times. The project would then be refused with 4029
        # for the rest of the gateway's life -- its own limit, spent on
        # connections that were never served.
        # Counted before anything is held, and released in the `finally` below
        # whatever happens after. The wake path accepts a real socket now, so a
        # connection waiting for a boot is a connection the project is using.
        allowed = entitlements.resolve(project["plan_code"], project["config_json"])
        decision = self.socket_limiter.acquire(
            project["id"], limit=allowed.realtime_connections
        )
        if not decision.allowed:
            log.info("project %s hit its Realtime connection limit", project_ref)
            await websocket.close(code=sockets.CLOSE_LIMIT)
            return

        try:
            port = project["realtime_port"]
            accepted = False
            if project["realtime_worker_state"] != "RUNNING" or not port:
                # The instance is asleep -- ADR-022 sleeps it after an idle
                # hour, and it is the largest thing on the node. The socket is
                # **accepted and held** while it boots rather than closed 1013
                # for the client to retry; see WAKE_HOLD_SECONDS for the
                # measurement that changed this.
                #
                # Nothing is read from the socket here, on purpose: the client's
                # `phx_join` waits in the ASGI receive queue and reaches the pump
                # once upstream is connected, so the frame carrying the
                # subscription is neither dropped nor answered by a gateway that
                # has no business answering it.
                log.info(
                    "project %s Realtime is %s; holding the socket while it wakes",
                    project_ref, project["realtime_worker_state"],
                )
                await websocket.accept(subprotocol=_held_subprotocol(websocket))
                accepted = True
                self._begin_wake(project, project_ref)
                woken = await self._await_realtime_ready(project_ref)
                if woken is None:
                    # Still nothing after the budget. 1013 is what the protocol
                    # has for exactly this: a client that does retry gets another
                    # chance, and one that does not has at least been told rather
                    # than left holding a silent socket.
                    log.warning(
                        "project %s Realtime did not become ready within %.0fs; closing %d",
                        project_ref, WAKE_HOLD_SECONDS, sockets.CLOSE_TRY_AGAIN,
                    )
                    await websocket.close(code=sockets.CLOSE_TRY_AGAIN)
                    return
                project, port = woken

            await self._serve_socket(
                websocket, project=project, project_ref=project_ref,
                upstream_path=upstream_path, identity=identity, port=port,
                presented=presented, accepted=accepted,
            )
        finally:
            # In a finally, always. A socket slot leaked while the socket is
            # gone is invisible until the project cannot open another one.
            self.socket_limiter.release(project["id"])

    async def _await_realtime_ready(self, project_ref: str) -> tuple[dict, int] | None:
        """Wait for a waking instance to report RUNNING with a port.

        Returns the refreshed routing row and its port, or None if the budget
        ran out. The row is re-read rather than carried over: the wake writes
        both the state and the port, so the values this connection arrived with
        are the stale ones by definition.

        Polled in a thread because every database call in this process is
        synchronous, and blocking the event loop here would stall every other
        socket this gateway is serving -- including the ones already proxying.
        """
        deadline = time.monotonic() + WAKE_HOLD_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(WAKE_POLL_SECONDS)
            project = await asyncio.to_thread(self._project, project_ref)
            if project is None or project["status"] not in SERVING_STATUSES:
                return None
            port = project["realtime_port"]
            if project["realtime_worker_state"] == "RUNNING" and port:
                return project, port
        return None

    def _begin_wake(self, project: dict, project_ref: str) -> None:
        """Start a project's Realtime instance without waiting for it.

        One wake per project at a time. Without the guard, a client reconnecting
        every second during the nine seconds a boot takes would ask for a fresh
        one on every attempt -- and each of those is a container start, which is
        the most expensive thing this process can be made to do.
        """
        project_id = project["id"]
        now = time.monotonic()
        with self._state_lock:
            if project_id in self._waking:
                return
            # And not again immediately after one failed. A client that
            # reconnects every second against an instance that will not start
            # would otherwise cost a container start every nine seconds,
            # indefinitely -- the guard above only stops the ones that overlap.
            if now - self._waked.get(project_id, 0.0) < WAKE_COOLDOWN_SECONDS:
                return
            self._waking.add(project_id)
            self._waked[project_id] = now

        def run() -> None:
            try:
                self._wake(project, REALTIME)
            except Exception:  # noqa: BLE001 - a wake failure must not kill the thread
                log.exception("could not wake Realtime for project %s", project_ref)
            finally:
                with self._state_lock:
                    self._waking.discard(project_id)
                # The cached row still says STOPPED, and the next connection
                # needs to see the port and the state this wake just wrote.
                self.forget(project_ref, project_id)

        threading.Thread(target=run, name=f"realtime-wake-{project_ref}", daemon=True).start()

    async def _serve_socket(
        self, websocket: WebSocket, *, project: dict, project_ref: str,
        upstream_path: str, identity, port: int, presented: str,
        accepted: bool = False,
    ) -> None:
        token = self._realtime_token(project, identity)
        headers = _socket_upstream_headers(websocket, token=token)
        subprotocols = _requested_subprotocols(websocket)

        try:
            upstream = await sockets.open_upstream(
                # Reconstructed from the validated ref, never passed through
                # from the client: this header is what names the tenant
                # upstream, so a forged Host must not be able to reach it.
                host=f"{project_ref}.{self.config.gateway_domain}",
                # This project's own instance, not a node-wide one. Slice 3 read
                # a single port from configuration, which was right under
                # ADR-031's shared server and would now send every project's
                # sockets to whichever instance happened to hold that port.
                port=port,
                path=upstream_path,
                query=_upstream_query(websocket, token=token),
                headers=headers,
                subprotocols=subprotocols,
            )
        except sockets.UpstreamUnavailable as exc:
            log.error("realtime upstream unavailable for project %s (%s)", project_ref, exc)
            await websocket.close(code=sockets.CLOSE_UPSTREAM_UNAVAILABLE)
            return

        negotiated = getattr(upstream, "subprotocol", None)
        if accepted:
            # Already open: this socket was held through a wake, so the client
            # has had its answer since before the instance existed. The one
            # thing that cannot be taken back is the subprotocol, so a
            # disagreement is reported rather than silently lived with.
            if negotiated != _held_subprotocol(websocket):
                log.warning(
                    "project %s: upstream negotiated subprotocol %r after the socket was "
                    "held open promising %r",
                    project_ref, negotiated, _held_subprotocol(websocket),
                )
        else:
            # Accepted only once the upstream is up, so a client is never handed
            # a working socket that has nothing behind it. The subprotocol echoed
            # is whatever upstream negotiated, not whatever the client asked for.
            await websocket.accept(subprotocol=negotiated)
        log.info("realtime socket serving project %s, proxying to port %s", project_ref, port)
        # Recorded once at accept and once at close, against Realtime's own
        # clock. Slice 3 recorded nothing because there was nothing to sleep;
        # under ADR-034 there is, and it is the largest thing on the node.
        #
        # Twice rather than continuously, and the second time is the one that
        # matters: a socket held open for hours sends no traffic the gateway
        # measures, so a clock stamped only at accept would offer a *busy*
        # project for sleep. Stamping at close means the idle window starts when
        # the last connection ends.
        self._record_activity(project["id"], REALTIME)
        try:
            await sockets.pump(
                websocket,
                upstream,
                # The client sends its key twice -- once in the query string,
                # which is replaced before the handshake, and again inside every
                # channel join. See `sockets.rewrite_access_token`.
                rewrite=lambda frame: sockets.rewrite_access_token(
                    frame,
                    presented=presented,
                    mint=lambda: self._realtime_token(project, identity),
                ),
            )
        except Exception:  # noqa: BLE001 - a proxy failure must close, not propagate
            log.exception("realtime proxy failed for project %s", project_ref)
        finally:
            self._record_activity(project["id"], REALTIME, force=True)
            await upstream.close()
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                # The client hung up first, which is the *normal* end of a
                # Realtime session rather than an error. Starlette raises
                # rather than no-opping, and left uncaught it logged a
                # traceback for every ordinary disconnect -- noise that would
                # bury the failures worth reading.
                pass

    def _realtime_token(self, project: dict, identity) -> str:
        """The JWT the Realtime server should see for this connection.

        ADR-028's keys are opaque, so the gateway translates -- the same
        arrangement `_service_role_token` makes for PostgREST, and for the same
        reason: a JWT handed to a customer is valid until it expires no matter
        what the platform later decides, while an opaque key is checked against
        the project on every connection.

        One deliberate difference from the request path. There, a publishable
        key produces *no* Authorization at all and PostgREST's `db-anon-role`
        selects the anonymous role. Realtime has no such fallback -- a socket
        with no token cannot join a channel -- so a publishable key is minted an
        explicit `anon` token instead of being dropped.

        The token is longer-lived than the request path's 60 seconds because a
        socket outlives a request, and it never leaves this node: the gateway
        mints it and hands it straight to a loopback connection.
        """
        role = "service_role" if identity.is_secret else "anon"
        now = int(time.time())
        return jwt.encode(
            {
                "role": role,
                "iss": "maludb-gateway",
                "iat": now,
                "exp": now + REALTIME_TOKEN_TTL_SECONDS,
            },
            self._jwt_secret(project["id"]),
            algorithm="HS256",
        )

    def _record_activity(
        self, project_id: uuid.UUID, surface: Surface, *, force: bool = False
    ) -> None:
        """Feeds the sleep policy. Rate-limited for the same reason
        `api_keys.last_used_at` is: one write per request to a hot row would
        serialise a project's traffic behind a row lock.

        Per surface, because they sleep independently: a project whose Data API
        is busy while nothing touches Auth should still have its Auth worker
        reclaimed.

        `force` skips the rate limit, and exists for the end of a socket. The
        limit is right for requests, which arrive constantly; it is wrong for a
        connection that closes after an hour, where the one write that matters
        is the last one -- suppressing it would start the idle window when the
        socket *opened*.
        """
        column = sql.Identifier(surface.activity_column)
        rate_limit = sql.SQL("") if force else sql.SQL(
            " AND ({col} IS NULL OR {col} < now() - interval '1 minute')"
        ).format(col=column)
        with db.connection() as conn:
            db.execute(
                conn,
                sql.SQL("UPDATE projects SET {col} = now() WHERE id = %s").format(col=column)
                + rate_limit,
                (project_id,),
            )
            conn.commit()


def create_app(gateway: Gateway) -> Starlette:
    async def endpoint(request: Request) -> Response:
        return await gateway.handle(request)

    async def socket_endpoint(websocket: WebSocket) -> None:
        await gateway.handle_websocket(websocket)

    return Starlette(
        routes=[
            # The socket route is listed first, but the two never compete:
            # Starlette matches on scope type, so an HTTP request cannot reach
            # the socket handler and an upgrade cannot reach the HTTP one.
            # A plain GET to /realtime/v1 therefore still answers 404 from the
            # request path, which is right -- that surface only exists over a
            # socket.
            WebSocketRoute("/{path:path}", socket_endpoint),
            Route("/{path:path}", endpoint, methods=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"]),
        ]
    )
