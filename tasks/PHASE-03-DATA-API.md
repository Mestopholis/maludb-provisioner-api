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

- [x] Wrong-project API key is rejected.
- [x] Extension functions are not reachable as RPC by `anon` or `authenticated`.
- [x] Tenant DDL triggers a PostgREST schema cache reload; a newly created table is queryable without restarting the worker.
- [x] The `anon` grant posture is documented and asserted.
- [x] The compatibility matrix is promoted only from tests run through the real gateway, not the Phase 00 prototype.
- [x] Revoked key is rejected.
- [x] Official client passes select/insert/update/delete/upsert/filter/RPC smoke tests.
- [x] Free API worker can be stopped/started without deleting DB.
- [x] Internal PostgREST endpoint is not internet-accessible directly. Workers bind loopback
      (migration 0008), so this is a property of the socket rather than of a firewall rule.
- [x] Compatibility matrix is updated from `planned` only for tested behaviors. Each promoted
      entry names the suite case that earned it.
- [x] Negative test J from `specs/tenant-role-model.md`: a free-tier project has no login
      role reachable from outside the gateway. The half this repository can assert is that no
      API response carries a tenant database credential, host, or port — asserted against the
      CI-enforced contract rather than against today's handlers. The other half, that the
      PostgreSQL port is not published, is node configuration and belongs to a deployment
      runbook rather than to a test here.

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
