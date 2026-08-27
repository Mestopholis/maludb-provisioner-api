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

- [x] Restore of one tenant has a tested runbook. Phase 11 slice 2: the
      procedure is in `docs/BACKUP-RECOVERY.md`, the tooling is
      `cp-manage restore run`, and `tests/test_restore.py` performs a real
      point-in-time restore of a real tenant on a throwaway cluster --
      asserting that only the pre-target write came back, that the live
      database was untouched, and that its neighbour kept serving.
- [ ] Node failure recovery is documented/tested.
- [ ] Tenant movement preserves stable project identity.
- [ ] Production pool can be separated from free pool.
