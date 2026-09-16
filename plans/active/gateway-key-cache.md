# Gateway key cache: verify the whole key, apply revocations, keep keys out of logs

Status: in progress (started 2026-09-15)
Human owner: Edward Honour
Agent/tool: Claude Code
Branch: `fix/gateway-key-cache`
Related task/phase: deployment rehearsal (`plans/active/deployment-rehearsal.md`); ADR-008, ADR-023
Dependencies: none

## Why

Found on the rehearsal deployment while testing the wildcard certificate through Nginx Proxy
Manager against project `8zn07rbf`, then confirmed with a secret key and a publishable key:

1. **A cached key check accepted a different key.** `KeyCache.resolve` stored a success under
   `(project_id, identifier)`, where the identifier is the key's first 8 characters — the public,
   listed `key_identifier`. A hit returned the stored identity without looking at the rest of the
   presented key. Once the real key had been used, `prefix + anything` got 200 for 30 seconds, and
   a project in use keeps that window open. For a secret key that is `service_role` for anyone who
   has seen the prefix. The same keying let `prefix + junk` cache a failure that locked the real
   key out for 5 seconds.
2. **Revocation waited for the TTL.** The control plane announces revocations on
   `maludb_key_revoked`, but nothing in the gateway listened: only `tests/test_gateway.py` called
   `apply_revocation`, by hand. A revoked key kept working for up to 30 seconds.
3. **Keys reached the journal in clear.** `uvicorn.run` installs uvicorn's own logging config
   after `build()` configured the JSON formatter, so uvicorn's lines bypassed redaction, and a
   websocket line carries `?apikey=<full key>`. The redaction pattern would not have caught it
   anyway: `mldb_[a-z]{2,6}_` misses `publishable`, and `[A-Za-z0-9]` stops at the first `-` or
   `_` of a `token_urlsafe` secret, leaving the rest of it in the line. The existing test used
   `mldb_sk_`, a format no key has.

## Scope

- `services/gateway/keys.py`: cache entries carry the peppered HMAC of the presented key; a hit
  counts only when it matches. A failure never displaces a live success. An invalidation that
  lands while a lookup is in flight stops that lookup caching its answer.
- `services/gateway/keys.py`: `RevocationListener`, a thread holding one autocommit connection
  that `LISTEN`s and applies announcements. It clears the cache whenever it (re)connects, because
  announcements sent while nothing listened are gone.
- `services/gateway/app.py`, `main.py`: start and stop the listener with the application; run
  uvicorn with `log_config=None` so its lines go through the redacting formatter.
- `services/gateway/keys.py`: successes and failures in separate LRU stores, each capped, since
  every distinct wrong key is now a miss and wrong keys are free to make.
- `services/control_plane/hashing.py`: `TOKEN_KINDS`, the one list of what this platform mints;
  `generate_token` refuses a kind not in it.
- `services/control_plane/logging.py`: build the key pattern from `TOKEN_KINDS`, and redact
  credential query parameters (`apikey`, `token`, `token_hash`, `access_token`, `refresh_token`,
  the two `X-Amz-` ones).

## Non-goals

- **Rate limiting failed authentication.** `Gateway.handle` authenticates before it reaches
  `limiter.acquire`, so a caller presenting wrong keys is not rate limited at all — and now that
  each distinct wrong key is a miss rather than a cached prefix, each one costs a control-plane
  query. Reordering the request path is a behaviour change that deserves its own review, so it is
  a separate branch. The cache bound below is what keeps that from being a memory problem in the
  meantime; it remains a load problem until that change lands.
  **Done in `fix/gateway-auth-miss-budget` (2026-09-16):** `limits.AuthMissBudget`, spent only on
  a lookup the cache cannot answer, rather than a reordering of the request path — which would
  have made the routing table a probe for what a project exposes, the property the current order
  exists for.
- The control-plane units run the `uvicorn` CLI, whose default config also bypasses the JSON
  formatter — for `uvicorn.error` as well as `uvicorn.access`, so unhandled tracebacks, not only
  query strings, print unredacted. No control-plane route takes a credential in the query string
  today; the traceback path is accepted here and noted for the units' own change.
- Surfacing listener health anywhere but the log. `revocations.listening` is on the object; no
  route, metric or preflight check reads it yet.

## Implementation steps

1. Cache verification and the failure/invalidation rules, with tests. **Done.**
2. Revocation listener, wired into the gateway lifespan and `build()`, with tests against a real
   `NOTIFY`, including reconnect. **Done.**
3. Logging: `log_config=None`, patterns, tests using real key formats. **Done.**
4. Security review; act on what it finds. **Done** — see the decision log.
5. Trailer, PR. Redeploy the gateway on the rehearsal node and repeat the tampered-key,
   revocation and log checks there.

## Test/verification

- `tests/test_gateway.py`: tampered secret and publishable keys refused against a warm cache;
  junk with a real prefix neither locks out the real key nor costs a database read for it;
  a flood of junk neither grows the cache without bound nor evicts a live success; an invalidation
  landing mid-lookup is not overwritten; revocation takes effect through the listener with a cache
  TTL far longer than the wait; a terminated listener reconnects and clears.
- `tests/test_logging_redaction.py`: every kind in `hashing.TOKEN_KINDS`, credential query
  parameters, and a real uvicorn access line through the configured handler.
- On the rehearsal: the checks from `scratchpad/cache_test.py` repeated through NPM.

## Risks

- One more control-plane connection per gateway process.
- A listener that silently stopped would restore finding 2 without any test failing in production;
  it logs every disconnect, and the TTL remains the backstop.

## Decision log

- 2026-09-15: verify on every hit with the HMAC already used as the stored verifier (ADR-023),
  rather than keying the cache by the full digest. Keying by digest would make every distinct junk
  key a new entry; keeping the identifier as the key keeps invalidation by identifier exact.
- 2026-09-15: the security review proposed skipping the reconnect `cache.clear()` when the outage
  was shorter than the TTL. **Rejected**: an entry cached a second before the drop still has most
  of its TTL to run, and a revocation announced during the gap reached nobody, so expiry does not
  cover it. Clearing on every session stands, with the comment saying why.
- 2026-09-15: the invalidation guard is per key and per project, not one counter for the process.
  A single counter let any revocation on the node suppress caching for every lookup in flight,
  which a customer could drive by revoking in a loop.

## Progress log

- 2026-09-15: findings confirmed on the rehearsal node; branch created from `8ba0f2d`.
- 2026-09-15: security review run. Eleven findings; the core fix held (no way to authenticate
  with a key you do not hold, ADR-008 intact, no stale positive while the listener lives, no
  injection, no lock-ordering problem). Acted on here: `pwreset` tokens were not redacted (HIGH —
  the pattern enumerated kinds by hand and the test parametrised the five that passed); the cache
  was unbounded; a flapping listener cleared the cache and reset its backoff on every connect; a
  listener that never connected was invisible; the process-wide generation counter; `stop()`
  losing a live thread and blocking the event loop; no `connect_timeout`; `digest` in the
  dataclass repr; `token_hash` and the `X-Amz-` parameters; `Gateway(cache=, revocations=)`
  silently ignoring one. Three of the new tests were weaker than their names and were rewritten
  to count database reads, to say what negative caching still buys, and to emit a real uvicorn
  line through the configured handler. The rate-limiter ordering is split out, as above.
