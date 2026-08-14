# Phase 11 — Production Resilience

## Objective

Add operational capabilities required for serious production workloads.

## Scope

- Backup/restore implementation.
- WAL/PITR where supported.
- Node pools.
- Drain/maintenance mode.
- Tenant movement/rebalancing.
- Disaster-recovery runbooks.
- Capacity alerts.

## Acceptance criteria

- [ ] Restore of one tenant has a tested runbook.
- [ ] Node failure recovery is documented/tested.
- [ ] Tenant movement preserves stable project identity.
- [ ] Production pool can be separated from free pool.
