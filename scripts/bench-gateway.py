#!/usr/bin/env python
"""Measure gateway throughput and added latency.

ADR-026 accepted Python in the data path as an MVP decision **on condition that
a measured number is recorded**, so the choice can be revisited on evidence
rather than instinct. This is that measurement, kept in the repository so the
next person can re-run it rather than trust a number in a commit message.

It measures the gateway's *overhead*: the same upstream is driven directly and
then through the gateway, and the difference is what the proxy costs. Absolute
numbers depend on the machine; the ratio is the part that travels.

Phase 10 slice 4 added a second question to the same script. ADR-056 puts egress
accounting on this path and says in as many words that the regression is
measured rather than asserted, so the storage surface is driven twice against
the same stub -- once with the meter that counts and flushes, once with one that
does nothing -- and the difference is what the accounting costs.

    MALUDB_CONTROL_PLANE_DATABASE_URL=... MALUDB_KEK_REF=... \\
    MALUDB_TOKEN_PEPPER_REF=... python scripts/bench-gateway.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import statistics
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import psycopg
import uvicorn

from services.control_plane import api_keys, config, crypto, db, identity, workers
from services.gateway import limits
from services.gateway.app import Gateway, create_app

BENCH_PASSWORD = "bench-only-throwaway-account"  # noqa: S105

REQUESTS = 500
CONCURRENCY = 20
GATEWAY_PORT = 28999


class _Upstream(BaseHTTPRequestHandler):
    """Stands in for PostgREST, doing as little as possible so the number
    reflects the gateway rather than the thing behind it."""

    def do_GET(self):  # noqa: N802
        payload = b'[{"id":1}]'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _NoMeter(limits.EgressMeter):
    """The storage path with the accounting removed, for comparison only.

    Not a configuration option: there is no supported way to run the gateway
    without counting egress, because a ceiling nobody counts against is not a
    ceiling. This exists so the cost of counting can be named.
    """

    def used(self, conn, *, project_id) -> int:
        return 0

    def add(self, project_id, bytes_served: int) -> None:
        return None

    def flush_due(self) -> bool:
        return False


def _seed(settings, key_ring, upstream_port: int) -> tuple[str, str]:
    ref = f"bn{uuid.uuid4().hex[:6]}"
    project_id = uuid.uuid4()
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}@example.com", password=BENCH_PASSWORD
        )
        plan = db.one(
            conn,
            # A deliberately huge allowance. Without it the benchmark measures
            # the cost of being refused rather than the cost of being served:
            # the free default is 300 requests a minute, and the first run of
            # this script after the limiter landed served 327 of 500 and
            # reported a latency that was mostly rejections.
            "INSERT INTO plans (code,name,config_json) VALUES (%s,'Bench',%s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
            (f"plan-{ref}", psycopg.types.json.Jsonb(
                {"limits": {"api_requests_per_window": 1_000_000,
                            "concurrent_api_requests": 1_000}}
            )),
        )["id"]
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
            "database_name, api_port, worker_state, storage_registered_at) "
            "VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,'RUNNING', now())",
            (project_id, org, ref, ref, plan, f"mldb_{ref}", upstream_port),
        )
        workers.ensure_jwt_secret(conn, project_id=project_id, key_ring=key_ring)
        issued = api_keys.create(
            conn, project_id=project_id, key_type=api_keys.SECRET, pepper=settings.token_pepper
        )
        conn.commit()
    return ref, issued.plaintext


async def _drive(url: str, headers: dict, label: str, concurrency: int = CONCURRENCY) -> dict:
    latencies: list[float] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=30) as client:
        async def one():
            async with semaphore:
                started = time.perf_counter()
                response = await client.get(url, headers=headers)
                latencies.append((time.perf_counter() - started) * 1000)
                return response.status_code

        # Warm the connection pool and the key cache before measuring.
        await asyncio.gather(*(one() for _ in range(concurrency)))
        latencies.clear()

        started = time.perf_counter()
        statuses = await asyncio.gather(*(one() for _ in range(REQUESTS)))
        elapsed = time.perf_counter() - started

    ok = sum(1 for s in statuses if s == 200)
    latencies.sort()
    return {
        "label": label,
        "concurrency": concurrency,
        "requests": REQUESTS,
        "ok": ok,
        "rps": round(REQUESTS / elapsed, 1),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(latencies[int(len(latencies) * 0.95)], 2),
    }


def main() -> int:
    settings = config.load()
    db.init_pool(settings.database_url)
    key_ring = crypto.KeyRing(settings.kek)
    with db.connection() as conn:
        key_ring.load(conn)

    # Threading, not the single-threaded HTTPServer: with a serial upstream
    # the measurement is of the stub queueing, and the gateway looks slow for
    # a reason that has nothing to do with it.
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    ref, key = _seed(settings, key_ring, upstream.server_port)

    # The node is prepared for Storage and its worker is the same stub, so the
    # two surfaces are measured against identical work behind the gateway.
    settings = dataclasses.replace(
        settings,
        storage_port=upstream.server_port,
        storage_s3_endpoint=settings.storage_s3_endpoint or "http://10.91.0.1:8333",
        storage_s3_access_key=settings.storage_s3_access_key or "bench",
        storage_s3_secret_key=settings.storage_s3_secret_key or "bench",
        storage_db_host=settings.storage_db_host or "10.91.0.1",
    )
    gateway = Gateway(config=settings, key_ring=key_ring, wake_sleeping=False)
    server = uvicorn.Server(
        uvicorn.Config(create_app(gateway), host="127.0.0.1", port=GATEWAY_PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    upstream_url = f"http://127.0.0.1:{upstream.server_port}/rest/v1/things"
    gateway_url = f"http://127.0.0.1:{GATEWAY_PORT}/rest/v1/things"
    gateway_headers = {"host": f"{ref}.{settings.gateway_domain}", "apikey": key}

    # Sequential first. This is the number that means something: with one
    # request in flight nothing queues, so the difference is the gateway's own
    # cost rather than a property of whatever is standing in for PostgREST.
    direct_1 = asyncio.run(_drive(upstream_url, {}, "upstream direct", concurrency=1))
    through_1 = asyncio.run(_drive(gateway_url, gateway_headers, "through gateway", concurrency=1))

    # Then concurrent, which says more about the stub than about the gateway --
    # a thread-per-request Python upstream saturates long before either does --
    # so it is reported as a floor, not as a throughput measurement.
    direct_n = asyncio.run(_drive(upstream_url, {}, "upstream direct", concurrency=CONCURRENCY))
    through_n = asyncio.run(_drive(gateway_url, gateway_headers, "through gateway", concurrency=CONCURRENCY))

    # And the storage surface: the same stub, the same gateway, reached through
    # the path that counts egress. Driven with the real meter and then with one
    # that does nothing, so the difference is the accounting rather than the
    # difference between two surfaces.
    storage_url = f"http://127.0.0.1:{GATEWAY_PORT}/storage/v1/object/bench/a.txt"
    counted_1 = asyncio.run(_drive(storage_url, gateway_headers, "storage counted", concurrency=1))
    counted_n = asyncio.run(
        _drive(storage_url, gateway_headers, "storage counted", concurrency=CONCURRENCY)
    )
    gateway.egress = _NoMeter()
    uncounted_1 = asyncio.run(
        _drive(storage_url, gateway_headers, "storage uncounted", concurrency=1)
    )
    uncounted_n = asyncio.run(
        _drive(storage_url, gateway_headers, "storage uncounted", concurrency=CONCURRENCY)
    )

    for row in (direct_1, through_1, direct_n, through_n,
                counted_1, uncounted_1, counted_n, uncounted_n):
        print(json.dumps(row))
    added = through_1["p50_ms"] - direct_1["p50_ms"]
    print(
        f"\nadded latency per request: {added:+.2f} ms at p50 "
        f"({direct_1['p50_ms']:.2f} -> {through_1['p50_ms']:.2f} ms), measured sequentially."
    )
    print(
        f"under {CONCURRENCY} concurrent: {through_n['rps']} rps through the gateway "
        f"vs {direct_n['rps']} rps direct. Both are bounded by the stub upstream, "
        "so treat this as a floor."
    )
    accounting = counted_1["p50_ms"] - uncounted_1["p50_ms"]
    print(
        f"egress accounting (ADR-056): {accounting:+.2f} ms at p50 "
        f"({uncounted_1['p50_ms']:.2f} -> {counted_1['p50_ms']:.2f} ms) sequentially, "
        f"and {counted_n['rps']} vs {uncounted_n['rps']} rps under {CONCURRENCY} concurrent."
    )

    with db.connection() as conn:
        db.execute(conn, "DELETE FROM project_egress WHERE project_id IN "
                         "(SELECT id FROM projects WHERE project_ref = %s)", (ref,))
        db.execute(conn, "DELETE FROM api_keys WHERE project_id IN "
                         "(SELECT id FROM projects WHERE project_ref = %s)", (ref,))
        db.execute(conn, "DELETE FROM project_credentials WHERE project_id IN "
                         "(SELECT id FROM projects WHERE project_ref = %s)", (ref,))
        db.execute(conn, "DELETE FROM projects WHERE project_ref = %s", (ref,))
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
