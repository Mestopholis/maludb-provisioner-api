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
9. Remove unsafe default connectivity/privileges. At minimum
   `REVOKE CONNECT ON DATABASE <tenant_db> FROM PUBLIC`, then grant `CONNECT`
   only to that project's roles. PostgreSQL grants `CONNECT` to `PUBLIC` by
   default, so skipping this leaves every tenant database reachable by every
   role on the node — verified, see ADR-014 and `docs/MALUDB.md`.
10. Bootstrap required schemas/extensions/roles. Installing `maludb_core`
    requires superuser, costs ~23 MB and ~2 s, and pulls in `vector`,
    `btree_gist`, `pg_trgm`, and `pgcrypto` via `CASCADE`. Record the installed
    extension versions against the project — dependency versions drift with the
    node's OS packages.
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

Implemented in `services/control_plane/jobs.py`; the mechanics are in
`specs/provisioning-state-machine.md`. In short: steps carry `done` predicates
that ask the node rather than reading `projects.status`, a failed attempt lands
in `RETRY_WAIT` with a time attached and becomes `FAILED` only after the attempt
cap, and each attempt keeps its own `provisioning_jobs` row.

### Operator commands

Retry and cleanup are CLI commands rather than anything automatic, for the same
reason node administration is (`services/control_plane/manage.py`) — and because
cleanup can destroy a database, which should take a person and a flag.

```bash
cp-manage project failed                  # what is stuck, on what error, retryable when
cp-manage project retry   --ref abcd1234  # resume at the first unfinished step
cp-manage project cleanup --ref abcd1234  # reclaim roles; keeps the database
cp-manage project cleanup --ref abcd1234 --allow-database-drop
```

Even with `--allow-database-drop`, cleanup refuses if the project ever reached
`PROVISIONED` or if the database holds a single tenant-created relation, and it
says so rather than exiting quietly. A cleanup that reclaims everything also
releases the node placement, so the project can be placed again instead of
holding capacity forever.

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
