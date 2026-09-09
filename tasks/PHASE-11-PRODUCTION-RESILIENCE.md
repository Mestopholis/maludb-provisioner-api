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
- [x] Node failure recovery is documented/tested. `cp-manage node rebuild`
      reconnects the control plane to a node restored from its stanza: it
      refuses a target that already carries projects, verifies that every
      restored tenant still owns its own `auth` and `storage` schemas (ADR-059),
      and repoints `projects.node_id` only for the tenants that verified. The
      lost node keeps its row, which carries the stanza and the encrypted admin
      DSN. `docs/BACKUP-RECOVERY.md` carries the runbook for a lost node, a
      degraded one, and a restore to the wrong point in time.

      **The RTO table in that runbook is deliberately empty.** No production-
      sized rebuild has been timed, and a figure from the test cluster quoted as
      an RTO would be worse than none.
- [x] Tenant movement preserves stable project identity. `cp-manage project
      move` preserves `project_ref`, database name, API keys and subscription
      rows while changing only `projects.node_id`, and `cp-manage project
      drain-report` turns drain into explicit operator work. The move freezes
      the source by revoking `CONNECT` rather than by restricting privileges
      (ADR-071), refuses a destination that is physically the source cluster,
      runs every refusal before the freeze so a refused move costs no downtime,
      and retains the source under `<db>_pre_move_<timestamp>` instead of
      dropping it. `tests/test_tenant_movement.py` is **20 passed, 0 skipped**
      against two distinct clusters -- including that a frozen tenant is refused
      while `pg_dump` still succeeds, that the release neither opens the
      database to `PUBLIC` nor hands `CONNECT` to a role that did not have it,
      and that a move onto the same cluster is refused.
- [ ] Production pool can be separated from free pool.
