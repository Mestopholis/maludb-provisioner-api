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
- [ ] Free project cannot bypass limits through direct DB access because none is exposed.
- [ ] Over-limit API workload is throttled/rejected predictably.
- [ ] Long-running queries terminate at configured timeout.
- [ ] Parallelism/resource settings are applied from plan.
- [ ] Database size is measured per project.
- [ ] Node stops receiving new projects when configured capacity threshold is reached.
- [ ] Capacity scoring counts warm projects separately from total projects (ADR-022).
- [ ] Auth workers are not started for projects that do not use Auth.
- [ ] Wake orchestration waits for worker readiness, not port-open; the first request after wake succeeds rather than returning `503 PGRST002`.
- [ ] Connection headroom is asserted: `warm_projects × backends_per_project` stays within `max_connections` minus reserved.

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
- **The compatibility suite runs with `MAILER_AUTOCONFIRM=true`.** A deliberate test
  posture, not a product default — `AuthSettings.autoconfirm` defaults to `False`, so every
  real project requires confirmation. What is not covered is the *confirmed* journey through
  `supabase-js` itself: signup, follow the link, then sign in. `tests/test_email_hook.py`
  proves that path with raw HTTP against GoTrue; a compatibility case would prove the
  official client handles it, including how it surfaces `email_not_confirmed`.
