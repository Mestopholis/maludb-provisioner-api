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
