# Phase 10 — Storage

## Objective

Implement the defined Supabase-compatible Storage subset using selected object storage.

## Scope

- Object-store provider abstraction.
- Buckets/object metadata.
- Upload/download/delete.
- Signed URLs.
- Authorization/RLS integration.
- Migration path from Supabase Storage.

## Acceptance criteria

- [ ] Object bytes are outside tenant Postgres DB.
- [ ] Metadata/authorization behavior passes compatibility tests.
- [ ] Cross-project object access is denied.
