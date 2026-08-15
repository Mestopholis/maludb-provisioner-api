# Phase 03 — Supabase-Compatible Data API

## Objective

Make the official Supabase JavaScript client perform initial CRUD/RPC operations against a MaluDB tenant project.

## Scope

- Project hostname routing.
- Publishable/secret API-key model.
- Key cache/validation.
- Per-project PostgREST configuration/process.
- Process start/stop lifecycle.
- `/rest/v1` routing.
- Initial compatibility tests.

## Proof milestone

```javascript
const client = createClient(
  'https://<project-ref>.maludb.com',
  '<publishable-key>'
)

const { data, error } = await client
  .from('customers')
  .select('*')
```

must work against a provisioned MaluDB tenant.

## Prerequisites

Phase 00 proved this milestone reachable with stock PostgREST 14.17 and
`supabase-js` 2.112.3 (16/16). Its findings are binding here — see
`tasks/PHASE-00-FEASIBILITY.md` and ADR-018.

## Acceptance criteria

- [ ] Wrong-project API key is rejected.
- [ ] Extension functions are not reachable as RPC by `anon` or `authenticated`.
- [ ] Tenant DDL triggers a PostgREST schema cache reload; a newly created table is queryable without restarting the worker.
- [ ] The `anon` grant posture is documented and asserted.
- [ ] The compatibility matrix is promoted only from tests run through the real gateway, not the Phase 00 prototype.
- [ ] Revoked key is rejected.
- [ ] Official client passes select/insert/update/delete/upsert/filter/RPC smoke tests.
- [ ] Free API worker can be stopped/started without deleting DB.
- [ ] Internal PostgREST endpoint is not internet-accessible directly.
- [ ] Compatibility matrix is updated from `planned` only for tested behaviors.
- [ ] Negative test J from `specs/tenant-role-model.md`: a free-tier project has no login
      role reachable from outside the gateway. Carried from Phase 02, which had no gateway
      to test it against.

## Carried from Phase 02

- ~~**CI cannot install `maludb_core`.**~~ Closed by slice 0 (2026-08-15). CI now
  builds the extension from a pinned upstream commit onto a PostgreSQL 17 cluster
  it creates itself, and `MALUDB_REQUIRE_MALUDB_CORE` makes an absent extension a
  failed run rather than a skipped test. First green run: `maludb_core 0.104.0`,
  180 passed, zero skipped.
- A dedicated provisioning superuser would be cleaner than reusing `postgres`.
- `maludb_core` hard-codes `public.gen_random_bytes`, which is why extensions cannot be
  relocated to their own schema (ADR-018). Still to be raised upstream against
  `maludb-core`; it is the root cause the revoke works around.
