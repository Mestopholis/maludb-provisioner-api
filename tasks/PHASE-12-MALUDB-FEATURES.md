# Phase 12 — MaluDB-Native Features

Plan: `plans/completed/phase-12-maludb-features.md`

**Decided 2026-09-12 (ADR-074):** the data-model graph leads — opt-in per
project, reached through PostgREST RPC via platform-owned wrappers, on every
plan with refresh limited per plan. ADR-074 amends ADR-015: the extension stays
unconditional; the customer-facing surface is opt-in.

**Decided 2026-09-12 (ADR-075):** before vector search, `vector` and
`maludb_core` versions are pinned per node from a tested list. Plan:
`plans/active/phase-12-extension-pinning.md`.

**Decided 2026-09-12 (ADR-076):** customer roles may execute extension functions,
which no role could; a PostgREST pre-request check keeps them off `/rpc`. Also
before vector search. Plan: `plans/completed/extension-function-grants.md`.

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

- [x] ADR-013 ratified, so "tenant" has one agreed meaning across both layers.
- [x] The per-tenant install question resolved — every tenant database
  (ADR-015, as amended by ADR-074).
- [x] A tenant-fleet extension upgrade procedure, since the extension is
  per-database. `cp-manage extension upgrade` (Phase 12 slice 1): a canary, then
  batches, each tenant upgraded and verified in one transaction so a failure
  leaves it on its previous version. Runbook in `docs/MALUDB.md`.

## Acceptance criteria

Met for the data-model graph (2026-09-12). Each later surface — the memory
pipeline, vector search, the knowledge graph — needs its own decision and plan,
and these criteria apply to it again.

- [x] Existing compatibility suite continues to pass — `tests/test_compatibility.py`,
  the 26 existing official-client cases unchanged beside 10 new ones.
- [x] New behavior is explicitly documented — `docs/MALUDB-FEATURES.md` for
  customers, `specs/compatibility-matrix.yaml` `maludb_extensions`, ADR-074.
- [x] MaluDB feature does not silently change Supabase method semantics —
  measured: the `public` schema's published OpenAPI description is identical
  before and after enabling, and a deliberate leak into `public` fails the test.
