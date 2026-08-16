#!/usr/bin/env python
"""What a Realtime connection costs the gateway.

`scripts/bench-gateway.py` measured the request path, which is what ADR-026's
conditional acceptance of Python in the data path rests on. That number does not
carry over to Phase 06: a request is served and gone, while a WebSocket is held
open, and the question changes from "how much latency does a request pay" to
**"how many sockets can one gateway process hold, and what does each one cost
while it is idle"**.

So this measures three things a request benchmark cannot:

- **Resident memory per open socket**, which is what decides how many a gateway
  can hold. Each proxied connection is two Python objects, two asyncio tasks and
  two TCP sockets -- one to the client, one to the node's Realtime server.
- **Handshake latency**, which is the socket's equivalent of added latency per
  request. It is paid once per connection rather than once per message, so it
  matters far less than the request-path figure does; it is reported so a
  regression is visible.
- **Round-trip latency for a frame** once the socket is up, which is what a
  subscriber actually experiences.

Absolute numbers depend on the machine. The per-socket memory figure is the one
that travels, because it is what capacity planning needs.

    MALUDB_CONTROL_PLANE_DATABASE_URL=... MALUDB_KEK_REF=... \\
    MALUDB_TOKEN_PEPPER_REF=... python scripts/bench-gateway-sockets.py [sockets]

The upstream is a trivial echo server, deliberately: what is being measured is
the gateway's own cost, not upstream Realtime's. **This says nothing about what
a Realtime process costs** -- that figure is still missing from ADR-022, because
upstream ships as a container image and this host has no container runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import threading
import time
import uuid

import psycopg
import uvicorn
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from services.control_plane import api_keys, config, crypto, db, identity, workers
from services.gateway.app import Gateway, create_app

BENCH_PASSWORD = "bench-only-throwaway-account"  # noqa: S105

SOCKETS = 200
GATEWAY_PORT = 28998
FRAME_SAMPLES = 100


def _rss_bytes() -> int:
    """This process's resident set, from /proc. No dependency on psutil."""
    with open("/proc/self/status") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def _seed(settings, key_ring) -> tuple[str, str]:
    ref = f"bs{uuid.uuid4().hex[:6]}"
    project_id = uuid.uuid4()
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}@example.com", password=BENCH_PASSWORD
        )
        plan = db.one(
            conn,
            # Generous, for the reason the request benchmark records: without it
            # this measures the cost of being refused rather than the cost of
            # being served.
            "INSERT INTO plans (code,name,config_json) VALUES (%s,'Bench',%s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
            (f"plan-{ref}", psycopg.types.json.Jsonb({"limits": {"realtime_connections": 100_000}})),
        )["id"]
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
            " database_name, realtime_enabled) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,TRUE)",
            (project_id, org, ref, ref, plan, f"mldb_{ref}"),
        )
        workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
        issued = api_keys.create(
            conn, project_id=project_id, key_type=api_keys.SECRET, pepper=settings.token_pepper
        )
        conn.commit()
    return ref, issued.plaintext


def _start_upstream() -> int:
    """An echo server on its own thread, doing as little as possible."""
    port_holder: dict[str, int] = {}
    started = threading.Event()

    async def handler(connection):
        async for message in connection:
            await connection.send(message)

    async def main():
        async with serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
            port_holder["port"] = server.sockets[0].getsockname()[1]
            started.set()
            await asyncio.Future()

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())

    threading.Thread(target=run, daemon=True).start()
    started.wait(5)
    return port_holder["port"]


async def _measure(ref: str, key: str, domain: str, count: int) -> dict:
    # The project hostname goes in the URI, not in additional_headers. Passing
    # it as a header appends a *second* Host, and the gateway reads the first --
    # which is the loopback address, so routing refuses it. The same trap the
    # gateway's own upstream connection fell into; see `sockets.open_upstream`.
    url = f"ws://{ref}.{domain}/realtime/v1/websocket?vsn=1.0.0&apikey={key}"
    handshakes: list[float] = []
    open_sockets = []

    async def open_one():
        return await connect(url, host="127.0.0.1", port=GATEWAY_PORT, ping_interval=None)

    # One socket first, so the key cache and the JWT secret are warm and the
    # measured handshakes are not paying for a database round trip the rest
    # will not.
    warm = await open_one()
    await warm.close()

    baseline = _rss_bytes()
    for _ in range(count):
        started = time.perf_counter()
        socket = await open_one()
        handshakes.append((time.perf_counter() - started) * 1000)
        open_sockets.append(socket)

    held = _rss_bytes()

    # Round trip on an established socket, which is what a subscriber feels.
    round_trips: list[float] = []
    probe = open_sockets[0]
    for i in range(FRAME_SAMPLES):
        started = time.perf_counter()
        await probe.send(f"ping-{i}")
        await probe.recv()
        round_trips.append((time.perf_counter() - started) * 1000)

    for socket in open_sockets:
        await socket.close()
    # Let the gateway's own cleanup run before reading memory back.
    await asyncio.sleep(1.0)
    released = _rss_bytes()

    handshakes.sort()
    round_trips.sort()
    return {
        "sockets": count,
        "rss_before_bytes": baseline,
        "rss_holding_bytes": held,
        "rss_after_close_bytes": released,
        # The figure capacity planning needs. Measured in this process, which
        # holds both ends -- see the caveat printed below.
        "bytes_per_socket_both_ends": round((held - baseline) / count),
        "handshake_p50_ms": round(statistics.median(handshakes), 2),
        "handshake_p95_ms": round(handshakes[int(len(handshakes) * 0.95)], 2),
        "frame_round_trip_p50_ms": round(statistics.median(round_trips), 3),
        "frame_round_trip_p95_ms": round(round_trips[int(len(round_trips) * 0.95)], 3),
    }


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else SOCKETS
    settings = config.load()
    db.init_pool(settings.database_url)
    key_ring = crypto.KeyRing(settings.kek)
    with db.connection() as conn:
        key_ring.load(conn)

    upstream_port = _start_upstream()
    ref, key = _seed(settings, key_ring)

    import dataclasses

    gateway = Gateway(
        config=dataclasses.replace(settings, realtime_port=upstream_port),
        key_ring=key_ring, wake_sleeping=False,
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(gateway), host="127.0.0.1", port=GATEWAY_PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    result = asyncio.run(_measure(ref, key, settings.gateway_domain, count))
    print(json.dumps(result, indent=2))

    per_socket_kb = result["bytes_per_socket_both_ends"] / 1024
    print(
        f"\n{count} concurrent Realtime sockets held {per_socket_kb:.1f} kB each of RSS.\n"
        "That figure covers BOTH ends -- the benchmark client and the gateway share this\n"
        "process -- so the gateway's own share is smaller, and this is an upper bound.\n"
        f"Handshake cost {result['handshake_p50_ms']} ms at p50; once open, a frame round\n"
        f"trips in {result['frame_round_trip_p50_ms']} ms at p50.\n"
        f"RSS after closing all of them: {result['rss_after_close_bytes'] / 1024 / 1024:.1f} MB "
        f"(held: {result['rss_holding_bytes'] / 1024 / 1024:.1f} MB)."
    )
    print(
        "\nWhat this does NOT measure: what a Realtime *server* process costs. The upstream\n"
        "here is an echo server. ADR-022 still has no Realtime density term."
    )

    with db.connection() as conn:
        db.execute(conn, "DELETE FROM api_keys WHERE project_id IN "
                         "(SELECT id FROM projects WHERE project_ref = %s)", (ref,))
        db.execute(conn, "DELETE FROM project_credentials WHERE project_id IN "
                         "(SELECT id FROM projects WHERE project_ref = %s)", (ref,))
        db.execute(conn, "DELETE FROM projects WHERE project_ref = %s", (ref,))
        conn.commit()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("MALUDB_ENV", "development")
    raise SystemExit(main())
