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

## Acceptance criteria

- [ ] Official Supabase client receives tested Postgres Changes.
- [ ] Cross-project events cannot leak.
- [ ] Connection limits are enforced.
- [ ] Free/paid enablement is entitlement-driven.
