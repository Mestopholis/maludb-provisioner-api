# Roadmap

## Phase 0 — Feasibility spike

**Complete (2026-08-15).** Proved that stock PostgREST and Supabase Auth run unmodified against a MaluDB tenant database and that the official `supabase-js` client passes CRUD/RPC/RLS against the result. Ordered before Phase 1 deliberately: it de-risks ADR-001 before any stack is chosen. See `tasks/PHASE-00-FEASIBILITY.md`.

## Phase 1 — Foundation

Repository conventions, control-plane skeleton, local/test environment, core domain model.

## Phase 2 — Tenant provisioning

Create database/roles on an existing MaluDB node with strong isolation and idempotent lifecycle state.

## Phase 3 — Supabase-compatible Data API

Gateway, project API keys, per-project PostgREST, official `supabase-js` CRUD/RPC smoke tests.

## Phase 4 — Auth

Per-project Auth configuration, JWT/RLS integration, basic password flows.

## Phase 5 — Resource governance

Gateway limits, PostgreSQL role/database limits, usage telemetry, storage accounting, worker sleep/wake.

## Phase 6 — Realtime

Postgres Changes first, quota-controlled.

## Phase 7 — Dashboard

Customer project management, keys, usage, SQL/schema tooling as allowed.

## Phase 8 — Supabase migration

Compatibility scanner, database migration, auth/storage migration increments.

## Phase 9 — Billing/upgrades

Subscriptions, entitlements, paid direct DB access, production tiers.

## Phase 10 — Storage

Supabase-compatible Storage surface using selected object storage.

## Phase 11 — Production resilience

Backups, restore, PITR, node pools, tenant movement, disaster recovery.

## Phase 12 — MaluDB-native differentiation

Memory/database capabilities exposed in ways that do not break Supabase compatibility.
