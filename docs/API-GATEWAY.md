# API Gateway and Project Routing

## Public routing

Target:

```text
https://<project-ref>.maludb.com/rest/v1/...
https://<project-ref>.maludb.com/auth/v1/...
https://<project-ref>.maludb.com/realtime/v1/...
https://<project-ref>.maludb.com/storage/v1/...
```

## Request flow

1. Parse and validate project ref from hostname.
2. Lookup project routing/key metadata from cache.
3. Reject non-active projects as appropriate.
4. Validate API key.
5. Ensure key belongs to the hostname project.
6. Apply plan-level API controls.
7. Resolve downstream project worker.
8. Start sleeping free worker if policy permits.
9. Proxy the request.
10. Record metrics/usage.

## API-key cache

Do not query the control-plane SQL database on every API request.

Expected model:

```text
gateway local cache / distributed cache
           |
       cache miss
           v
     control-plane DB
```

Revocation/update paths must invalidate cached material quickly.

## Requests that carry no API key

One exception to "validate the API key on every request": a link followed from
an email. A browser navigating to a confirmation or password-reset URL sends no
`apikey` header, so requiring one answers 401 for every confirmation on the
platform.

`/auth/v1/verify` is therefore reachable without a key. The credential on those
requests is the single-use token in the query string, which GoTrue verifies.

The exemption is an **exact path, not a prefix**, and everything else still
applies: the project still comes from the hostname, a project that is not
serving still refuses, and no `Authorization` is forwarded — minting a
`service_role` token for an anonymous link-follower would hand admin rights to
anyone holding a confirmation URL.

There is a second, added in Phase 10 slice 4 and the same shape: **a `GET` or
`HEAD` under `/storage/v1/object/public/`**. `getPublicUrl` produces a URL with
no key in it, and a browser following one sends an `apikey` header for nobody.

Read-only, deliberately. The prefix on its own would let anyone who knows a
project hostname `DELETE` from a public bucket on the assumption that upstream
refuses it for want of a token — and a gateway that relies on what is behind it
to make up for what it let through is one upstream default away from a hole.
The bytes are counted against the project's egress either way (ADR-056): they
are the project's whether or not a key was presented, and this path is where a
free project's egress actually goes.

## The Storage surface is one shared worker per node

`/storage/v1` is proxied to `storage-api`, and ADR-058 makes that **one
container serving every tenant on the node** rather than one per project. Four
consequences, none of which the other surfaces have:

- **There is no port to look up, nothing to wake and no activity clock.** The
  upstream is the node's own `MALUDB_STORAGE_PORT`. Sleeping it would sleep
  every tenant on the node, so it is not modelled as a per-project worker at
  all.
- **The tenant is named by a header the gateway sets.** `storage-api` resolves
  a tenant from `X-Forwarded-Host`, matched against a pattern built from the
  project-ref format. The gateway sets it from the hostname it already
  authenticated, and the client's own copy is dropped on the way in — that drop
  is the whole tenancy control on this surface, because a forwarded host the
  caller could choose is a tenant the caller could choose.
- **A project is registered with the worker on demand.** Provisioning registers
  it and treats a failure as a delay rather than a failed project; the first
  Storage request registers one that is not, so a container that was down when
  a project was created costs a delay rather than a broken surface.
- **Two ceilings are enforced here** (ADR-056, ADR-060): egress over
  `egress_bytes_per_month` answers 429 with `Retry-After` to the month
  boundary, and a project over `object_storage_bytes` is refused uploads with
  413. Reads, lists and deletes are never refused for a full project — they are
  the only way back under the ceiling.

Egress is counted in the gateway process and flushed in batches, which is what
keeps it off the measured latency path; the cost is measured in ADR-056 rather
than asserted.

## The Realtime surface is a WebSocket, and that changes six things

`/realtime/v1` is served over a socket only. A plain HTTP request to it still
answers 404, which is correct: a client that did not upgrade has not asked for
anything the platform can answer.

The authentication *order* is identical to the request path — project from the
hostname first, key checked against that project — because that is the property
ADR-008 exists for and it does not become less true over a socket. Six things
around it do differ, and each is a decision rather than an accident.

**The key arrives in the query string.** A browser cannot set headers on a
WebSocket handshake: the browser API takes a URL and an optional subprotocol
list and nothing else. So supabase-js connects to
`/realtime/v1/websocket?apikey=<key>&vsn=1.0.0`, and a gateway that demanded a
header would work from Node and fail from every browser. Header forms are still
accepted for server-side clients. The cost is real — a key in a query string is
a key in proxy logs and browser history — and it is an argument for keys that
can be revoked, which ADR-028's are.

**Refusal happens before the socket is accepted.** A denied connection is closed
during the handshake, so a caller that fails authentication never holds an open
socket. Every pre-authentication refusal uses close code 1008 without exception,
which is the socket's version of the uniform 401: a distinguishable rejection is
an oracle for which refs exist and which keys are live. After a caller has proved
it holds a key for the project, named codes are used — 4004 for a project without
Realtime enabled, 4029 for one at its connection limit — because at that point
naming the reason is help rather than an oracle.

**Upstream headers are an allowlist, not a filtered copy.** The handshake carries
`Sec-WebSocket-Key` and friends, which the client library regenerates for its own
connection and which would collide if forwarded. `Host` is the one that matters:
upstream Realtime identifies a tenant from the subdomain, so the gateway rebuilds
it from the validated project ref rather than passing the client's through, and
it travels in the connection URI rather than as an extra header — passing it as a
header appends a *second* `Host` and leaves the tenant-identifying header
ambiguous.

**Connections are counted, not rated.** A socket is not a request: one held open
for an hour spends a single token and then costs nothing, and a client that
reconnects on every network blip burns tokens for reasons unrelated to load. So
Realtime has its own limiter over `realtime_connections`, which refuses a limit
of zero rather than failing open — zero is the free tier, not a misconfiguration.

**One frame is read rather than forwarded.** The gateway is not a Phoenix client
and does not want to become one, but the official client sends its API key
*twice*: in the query string, which the gateway replaces with a minted JWT, and
again as `access_token` in the payload of every channel join. On Supabase the
anon key is itself a JWT and both work; ADR-028's keys are opaque, so the copy
inside the frame reaches upstream and every channel fails with `MalformedJWT`.
The gateway therefore parses that one frame and replaces that one field
(ADR-036), leaving anything unparseable, anything that is not a join, and any
end-user JWT byte for byte as it arrived.

**A sleeping project is asked to come back.** Realtime instances sleep when idle
and take about nine seconds to wake, which is longer than the ten the official
client waits before abandoning a connection. So the gateway closes with 1013,
starts the wake in the background — once per project, however many clients ask —
and lets the client's own reconnect land on a ready instance. The port it then
proxies to is the project's own (ADR-034), read from the project row rather than
from configuration.

## Security

- Never route solely because an API key exists; verify project/key match.
- Do not trust `Host` without allowed-domain validation.
- Protect internal worker endpoints from direct internet access.
- Sanitize proxy headers.
- Apply request-body limits.
- Avoid exposing internal node/database names to public clients.
