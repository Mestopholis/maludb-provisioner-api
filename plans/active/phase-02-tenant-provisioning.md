# Execution Plan: Phase 02 — Tenant Provisioning

Status: IN PROGRESS
Human owner: repository owner
Agent: Claude Code
Branch: `feat/phase-02-slice-1` (slice 1), further branches per slice
Related task: `tasks/PHASE-02-TENANT-PROVISIONING.md`
Dependencies: none — Phase 01 complete, ADR-013 ratified

## Objective

Create a customer project as an isolated database with constrained roles on an
existing MaluDB node, provisioned by a state machine that is safe to retry and
never destroys data it might not own.

This is the first phase where a defect means **cross-tenant data access** rather
than a broken build. Every slice gets its own security review.

## Preconditions

- [x] Phase 01 complete — identity, envelope encryption, migrations, CI.
- [x] `specs/tenant-role-model.md` specifies the exact provisioning SQL.
- [x] ADR-014, ADR-015, ADR-018 encode lockdown, extension and bootstrap
      hardening, each verified empirically during the Phase 00 spike.
- [x] `project_credentials` exists, so generated secrets have somewhere to live.

## Slices

Sequential, with review between. Each is independently verifiable.

### Slice 1 — Node registry, capacity, placement

Node registration and health recording, capacity scoring, and atomic placement.
No tenant database is created, so the blast radius is small. Verifiable on its
own: a node registers, health is recorded, a project is placed, and a node at
its capacity threshold stops receiving projects.

### Slice 2 — Roles, database, lockdown

The security-critical core. Project-ref generation, roles per
`specs/tenant-role-model.md`, database creation, `REVOKE CONNECT ... FROM
PUBLIC`, `maludb_core` install, plan settings applied to the authenticator
`IN DATABASE`, and credentials persisted encrypted in `project_credentials`.
Negative tests A–J from the role-model spec land here.

### Slice 3 — Tenant bootstrap

Versioned bootstrap SQL applied inside the tenant database: `auth` helper
functions, the ADR-018 extension-function revoke, schema conventions, and
recording bootstrap and extension versions per project.

### Slice 4 — State machine, idempotency, retry, cleanup

Orchestration tying slices 2 and 3 together safely. Retry after simulated
failure at each state boundary, and cleanup that never drops a database that
might hold customer data.

## Decisions taken without consultation

The owner was unavailable when slice 1 began. These are recorded so they can be
overruled cheaply.

1. **Phase 02 terminates at a validated database, not a serving API.** The
   state machine's `API_CONFIGURING` and `ROUTING_CONFIGURING` states are Phase
   03 concerns — PostgREST configuration, worker startup, gateway routing. In
   Phase 02 a project reaches a terminal `PROVISIONED` state after `VALIDATING`
   confirms the database and roles. Phase 03 carries it from there to `ACTIVE`.
   Rationale: Phase 03's task file owns the API surface, and a project with no
   route is not meaningfully "active".

2. **No separate placement-reservation table.** I said one was needed when
   scoping this; on implementation it is not. A reservation is precisely "this
   project is assigned to this node", which `projects.node_id` already
   expresses. Reserving inside a transaction that holds `SELECT ... FOR UPDATE`
   on the node row makes the capacity check and the assignment atomic, which a
   separate table would not improve. Failed projects continue to count against
   capacity until cleaned up, which is correct — they may already hold a
   database.

3. **Node administration is an operator CLI, not an HTTP API.** Registering
   nodes and recording health are platform-staff operations. `docs/ACCOUNTS.md`
   describes staff access as explicit, time-bounded and audited, but no staff
   role exists yet. Rather than invent one prematurely, or add an admin HTTP
   surface that authenticates against customer credentials, these are
   management commands. When the staff model lands, the HTTP surface can be
   added on top of the same functions.

## Non-goals

- PostgREST and Auth worker configuration or startup — Phase 03.
- Gateway routing registration — Phase 03.
- Tenant movement between nodes — Phase 11.
- Warm-project accounting. `docs/CAPACITY.md` shows warm and total projects
  have different cost profiles, but worker state does not exist until Phase 03.
  Capacity scoring is structured for both and currently enforces total only.

## Verification

- [ ] Every acceptance criterion in `tasks/PHASE-02-TENANT-PROVISIONING.md`.
- [ ] Negative tests A–J in `specs/tenant-role-model.md`.
- [ ] A security review per slice, not per phase.
- [ ] Provisioning verified against the real MaluDB install, not a mock.

## Risks

- **Cross-tenant isolation is the whole point.** A defect here is a data
  breach, not a bug. Slice 2 is reviewed alone for that reason.
- **Generated SQL identifiers.** Phase 02 is the first code to build database
  and role names from project metadata. `AGENTS.md` names this a primary review
  concern; refs are validated before use and identifiers quoted via
  `psycopg.sql.Identifier`, never string formatting.
- **Concurrent placement.** Two simultaneous provisioning runs must not
  oversubscribe a node. Addressed by row-level locking, and tested
  concurrently rather than assumed.
- **Partial provisioning.** A run that dies midway leaves real objects on a
  real cluster. Slice 4 owns retry-safety; until then failures are cleaned up
  manually on the development node.

## Decision log

- 2026-08-15 — Plan created, four slices. Three decisions taken without
  consultation, recorded above.

## Progress log

- 2026-08-15 — Slice 1 complete: node registry, capacity scoring, atomic
  placement, operator CLI. 20 new tests including a concurrency test that
  eight threads racing for three slots yields exactly three placements.
  121 tests overall.
- 2026-08-15 — Slice 2: roles, database, lockdown, encrypted credentials,
  isolation verification. 15 provisioning tests run against the real MaluDB
  cluster and, in CI, against the plain PostgreSQL service container -- the
  extension assertions skip there, the isolation properties do not.
  139 tests overall.
- 2026-08-15 — Security review of slice 1 found one issue: release_placement
  allowed FAILED projects to be unplaced, orphaning a tenant database the
  control plane could no longer reach for deletion or suspension. Fixed by
  gating on the recorded fact — whether a database exists — rather than the
  status label. 124 tests. Awaiting review.
