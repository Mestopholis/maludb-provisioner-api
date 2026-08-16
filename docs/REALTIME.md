# Realtime

## Status

Phase 06 in progress. The validation this document required before
implementation was run on 2026-08-16 — results in
`specs/realtime-replication-model.md`, decisions ratified as ADR-031 and
ADR-032.

Slices 1 through 5 are complete. **Postgres Changes reach the official
`@supabase/supabase-js` client through the gateway, and a test proves it**:
`tests/test_realtime_compat.py` provisions a tenant, enables Realtime, lets the
gateway wake the project's own Realtime instance, and asserts that a row written
to a tenant table arrives at the client. `postgres_changes` is `supported` in
`specs/compatibility-matrix.yaml` on the strength of that test and nothing
else.

Two things that proof established, both recorded in
`specs/realtime-server-model.md`:

- **Realtime is one instance per project** (ADR-034), not one shared per node.
  Upstream derives its replication slot names from a server-level setting and
  PostgreSQL's are cluster-unique, so a shared server serves exactly one tenant
  per cluster — and the second tenant subscribes successfully and then silently
  receives nothing.
- **A Realtime instance costs ~146 MB**, against 31.8 MB for an entire warm
  project. It is by a wide margin the most expensive capability a project can
  enable, which is ADR-022's long-missing density term.

What slice 5 built: per-project Realtime workers under systemd and Podman, the
metadata database each instance keeps its tenant registry in, tenant
registration and deregistration over the server's admin API, the gateway's
per-project upstream lookup and wake, the sleep policy, and the compatibility
test above.

**Still unproven: RLS for Postgres Changes.** The replicator reads every table
past grants and row-level security, so the Realtime server is the only thing
that can enforce them. The compatibility test shows that a subscriber holding an
`anon` token receives changes from a table `anon` may select; it does not yet
show that a subscriber is refused rows a policy hides. That belongs with the
Broadcast and Presence work, and until it exists the matrix says
`postgres_changes` is supported, not that RLS over it is.

## Preparing a node

A node cannot host Realtime until it has been prepared and checked. Three of the
five settings need a cluster restart, which is an outage for every tenant
already on it, so this belongs in node build — a node prepared afterwards costs
downtime.

Node preparation for Realtime is now five settings **and three other things**:

- a container runtime and the pinned image (ADR-033), since upstream ships no
  binary;
- the `maludb-realtime@.service` unit from `deploy/`, installed like the other
  two worker units;
- a **Realtime data address** (ADR-035) — a private address on an interface of
  its own that PostgreSQL also listens on, named by `MALUDB_REALTIME_DB_HOST`.
  A Realtime instance is a container with no route to the node's loopback, and
  that is deliberate: with one it reaches every other worker on the node,
  including other tenants' PostgREST, which answers anonymous requests to
  anything that can open its port. ADR-031's `pg_hba.conf` reject has to name
  the new address too, or it re-opens the hole it closed.

```bash
cp-manage node realtime-check --name n1
```

It reads the node, records the answer on the node row, and exits non-zero if the
node is not ready, so a build script fails rather than printing a reason into a
log nobody reads. Placement reads the recorded answer: an unchecked node refuses
Realtime projects and keeps accepting every other kind, because the slot ceiling
should not strand capacity ADR-022 measured as usable.

The settings themselves are in `specs/realtime-replication-model.md` under
"Required node preparation". `scripts/realtime-test-cluster.sh` builds a
throwaway cluster carrying all of them, which is what the test suite runs
against.

## Enabling a project

```bash
cp-manage project realtime --ref abcd1234 --enable
cp-manage project realtime --ref abcd1234 --disable
```

Entitlement first, then node capacity, then the node itself — ordered so the two
refusals a customer can actually hit ("your plan does not include this", "this
node is full") never require touching the tenant database to discover.
`realtime_connections` is `0` on free, which is what makes free projects refuse.

Enabling creates the project's `mldb_<ref>_replicator` role — the one tenant role
holding `REPLICATION` — stores its credential, and takes one of the node's
replication slots. Disabling drops the slot, drops the role, and revokes the
credential. Turning a capability off reduces what exists, not just what is
reachable.

A plan downgrade takes Realtime away on the next provisioning run, the same way
direct SQL works. An *upgrade* does not turn it on: enabling creates a role
holding `REPLICATION`, which should be somebody's decision rather than a side
effect of a billing change.

**Custom plans must state `realtime_connections` explicitly.** Entitlement
resolution falls back to the free tier for any plan code it does not recognise,
and free is `0` — so a bespoke plan whose `config_json` omits the limit resolves
to "not entitled", and the next provisioning run will remove Realtime from
projects on it. That is the safe direction for a limit and the wrong surprise to
discover from a customer. The maintenance pass reports projects in that state
before a provisioning run acts on it:

```
rte00003: plan no longer includes Realtime, but the slot is still held;
          the next provisioning run removes it
```

### Choosing what replicates

Every tenant gets an empty `supabase_realtime` publication from bootstrap 009,
owned by the tenant admin role. A paid customer adds tables to it exactly as
they would on Supabase:

```sql
ALTER PUBLICATION supabase_realtime ADD TABLE your_table;
```

A table that is not in the publication produces no events. That is upstream's
behaviour, and it is not an error the client can see.

Note what the publication is *not*: it is not the authorisation boundary. The
replicator reads every table in the database past grants and RLS regardless, so
removing a table from the publication is choosing not to broadcast it, not
securing it. RLS for Postgres Changes is enforced in the Realtime server, and
slice 4 has to prove it is.

## The project's own server

Each Realtime project runs one instance of upstream's server (ADR-034), started
on demand and slept when idle:

```bash
cp-manage project realtime-worker --ref abcd1234 --status
cp-manage project realtime-worker --ref abcd1234 --start
cp-manage project realtime-worker --ref abcd1234 --stop
```

Rarely needed by hand. The gateway starts an instance when a client connects to
a project whose instance is asleep, and `cp-manage maintenance run` stops one
that has had no connection for an hour — an hour rather than the fifteen minutes
the other workers get, because the two sides of the trade are different here:
an instance is worth ~146 MB, and waking one costs **9 seconds** against
PostgREST's third of a second.

That nine seconds is why a client's first connection after a sleep is **refused
with close code 1013** while the wake proceeds in the background (ADR-036). The
official client reconnects on its own backoff and the next attempt succeeds; a
gateway that held the socket instead would fail the same connection ten seconds
later, because ten seconds is when the client gives up.

Each instance has its own metadata database, `maludb_realtime_<ref>`, which is
platform state rather than tenant data — it holds the server's tenant registry
and the replicator credential it connects with, encrypted under a key derived
per instance. It is dropped when Realtime is disabled. One database per project
rather than one per node is not tidiness: upstream's peer discovery runs through
that database, so a shared one would cluster every tenant's Realtime server into
a single distributed Erlang cluster (ADR-035).

## Connecting

```text
wss://<project-ref>.<gateway-domain>/realtime/v1/websocket?apikey=<key>&vsn=1.0.0
```

Which is what `@supabase/supabase-js` builds on its own — the key travels in the
query string because a browser cannot set headers on a WebSocket handshake. The
gateway also accepts the `apikey` and `Authorization` headers for server-side
clients.

The client also sends its key a **second** time, inside the payload of every
channel join. On Supabase that value is a JWT; ADR-028's keys are opaque, so the
gateway translates that one field on the way through (ADR-036). Without it the
socket connects and every channel fails with `MalformedJWT` — which is what the
compatibility suite found, and what a hand-written test client would not have.

The project comes from the hostname and the key is checked against *that*
project, exactly as on the request path. A refused connection is closed during
the handshake, so nothing that failed authentication ever holds a socket.
`docs/API-GATEWAY.md` has the close codes and the rest of the differences from
the HTTP path.

Connection counts come from `realtime_connections` on the plan — the same number
that decides whether a project may enable Realtime at all.

## Watching the slots

```bash
cp-manage realtime slots [--node n1]     # every slot a node holds
cp-manage capacity report                # slots as a ceiling, beside warm and connections
cp-manage maintenance run                # detects invalidation, writes the audit event
cp-manage project realtime-recover --ref abcd1234   # re-create an invalidated slot
```

An invalidated slot means that project has stopped receiving changes and nothing
in its connection says so. Re-creating the slot resumes from the present and
**does not replay the gap**; every report says so, because the alternative is a
customer who assumes a backfill that never happened.

Recovery is deliberately a command a person runs, not something the maintenance
pass does. ADR-032 makes invalidation a project-visible incident, and a platform
that repaired it quietly would turn a reportable failure back into a silent one
— which is the outcome the whole design exists to avoid.

## Compatibility target

Initial target: Supabase-compatible Postgres Changes.

Later:

- Broadcast;
- Presence.

## Resource concerns

Realtime has persistent connections and different economics from REST.

Controls must include:

- enabled/disabled state by project/plan;
- max simultaneous connections;
- message/change rates;
- payload limits;
- idle behavior;
- per-node/service capacity.

Do not automatically allocate unlimited Realtime resources to every free project.

## Architecture question — answered 2026-08-16

Before implementation, validate the upstream Realtime multi-tenancy model against the MaluDB database-per-project architecture. Prefer using upstream protocol/software before inventing a new client protocol.

The upstream model fits, with one precondition the architecture did not previously have. Logical decoding requires the PostgreSQL `REPLICATION` role attribute, and a role holding it can take a physical base backup of **every database on the cluster** — the ADR-014 `CONNECT` lockdown does not reach that path. Containing it is node configuration (a `pg_hba.conf` reject of physical replication), not tenant provisioning, and it is required whether Realtime runs shared or per-project. See `specs/realtime-replication-model.md` and ADR-031.

Two further properties belong here rather than in the implementation. A replication consumer reads every table in its database past grants and row-level security, so **RLS for Postgres Changes is enforced by the Realtime server, not by PostgreSQL** — the compatibility suite must prove it is. And a stalled consumer pins WAL until the disk fills, taking down every tenant on the node, so bounded retention is mandatory and an invalidated slot is a reportable incident (ADR-032).

Not yet answered: what a Realtime process costs. Upstream ships as a container image only and Docker is absent from the development host, so ADR-022's density numbers have no Realtime term yet.
