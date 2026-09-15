"""The egress proxy: three hosts, and nothing else (ADR-079, memory slice 5b).

ADR-079 decision 6 puts the memory worker's outbound access in deployment rather
than in code, and this proxy is that deployment. So its refusals are the property:
another verb, another host, another port, an address literal, and -- the one that
matters most on a node -- an allowed name that resolves to a private address.

Driven over real sockets on loopback. The resolver is replaced so no test reaches
the internet, and a tunnel that is allowed is shown to carry bytes both ways to a
local stand-in for the provider.
"""

from __future__ import annotations

import asyncio

import pytest

from services.control_plane import egress_proxy
from services.control_plane.egress_proxy import EgressProxy


async def _exchange(proxy: EgressProxy, head: bytes, payload: bytes | None = None) -> tuple[bytes, bytes]:
    server = await proxy.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(head)
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        echoed = b""
        if payload is not None and status.startswith(b"HTTP/1.1 200"):
            writer.write(payload)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(payload)), 5)
        writer.close()
        return status, echoed
    finally:
        server.close()
        await server.wait_closed()


def _proxy(addresses=("93.184.216.34",), connect=None) -> EgressProxy:
    async def resolve(_host, _port):
        return list(addresses)

    async def refuse_connect(*_args):
        raise AssertionError("a refused request must never open an upstream connection")

    return EgressProxy(resolve=resolve, connect=connect or refuse_connect)


@pytest.mark.parametrize(("head", "status"), [
    (b"GET http://api.openai.com/ HTTP/1.1\r\nHost: api.openai.com\r\n\r\n", b"405"),
    (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"403"),
    (b"CONNECT api.openai.com:80 HTTP/1.1\r\n\r\n", b"403"),
    (b"CONNECT api.openai.com.evil.test:443 HTTP/1.1\r\n\r\n", b"403"),
    (b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n", b"403"),
    (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", b"403"),
    (b"CONNECT user@api.openai.com:443 HTTP/1.1\r\n\r\n", b"403"),
])
def test_anything_but_a_connect_to_an_allowed_host_on_443_is_refused(head, status):
    answer, _ = asyncio.run(_exchange(_proxy(), head))
    assert answer.split(b" ")[1] == status, answer


@pytest.mark.parametrize("addresses", [("10.0.0.5",), ("127.0.0.1",), ("93.184.216.34", "192.168.1.9"),
                                       ("169.254.169.254",), ("::ffff:10.1.2.3",), ("fd00::1",)])
def test_an_allowed_name_that_resolves_to_a_private_address_is_refused(addresses):
    answer, _ = asyncio.run(_exchange(_proxy(addresses), b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n"))
    assert answer.startswith(b"HTTP/1.1 403"), answer


def test_an_allowed_tunnel_connects_to_the_address_it_checked_and_carries_bytes_both_ways():
    dialled = []

    async def run():
        async def echo(reader, writer):
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()

        provider = await asyncio.start_server(echo, "127.0.0.1", 0)
        provider_port = provider.sockets[0].getsockname()[1]

        async def connect(address, port):
            dialled.append((address, port))
            return await asyncio.open_connection("127.0.0.1", provider_port)

        try:
            return await _exchange(_proxy(connect=connect), b"CONNECT API.OpenAI.com.:443 HTTP/1.1\r\n\r\n",
                                   payload=b"tls bytes")
        finally:
            provider.close()

    answer, echoed = asyncio.run(run())
    assert answer.startswith(b"HTTP/1.1 200") and echoed == b"tls bytes"
    assert dialled == [("93.184.216.34", 443)], "the proxy dials the address it checked, not the name again"


def test_a_request_head_larger_than_the_limit_is_dropped_without_an_answer():
    async def run():
        server = await _proxy().start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT api.openai.com:443 HTTP/1.1\r\nX: " + b"a" * (egress_proxy.MAX_HEAD_BYTES * 2))
            await writer.drain()
            return await asyncio.wait_for(reader.read(), 5)
        finally:
            server.close()

    assert asyncio.run(run()) == b""


@pytest.mark.parametrize("value", ["0.0.0.0:3128", "10.0.0.1:3128", "[::]:3128", "3128"])
def test_it_refuses_to_listen_anywhere_but_loopback(value):
    with pytest.raises(SystemExit):
        egress_proxy.listen_address(value)


def test_loopback_listen_addresses_are_accepted():
    assert egress_proxy.listen_address("127.0.0.1:3128") == ("127.0.0.1", 3128)
    assert egress_proxy.listen_address("[::1]:3128") == ("::1", 3128)
