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

## Acceptance criteria

- [ ] Wrong-project API key is rejected.
- [ ] Revoked key is rejected.
- [ ] Official client passes select/insert/update/delete/upsert/filter/RPC smoke tests.
- [ ] Free API worker can be stopped/started without deleting DB.
- [ ] Internal PostgREST endpoint is not internet-accessible directly.
- [ ] Compatibility matrix is updated from `planned` only for tested behaviors.
