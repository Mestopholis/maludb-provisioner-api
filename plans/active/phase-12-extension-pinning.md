# Execution Plan: Phase 12 — Extension version pinning (ADR-075)

Status: IN PROGRESS — pinning slice 0 measured 2026-09-12 (`specs/extension-pinning-model.md`); pinning slice 1 is next.

**Slices here are numbered "pinning slice N"** so they are not mistaken for the
data-model graph's slices 0–6 in `plans/completed/phase-12-maludb-features.md`.
Human owner: Joseph Lehman
Agent: Claude Code
Branch: `plan/adr-075-extension-pinning`, then one branch per pinning slice
Related task: `tasks/PHASE-12-MALUDB-FEATURES.md`
Dependencies: Phase 12's data-model graph merged, including graph slice 6 (#118),
because pinning slice 3 extends the extension upgrade command graph slice 1 built and its
per-tenant step re-enables data-model schemas. Migration numbers below assume
#118's `0035` is on `main`.

## Objective

Make one sentence true that is currently false:

> No two tenants on a node can differ in `vector` or `maludb_core` version once
> an upgrade run has passed them, no node runs a version CI has not tested, and a
> node whose packages have moved without the platform knowing takes no new work.

This is a prerequisite, not a feature. Vector search gets its own decision and
plan afterwards, and assumes this one.

## What ADR-075 decided, so this plan does not relitigate it

1. The pin controls what the node runs: packages held, versions installed
   explicitly, a node that disagrees refused.
2. `vector` and `maludb_core` exactly; contrib follows the PostgreSQL minor,
   recorded and reported, never held.
3. `cp-manage extension upgrade` covers `vector`; a pin change is one procedure
   — pin, package, tenants through canary-then-batches.
4. A mismatched or unpinned node takes no new projects, restores or moves in,
   and keeps serving. The upgrade run is the one operation allowed.
5. `specs/` lists the versions CI tested; each node's pin is an audited
   control-plane row that must be on that list.

## What is already true

- `provisioning.install_extension` runs `CREATE EXTENSION IF NOT EXISTS
  maludb_core CASCADE`, which installs `vector`, `btree_gist`, `pg_trgm` and
  `pgcrypto` at whatever `default_version` the node provides, then records
  `projects.extension_versions` (migration 0005).
- `cp-manage extension upgrade` (Phase 12 slice 1, `extension_upgrade.py`)
  upgrades `maludb_core` only: canary, batches, one transaction per tenant,
  verifying ADR-018's revoke, ADR-045's trigger and re-enabling a platform-owned
  memory schema; every attempt in `extension_upgrades` (migration 0032). It
  refuses `draining` and `unhealthy` nodes, not `maintenance`.
- `nodes.NodeCapacity.rejection_reason` is what placement consults, and
  `tenant_movement` refuses a target whose `rejection_reason` is set — so a new
  reason reaches moves without a second check. `realtime_ready` and
  `backup_ready` are the "unchecked reads as unprepared" precedent, stored in
  `capacity_json` by `cp-manage node realtime-check` / `backup-check`.
- CI builds `maludb_core` from `MALUDB_CORE_REF` and installs
  `postgresql-17-pgvector` from apt **unpinned**.

## What was not known, and is why pinning slice 0 came first

Answered by pinning slice 0; each bullet says what was found.

- **Nobody has measured an `ALTER EXTENSION vector UPDATE` on a tenant with
  data.** Whether it takes locks that block customer queries, whether it touches
  HNSW/IVFFlat indexes, and how long it takes are unknown. The per-tenant
  transaction design assumes it is quick and catalogue-only.
  **Found:** 356–511 ms, catalogue locks only, no reader or writer waited, no
  index rebuilt or invalidated; nearly all the time is ADR-018's trigger. Every
  0.8.x upgrade script is empty — the package is the upgrade (findings 1, 4).
- **Nobody has run an older `vector` extension version under a newer library.**
  Decision 3 says a lagging tenant keeps serving "because pgvector's newer
  library runs older extension versions". That is pgvector's stated policy, not
  something this repository has observed. **Found:** it does, in sessions opened
  before and after the swap — but a session opened before keeps the *old* library
  mapped until it disconnects (findings 2, 3).
- **Whether ADR-018's event trigger covers functions a `vector` update adds** is
  assumed from its design; the `maludb_core` upgrade path verified it, `vector`'s
  has not. **Found:** it does (finding 5).
- **Whether `maludb_core` constrains the `vector` version** it runs against —
  its control file says `requires = 'vector, …'` with no version — is unknown in
  practice. **Found:** 0.104.0 and its own HNSW index work under 0.8.6
  (finding 3).
- **Where a restore touches the node.** Decision 4 refuses restores on a
  mismatched node; the restore path's actual contact with the tenant's node has
  to be confirmed before choosing where that check goes. **Found:** both moves
  and restores `pg_restore` a dump whose `CREATE EXTENSION` carries no version,
  so the receiving node's package decides the tenant's version (finding 6).

**And one finding outside pinning** (finding 7): no customer role — not even
`service_role` or the tenant admin — can execute any extension function in
`public`, so `vector`'s distance operators, `similarity()` and `crypt()` are all
refused. ADR-018's revoke from `PUBLIC` took every role with it. Recorded in
`docs/OPEN-QUESTIONS.md` for the owner; it blocks vector search, not this plan.

## Scope

- A tested-versions list in `specs/`, and a test that keeps CI honest to it.
- Per-node pins, set and shown through `cp-manage`, audited, refused off-list.
- A node check comparing each pin with what PostgreSQL reports; a placement
  rejection for a mismatched or unpinned node, reaching moves and restores.
- Provisioning installs pinned versions explicitly and re-checks at install.
- `cp-manage extension upgrade` for `vector`; a drift report of tenants lagging
  their node's pin, and of contrib versions and PostgreSQL minors across nodes.
- CI installs the newest listed versions exactly.
- The runbook: holding packages at node build, rolling out a pin change, and the
  note that existing deployments stop placing until each node is pinned.

## Non-goals

- **Vector search itself.** A later decision and plan.
- **Pinning the PostgreSQL minor or contrib extensions.** Decision 2.
- **Installing packages from the control plane.** The hold and the install are
  node-build and operator steps; the control plane verifies outcomes.
- **Downgrades.** PostgreSQL has no general extension downgrade; a pin may only
  move to a version the node's tenants can reach by `ALTER EXTENSION … UPDATE`.
- **The pre-existing `maludb` database at `vector` 0.8.3.** Not a tenant.
- **Customer-installed extensions beyond `vector`** (ADR-045's allowlist). They
  are contrib and follow the minor.

## Implementation steps

### Pinning slice 0 — Measure before building (done 2026-09-12)

No product code. A spike on a disposable cluster, in the class of the data-model
graph's slice 0, recorded in `specs/extension-pinning-model.md` with a script that
reproduces it.

- Build tenants with a table carrying an HNSW and an IVFFlat index and some
  thousands of rows at `vector` 0.8.4; install 0.8.6's package; confirm the
  tenants still answer vector queries **before** any `ALTER EXTENSION`.
- `ALTER EXTENSION vector UPDATE TO '0.8.6'` on one: locks taken (from
  `pg_locks` in a second session, with a concurrent query), time, whether
  indexes are rebuilt or invalidated, and every function it adds to `public`.
- Confirm ADR-018's trigger revokes those from `anon`, as an outcome.
- Confirm `maludb_core` 0.104.0 loads and its vector-backed functions run under
  0.8.6.
- Confirm where `restore.py` and `tenant_movement.py` touch a node, to place
  decision 4's refusal.

**Stop and report** if an update takes a lock that blocks reads for longer than a
catalogue change, or rebuilds indexes: decision 3's one-transaction-per-tenant
run would then be a customer outage per tenant, and that goes back to the owner
rather than into code.

### Pinning slice 1 — Pins, the list, and the refusal

- `specs/extension-versions.yaml`: for `vector`, each tested `extversion` with its
  Debian package version; for `maludb_core`, each tested `extversion` with its
  upstream commit. Loaded and validated by a small module.
- Migration `0036_node_extension_pins.sql`: one row per node and extension —
  version, who set it, when. Not in `capacity_json`: a pin is an operator
  decision with an audit trail, not a measurement.
- `cp-manage node pin set --node <n> --extension vector --version 0.8.4`,
  refused off-list; `node pin show`. Audit `node.extension_pin.set`.
- `cp-manage node extension-check --node <n>`: reads `default_version` for each
  pinned extension and `server_version`, stores the result with a timestamp
  beside the Realtime and backup checks, and exits non-zero on a mismatch.
- `NodeCapacity.rejection_reason` gains the pin reason — no pin, or pin ≠
  provided — naming the extension, both versions and the command to run.
- Restores refuse on the same reason at `restore_tenant`'s start: a restore
  `pg_restore`s into the live node, so it installs that node's `default_version`
  (pinning slice 0, finding 6).
- **Moves are refused between nodes whose pins differ**, not only onto a
  mismatched node. A dump carries `CREATE EXTENSION` with no version, so a lower
  target silently downgrades the tenant and a higher one lands it ahead of the
  run (finding 6).
- **A pin may not move down on a node with tenants**: their catalogues have no
  downgrade path (finding 6).
- The node check also reports **backends still mapping a replaced `vector.so`**,
  read from `/proc/<pid>/maps`, and records the node as at its pin only when there
  are none — an installed package does not reach sessions already open
  (finding 2).
- Test fixtures pin their nodes, since an unpinned node now places nothing.

### Pinning slice 2 — Provisioning installs the pin

- `install_extension` issues `CREATE EXTENSION IF NOT EXISTS vector VERSION
  <pin>` then `maludb_core VERSION <pin> CASCADE` (the rest follow the minor),
  after re-reading `default_version` on the tenant connection and refusing if it
  disagrees with the pin — the check at the moment of install, which a stale
  node report cannot give.
- Verifies installed versions equal the pins before recording
  `extension_versions`. A refusal leaves the project retryable, like every other
  provisioning step.

### Pinning slice 3 — The upgrade run covers `vector`, and drift is reported

- `extension_upgrade` takes `--extension vector|maludb_core`, defaulting the
  target to the node's pin rather than to the package's default, and refusing a
  target that is not the pin.
- The per-tenant verification gains `vector`'s outcomes from pinning slice 0:
  nothing added is executable by `anon` or `authenticated` (finding 5), and no
  HNSW or IVFFlat index is left invalid (finding 4). One transaction per tenant
  stands — the update blocked no reader or writer — at ~0.5 s a tenant, nearly
  all of it ADR-018's trigger.
- Order when both move: `vector` first, since `maludb_core` requires it.
- `cp-manage extension drift`: tenants whose recorded versions lag their node's
  pins, nodes with no pin or a stale check, and contrib versions and PostgreSQL
  minors across the fleet, reported and never refused. It says what lag means: for
  a step with no DDL a lagging `extversion` is bookkeeping, not old code.

### Pinning slice 4 — CI and the runbook

- CI installs `postgresql-17-pgvector=<newest listed>` exactly, and a test fails
  if `MALUDB_CORE_REF` or the installed `vector` is not the list's newest entry.
- `docs/MALUDB.md` runbook: `apt-mark hold` at node build; a pin change in the
  order pin → package → cycle the node's workers → `extension-check` → upgrade
  run; pruned versions and
  `apt-archive.postgresql.org`; the rollout note that an existing deployment
  places nothing until each node is pinned.
- `docs/DEPLOYMENT.md` pointer, OPEN-QUESTIONS closure,
  plan to `plans/completed/`.

## Verification

- [x] Pinning slice 0 measurements recorded, with a reproducing script.
- [ ] An unpinned node and a node whose `default_version` disagrees each refuse
      placement, a move in, and a restore — and each still serves its existing
      tenants (negative control: remove the check, watch placement succeed).
- [ ] A pin off the tested list is refused.
- [ ] Provisioning on a node whose package moved after its last check is refused
      at install, not placed.
- [ ] A `vector` upgrade run leaves every tenant at the pin, stops at a failing
      tenant with it rolled back, and nothing added by the update is callable by
      `anon`.
- [ ] CI fails when its installed versions are not the list's newest.
- [ ] Existing suites unchanged, compatibility included.

## Risks

- **Deploying pinning slice 1 stops placement on every existing node** until pinned.
  Intended (decision 4), but it is an outage of project creation if deployed
  without the runbook step; the PR description and the release note say so.
- **pgdg prunes old packages.** A node rebuilt after pruning cannot install its
  pin from the main repo; the runbook names the archive, and a version available
  from neither comes off the list.
- **A slow `vector` update inside the per-tenant transaction** holds that tenant's
  locks for its duration. ~~Unmeasured.~~ Measured: catalogue locks only, no
  reader or writer waited (finding 4).
- **Refusing restores on a mismatched node** delays a point-in-time recovery until
  the mismatch is fixed. Knowingly accepted by ADR-075; the rejection names the
  fix so an operator mid-incident is not left to find it.

## Decision log

- 2026-09-12 — ADR-075 accepted, five questions decided one at a time by the
  repository owner: where the pin lives, what is pinned, rollout, mismatch
  behaviour, and where the manifest lives.
- 2026-09-12 — Pins as rows rather than in `capacity_json`: `capacity_json` holds
  what a check measured, and a pin is what an operator decided. Mixing them would
  let a re-run check overwrite a decision.

## Progress log

- 2026-09-12 — Plan written. No code.
- 2026-09-12 — **Pinning slice 0 measured**, in a disposable PostgreSQL 17.11
  container with real provisioned tenants: `specs/extension-pinning-model.md`,
  `scripts/spike-extension-pinning.py`. The stop condition was not met, so the
  one-transaction-per-tenant rollout stands. Three amendments to pinning slice 1
  (moves between differing pins, no downward pins, backends on a replaced
  library) and one finding for the owner outside this plan: extension functions
  in `public` are unusable by every customer role.
