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

## Data safety invariant

Once a project may contain customer data, a provisioning retry/cleanup path must never drop the database merely to restore desired state.
