# Architecture

## High-level architecture

```text
Internet
   |
   v
MaluDB Gateway
   |
   +-- project resolution
   +-- API key validation
   +-- rate/concurrency enforcement
   +-- project status checks
   |
   v
Project Router
   |
   +----------------+----------------+
   |                |                |
PostgREST         Auth          Realtime (later)
per project      per project     controlled/opt-in
   |                |                |
   +----------------+----------------+
                    |
             pooling layer
             (when required)
                    |
                    v
              MaluDB nodes
                    |
       +------------+-------------+
       |            |             |
     db_A          db_B          db_C
```

## Infrastructure model

MaluDB nodes are pre-provisioned VMs running on Proxmox. Customer project creation does not create or clone a VM.

Each MaluDB node contains one MaluDB/PostgreSQL cluster that hosts many tenant databases.

```text
Proxmox
  |
  +-- MaluDB VM 01
  |     +-- PostgreSQL/MaluDB cluster
  |           +-- tenant DB A
  |           +-- tenant DB B
  |           +-- tenant DB C
  |
  +-- MaluDB VM 02
        +-- PostgreSQL/MaluDB cluster
              +-- tenant DB D
              +-- tenant DB E
```

## Project identity

Every project has a random/stable `project_ref`.

Example:

```text
project_ref: 4f9a8c2d
database:    mldb_4f9a8c2d
api URL:     https://4f9a8c2d.maludb.com
```

Do not directly use unvalidated user-provided project names as SQL identifiers.

## API-service model

Initial implementation:

- one PostgREST configuration/process per active project;
- one Auth configuration/process per active project;
- free project API processes may stop after an inactivity period and start on demand;
- paid project processes may remain warm;
- the database remains present even when an API worker sleeps.

This is an MVP implementation choice, not a guarantee that the platform will permanently use one OS process per tenant.

## Gateway

Responsibilities:

- resolve project from hostname;
- reject unknown/suspended/deleting projects;
- validate project API key;
- ensure hostname project and key project match;
- apply plan rate/concurrency rules;
- resolve service route;
- trigger worker startup when allowed;
- proxy request;
- emit usage/latency/error telemetry.

Do not place database superuser credentials in the gateway.

## Control plane

The control plane owns:

- accounts;
- organizations (optional early, expected later);
- projects;
- MaluDB nodes;
- project placement;
- plans;
- API key metadata;
- lifecycle/provisioning jobs;
- worker/service state;
- usage records;
- subscription/billing state later;
- backup metadata later.

## Data plane

The data plane consists of:

- gateway/router;
- per-project PostgREST/Auth processes;
- optional pooling;
- MaluDB nodes;
- tenant databases;
- Realtime/Storage components when enabled.

## Key separation

Do not conflate:

1. project API keys used by applications;
2. signed-in end-user Auth JWTs;
3. internal database/service credentials;
4. paid direct-database credentials.

They have different scopes and lifecycles.

## Scaling strategy

Scale in this order:

1. add more tenant databases to existing healthy nodes while within capacity;
2. add more API-service hosts;
3. add connection pooling;
4. add more MaluDB nodes;
5. introduce separate node pools for workload classes;
6. introduce tenant movement/rebalancing;
7. reconsider per-project API processes only when measured density requires it.
