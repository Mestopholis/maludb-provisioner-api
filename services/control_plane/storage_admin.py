"""The storage worker's admin API, and nothing that needs a key.

Split out of `storage_workers` for one reason: free slice 10c runs a pass **on the node**, and the
node's rule -- `node_maintenance`'s, ADR-038's -- is that node-side code must not be able to reach
the key ring. `storage_workers` derives the worker's secrets, so it imports `crypto`, `config` and
the provisioning machinery; a node process importing it inherits all of that. These functions need
none of it. They need an address, a credential someone else already has, and `httpx`.

So this module imports `httpx` and `models`, and `tests/test_node_storage.py` walks its closure to
keep it that way. `storage_workers` re-exports everything here, so callers on the control plane are
unchanged and there is still one name for each of these operations.
"""

from __future__ import annotations

import json

import httpx

from services.control_plane import models

ADMIN_TIMEOUT_SECONDS = 15.0


class StorageWorkerError(RuntimeError):
    """The storage worker could not be configured, started, or registered."""


def _admin(
    method: str,
    path: str,
    *,
    admin_port: int,
    api_key: str,
    payload: dict | None = None,
    expect_missing_ok: bool = False,
) -> dict | None:
    """One call to the worker's admin API, on loopback.

    The credential goes in an **`apikey`** header. `Authorization` answers 401,
    which slice 0 recorded because it costs an hour to discover and reads like a
    wrong key rather than a wrong header.
    """
    url = f"http://127.0.0.1:{admin_port}{path}"
    try:
        response = httpx.request(
            method,
            url,
            headers={"apikey": api_key, "content-type": "application/json"},
            content=json.dumps(payload) if payload is not None else None,
            timeout=ADMIN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        # Never the exception's text: a transport error can echo the URL, and
        # the URL is harmless but the habit is worth keeping consistent.
        raise StorageWorkerError(
            f"the storage worker's admin API did not answer ({type(exc).__name__})"
        ) from None

    if expect_missing_ok and response.status_code == 404:
        return None
    if response.status_code >= 400:
        # The body can carry a tenant's configuration; the status alone is what
        # a caller needs and what a log should hold.
        raise StorageWorkerError(
            f"the storage worker's admin API answered {response.status_code} to {method} {path}"
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def deregister_tenant(*, admin_port: int, api_key: str, project_ref: str) -> None:
    """Remove a tenant from the shared worker.

    A 404 is success: the goal is that the worker does not serve this tenant,
    and a worker that has never heard of it already meets that.
    """
    if not models.is_valid_project_ref(project_ref):
        raise StorageWorkerError(f"invalid project ref {project_ref!r}")
    _admin(
        "DELETE",
        f"/tenants/{project_ref}",
        admin_port=admin_port,
        api_key=api_key,
        expect_missing_ok=True,
    )


def tenant_known(*, admin_port: int, api_key: str, project_ref: str) -> bool:
    """Whether the worker currently holds a configuration for this tenant.

    Presence, and nothing else. The admin API answers this with the tenant's
    **whole** configuration -- its database URL, which carries a live password,
    and its JWT signing secret -- so the body is discarded here rather than
    returned. A caller that never receives it cannot log it, which is the same
    rule `_admin` follows for error bodies and for the same reason.

    A 404 is the answer this exists to get: it is what a worker whose
    multitenant database was rebuilt says about a tenant the control plane
    believes it serves.
    """
    if not models.is_valid_project_ref(project_ref):
        raise StorageWorkerError(f"invalid project ref {project_ref!r}")
    found = _admin(
        "GET",
        f"/tenants/{project_ref}",
        admin_port=admin_port,
        api_key=api_key,
        expect_missing_ok=True,
    )
    return found is not None


def known_tenants(*, admin_port: int, api_key: str) -> tuple[str, ...]:
    """Every tenant the worker currently holds a configuration for, by ref and nothing else.

    The admin API answers with each tenant's **whole** configuration -- a database URL carrying a
    live password, and a JWT signing secret -- so only the ids are kept. A caller that never
    receives the rest cannot log it, which is `tenant_known`'s rule for the same reason.

    Refs that are not project refs are dropped rather than returned: they came from a worker's
    database, they are about to be used in a URL path, and nothing else here trusts that shape
    without checking it.
    """
    listing = _admin("GET", "/tenants", admin_port=admin_port, api_key=api_key)
    if not isinstance(listing, list):
        raise StorageWorkerError("the worker's tenant listing was not a list")
    return tuple(
        item["id"] for item in listing
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and models.is_valid_project_ref(item["id"])
    )


def is_ready(*, admin_port: int, api_key: str, timeout: float = 2.0) -> bool:
    """Whether the worker is up and has migrated its multitenant database.

    Asks a question only a migrated instance can answer, rather than checking
    that a port is open: the container accepts connections before it has run its
    own migrations, and a readiness check that a half-started worker passes is
    how a tenant gets registered into a database that has no table for it.
    """
    try:
        response = httpx.get(
            f"http://127.0.0.1:{admin_port}/tenants",
            headers={"apikey": api_key},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


__all__ = [
    "ADMIN_TIMEOUT_SECONDS",
    "StorageWorkerError",
    "deregister_tenant",
    "is_ready",
    "known_tenants",
    "tenant_known",
]
