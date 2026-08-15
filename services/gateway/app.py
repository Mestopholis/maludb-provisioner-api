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

import logging
import threading
import time
import uuid

import httpx
import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from services.control_plane import crypto, db, provisioning, workers
from services.gateway import keys, routing

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


def _upstream_authorization(identity, presented: str, request: Request, jwt_secret: str) -> str | None:
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
        wake_sleeping: bool = True,
    ) -> None:
        self.config = config
        self.key_ring = key_ring
        self.cache = cache or keys.KeyCache()
        self.client = client or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
        self.supervisor = supervisor
        self.wake_sleeping = wake_sleeping
        self._projects: dict[str, tuple[float, dict | None]] = {}
        self._secrets: dict[uuid.UUID, str] = {}
        self._activity: dict[uuid.UUID, float] = {}
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
                "SELECT id, status, api_port, worker_state, database_name FROM projects "
                " WHERE project_ref = %s AND deleted_at IS NULL",
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

    def _wake(self, project: dict) -> int | None:
        """Bring a slept worker up, returning the port it serves on.

        ADR-022: waking must wait for readiness rather than for the port to
        open, which `start_worker` already does -- hence going through it
        rather than issuing a systemctl start from here.
        """
        if not self.wake_sleeping or self.supervisor is None:
            return None
        with db.connection() as conn:
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
        if not presented:
            return _deny()

        project = self._project(project_ref)
        if project is None or project["status"] not in SERVING_STATUSES:
            return _deny()

        identity = self._authenticate(presented, project["id"])
        if identity is None:
            return _deny()

        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _deny(413, "request body too large")

        # Optimistic: a project recorded as RUNNING is proxied to directly.
        # Probing readiness first would double the upstream round trips on every
        # single request to buy information that is almost always "yes", and the
        # rare "no" is caught below by the connection failing.
        port = project["api_port"] if project["worker_state"] == "RUNNING" else None
        if port is None:
            try:
                port = self._wake(project)
            except workers.WorkerError:
                log.error("could not bring up a worker for project %s", project_ref)
                return _deny(503, "project is temporarily unavailable")
        if port is None:
            return _deny(503, "project is temporarily unavailable")

        # Only a secret key needs the signing secret; asking for it on the
        # publishable path would pay for a token that is never minted.
        jwt_secret = self._jwt_secret(project["id"]) if identity.is_secret else ""
        authorization = _upstream_authorization(identity, presented, request, jwt_secret)
        headers = _forwarded_headers(request, presented=presented, authorization=authorization)

        try:
            upstream = await self._proxy(request, port, body, headers)
        except httpx.HTTPError:
            # The recorded state said RUNNING and the socket disagreed -- a
            # worker that died, or a control plane that lost track of it. Wake
            # once and retry, because the alternative is serving 502 until
            # something else notices.
            try:
                woken = self._wake(project)
            except workers.WorkerError:
                woken = None
            if woken is None:
                log.error("upstream request failed for project %s", project_ref)
                return _deny(502, "upstream request failed")
            try:
                upstream = await self._proxy(request, woken, body, headers)
            except httpx.HTTPError:
                # Never surface the driver's message: it names the internal host
                # and port, which docs/API-GATEWAY.md forbids exposing.
                log.error("upstream request failed for project %s after a wake", project_ref)
                return _deny(502, "upstream request failed")

        self._record_activity(project["id"])
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

    async def _proxy(self, request: Request, port: int, body: bytes, headers: dict) -> httpx.Response:
        return await self.client.request(
            request.method,
            f"http://127.0.0.1:{port}{request.url.path}",
            params=dict(request.query_params),
            content=body,
            headers=headers,
        )

    def _record_activity(self, project_id: uuid.UUID) -> None:
        """Feeds the sleep policy. Rate-limited for the same reason
        `api_keys.last_used_at` is: one write per request to a hot row would
        serialise a project's traffic behind a row lock."""
        with db.connection() as conn:
            db.execute(
                conn,
                "UPDATE projects SET worker_last_active_at = now() WHERE id = %s "
                " AND (worker_last_active_at IS NULL OR worker_last_active_at < now() - interval '1 minute')",
                (project_id,),
            )
            conn.commit()


def create_app(gateway: Gateway) -> Starlette:
    async def endpoint(request: Request) -> Response:
        return await gateway.handle(request)

    return Starlette(
        routes=[
            Route("/{path:path}", endpoint, methods=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"]),
        ]
    )
