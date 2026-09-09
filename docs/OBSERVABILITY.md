# Observability

## Per-project metrics

Collect where practical:

### API

- request count;
- status classes/errors;
- latency;
- rejected/throttled requests;
- bytes in/out.

### Database

- connections;
- active queries;
- query duration;
- transactions/queries;
- rows read/written where practical;
- database size;
- temp-file usage;
- deadlocks/lock waits;
- WAL generation where practical.

### Email

Per project, sourced from the relay (ADR-019):

- sent;
- delivered;
- hard/soft bounces;
- complaints;
- quota-rejected sends;
- complaint rate, which drives abuse review and automatic sending suspension.

Recipient addresses must not be stored in the control plane in plaintext — see
`specs/control-plane-schema.sql`.

### Realtime later

- connections;
- messages;
- changes delivered;
- rejected/limited events.

## Node metrics

- CPU;
- memory;
- disk used/free;
- disk latency/IO;
- PostgreSQL connection pressure;
- active queries;
- checkpoint/WAL pressure;
- tenant count;
- node health.

## Uses

Metrics drive:

- customer dashboard;
- node scheduling;
- throttling;
- incident response;
- pricing/plan design;
- capacity planning.

## Logging

All logs must include safe correlation identifiers such as request ID and project ID/ref, but never full secrets.

## Alerts

Everything below is produced today by `cp-manage maintenance run`, which is one
process reading the control plane and, for some passes, the nodes. Each pass
returns handled/failed counts plus notes; the notes are the alert text.

| Condition | Pass | What it means |
| --- | --- | --- |
| Node at 80% of a ceiling — projects, warm projects, connections | `capacity` | Placement still succeeds. Somebody has time to add a node or move tenants (ADR-066: by hand). |
| Node **at** a ceiling | `capacity` | Placement is already refusing. A customer creating a project sees it. |
| Free disk below twice the placement floor, or unreported | `capacity` | The ceiling that takes a node down rather than merely refusing work. Unreported means the node stopped sending health. |
| Node with no backup, or a stale one | `backups` | ADR-064. The node is still fine for the tenants on it; what is lost is the ability to rebuild it. |
| A tenant's replication slot is gone or inactive | `slots` | ADR-032 made invalidation the designed outcome of a stalled consumer, so this is expected rather than exceptional. The project then receives no changes and **nothing in its connection says so**. |
| Provisioning stuck, retried, or failing | `retry` | A project the customer believes exists. |
| Subscription past its grace window | `grace` | ADR-051. |
| Objects the metadata and the store disagree about | `objects` | Expected after a point-in-time restore, which returns rows to the past and leaves bytes in the present (ADR-069). |
| Project entitlements drifted from its plan | `plan_drift` | Report only; nothing reconciles automatically. |

**Delivery is unwired, deliberately and visibly.** There is no pager
integration, no alert manager, and no threshold stored anywhere but the
process's own defaults. The maintenance run is the only channel, so a
deployment that does not run it on a schedule and read its output has none of
the above — which is a deployment question rather than a control-plane one, and
`plans/` names it a non-goal rather than pretending otherwise. The one number
that is a parameter today is the capacity warning fraction
(`maintenance.CAPACITY_WARN_AT`, 0.8), because per-node targets are still open
in `docs/CAPACITY.md`.

Alert *thresholds* for the per-project and node metrics above are not set here
either. They need the production hardware profile that `docs/CAPACITY.md` open
items are waiting on.
