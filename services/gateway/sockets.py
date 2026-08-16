"""Proxying a WebSocket, which the gateway had never done before Phase 06.

Everything the gateway proxied until now was request/response: read a body,
make one upstream call, return one response. A Realtime connection is a
long-lived bidirectional stream, and almost nothing about the HTTP path carries
over -- not the timeout, not the body limit, not the retry-once-on-failure
behaviour, and not the cost profile ADR-026 measured to justify Python in the
data path.

So the plumbing lives here, apart from the request path, and `app.py` keeps the
part that is a security property: the order in which a connection is
authenticated before any of this runs.

Three things this deliberately does:

- **Refuses before accepting.** A denied connection is closed during the
  handshake, so a caller who fails authentication never holds an open socket.
  Accepting first and closing after would be simpler and would mean every
  rejected caller still cost a connection.
- **Bounds message size in both directions.** The HTTP path has
  `MAX_BODY_BYTES`; without an equivalent here, one client frame is an
  unbounded allocation.
- **Copies nothing it was not asked to.** Frames are forwarded verbatim, in
  both directions, without being parsed. The gateway is not a Phoenix client
  and should not learn to be one -- interpreting the channel protocol would put
  a parser for attacker-controlled input on the path that authenticates it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

log = logging.getLogger(__name__)

# The analogue of MAX_BODY_BYTES. Phoenix channel frames are small -- a join, a
# heartbeat, a change payload -- so this is generous rather than tight, and it
# exists to bound an allocation rather than to shape traffic.
MAX_MESSAGE_BYTES = 1 * 1024 * 1024

# How long the upstream handshake may take. Short: the Realtime server is on
# this node, so a slow handshake means it is unhealthy, and making a client wait
# 30 seconds to be told that helps nobody.
UPSTREAM_CONNECT_TIMEOUT = 10.0

# Close codes. 1008 (policy violation) is used for every pre-authentication
# refusal without exception, so the code cannot be used to tell "wrong domain"
# from "no such project" from "wrong key" -- the same uniformity the HTTP path
# gets from answering 401 to all of them.
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_UPSTREAM_UNAVAILABLE = 1011
# 1013, "try again later". Sent when a project's Realtime instance is asleep:
# the wake starts in the background and the client is asked to come back rather
# than held for the nine seconds a boot takes -- longer than the official
# client's own connection timeout, so holding it fails the same connection more
# slowly. phoenix.js reconnects on a close with backoff, which is what makes
# this the cooperative answer rather than a refusal.
CLOSE_TRY_AGAIN = 1013
# Application codes, used only *after* the caller has proved it holds a key for
# this project. At that point naming the reason is help rather than an oracle.
CLOSE_NOT_ENABLED = 4004
CLOSE_LIMIT = 4029


class UpstreamUnavailable(RuntimeError):
    """The node's Realtime server could not be reached."""


async def open_upstream(
    *, host: str, port: int, path: str, query: str, headers: dict[str, str],
    subprotocols: list[str] | None,
):
    """Connect to the node's Realtime server, as the project's own hostname.

    Two separate things, and conflating them is a bug worth naming because this
    code had it: **the name in the request and the address it is sent to.**

    Upstream Realtime identifies a tenant from the `Host` header, so the request
    must carry `<ref>.<gateway_domain>`. The connection must nevertheless go to
    loopback, because nothing about a client's request may choose where the
    gateway connects -- the same rule the HTTP proxy follows.

    The obvious way to do that -- putting `Host` in `additional_headers` --
    silently produces a request with **two** `Host` headers, since the library
    already derives one from the URI. That is a protocol violation, and worse,
    it makes the tenant-identifying header ambiguous: an upstream reading the
    first sees `127.0.0.1`, one reading the last sees the project. So the
    hostname goes in the URI and `host`/`port` redirect the socket, which is the
    same separation `curl --resolve` makes.
    """
    uri = f"ws://{host}{path}"
    if query:
        uri = f"{uri}?{query}"
    try:
        return await ws_connect(
            uri,
            host="127.0.0.1",
            port=port,
            additional_headers=headers,
            subprotocols=subprotocols or None,
            open_timeout=UPSTREAM_CONNECT_TIMEOUT,
            max_size=MAX_MESSAGE_BYTES,
            # The gateway does its own liveness through the client's own
            # heartbeats, which Phoenix sends anyway; a second ping loop would
            # keep a socket alive that the client had already abandoned.
            ping_interval=None,
        )
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
        # Never surface the driver's message: it names the internal host and
        # port, which docs/API-GATEWAY.md forbids exposing. It is logged at
        # debug on the node, where it is the only thing that distinguishes "the
        # server is not up" from "the server refused this handshake" -- a
        # distinction that cost a debugging round when only the class name was
        # kept.
        log.debug("realtime upstream handshake failed: %s: %s", type(exc).__name__, exc)
        raise UpstreamUnavailable(type(exc).__name__) from None


def rewrite_access_token(payload: str, *, presented: str, mint) -> str:
    """Swap the customer's opaque key for a JWT inside a channel-join frame.

    The one place the gateway looks inside a frame, and it is not optional.
    `@supabase/supabase-js` sends its key **twice**: in the query string, which
    this gateway already replaces, and again as `access_token` in the payload of
    every `phx_join`. On Supabase both are the same value and both are a JWT --
    the anon key *is* a signed token there. ADR-028 made MaluDB's keys opaque
    instead, so the copy inside the frame reaches upstream unmodified and the
    server answers `MalformedJWT: The token provided is not a valid JWT`. The
    socket connects, every channel fails, and the client reports a channel
    error with nothing in it about keys.

    Found by the compatibility suite and by nothing else, which is the argument
    `AGENTS.md` makes for testing with the official client.

    Deliberately narrow, because slice 3's rule -- that the gateway is not a
    Phoenix client and should not learn to be one -- is still right:

    - only a frame that parses as JSON is touched, and anything unparseable is
      forwarded byte for byte;
    - only the exact string the caller authenticated with is replaced, so a
      frame carrying a real end-user JWT passes through untouched;
    - a fresh token is minted per frame rather than reusing the connection's,
      which also fixes an expiry the socket would otherwise outlive: a channel
      joined an hour into a connection gets a token minted now.

    Both Phoenix serialisers are handled. v1 sends an object; v2 sends
    `[join_ref, ref, topic, event, payload]`, which is what the official client
    uses by default and what a naive object-only implementation would silently
    miss.
    """
    if presented not in payload:
        # The overwhelmingly common case -- a heartbeat, an ack, an ordinary
        # message -- costs one substring search and no parsing.
        return payload
    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        return payload

    if isinstance(decoded, dict):
        body = decoded.get("payload")
    elif isinstance(decoded, list) and len(decoded) == 5:
        body = decoded[4]
    else:
        return payload

    if not isinstance(body, dict) or body.get("access_token") != presented:
        return payload
    body["access_token"] = mint()
    return json.dumps(decoded)


async def pump(client: WebSocket, upstream: Any, *, rewrite=None) -> None:
    """Forward frames both ways until either side goes away.

    Both directions run as tasks and the first to finish cancels the other,
    because a socket is only closed when *both* halves are: leaving the
    upstream-to-client task running after the client has gone would hold the
    upstream connection open for a client that no longer exists, which is how a
    connection-slot leak becomes permanent.
    """
    to_upstream = asyncio.create_task(_client_to_upstream(client, upstream, rewrite))
    to_client = asyncio.create_task(_upstream_to_client(client, upstream))
    done, pending = await asyncio.wait(
        {to_upstream, to_client}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    # Await the cancelled tasks so their cleanup runs before the caller closes
    # anything, and so an exception raised during cancellation is not reported
    # as "task exception was never retrieved".
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        exc = task.exception()
        if exc is not None and not isinstance(exc, WebSocketDisconnect | websockets.exceptions.ConnectionClosed):
            raise exc


async def _client_to_upstream(client: WebSocket, upstream: Any, rewrite=None) -> None:
    while True:
        try:
            message = await client.receive()
        except WebSocketDisconnect:
            return
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return
        payload = message.get("text")
        if payload is None:
            payload = message.get("bytes")
        if payload is None:
            continue
        if len(payload) > MAX_MESSAGE_BYTES:
            # Refused here rather than forwarded: the point of the bound is that
            # the gateway does not hand an unbounded frame to anything else.
            await client.close(code=CLOSE_TOO_BIG)
            return
        if rewrite is not None and isinstance(payload, str):
            # Bounded before parsing, and only text frames: the size check above
            # is what keeps this from being an unbounded parse of
            # attacker-controlled input.
            payload = rewrite(payload)
        await upstream.send(payload)


async def _upstream_to_client(client: WebSocket, upstream: Any) -> None:
    async for message in upstream:
        if isinstance(message, str):
            await client.send_text(message)
        else:
            await client.send_bytes(message)
