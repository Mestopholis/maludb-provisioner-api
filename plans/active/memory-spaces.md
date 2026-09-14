# Execution Plan: memory spaces (ADR-079)

Status: IN PROGRESS — slice 0 (measurement) done 2026-09-14; ADR-079 accepted. The
vector wrapper fence (a prerequisite) is its own pull request.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/memory-pipeline-spike` for slice 0 and the ADR; one branch per slice after
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md`
Dependencies: ADR-074 (wrappers, platform-run refresh), ADR-077 (definer pattern; fenced),
ADR-078 (pipeline data survives moves and restores), ADR-023 (KEK), ADR-038 (worker split)

**Slices are numbered "memory slice N".**

## Objective

A customer sends text to a named memory space with their secret key and, using their
own provider keys, gets searchable memory back — with no superuser-owned code reachable
from a request, per-item results for every write, and per-plan limits enforced before
work is queued.

## Scope

- Memory spaces per project: create, list, delete; opt-in per project; plan limits.
- Provider keys per project: store (write-only), rotate, delete; three fixed providers.
- Ingest of raw text → extraction → embeddings → harvest → searchable, run by a dedicated
  memory worker as a per-project writer.
- Direct ingest of already-embedded edges (the path slice 0 measured).
- Search through a narrow reader wrapper.
- Moves, restores and extension upgrades with spaces present.

## Non-goals

- Space-scoped keys, end-user (JWT) access, cross-space sharing (ADR-079 decision 2).
- Platform-paid models; customer-supplied endpoints (decisions 4, 5).
- The knowledge graph and bitemporal surfaces beyond what search and harvest need.
- MaluDB accounts (decision 1).

## Preconditions

- The ADR-077 fence merged: vector wrappers filter `owner_schema = 'maludb_core'` and
  search by compartment id.
- maludb_core 0.105.x pinned (0.105.1 once maludb-core#32 merges, so embeddings survive a
  dump exactly).

## Implementation steps

### Memory slice 0 — Measure (done 2026-09-14)

`specs/maludb-memory-pipeline-model.md`, `scripts/spike-memory-pipeline.py`.

### Memory slice 1 — The writer role, measured before anything depends on it (done 2026-09-14)

- A per-project `LOGIN` writer with `CREATE` on its spaces and `CONNECT` on its own database
  only (ADR-014): does it pass `maludb_memory_ingest_edge`, `request_extraction` and
  `harvest_extractions`? Which extension role or grants does it need to `EXECUTE` them,
  and what else do those grants reach — other spaces in the database, other databases on
  the cluster (the `maludb_*` roles are cluster-wide)?
- If it cannot be made narrow, stop and reopen ADR-079 decision 6.

**As built/measured** (`specs/maludb-memory-pipeline-model.md` § "Memory slice 1",
`scripts/spike-memory-pipeline.py writer`): decision 6 **holds as written, with one
correction and one addition.** The minimal working set is a `LOGIN` writer
(`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT`), `CONNECT` on
its own database only, `USAGE` on `maludb_core`, `USAGE` + `CREATE` on each space, and
per-object `EXECUTE` on that space's facades — measured end to end (ingest → request →
drain → harvest → search). Correction: the extension's *executor rights are not needed* —
`maludb_memory_executor` membership (cluster-wide, `NOINHERIT`, holding EXECUTE on
auth/secret functions) is strictly wider than the per-object grants and unnecessary; the
guard reads `session_user`, so `SET ROLE` on the node connection would not satisfy it.
Reach is bounded to the one database, the granted space(s), and `maludb_core` only through
the granted facades (0 PUBLIC-executable functions). No escalation found through `CREATE`
on the space (definer paths keep the space off `search_path`; the writer owns nothing it
can trigger/replace/drop) — one latent invariant flagged for slice 2's per-upgrade check:
assert every space-first `SECURITY DEFINER` function references only qualified objects.
Addition: the writer is a new per-project role; slice 2 must add it to `TenantNames`,
create it on a move's target (`prepare_target_roles`) and in `restore.missing_roles` —
a dump/restore round trip confirmed its grants are silently dropped when the role is
absent on the target, exactly like `create_vectors_role` (ADR-077).

### Memory slice 2 — Spaces

- `projects`-scoped space catalogue in the control plane; reserved schema names; per-plan
  limits (spaces, memories, ingests/hour) in `entitlements` and `specs/plans-and-limits.yaml`.
- Enable a space: re-verify the project's vector wrappers first (fenced), then
  `enable_memory_schema` over the platform connection; delete a space.
- Move, restore and upgrade a tenant with spaces: the space's superuser-owned objects and
  data arrive, ownership verified (extends the end-to-end move test).

### Memory slice 3 — Search

- `maludb.memory_search(space, query, ...)` owned by a per-project reader, grants derived
  from the installed extension; parity test against the facade on the pinned version.
- `anon`/`authenticated` refused; unknown space 404; another space's rows never returned.

### Memory slice 4 — Provider keys

- Per-project secret type; write-only API; KEK-encrypted; never logged; deleted with the
  project; dashboard form.

### Memory slice 5 — Ingest and the worker

- Enqueue routes (edges with embeddings; raw text) with limits at enqueue; request status
  with per-item results.
- Dedicated memory worker process and unit; writer connection per tenant; outbound only to
  the three provider hosts (enforced in `deploy/`, asserted by a test like
  `tests/test_deploy_units.py`).
- Extraction → embeddings → harvest; skipped items reported.

### Memory slice 6 — Compatibility, docs, launch

- Official-client test (`supabase.schema('maludb').rpc('memory_search', ...)`).
- `docs/MALUDB-FEATURES.md`, compatibility matrix, limits confirmed by the owner.

## Verification

- [x] Slice 0 measurements recorded with reproduction.
- [x] Writer role passes the facades narrowly (measured slice 1; decision 6 holds, executor
      rights not needed — per-object `EXECUTE` on the space facades is enough).
- [ ] No superuser-owned function reachable from any request role (asserted, like ADR-077).
- [ ] Space isolation: search and ingest through the platform never touch another space.
- [ ] Provider keys never appear in logs, responses, dumps of the control plane in clear.
- [ ] Worker egress limited to three hosts, asserted.
- [ ] Move, restore and upgrade with spaces verified end to end.
- [ ] Every write reports skipped items.

## Risks

- **The writer role needs a cluster-wide `maludb_*` grant.** Would widen reach across every
  database on the node; slice 1 measures it before anything is built on it.
- **Search parity drifts on an upgrade.** The wrapper re-implements upstream's query;
  mitigation: parity test in the ADR-075 tested-versions gate.
- **Silent skips.** Upstream succeeds while dropping items; mitigation: per-item results
  compared against what was sent.
- **Queue latency** unmeasured; agents that write then search may not see their write yet.
  Measure in slice 5 and publish it.
- **Egress from the control-plane host** is new; restrict in deployment, not just code.

## Decision log

- 2026-09-14 — ADR-079 accepted: memory spaces; secret key only; wrapper reads, platform
  writes; platform calls models with the customer's keys (revised from customer-supplied
  outputs the same day); OpenAI/Anthropic/Voyage fixed hosts; dedicated worker as a
  per-project writer; every plan with tiered limits.

## Progress log

- 2026-09-14 — Slice 0 measured (spec above). Found ADR-077's vector wrappers unfenced;
  fix raised as a separate pull request before any space can exist.
- 2026-09-14 — Slice 1 measured (`writer` subcommand, spec § "Memory slice 1"). Decision 6
  holds: a narrow per-project writer login runs the whole pipeline through the space facades
  on their definer rights, needing only `CONNECT` (own db), `USAGE` (`maludb_core`),
  `USAGE`+`CREATE` (its spaces) and per-object `EXECUTE` on the space facades — not the
  extension's executor rights. Reach bounded to its own db and granted spaces; no
  escalation through `CREATE`. Corrections carried into slice 2: (1) provision the writer
  role on move/restore targets like `create_vectors_role`; (2) add a per-upgrade assertion
  that space-first `SECURITY DEFINER` functions reference only qualified objects.
