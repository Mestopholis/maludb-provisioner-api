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

- [ ] Limits are configuration-driven.
- [x] Free project cannot bypass limits through direct DB access because none is exposed.
      ADR-005, enforced since Phase 02 and asserted by negative test J in Phase 03.
- [ ] Over-limit API workload is throttled/rejected predictably.
- [ ] Long-running queries terminate at configured timeout.
- [ ] Parallelism/resource settings are applied from plan.
- [ ] Database size is measured per project.
- [ ] Node stops receiving new projects when configured capacity threshold is reached.
- [~] Capacity scoring counts warm projects separately from total projects (ADR-022).
      **Partially met.** `capacity_of` computes `current_warm_projects` and
      `max_warm_projects`; `rejection_reason()` never reads them, so warm capacity is
      measured and not enforced. Slice 4.
- [x] Auth workers are not started for projects that do not use Auth. `auth_enabled`,
      Phase 04 slice 1.
- [x] Wake orchestration waits for worker readiness, not port-open; the first request after
      wake succeeds rather than returning `503 PGRST002`. Phase 03 slice 2.
- [ ] Connection headroom is asserted: `warm_projects × backends_per_project` stays within `max_connections` minus reserved.

## Found during Phase 05

- **The tenant admin role cannot create tables.** `mldb_<ref>_admin` has no privilege on
  `public` at all, despite `specs/tenant-role-model.md` describing it as the role for paid
  direct SQL. Found by the storage tests; recorded there in full. Needs a role-model
  decision, not a passing fix.

## Carried from Phase 04

- **A real bounce has never made the round trip.** MaluMail has no delivery webhooks
  (ADR-029), so the platform polls `GET /v1/suppressions` into `email_suppressions`. The
  reconciliation is tested against a stub, and suppression is enforced before every send —
  but nothing has verified that a genuinely undeliverable address bounces, that MaluMail
  adds it, and that the poll picks it up. Needs a real bounce, which means an address that
  hard-bounces on purpose.
- **Email quota is metered but not billed or surfaced.** `emails_per_day` is enforced from
  the plan entitlement on `platform_default`; nothing reports usage to a customer or to
  billing. Phase 09 owns billing, but the counter lives in `email_events` now.
- ~~The compatibility suite runs with `MAILER_AUTOCONFIRM=true`.~~ Closed 2026-08-15. The
  suite now runs with confirmation on throughout, and the confirmed journey is covered
  through the official client. Adding it found that the gateway answered `401` for every
  confirmation link, because a browser following one sends no `apikey` header — see
  `PUBLIC_AUTH_PATHS` and `docs/API-GATEWAY.md`.
