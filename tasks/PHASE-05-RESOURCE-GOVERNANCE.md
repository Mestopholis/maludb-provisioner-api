# Phase 05 — Resource Governance

## Objective

Protect shared nodes from noisy-neighbor free/API workloads.

## Scope

- Plan entitlement service/config.
- Gateway rate limit.
- Gateway concurrent request limit.
- PostgREST pool configuration.
- Role/database statement/resource settings.
- Database size accounting.
- Quota warnings/write restriction.
- Free worker inactivity sleep/wake.
- Node capacity metrics.

## Acceptance criteria

- [x] Limits are configuration-driven. One resolver, `entitlements`, with every value
      overridable per project through `plans.config_json`.
- [x] Free project cannot bypass limits through direct DB access because none is exposed.
      ADR-005, enforced since Phase 02 and asserted by negative test J in Phase 03.
- [x] Over-limit API workload is throttled/rejected predictably. Rate and concurrency,
      each naming itself in the refusal (ADR-030).
- [x] Long-running queries terminate at configured timeout, from the plan's
      `statement_timeout_ms`. ADR-017 is unchanged: these are defaults for well-behaved
      clients, not enforcement.
- [x] Parallelism/resource settings are applied from plan.
- [x] Database size is measured per project, net of a baseline recorded per project
      rather than assumed from a constant.
- [x] Node stops receiving new projects when configured capacity threshold is reached --
      project count, warm count, connection headroom, or free disk.
- [x] Capacity scoring counts warm projects separately from total projects (ADR-022), and
      `rejection_reason()` now reads them. Auth workers are counted too: ADR-022 made Auth a
      per-project cost, and a projection that ignored it would let a node fill past its
      ceiling.
- [x] Auth workers are not started for projects that do not use Auth. `auth_enabled`,
      Phase 04 slice 1.
- [x] Wake orchestration waits for worker readiness, not port-open; the first request after
      wake succeeds rather than returning `503 PGRST002`. Phase 03 slice 2.
- [x] Connection headroom is asserted, projected from each warm project's own plan rather
      than a per-project average, and with an allowance held back so a full node stays
      administrable.

## Found during Phase 05

- ~~The tenant admin role cannot create tables.~~ Fixed 2026-08-16. It turned out to be
  two bugs: no privilege on `public`, and **no password on the role at all** while
  provisioning stored a `db_admin` credential regardless. See
  `specs/tenant-role-model.md`.

## Carried from Phase 04

- **A real bounce has never made the round trip.** Narrowed 2026-08-16, not closed. The
  reconciliation now runs against the **live** MaluMail API — a suppression added through
  `POST /v1/suppressions` is reconciled into `email_suppressions` and removed again, so the
  contract and the parsing are both verified rather than stubbed. What remains unverified
  is MaluMail's own bounce detection: that a genuinely undeliverable address bounces and
  gets added. That needs an address which really hard-bounces and MaluMail's asynchronous
  processing, neither of which this repository drives. It is MaluMail's behaviour to
  demonstrate, and the honest place to leave it is stated rather than implied.
- **Email quota is metered but not billed or surfaced.** `emails_per_day` is enforced from
  the plan entitlement on `platform_default`; nothing reports usage to a customer or to
  billing. Phase 09 owns billing, but the counter lives in `email_events` now.
- ~~The compatibility suite runs with `MAILER_AUTOCONFIRM=true`.~~ Closed 2026-08-15. The
  suite now runs with confirmation on throughout, and the confirmed journey is covered
  through the official client. Adding it found that the gateway answered `401` for every
  confirmation link, because a browser following one sends no `apikey` header — see
  `PUBLIC_AUTH_PATHS` and `docs/API-GATEWAY.md`.
