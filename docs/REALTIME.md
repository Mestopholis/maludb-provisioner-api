# Realtime

## Status

Deferred until the Data API and Auth foundations are stable.

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

## Architecture question

Before implementation, validate the upstream Realtime multi-tenancy model against the MaluDB database-per-project architecture. Prefer using upstream protocol/software before inventing a new client protocol.
