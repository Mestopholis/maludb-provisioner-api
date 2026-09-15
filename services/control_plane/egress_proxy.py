"""The egress proxy: the memory worker's only way to the internet (ADR-079, memory slice 5b).

ADR-079 decision 6 gives the memory worker outbound access to three hosts and no
others, "enforced in deployment, not only in code". systemd cannot allow a
hostname, and an address allowlist would allow every other site sharing a
provider's CDN address, so the worker's unit keeps `IPAddressDeny=any` and this
process -- its own unit, its own user, nothing else in it -- is where the three
hosts are enforced.

**What it accepts:** an HTTP `CONNECT` to exactly `api.openai.com`,
`api.anthropic.com` or `api.voyageai.com`, port 443. Anything else -- another
verb, another host, another port, an address literal, a request head over 8 KiB
or slower than 10 seconds -- is answered with an error and closed.

**What it checks after that:** it resolves the name itself and refuses when any
address is not globally routable, then connects to the address it checked
rather than resolving again. A poisoned or rebound name therefore cannot turn
the proxy toward the node's private network.

**What it never sees:** the traffic. The tunnel carries TLS the worker
negotiates with the provider, so the proxy holds no key and could not read one.

It listens on loopback only and refuses to start otherwise: on any other
interface it would offer these three hosts to whatever can reach it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import signal
import socket

from services.control_plane import logging as cp_logging

log = logging.getLogger("maludb.egress_proxy")

ALLOWED_HOSTS = frozenset({"api.openai.com", "api.anthropic.com", "api.voyageai.com"})
ALLOWED_PORT = 443
MAX_HEAD_BYTES = 8 * 1024
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 300.0
DEFAULT_LISTEN = "127.0.0.1:3128"


def address_allowed(address: str) -> bool:
    """Globally routable, and not one of the ranges `is_global` still admits."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not parsed.is_multicast


def parse_authority(target: str) -> tuple[str, int] | None:
    """`host:port` from a CONNECT target, or None for anything but a plain name."""
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or not host or "@" in host or "[" in host or "/" in host:
        return None
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host, int(port)


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class EgressProxy:
    def __init__(self, *, allowed_hosts=ALLOWED_HOSTS, allowed_port: int = ALLOWED_PORT, resolve=_resolve,
                 allowed_address=address_allowed, connect=asyncio.open_connection):
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allowed_port = allowed_port
        self._resolve = resolve
        self._allowed_address = allowed_address
        self._connect = connect

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD_BYTES)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except Exception:  # noqa: BLE001 - one connection must never stop the proxy
            log.exception("egress proxy connection failed")
        finally:
            writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, status: str, reason: str, target: str | None) -> None:
        log.warning("egress refused: %s (%r)", reason, (target or "")[:100])
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
            await self._refuse(writer, "405 Method Not Allowed", "not a CONNECT", request_line)
            return
        authority = parse_authority(parts[1])
        if authority is None or authority[0] not in self.allowed_hosts or authority[1] != self.allowed_port:
            await self._refuse(writer, "403 Forbidden", "host not allowed", parts[1])
            return
        host, port = authority
        try:
            addresses = await asyncio.wait_for(self._resolve(host, port), CONNECT_TIMEOUT)
        except (OSError, TimeoutError):
            await self._refuse(writer, "502 Bad Gateway", "name did not resolve", host)
            return
        if not addresses or not all(self._allowed_address(a) for a in addresses):
            await self._refuse(writer, "403 Forbidden", "name resolved to a non-public address", host)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._connect(addresses[0], port), CONNECT_TIMEOUT)
        except (OSError, TimeoutError):
            await self._refuse(writer, "502 Bad Gateway", "provider unreachable", host)
            return
        log.info("egress tunnel to %s", host)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await _pipe(reader, writer, upstream_reader, upstream_writer)
        finally:
            upstream_writer.close()


async def _pipe(a_reader, a_writer, b_reader, b_writer) -> None:
    async def copy(source, sink):
        while True:
            chunk = await asyncio.wait_for(source.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            sink.write(chunk)
            await sink.drain()
        if sink.can_write_eof():
            sink.write_eof()

    tasks = [asyncio.create_task(copy(a_reader, b_writer)), asyncio.create_task(copy(b_reader, a_writer))]
    try:
        await asyncio.gather(*tasks)
    except (OSError, TimeoutError, ConnectionError):
        pass
    finally:
        for task in tasks:
            task.cancel()


def listen_address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    try:
        loopback = ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        loopback = False
    if not port.isdigit() or not loopback:
        raise SystemExit(f"MALUDB_EGRESS_PROXY_LISTEN must be a loopback address and port, got {value!r}")
    return host.strip("[]"), int(port)


async def _serve(host: str, port: int) -> None:
    server = await EgressProxy().start(host, port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    log.info("egress proxy listening on %s:%s for %s", host, port, ", ".join(sorted(ALLOWED_HOSTS)))
    async with server:
        await stop.wait()


def main() -> int:
    cp_logging.configure()
    host, port = listen_address(os.environ.get("MALUDB_EGRESS_PROXY_LISTEN", DEFAULT_LISTEN))
    asyncio.run(_serve(host, port))
    return 0


__all__ = ["ALLOWED_HOSTS", "EgressProxy", "address_allowed", "listen_address", "parse_authority"]


if __name__ == "__main__":
    raise SystemExit(main())
