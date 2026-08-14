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
