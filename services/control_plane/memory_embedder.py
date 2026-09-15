"""The query embedder: turns a search query into a vector (ADR-079, memory slice 6a).

A space fed with text holds vectors from a model the customer never called, so a
caller with only a question could not search it. The gateway's
`POST /memory/v1/spaces/{space}/search` asks this service for the query's vector and
runs the search wrapper with it.

**Why a service of its own, here.** Provider keys and provider egress exist only on
the control-plane host (decisions 4 and 6); the gateway runs on a node and can read
neither. This process holds both, reaches providers only through
`maludb-egress-proxy`, and answers one internal route.

**Who may ask.** Exactly the callers the gateway already admits: the request carries
the customer's own secret key, verified here against the named project with
`api_keys.authenticate` -- the gateway's own check, run again rather than trusted. No
platform secret is shared with nodes, so a compromised node can only use keys it has
already seen, which is the reach it had before.

**What it holds.** `cp_memory_embedder` (`memory_worker_grants.EMBEDDER_READS`): key
hashes, live provider keys, projects and spaces' embedding models. No memory writer
credential and no ingest. It refuses to start in production as anything wider.

**What it never does.** Log a query, a key or a vector; return a provider's message
unscrubbed; follow a model name anywhere but a request body to a fixed host.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import uuid
from dataclasses import dataclass

import psycopg
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.control_plane import (
    api_keys,
    crypto,
    db,
    entitlements,
    memory_worker_grants,
    model_providers,
    models,
    provider_keys,
)
from services.control_plane import config as config_module
from services.control_plane import logging as cp_logging
from services.control_plane.memory_worker import require_egress_proxy
from services.gateway import limits

log = logging.getLogger("maludb.memory_embedder")

MAX_QUERY = 2_000
ROUTE = "/internal/memory/embed"
DEFAULT_BIND = "127.0.0.1:8114"


class EmbedIn(BaseModel):
    project_ref: str
    space: str
    text: str


class EmbedRefused(Exception):
    def __init__(self, status: int, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class Embedded:
    embedding: list[float]
    provider: str
    model: str


# A provider failure as an HTTP answer to the gateway, and through it to the caller.
_PROVIDER_STATUS = {"auth": 424, "billing": 424, "bad_request": 424, "rate_limited": 429, "unavailable": 503,
                    "refused": 422, "bad_output": 502}


def embed_query(conn: psycopg.Connection, *, presented: str, body: EmbedIn, key_ring: crypto.KeyRing,
                pepper: bytes, models_client, limiter=None) -> Embedded:
    """Authenticate, find the space's model and the project's key, and embed. Raises `EmbedRefused`.

    **Limited here as well as at the gateway.** Anything on the private network holding a
    project's secret key could call this route directly, and each call spends the
    customer's provider quota; the project's own plan rate applies in this process too.
    """
    if not models.is_valid_project_ref(body.project_ref):
        raise EmbedRefused(401, "unauthorized")
    if not body.text.strip() or len(body.text) > MAX_QUERY:
        raise EmbedRefused(422, f"the query must be text of 1 to {MAX_QUERY} characters")
    project = db.one(conn, "SELECT p.id, pl.code AS plan_code, pl.config_json FROM projects p "
                           "  LEFT JOIN plans pl ON pl.id = p.plan_id "
                           " WHERE p.project_ref = %s AND p.deleted_at IS NULL", (body.project_ref,))
    # An unknown project is verified against a placeholder rather than refused at once,
    # so how long the answer takes does not say which refs exist.
    identity = api_keys.authenticate(conn, presented=presented, project_id=project["id"] if project else uuid.uuid4(),
                                     pepper=pepper)
    if project is None or identity is None:
        raise EmbedRefused(401, "unauthorized")
    if not identity.is_secret:
        raise EmbedRefused(403, "memory requires the project's secret key")
    space = db.one(conn, "SELECT embedding_provider, embedding_model FROM memory_spaces "
                         " WHERE project_id = %s AND name = %s AND state = 'active'",
                   (project["id"], body.space))
    if space is None:
        raise EmbedRefused(404, f"no memory space {body.space!r}")
    if not space["embedding_provider"]:
        raise EmbedRefused(409, f"memory space {body.space!r} has no embedding model, so it can only be searched "
                                "with a vector; a manager can set one with PUT "
                                "/v1/projects/{ref}/maludb/memory/spaces/{name}/models")
    key = provider_keys.load_key(conn, project_id=project["id"], provider=space["embedding_provider"],
                                 key_ring=key_ring)
    conn.commit()  # `authenticate` may have recorded use; nothing below writes.
    if key is None:
        provider = space["embedding_provider"]
        raise EmbedRefused(409, f"this project has no {provider} API key; a manager can set one with "
                                f"PUT /v1/projects/{{ref}}/maludb/memory/provider-keys/{provider}")
    allowed = entitlements.resolve(project["plan_code"], project["config_json"])
    decision = limiter.acquire(project["id"], rate=allowed.api_requests_per_window,
                               window_seconds=allowed.api_window_seconds,
                               concurrency=allowed.concurrent_api_requests) if limiter else None
    if decision is not None and not decision.allowed:
        raise EmbedRefused(429, decision.message, retry_after=decision.retry_after_seconds)
    try:
        [vector] = models_client.embed(space["embedding_provider"], space["embedding_model"], key, [body.text])
    except model_providers.ProviderError as exc:
        raise EmbedRefused(_PROVIDER_STATUS.get(exc.kind, 502), str(exc)) from None
    finally:
        if decision is not None:
            limiter.release(project["id"])
    return Embedded(embedding=vector, provider=space["embedding_provider"], model=space["embedding_model"])


def create_app(*, key_ring: crypto.KeyRing | None = None, pepper: bytes | None = None, models_client=None,
               limiter=None) -> FastAPI:
    """The embedder's application. Arguments are for tests; production reads configuration."""
    if key_ring is None:
        cfg = config_module.load()
        cp_logging.configure()
        require_egress_proxy(environment=cfg.environment, proxy=os.environ.get("MALUDB_MEMORY_EGRESS_PROXY"))
        db.init_pool(cfg.database_url)
        key_ring = crypto.KeyRing(cfg.kek)
        with db.connection() as conn:
            assert_narrowed(conn, environment=cfg.environment)
            key_ring.load(conn)
        pepper = cfg.token_pepper
    limiter = limiter or limits.LocalLimiter()
    models_client = models_client or model_providers.Models(
        proxy=os.environ.get("MALUDB_MEMORY_EGRESS_PROXY") or None)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post(ROUTE)
    def embed(body: EmbedIn, request: Request, apikey: str = Header(default="")) -> JSONResponse:  # noqa: ARG001
        if not apikey:
            return JSONResponse({"message": "unauthorized"}, status_code=401)
        try:
            with db.connection() as conn:
                done = embed_query(conn, presented=apikey, body=body, key_ring=key_ring, pepper=pepper,
                                   models_client=models_client, limiter=limiter)
        except EmbedRefused as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            return JSONResponse({"message": str(exc)}, status_code=exc.status, headers=headers)
        log.info("embedded a query for project %s (%s dimensions)", body.project_ref, len(done.embedding),
                 extra={"extra_fields": {"project_ref": body.project_ref}})
        return JSONResponse({"embedding": done.embedding, "provider": done.provider, "model": done.model})

    return app


def assert_narrowed(conn: psycopg.Connection, *, environment: str) -> None:
    """Refuse production as anything wider than `cp_memory_embedder`, or as a gateway."""
    role = db.one(conn, "SELECT current_user AS role")["role"]
    wider = memory_worker_grants.embedder_violations(conn, role)
    member = db.one(conn, "SELECT public.is_memory_embedder() AS member")["member"]
    gateways = memory_worker_grants.gateway_members(conn, memory_worker_grants.EMBEDDER_GROUP_ROLE)
    conn.rollback()
    problems = []
    if wider:
        problems.append(f"the embedder's role {role!r} can read {', '.join(wider)}")
    if not member:
        problems.append(f"role {role!r} is not a member of {memory_worker_grants.EMBEDDER_GROUP_ROLE}, so it "
                        "sees no project and authenticates nothing")
    if gateways:
        problems.append(f"gateway roles {gateways} are also embedders, which lets them read their node's "
                        "provider keys")
    if not problems:
        return
    message = ("; ".join(problems)
               + ". Create a LOGIN role in cp_memory_embedder and run `cp-manage memory-worker grant`")
    if environment == "production":
        raise RuntimeError(message)
    log.warning("%s -- refused in production; allowed here because MALUDB_ENV=%s", message, environment)


def bind_address(value: str) -> tuple[str, int]:
    """A loopback or private address: nodes reach it over the private network, never the internet."""
    host, _, port = value.rpartition(":")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        # `is_private` is true of 0.0.0.0 and ::, which mean every interface.
        allowed = (address.is_loopback or address.is_private) and not address.is_unspecified
    except ValueError:
        allowed = False
    if not port.isdigit() or not allowed:
        raise SystemExit(f"MALUDB_MEMORY_EMBEDDER_BIND must be a loopback or private address and port, got {value!r}")
    return host.strip("[]"), int(port)


def main() -> int:
    import uvicorn

    host, port = bind_address(os.environ.get("MALUDB_MEMORY_EMBEDDER_BIND", DEFAULT_BIND))
    uvicorn.run(create_app(), host=host, port=port, log_config=None, access_log=False)
    return 0


__all__ = ["ROUTE", "EmbedIn", "EmbedRefused", "assert_narrowed", "bind_address", "create_app", "embed_query"]


if __name__ == "__main__":
    raise SystemExit(main())
