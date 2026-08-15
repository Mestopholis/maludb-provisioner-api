# Provisioning State Machine

## Primary states

```text
REQUESTED
   |
   v
PLACEMENT_RESERVED
   |
   v
ROLES_CREATING
   |
   v
DATABASE_CREATING
   |
   v
BOOTSTRAPPING
   |
   v
KEYS_CONFIGURING
   |
   v
API_CONFIGURING
   |
   v
ROUTING_CONFIGURING
   |
   v
VALIDATING
   |
   v
ACTIVE
```

## Failure model

Each operational state may transition to:

```text
RETRY_WAIT
FAILED
```

`FAILED` does not imply that data may be deleted.

## Other lifecycle states

```text
ACTIVE
PAUSING
PAUSED
RESUMING
SUSPENDING
SUSPENDED
UPGRADING
DELETING
DELETED
```

Free API-worker sleep is service state, not project/database deletion.

## Idempotency

Each state handler must be safe to re-run or must explicitly detect that its work has already completed.

Implemented in `services/control_plane/jobs.py` as a list of steps, each with a `done` predicate. The predicates ask the **node** what is true — does the role exist, does the database exist — rather than reading `projects.status`. Status records what the control plane believed when it last wrote a row; a step that died midway leaves the two disagreeing, and the node is the one telling the truth.

Two consequences worth stating explicitly:

- `ROLES_CREATING` is not done until the roles exist **and** their passwords are recoverable from `project_credentials`. Roles without stored credentials is the state that strands a tenant: the passwords were generated in memory and lost, so nothing can authenticate and no later step can repair it. A retry therefore resets the passwords rather than skipping the step.
- `VALIDATING` is never considered done. It is a check, it is cheap, and its entire purpose is to stand between a half-provisioned tenant and a customer.

A partial unique index allows one open job per project, so two workers that pick up the same project race on the insert and exactly one proceeds. Concurrent runs would both reset the role passwords, and whichever committed second would leave the other's stored credential pointing at a password that no longer works.

## Retry

A failed attempt moves the project to `RETRY_WAIT` with a `retry_after` time, not to `FAILED`. `RETRY_WAIT` without a time is a project that gets retried immediately and fails immediately. After `MAX_ATTEMPTS` the project becomes `FAILED` and stops being retried automatically.

Every attempt gets its own `provisioning_jobs` row. Overwriting one row in place destroys exactly the history an operator needs — that a tenant failed twice before it succeeded.

`error_detail` never carries driver text. `CREATE ROLE ... PASSWORD 'literal'` appears verbatim in psycopg's error message, and `provisioning_jobs` is read by every operator dashboard and quoted into every support transcript. SQLSTATE carries the diagnosis without the statement.

## Data safety invariant

Once a project may contain customer data, a provisioning retry/cleanup path must never drop the database merely to restore desired state.

`jobs.cleanup` enforces this by refusing rather than acting:

- it only accepts projects in `FAILED` or `RETRY_WAIT`, so a provisioned or active project is not a cleanup candidate at any privilege level;
- it drops no database unless explicitly told it may;
- even then it refuses if the project ever reached `PROVISIONED`, or if the database holds a single relation the tenant created;
- roles are dropped only once the database is gone. Dropping them first leaves a database whose grants name roles that no longer exist — reachable by nobody, reclaimable by nothing;
- the shared `anon` / `authenticated` / `service_role` names are cluster-wide and are never dropped.

It returns a report saying what it refused and why, rather than succeeding quietly having done nothing.
