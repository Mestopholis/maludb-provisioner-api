# Phase 06 — Realtime

## Objective

Add quota-controlled Supabase-compatible Postgres Changes.

## Scope

- Validate upstream Realtime topology for project DB model.
- Route `/realtime/v1`.
- Project auth/authorization.
- Postgres Changes.
- Connection/message quotas.
- Compatibility tests.

## What the node measurements already establish

Measured 2026-08-16 before planning, because `docs/REALTIME.md` asks for the
upstream topology to be validated first. Detail in
`plans/active/phase-06-realtime.md`.

- `wal_level` is `replica`, so Postgres Changes cannot work at all until a node
  is prepared with `logical` — which needs a **cluster restart**, i.e. an outage
  for every tenant already on it.
- A logical replication slot is bound to one database, so Realtime needs **one
  slot per tenant**. There is no multiplexing.
- `max_replication_slots = 10` against ADR-022's warm ceiling of ~24 projects.
  **Realtime's ceiling is under half the node's**, and it is a third resource
  alongside projects and connections.
- `max_slot_wal_keep_size = -1`, unbounded. A stalled consumer pins WAL
  indefinitely, fills the disk, and stops writes for **every tenant on the
  node**. Bounding this is slice 1 and is not optional.

## Acceptance criteria

- [ ] Official Supabase client receives tested Postgres Changes.
- [ ] Cross-project events cannot leak.
- [ ] Connection limits are enforced.
- [x] Free/paid enablement is entitlement-driven. `realtime_connections` is already `0`
      on free from Phase 05 slice 1. *(Slice 2. No new flag — the number that says how
      much Realtime a plan includes is the same number that says whether it includes any.
      A downgrade removes it on the next provisioning run; an upgrade does not add it,
      because enabling creates a role holding `REPLICATION`.)*
- [x] A node that cannot host Realtime says so at registration, rather than failing at
      provisioning time. *(Slice 1. Checked by `cp-manage node realtime-check` during node
      bring-up and recorded on the node row; a node nobody has checked reads as not ready.
      Registration itself holds no admin DSN, so the check cannot happen inside it.)*
- [x] The replication-slot ceiling is enforced in placement, not merely measured — the
      lesson Phase 05 spent four slices on. *(Slice 1. `reserve_placement(needs_realtime=True)`
      refuses under the same node row lock that makes the connection ceiling enforceable;
      a node out of slots still accepts projects that do not want Realtime.)*
- [x] A stalled consumer is demonstrated **not** to fill the node's disk. *(Slice 1,
      `tests/test_realtime_node.py::test_r4_...`, against a real cluster: the slot is
      invalidated and the platform reports it. The reporting half is the point — a
      contained failure nobody is told about is indistinguishable from data loss.)*

Slice 1 adds one the phase did not originally list, because the spike found it:

- [x] A role holding `REPLICATION` cannot take a base backup of the cluster (ADR-031),
      asserted with a real `pg_basebackup` against a node whose `pg_hba.conf` is under
      test — and the check shown capable of reporting a node that permits it.
