# Phase 12 — MaluDB-Native Features

## Objective

Expose MaluDB's differentiating memory/database capabilities without weakening Supabase compatibility.

## Scope

The MaluDB feature set is inventoried in `docs/MALUDB.md` (extension 0.104.0).
Candidate surfaces, to be prioritized into a product decision:

- memory pipeline (source → claim → fact → episode) and bitemporal history with
  supersession;
- SVPOR knowledge graph: path finding, communities, degree/surprise analytics,
  `maludb_graph_import`;
- relational data-model graph (`maludb_datamodel_refresh` /
  `maludb_datamodel_describe`) — plausibly the most immediately marketable
  surface for developer tooling;
- vector search, retrieval planner, and query hints;
- workflow extraction and the governed skill runtime;
- model registry and embedding adapters.

Delivery mechanisms:

- SQL functions inside the tenant database via the `maludb_core` schema;
- separate `/maludb/v1` gateway endpoints;
- MaluDB SDK (Python, Node.js, PHP, C drivers already exist upstream);
- optional Supabase-compatible integration helpers.

## Prerequisites

- ADR-013 ratified, so "tenant" has one agreed meaning across both layers.
- The per-tenant install question resolved: is `maludb_core` present in every
  tenant database, or installed on opt-in? See `docs/OPEN-QUESTIONS.md`.
- A tenant-fleet extension upgrade runbook, since the extension is per-database.

## Acceptance criteria

- [ ] Existing compatibility suite continues to pass.
- [ ] New behavior is explicitly documented.
- [ ] MaluDB feature does not silently change Supabase method semantics.
