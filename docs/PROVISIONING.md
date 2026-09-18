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

## Deleting a project (free slice 10b)

`cleanup` reclaims a **failed** project and refuses a database that was ever handed over. That
refusal is deliberate -- it is what stops a reconciliation pass destroying customer data to restore
desired state -- so deletion is a separate path, and the only one that destroys a live tenant.

1. **The request** (`jobs.request_deletion`, from `DELETE /v1/projects/{ref}` or
   `cp-manage project delete`). The status becomes `DELETING`, which is outside the gateway's
   `SERVING_STATUSES`, and **every API key is revoked**. Both happen before the customer is answered:
   a key already in an app must stop working now, not when a worker reaches it.
2. **The work** (`jobs.delete_project`, in the provisioner). It refuses a project with no recorded
   request, a database whose name disagrees with the one the ref derives, and a project with a
   provisioning attempt still open. Then, in order: the storage worker's registration, the database,
   the objects in the platform bucket, the per-tenant roles, and the project's stored credentials.
3. **The record.** `status = 'DELETED'`, `deleted_at` set, `node_id` cleared, two audit events. The
   row is kept: a project ref appears in a public hostname (ADR-008) and must never be reissued, and
   the audit trail has to outlive the data it describes.

**Every** per-tenant role, not the ones every project has. Four are conditional -- `replicator`
(Realtime), `vectors` (ADR-077), `memwriter` and `memreader` (ADR-079) -- and the list `_drop_roles`
carried was written out by hand and had not grown with them, so the first deletion on the rehearsal
deployment left `memreader` and `memwriter` on the cluster, `memwriter` being a LOGIN role, while the
audit recorded a complete deletion. The list now comes from `TenantNames.roles`, which derives it from
the names themselves; a role that does not exist is skipped, and a new role name joins by being a field.

**The stored credentials go too** (`project_credentials`, one encrypted password per role). Once the
roles are dropped the rows authenticate nothing, and what remains is recoverable plaintext for a
project the customer asked to be rid of -- in every control-plane dump, and unwrapped one by one by
`control-plane verify`. They are deleted rather than marked `revoked_at`, which is the rotation path's
answer and keeps the ciphertext. `api_keys` rows are *not* deleted: those are stored hashed, so a
revoked row is a record rather than a secret, and the audit trail wants it.

**So do the customer's model provider keys** (`project_provider_keys`), and that is the stronger case.
A `project_credentials` row authenticates a role deletion has just dropped, so what survived was
useless as well as wrong; an Anthropic, OpenAI or Voyage key goes on working at the provider, and
spending the customer's money, after they have deleted the project they gave it to. `set_key` marks
the row it supersedes `revoked_at` and keeps the ciphertext -- right for rotation, wrong for a project
that no longer exists -- and `0043`'s `ON DELETE CASCADE` never fires, because deleting a project keeps
its row on purpose. So until free slice 10e nothing removed them at all.

`remove_key`, the customer's own removal of a key from a *live* project, deletes the row outright
rather than revoking it (also 10e): nothing reads a revoked row -- every query filters
`revoked_at IS NULL` -- so keeping the ciphertext made "remove" mean "stop using, still hold". The
record of the removal is an audit event carrying the provider and the key's four-character hint. The
row superseded by a rotation is still kept, which `0043` states as the design and which is a decision
to revisit rather than a fix.
