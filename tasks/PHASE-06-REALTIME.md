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
- [ ] Free/paid enablement is entitlement-driven. `realtime_connections` is already `0`
      on free from Phase 05 slice 1.
- [ ] A node that cannot host Realtime says so at registration, rather than failing at
      provisioning time.
- [ ] The replication-slot ceiling is enforced in placement, not merely measured — the
      lesson Phase 05 spent four slices on.
- [ ] A stalled consumer is demonstrated **not** to fill the node's disk.
