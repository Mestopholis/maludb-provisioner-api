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
- [ ] Tenant movement preserves stable project identity. Phase 11 slice 7 is
      implemented but not yet accepted: `cp-manage project move` preserves
      `project_ref`, database name, API keys and subscription rows while
      changing only `projects.node_id`, and `cp-manage project drain-report`
      turns drain into explicit operator work. The focused tests are written in
      `tests/test_tenant_movement.py`, but local validation on 2026-09-08
      skipped because `MALUDB_CONTROL_PLANE_DATABASE_URL` was unset.
- [ ] Production pool can be separated from free pool.
