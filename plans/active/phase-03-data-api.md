# Execution Plan: Phase 03 — Supabase-Compatible Data API

Status: IN PROGRESS — slices 0 and 1 merged; slice 2 next
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-03-slice-*`, one per slice
Related task: `tasks/PHASE-03-DATA-API.md`
Dependencies: Phase 02 complete (merged 2026-08-15). Slice 0 below is a hard
prerequisite for the security claims this phase makes.

## Objective

The official Supabase JavaScript client performs CRUD and RPC against a
provisioned MaluDB tenant, over a public hostname, authenticated with a
project-scoped API key:

```javascript
const client = createClient('https://<project-ref>.maludb.com', '<publishable-key>')
const { data, error } = await client.from('customers').select('*')
```

Phase 00 proved this reachable with stock PostgREST 14.17 and `supabase-js`
2.112.3 (16/16). This phase makes it real, multi-tenant, and safe.

## What changes about risk in this phase

Phase 02's failure mode was a tenant reaching another tenant's database with a
credential it already held. Phase 03's is different and larger: **the gateway
becomes the only thing standing between a public HTTP request and a tenant's
data.** A key/project mismatch is a cross-tenant read by anyone on the internet
holding any valid key.

It also changes the standing of an existing gap. Three tests — including whether
`anon` can reach `gen_salt`, the finding ADR-018 exists for — currently run on a
developer machine only. Until Phase 03 those functions needed a database
credential to reach. From Phase 03 they are one publishable key away. That is
why slice 0 comes first rather than being tidied up later.

## Preconditions

- [x] Phase 02 complete — tenants provision, harden, and validate.
- [x] ADR-018 hardening is self-healing (bootstrap 005) and gated by `verify()`.
- [x] `api_keys` table exists with `key_identifier` / `verification_data`,
      already shaped for the Class A hashed storage ADR-023 requires.
- [x] `hashing.generate_token` / `verify_token` exist from Phase 01 and are the
      right primitive for high-entropy keys.
- [x] The three opening decisions, taken 2026-08-15 and recorded as ADR-026,
      ADR-027 and ADR-028.

## Opening decisions — taken 2026-08-15

Surfaced rather than assumed, and each now recorded as an ADR. The reasoning is
in `docs/DECISIONS.md`; the options considered are kept here because a rejected
option is the cheapest thing to re-read when a decision is revisited.

### 1. Gateway implementation → **ADR-026**

Every tenant data request passes through this, so it is both the security
boundary and the throughput ceiling.

| Option | For | Against |
|---|---|---|
| **Python ASGI proxy** (Starlette/httpx), same stack as ADR-024 | One language, one test harness; key validation, cache, and routing in the code that already owns them; easy to test cross-tenant properties directly | Python sits in the data path for every byte of tenant traffic; throughput and latency are a real question at density |
| **Caddy/nginx in front, control plane decides** — reverse proxy handles TLS and transport, calls the control plane to authorize and resolve the upstream | Fast data path, mature TLS and wildcard-cert handling, gateway can cache authorization decisions | Dynamic per-request upstream selection is awkward in both; the routing logic ends up split across two systems, which is exactly where a key/project mismatch hides |
| **Envoy** with a control-plane xDS service | Built for this; per-route policy, good observability | Heaviest operationally, and the least familiar to a two-person team |

**Decided: the Python ASGI proxy**, explicitly as an MVP with a measured
throughput number recorded when slice 3 lands. The security property that
matters most in this phase — that a key is checked against the hostname's
project on every request — is the one thing I would rather have in a single
testable place than split across a proxy config and a callback. ADR-022 already
establishes the precedent of measuring rather than assuming, and replacing the
transport later does not change the control plane.

### 2. Worker supervision → **ADR-027**

ADR-007 permits per-project PostgREST processes; ADR-022 requires that starting
one waits for **readiness, not port-open**, because PostgREST answers
`503 PGRST002` until its schema cache loads.

**Decided: systemd template units** (`maludb-postgrest@<ref>.service`).
Restart policy, logging, and resource limits come free and are auditable by an
operator who does not know this codebase. The alternative — the control plane
spawning and tracking subprocesses — puts process supervision in a web
application, and a control-plane restart then orphans every tenant's worker.

### 3. API key format → **ADR-028**

Supabase's newer format is `sb_publishable_<random>` / `sb_secret_<random>`.
Compatibility (ADR-001) argues for mirroring the shape; identity argues for our
own prefix.

**Decided: `mldb_publishable_<random>` and `mldb_secret_<random>`.** (Recorded
first as `mdb_`; corrected during slice 1 to match every other generated
identifier in the system. See ADR-028.) The
client does not parse the key — it is an opaque bearer token in a header — so a
distinct prefix costs no compatibility and buys two things: a leaked key is
attributable to MaluDB at a glance, and secret-scanning rules can match it.
Record the divergence in `specs/compatibility-matrix.yaml` as intentional.

## Slices

Sequential, with a security review between each, as in Phase 02.

### Slice 0 — `maludb_core` in CI

Not a feature. A CI image carrying PostgreSQL 17 plus `maludb_core`, so the
three currently-skipped tests run on every push — including whether `anon` can
reach `gen_salt`.

Small, and deliberately first: every security claim this phase makes about the
exposed RPC surface rests on tests that do not currently run anywhere but my
machine. Verifiable on its own — CI reports zero skips for the extension tests.

### Slice 1 — Project API keys

Generation, hashed storage, issuance, revocation, and validation. Control-plane
only; nothing is routed yet, so the blast radius is a table.

- Publishable and secret keys per ADR-008, Class A hashed per ADR-023 — the
  plaintext is returned exactly once, at creation, and is unrecoverable after.
- `key_identifier` is the lookup handle so validation is one indexed read rather
  than a scan-and-compare over every key on the platform.
- Revocation is immediate and testable, matching the identity work in Phase 01.
- `last_used_at` updated off the request path, not synchronously — it must not
  turn every read into a write.

### Slice 2 — PostgREST worker lifecycle

Per-project configuration, start, stop, and readiness.

- Config generated from the project's `authenticator` credential, read back
  through the key ring — the worker never sees a superuser DSN
  (`docs/ARCHITECTURE.md`).
- Readiness means PostgREST answers, not that the port opened (ADR-022).
- Free-tier sleep and wake. ADR-022 measured sub-second cold start, which is
  what makes an aggressive sleep policy correct rather than a compromise.
- **Schema cache reload** (Phase 00 finding 3): an event trigger in the tenant
  database issuing `NOTIFY pgrst, 'reload schema'` on DDL. This is a tenant
  bootstrap file, so it lands as bootstrap `006` — the existing five are
  immutable. Without it, a table a customer creates is invisible until the
  worker restarts, which is the "it cannot be left to worker restarts" the
  Phase 00 findings call out.

### Slice 3 — Gateway: hostname routing, key validation, proxy

The security-critical core, reviewed alone for the same reason slice 2 of Phase
02 was.

- Parse and validate the project ref from the hostname; never trust `Host`
  without allowed-domain validation (`docs/API-GATEWAY.md`).
- **Verify the key belongs to the hostname's project** (ADR-008). This is the
  cross-tenant control, and it gets a dedicated negative test: a valid key for
  project A presented against project B's hostname must be rejected, not served.
- Reject non-active projects; wake a sleeping free worker if policy permits.
- Key cache with prompt invalidation on revoke — `docs/API-GATEWAY.md` forbids a
  control-plane query per request, and a cache that outlives a revocation is a
  revocation that did not happen.
- Internal worker endpoints not reachable from the internet.

### Slice 4 — Compatibility tests with the official client

`@supabase/supabase-js` against a real provisioned tenant through the real
gateway: select, insert, update, delete, upsert, filters, ordering, range,
count, RPC, and RLS behaviour.

`specs/compatibility-matrix.yaml` entries move from `planned` to supported
**only** for what these tests cover, per its own stated policy and the
`AGENTS.md` rule against claiming compatibility the matrix and tests do not
support.

## Non-goals

- `/auth/v1` and GoTrue — Phase 04. Phase 00 finding 4 (GoTrue writing
  `schema_migrations` into the exposed schema) is already mitigated in bootstrap
  002 and needs no work here.
- Realtime and Storage surfaces.
- **The transaction-mode pooler.** ADR-022 requires one before roughly 25 warm
  projects per node at default `max_connections`. Phase 03 will not approach
  that on a development node, but the slice 2 worker accounting must track warm
  count separately so the threshold is visible when it arrives.
- Custom domains, and the wildcard TLS/DNS strategy — still open questions.
- Rate limiting beyond what ADR-009 already layers; usage metering.
- Legacy Supabase key-format compatibility. ADR-028 uses a MaluDB prefix; the
  divergence is intentional and goes in the compatibility matrix.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-03-DATA-API.md`, including
      negative test J carried from Phase 02.
- [ ] A security review per slice, not per phase.
- [ ] The proof milestone runs against a tenant provisioned by Phase 02 code,
      not a fixture.
- [x] CI runs the `maludb_core` tests — zero extension-related skips.
- [ ] Compatibility matrix promoted only from tests through the real gateway.

## Risks

- **A key/project mismatch is a public cross-tenant read.** The largest single
  risk in the project so far. Mitigated by making it one function in one place,
  reviewed alone, with the mismatch case as an explicit negative test rather
  than an assumed property.
- **Cache invalidation on revocation.** A revoked key still served from cache is
  indistinguishable from no revocation at all. Needs a bounded, tested staleness
  window, not "it expires eventually".
- **Python in the data path** (decision 1). Accepted knowingly for MVP; slice 3
  must record a measured throughput number so the decision can be revisited on
  evidence rather than instinct.
- **Worker density.** ADR-022 measured connections, not memory, as the binding
  constraint. Warm-project accounting must land in slice 2 or the ceiling is
  invisible until it is hit.
- **Schema cache staleness** makes a correct tenant schema look broken. The
  event trigger is the fix; it needs a test that creates a table and queries it
  through the API without restarting anything.

## Decision log

- 2026-08-15 — Plan created. Three decisions surfaced for the owner rather than
  taken: gateway implementation, worker supervision, API key format.
- 2026-08-15 — All three decided by the owner, matching the recommendations, and
  recorded as ADR-026, ADR-027 and ADR-028. The corresponding entries in
  `docs/OPEN-QUESTIONS.md` are closed. Slice 0 is unblocked.

## Progress log

- 2026-08-15 — Plan created, five slices. Not started.
- 2026-08-15 — Slice 3 complete: the gateway. The project comes from the
  hostname first and the key is checked against *that* project, which is the
  whole security property -- resolving the project from the key instead would
  make the hostname decorative and every key a key to every project. Mutating
  the gateway to do exactly that fails three tests, so the control is verified
  rather than asserted. Every refusal answers the same 401 body, because a
  distinguishable failure is an oracle for which refs and keys exist. Host
  parsing rejects the suffix-confusion case (`ref.maludb.local.evil.com`) that
  a `startswith` or `in` check would accept. Revocation is announced on a
  LISTEN/NOTIFY channel inside the revoking transaction, so a rolled back
  revoke never tells a gateway to forget a live key; the TTL is the backstop,
  not the mechanism. ADR-026's required measurement is recorded: +6.3 ms per
  request, and taking it rewrote the implementation -- the first version made
  three or four database round trips per request and decrypted a signing key
  that never changes, every time. 265 tests.
- 2026-08-15 — Slice 2 complete: PostgREST worker lifecycle. Config is rendered
  from the project's authenticator credential and written 0600 before it has
  content, since creating the file then chmod-ing it leaves a window in which
  the password and JWT secret are world readable. Readiness asks the worker to
  answer rather than checking the port, per ADR-022. Warm accounting moved from
  project *status* to worker state: counting by status charged every sleeping
  free project against the connection ceiling it demonstrably was not consuming,
  which is the opposite of what ADR-022 measured. The JWT secret lands here
  rather than in Phase 04 because PostgREST needs it first and it is the same
  secret GoTrue will need -- two secrets would give a project whose own Auth
  tokens its own Data API rejects. Bootstrap 006 sends `NOTIFY pgrst, 'reload
  schema'` on DDL; removing it reproduces the Phase 00 finding exactly
  (`PGRST205: Could not find the table 'public.notes' in the schema cache`)
  against a real PostgREST 14.17, now installed in CI so that assertion runs
  everywhere. 226 tests.
- 2026-08-15 — Slice 1 complete: project API keys. Storage splits by ADR-023
  class rather than by key sensitivity -- a secret key is Class A (an HMAC
  verifier, unrecoverable), a publishable key is Class B (envelope encrypted)
  because it is public by design and a dashboard must display it again next
  month. Migration 0007 makes that split a CHECK constraint, since a Class A
  secret stored Class B would not be visible in review of the code that wrote
  it. `authenticate` takes the expected project as a *required* argument and
  compares internally, so ADR-008's cross-tenant check cannot be omitted by a
  caller; removing the comparison was confirmed to fail the test. Issuance is an
  operator CLI (`cp-manage key ...`) rather than HTTP: `specs/control-plane-api.yaml`
  defines no key-management endpoints, and designing that surface belongs with
  the dashboard in Phase 07. 198 tests.
- 2026-08-15 — Slice 0 complete: CI builds `maludb_core` from a pinned upstream
  commit onto a PostgreSQL 17 cluster it creates itself, replacing the plain
  `postgres:17` service container. Two environment hazards were found by running
  it rather than by reading it — the runner has `create_main_cluster` disabled,
  so installing the package produced no server at all, and the extension links
  against `-lcrypto -lcurl`, which `build-essential` does not provide.
  `MALUDB_REQUIRE_MALUDB_CORE` makes an absent extension a failed run rather
  than a skipped test, so the gap cannot silently reopen. First green run
  reports `maludb_core 0.104.0`, 180 passed, zero skipped — the ADR-018
  assertions have now executed somewhere other than a developer machine.
