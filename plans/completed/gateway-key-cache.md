# Gateway key cache: verify the whole key, apply revocations, keep keys out of logs

Status: **complete** 2026-09-16. Merged as #187 (the cache, the listener, the logging) and #190
(ADR-081, the budget the first one made necessary), deployed to the rehearsal VMs at `5aebc00`,
and each of the three findings re-measured against the live deployment rather than assumed.
Human owner: Edward Honour
Agent/tool: Claude Code
Branch: `fix/gateway-key-cache`, then `fix/gateway-auth-miss-budget`
Related task/phase: deployment rehearsal (`plans/active/deployment-rehearsal.md`, findings 23-25);
ADR-008, ADR-023, ADR-081
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
   revocation and log checks there. **Done** — #187 merged, both VMs redeployed, all three
   re-measured live (below).
6. The budget the first step made necessary: every distinct wrong key became a database round
   trip, and authentication happens before the request limiter. **Done** — #190, ADR-081.

## Test/verification

- `tests/test_gateway.py`: tampered secret and publishable keys refused against a warm cache;
  junk with a real prefix neither locks out the real key nor costs a database read for it;
  a flood of junk neither grows the cache without bound nor evicts a live success; an invalidation
  landing mid-lookup is not overwritten; revocation takes effect through the listener with a cache
  TTL far longer than the wait; a terminated listener reconnects and clears.
- `tests/test_logging_redaction.py`: every kind in `hashing.TOKEN_KINDS`, credential query
  parameters, and a real uvicorn access line through the configured handler.
- `tests/test_limits.py`: the budget's ceiling, refill, per-project isolation, node ceiling,
  backwards clock and `forget` — in that module rather than `test_gateway.py`, which skips whole
  without a control-plane DSN.
- On the rehearsal, through NPM, after redeploy at `5aebc00`:

  | Check | Before | After |
  |---|---|---|
  | Tampered secret key, warm cache | 200 | 401 |
  | Tampered publishable key, warm cache | 200 | 401 |
  | Revoked key at +0s / +2s / +5s | 200 | 401 |
  | Key in the gateway's journal | full key | `apikey=[REDACTED]` |
  | Ordinary traffic (schema route, live keys) | 200 | 200, no budget warnings |

  Each of the four new tests in #190 was also checked against its own fix disabled: the two
  budget tests fail with `spend` stubbed to allow, the three cache tests with `peek` reverted to
  exact-match-only. That check exists because the previous review found a test of this same
  property passing with the fix removed.

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
- 2026-09-16: #187 merged on a green run; both VMs redeployed and the three findings re-measured
  live. The one test that failed in the full suite was the new uvicorn-line one, and the cause was
  worth the two passes it took to find: other tests build a `uvicorn.Config`, whose
  `configure_logging()` is process-wide — it gives the whole `uvicorn` logger family its own
  handlers with `propagate=False` *and* sets their level to ERROR, so resetting `uvicorn.access`
  alone was not enough. The test now establishes the state `log_config=None` produces.
- 2026-09-16: second security review, on the budget. Eleven findings, two HIGH. The exhausted
  state is not a window that passes — an attacker can hold a bucket empty — and the cache is
  emptied by the very events that accompany an attack, so "cached keys keep working" was doing
  unearned work. Mitigated by letting the cache refuse a known identifier with a wrong digest
  for free (`key_identifier` is UNIQUE, a verifier never changes), which takes the cheap attack
  off the budget entirely; the residual is ADR-081, accepted by the owner. The second HIGH was
  ours again: the test asserting the safe side of the trade-off passed with the fix removed. Every
  new test is now checked against its own fix disabled before commit.
- 2026-09-16: both PRs merged, plan complete. Follow-ups live in ADR-081's revisit conditions
  (reserved headroom for known identifiers, the 429 on exhaustion, a reliable caller address, and
  `_authenticate` not blocking the event loop) rather than here.
