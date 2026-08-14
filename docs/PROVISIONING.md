# Tenant Provisioning

## Trigger

A customer creates a project.

## Provisioning outline

1. Validate account eligibility/project limits.
2. Generate project ID/ref.
3. Select a healthy MaluDB node.
4. Reserve placement.
5. Generate internal database/role names.
6. Generate service credentials.
7. Create constrained cluster roles.
8. Create the tenant database owned by the platform role.
9. Remove unsafe default connectivity/privileges.
10. Bootstrap required schemas/extensions/roles.
11. Bootstrap Supabase-compatibility objects.
12. Bootstrap MaluDB objects/extensions.
13. Generate project API keys/JWT key material as required.
14. Generate PostgREST/Auth configuration.
15. Start required project API workers.
16. Register/update gateway routing.
17. Run database/API health tests.
18. Run minimal Supabase-client compatibility smoke test.
19. Mark project ACTIVE.

## Retry behavior

Provisioning must be stateful and safe to retry.

Examples:

- if role exists with correct recorded ownership, continue;
- if database exists but bootstrap version is incomplete, resume migration;
- never drop/recreate a database merely because a later service-registration step failed;
- cleanup must distinguish an unused failed project from one that may contain customer data.

## Bootstrap versioning

Tenant bootstrap SQL should be versioned.

Proposed structure:

```text
bootstrap/
  001_roles.sql
  002_extensions.sql
  003_api.sql
  004_auth.sql
  005_realtime.sql
  006_storage.sql
  007_maludb.sql
```

Actual split can change once implementation begins.

## Node drain/movement

Not MVP, but all placement metadata must permit a future project database to move between nodes without changing its stable project ref.
