# MaluDB

This document defines what MaluDB actually is, what it requires, and what it
implies for the MaluDB Platform architecture.

Source repository: https://github.com/maludb/maludb-core

Facts marked **verified** were observed directly on the development host on
2026-08-15 against a running install. Facts marked *documented* come from the
`maludb-core` repository and have not yet been re-tested here. Re-verify before
relying on any of this for production node builds.

## What MaluDB is

**MaluDB is stock PostgreSQL 17 plus a C extension named `maludb_core`.**

It is not a PostgreSQL fork, not a wire-compatible reimplementation, and not a
separate server process. It is an ordinary PGDG PostgreSQL 17 cluster with an
extension installed into a database.

This resolves the largest open assumption in this repository. Every
PostgreSQL-dependent design in `docs/ARCHITECTURE.md`, `docs/TENANCY.md`, and
`docs/RESOURCE-GOVERNANCE.md` — `CREATE DATABASE`, cluster-scoped roles, RLS,
`ALTER ROLE ... IN DATABASE`, `statement_timeout`, logical replication, WAL
archiving, `pg_database_size()` — is available with standard PostgreSQL
semantics, because it *is* standard PostgreSQL.

MaluDB is a memory/knowledge DBMS layered on that foundation: a
source → claim → fact → episode pipeline, an SVPOR knowledge graph, bitemporal
history, vector and full-text retrieval, and an agent/skill runtime.

## Verified environment

Observed on the development host, 2026-08-15:

| Property | Value |
|---|---|
| Server | PostgreSQL 17.10 (Ubuntu 17.10-1.pgdg24.04+1), x86_64 |
| Extension | `maludb_core` 0.104.0 (repo tag `v4.5.0`) |
| Extension schema | `maludb_core` (`relocatable = false`) |
| Required extensions | `vector`, `btree_gist`, `pg_trgm`, `pgcrypto` |
| `shared_preload_libraries` | `pgaudit,pg_stat_statements` |
| `max_connections` | 100 (PostgreSQL default — not yet tuned) |
| `max_worker_processes` | 8 (default) |
| `wal_level` | `replica` |
| Extension objects | 157 tables, 13 views, 532 functions |
| RLS | 128 tables with row security enabled, 164 policies |
| License | PostgreSQL License (BSD-style) |
| Platforms | Ubuntu 24.04 LTS, x86_64 + arm64 (*documented*) |

The development host is a single machine running the extension, a Python API
server on port 8000, and the platform repositories. It is not a
representative production node and must not be used to derive capacity numbers
beyond the per-database measurements below.

## Installation model

The extension is **per-database**, not per-cluster. Installing it into one
database does not make it available in another.

```sql
CREATE EXTENSION maludb_core CASCADE;   -- installs vector, btree_gist, pg_trgm, pgcrypto
```

`maludb_core.control` does not set `trusted = true`, so installation requires
superuser. A tenant role cannot install it — **verified**: a plain login role
attempting `CREATE EXTENSION` was refused. This is consistent with ADR-010; the
platform installs the extension during provisioning and the customer never can.

Upstream `scripts/maludb-bootstrap` builds and installs PostgreSQL 17 from PGDG
along with pgvector, pgaudit and pg_partman, creates a `maludb` database,
installs the extension into it, and sets its `search_path` (*documented*). For
platform nodes this is a node-build concern, not a per-tenant concern — the
platform creates tenant databases itself.

### Application schemas are enabled explicitly

MaluDB does not modify existing schemas automatically. Each application schema
is enabled by hand:

```sql
CREATE SCHEMA app AUTHORIZATION app;
ALTER ROLE app SET search_path TO app, maludb_core, public;
SELECT * FROM maludb_core.enable_memory_schema();
```

`enable_memory_schema(p_schema name DEFAULT current_schema())` installs the
schema-local facades (`maludb_subject`, `maludb_document`, `maludb_memory_pool`,
and similar). The `search_path` order matters: schema-local facades must resolve
before the shared extension schema.

## Measured per-tenant cost

**Verified** by creating a scratch database, installing the extension, measuring,
and dropping it:

| Measurement | Value |
|---|---|
| Empty PostgreSQL database | 7.6 MB |
| After `CREATE EXTENSION maludb_core CASCADE` | 23 MB |
| Extension's own contribution | ~15 MB |
| `createdb` time | 0.6 s |
| `CREATE EXTENSION ... CASCADE` time | 1.9 s |

Two consequences for the platform:

1. **Storage floor per tenant is ~23 MB before the customer stores anything.**
   1,000 tenant databases on a node is ~23 GB of pure baseline. This must be
   an input to the node capacity model in `docs/RESOURCE-GOVERNANCE.md` and to
   free-tier storage quotas — a "100 MB free tier" measured with
   `pg_database_size()` would be 23% consumed at creation.
2. **Provisioning cost is ~2.5 s for the database and extension**, which is
   comfortably inside any reasonable project-creation SLO. The provisioning
   budget will be dominated by later steps (bootstrap, worker start, routing),
   not by MaluDB itself.

MaluDB is installed into **every** tenant database (ADR-015). There is no opt-in
path and no "MaluDB-enabled" project flag, so the ~23 MB floor applies to every
project and extension upgrades are a whole-fleet operation with no tenant
skipped.

## Role model

`maludb_core` defines a family of **cluster-scoped** roles. Because PostgreSQL
roles are cluster-global, these are shared by every tenant database on a node.

Capability roles (*documented*, attributes **verified**):

| Role | Purpose | `BYPASSRLS` |
|---|---|---|
| `maludb_memory_admin` | Full CRUD on governed tables; can grant cross-tenant access | yes |
| `maludb_memory_executor` | Default authenticated role; CRUD on own tenant rows | no |
| `maludb_memory_auditor` | Read-only across all governed tables | yes |
| `maludb_memory_reader` | Read-only, schema-local | no |
| `maludb_skill_curator` | Curates public skills in `maludb_public` | no |
| `maludb_queue_worker` | Queue processing | no |
| `maludb_secret_consumer` | Secret resolution | no |
| `maludb_rest_dispatcher` | REST endpoint dispatch | no |
| `maludb_llm_admin`, `maludb_llm_auditor` | Model/prompt tier admin and audit | yes |
| `maludb_llm_executor`, `maludb_llm_model_admin`, `maludb_llm_prompt_approver`, `maludb_llm_prompt_author` | Model/prompt tier | no |
| `maludb_modeld`, `maludb_mc2dbd` | Service login roles | yes |

Convenience roles intended for operators to grant: `maludb_read`,
`maludb_user`, `maludb_admin`.

### Two role hazards for the platform

**1. Six roles carry `BYPASSRLS`** — `maludb_memory_admin`,
`maludb_memory_auditor`, `maludb_llm_admin`, `maludb_llm_auditor`,
`maludb_modeld`, `maludb_mc2dbd`. `BYPASSRLS` is a role attribute, so it applies
in every database that role can reach. None of these may ever be granted to a
customer role. The `maludb_modeld` and `maludb_mc2dbd` service logins must be
treated as platform infrastructure credentials under `docs/SECURITY.md`, not as
tenant credentials.

**2. The `maludb` role is a superuser on this install** — **verified**:
`rolsuper = t`, `rolcreatedb = t`, `NOLOGIN`. Upstream documents
`GRANT maludb TO <role>` as a convenience alias for `maludb_user`, guarded to
apply "only on installs where that role name is not already occupied by an
operator login." On this host the name *is* occupied, by a superuser. On a
platform node, `GRANT maludb TO <customer role>` would hand a customer
superuser. The platform must never issue that grant and should assert its
absence in the isolation test suite.

`maludb_user` and `maludb_admin` are **verified** non-superuser and are the
correct grants for tenant use.

## Verified isolation finding: default cross-database CONNECT

This is the most important operational finding for the database-per-tenant model.

**By default, any role in the cluster can connect to any tenant database.**
Verified: a freshly created login role with no grants of any kind connected to
an unrelated tenant database and read its catalog (1,563 rows from `pg_class`).

```text
tenant-B role -> tenant-A database  =>  CONNECTED        (default)
REVOKE CONNECT ON DATABASE <db> FROM PUBLIC
tenant-B role -> tenant-A database  =>  FATAL: permission denied for database
```

PostgreSQL grants `CONNECT` to `PUBLIC` on every new database. Step 9 of the
provisioning outline in `docs/PROVISIONING.md` ("remove unsafe default
connectivity/privileges") therefore has a concrete, mandatory meaning:

```sql
REVOKE CONNECT ON DATABASE <tenant_db> FROM PUBLIC;
GRANT  CONNECT ON DATABASE <tenant_db> TO <tenant roles>;
```

Without this, `docs/TENANCY.md`'s first isolation requirement — "a tenant must
not be able to connect to another tenant database" — is violated on every node
by default. The negative test already listed in `docs/TESTING.md` ("tenant A
attempts to connect to tenant B database") must be a blocking test in Phase 02.

Note also that role-level settings applied for resource governance must be
scoped per database, since roles are cluster-global, **and must target the login
role** — a setting applied to a role that is only ever entered via `SET ROLE`
silently does nothing:

```sql
ALTER ROLE mldb_<ref>_authenticator IN DATABASE <tenant_db> SET work_mem = '...';  -- correct
ALTER ROLE authenticated            IN DATABASE <tenant_db> SET work_mem = '...';  -- no effect
ALTER ROLE mldb_<ref>_authenticator                         SET work_mem = '...';  -- leaks cluster-wide
```

These settings are defaults rather than enforcement: most are session-settable
by any client holding direct SQL. See ADR-017 and `docs/RESOURCE-GOVERNANCE.md`.

## Capabilities the extension provides

From `maludb_core.control` and the function catalog (**verified** by inspecting
`pg_proc`):

- memory pipeline: source → claim → fact → episode/memory
- SVPOR knowledge graph, unified edge view, path finding, communities, degree
  and surprise analytics
- bitemporal time (valid + transaction time) with supersession — corrections
  never overwrite history
- provenance ledger (`malu$derivation_ledger`) required on every derived object
- authorization-aware retrieval checked at planning, expansion, and assembly
- vector search (pgvector), FTS, `pg_trgm` fuzzy matching, retrieval planner
- workflow extraction and a governed skill runtime; skill discovery and public
  skills in the reserved `maludb_public` schema
- relational data-model graph: `maludb_datamodel_refresh` /
  `maludb_datamodel_describe` introspect `pg_catalog` into the graph namespace
- graph import: `maludb_graph_import(namespace, graph, options)`, capped at
  50k nodes / 200k links
- model registry, embedding adapters, dual-space routing
- **in-database auth tokens**: `auth_token_create` / `auth_token_verify` /
  `auth_token_revoke`, with account scoping, scopes, allowed CIDRs, expiry, and
  peppered token hashing
- **in-database secret store**: `secret_set` / `secret_get_metadata` /
  `secret_revoke`, plus external secret references and a master-key model
- **storage adapter registry**: `register_storage_adapter`
- **REST endpoint catalog**: `malu$rest_endpoint` with `malu$rest_invocation`
  audit rows
- tenant binding through the `maludb_core.current_account_id` GUC

### No background workers

**Verified**: `_PG_init` in `src/maludb_core.c` is empty. The extension
registers no background workers and is not required in
`shared_preload_libraries`. The "background worker" described in the reindex
protocol is an external service that claims work over SQL, not a PostgreSQL
bgworker.

This is materially good news for the shared-cluster model: installing the
extension into N tenant databases does not consume N worker slots and does not
contend for `max_worker_processes` (currently 8). Only `pgaudit` requires
preloading, and that is a cluster-level node-build decision.

## Companion services

MaluDB ships external services alongside the extension (*documented*; not yet
evaluated for the platform):

| Service | Role |
|---|---|
| `maludb_modeld` | Model gateway (has a systemd unit) |
| `maludb_mc2dbd` | Database MCP listener |
| `mcp-broker` | External-tool MCP broker |
| `maludb-restd` | V3 REST gateway, catalog-driven dispatch |
| `maludb-realtimed` | V3 SSE event stream |
| `maludb-pageindexd` | V4 PageIndex / ChatIndex builder |
| `maludb-logsd` | Log service |

`maludb-restd` is an early-stage service: its README records that TLS, JWT
signature verification, request-body binding, and the full endpoint catalog are
all still outstanding. It should not be assumed production-ready.

## Extension upgrades across a tenant fleet

**Verified**: 146 files are installed under
`/usr/share/postgresql/17/extension/`, including a complete chain of
`maludb_core--<from>--<to>.sql` update scripts, so `ALTER EXTENSION maludb_core
UPDATE` is supported.

Because the extension is per-database, a version upgrade must be applied to
**every tenant database on the node**, not once per cluster.

### Upgrading `maludb_core` across a node (ADR-074 decision 5)

Built in Phase 12 slice 1. Operator-run by design: an automatic schema change
to every customer database is the data-changing control plane ADR-066 prevents.

```bash
# 1. Set the node's pin, install that package on the node, run
#    `cp-manage node extension-check`. Nothing below does any of that.
# 2. The canary: one tenant, upgraded and verified, then the run stops.
cp-manage extension upgrade --node node-01 --extension vector
# 3. Look at that tenant. Then batches, as many runs as it takes.
cp-manage extension upgrade --node node-01 --extension vector --batch-size 20
# 4. The same for maludb_core (the default --extension), after vector.
cp-manage extension upgrade --node node-01
```

Since pinning slice 3 (ADR-075) the target is always the node's pin: a node with
no pin, a `--to` other than the pin, or packages that do not provide the pin are
refused before any tenant is opened. `vector` goes first — `maludb_core` refuses a
tenant whose `vector` is not at the pin. `cp-manage extension drift` shows which
tenants still lag. A run is refused outright if the node is `draining` or `unhealthy`, a
tenant on it is `MOVING`, or a restore is `running` there, and while another
upgrade holds the node.

**Each tenant is upgraded and verified in one transaction.** That is the whole
design. PostgreSQL has no general extension downgrade, so a check after
`COMMIT` could only report a broken tenant; inside the transaction a failure
undoes the upgrade. What is verified, as outcomes:

- the installed version is the target, and `maludb_core_version()` agrees;
- `tenant_bootstrap.verify` passes — ADR-018's revoke on `public`, whose exact
  failure case is an upgrade that adds a function there, and ADR-045's trigger;
- if the project has a **platform-owned** `maludb_memory` schema,
  `enable_memory_schema` is re-run and the data-model facades exist. Measured
  in Phase 12 slice 0: `ALTER EXTENSION` does **not** rebuild an enabled
  schema's facades, so skipping this would strand every enabled project on the
  old ones;
- if the tenant has **vector compartments** (ADR-077) — enabled now or before —
  the owner role's grants are re-derived from the new extension, narrowed of
  anything it no longer needs, the wrappers are re-installed, and every wrapper
  is exercised as `service_role`. A release that breaks them rolls the tenant
  back. The exercise lifts the tenant's vector limits inside its own rolled-back
  savepoint, so a project at its limit, or on a plan cut to zero, does not block
  its own upgrade.

**The first failure stops the run.** The failing tenant is rolled back and is
still on its previous version, and nothing after it is attempted. The command
exits 2 and says so, because the instinct on reading "failed" mid-fleet is to
assume a half-upgraded database. Fix the cause and re-run; tenants already at
the target are recorded as current and cost a read.

**A `maludb_memory` schema a customer created is left alone.** The tenant admin
holds `CREATE ON DATABASE`, so a customer can create a schema by that name.
Re-enabling it would put superuser-owned `SECURITY DEFINER` functions inside a
schema the customer owns, and failing on it would let any customer block a
node's upgrade — a security release included — by naming a schema. So it is
skipped, noted against the tenant, and the upgrade proceeds.

Every attempt lands in `extension_upgrades` — upgraded, current, failed or
skipped, with the reason — and `projects.extension_versions` is updated only for
tenants that verified.

### Pinning `vector` and `maludb_core` per node (ADR-075)

Each node is pinned to one version of each, from `specs/extension-versions.yaml` —
the versions CI has run against — and the OS package is held:

```bash
cp-manage node pin set --node node-01 --extension vector --version 0.8.6
cp-manage node pin set --node node-01 --extension maludb_core --version 0.104.0
cp-manage node extension-check --name node-01    # non-zero if it disagrees
cp-manage node pin show --node node-01
```

**A node that disagrees takes no new projects, no restores and no moves in, and
keeps serving the tenants it has.** It disagrees when a pin is missing, when it
has not been checked since a pin changed, when its packages provide another
version, or when any backend still has a replaced `vector.so` mapped. The last
one is not hypothetical: pinning slice 0 measured that an installed package does
not reach a session already open, and PostgREST holds its pool for as long as it
runs.

- **Change the pin before the package.** Between the two the node refuses work,
  so one that never gets the package stays out of placement loudly.
- **A pin cannot move down on a node with tenants** — their catalogues keep the
  newer version and `ALTER EXTENSION` has no path back.
- **A move between nodes whose pins differ is refused**: a dump's
  `CREATE EXTENSION` carries no version, so the tenant would arrive at the
  target's.

Every pin change is an audit event, `node.extension_pin.set`, with the operator's
account.

**Provisioning installs the pin, and asks the node again first.** The recorded
check is what let a project be placed, but a package can move between that check
and the install. So provisioning reads what the node provides on the connection
it installs with, refuses a disagreement before creating anything, installs
`vector` and then `maludb_core` with the version named, and verifies the result —
an earlier attempt that left an extension at another version is refused too,
because `CREATE EXTENSION IF NOT EXISTS` would silently keep it. A refused
project is retryable once the node and its pin agree.

#### Changing a pin, in order

The node is out of placement from step 1 until step 4 passes, and keeps serving
throughout.

```bash
# 1. The pin. Refused unless specs/extension-versions.yaml lists the version.
cp-manage node pin set --node node-01 --extension vector --version <new>
# 2. The package, at the list's package_version exactly, and held again.
sudo apt-mark unhold postgresql-17-pgvector
sudo apt-get install -y postgresql-17-pgvector=<package_version>
sudo apt-mark hold postgresql-17-pgvector
# 3. Cycle the node's workers, so no backend keeps the replaced vector.so mapped.
sudo systemctl restart 'maludb-postgrest@*' 'maludb-gotrue@*' 'maludb-realtime@*' maludb-storage
# 4. Check. Non-zero, naming the reason, until the node agrees.
cp-manage node extension-check --name node-01
# 5. The tenants: a canary, then batches (above). vector before maludb_core.
cp-manage extension upgrade --node node-01 --extension vector
cp-manage extension drift --node node-01
```

If step 4 still reports backends on a replaced `vector.so` after step 3, they are
sessions no worker unit owns — a paid project's direct connection, or an
operator's `psql`. They end when those sessions do.

For `maludb_core` the same order holds with `make install` from the listed
`commit` in place of the `apt-get` line; it is not held by apt, and a routine
upgrade does not move it.

#### Adding a version to the list

A version is listed once CI has run the suite against it, and CI installs only
the newest listed. So adding one is one pull request that changes three things
together: the entry in `specs/extension-versions.yaml`, and in
`.github/workflows/ci.yml` either `PGVECTOR_PACKAGE_VERSION` or `MALUDB_CORE_REF`.
`tests/test_tested_versions.py` fails the pull request if they disagree, and with
`MALUDB_REQUIRE_TESTED_VERSIONS` set, as CI sets it, fails the run if the node CI
built does not provide exactly the newest listed versions.

#### When pgdg has pruned a listed package

apt.postgresql.org does not keep old builds forever (on 2026-09-13 it offered
pgvector 0.8.4, 0.8.5 and 0.8.6 for noble). A node rebuilt after its pin's build
is dropped cannot install it from the main repository. Add
`apt-archive.postgresql.org`, which keeps builds the main repository has dropped,
and install the same `package_version`. A version available
from neither comes off the list, and a node still pinned to it can only move up.

### Vector compartments (ADR-077)

A customer turns them on through `POST /v1/projects/<ref>/maludb/vectors/enable`,
which the provisioner performs; an operator can do the same directly:

```bash
cp-manage project maludb enable --ref <ref> --feature vectors
cp-manage project maludb disable --ref <ref> --feature vectors
```

What enabling builds, in one transaction:

- **`mldb_<ref>_vectors`**, `NOLOGIN`, no attributes, no memberships, owning the
  wrappers. Its grants are **derived from the installed `maludb_core`** —
  following the wrappers' entry points through every function, table,
  constraint, default and trigger they reach — fenced to `malu$vector_*` and
  `malu$ann_*` tables and to invoker functions, then **exercised** repeatedly
  before commit. A grant anyone adds to it by hand is refused on the next enable.
- **`maludb_private`**, a platform schema no customer role can use, holding the
  wrappers' helpers and `vector_limits` — the plan's limits, rewritten by every
  plan change (`plan_apply.apply`).
- **Eight wrappers in `maludb`**, `EXECUTE` to `service_role` only, and `maludb`
  published on the project's Data API.

`maludb` stays published while either MaluDB feature is on; turning one off
withdraws it only if the other is off too. Nothing is ever dropped by disabling.

**Moves and restores carry the vector store** (`extension_data.carry`): `pg_dump`
does not, because `maludb_core` registers none of its tables for dumping
(maludb-core#27). A move creates `mldb_<ref>_vectors` on the target before the
load, because a dump's grants to a role the target lacks are dropped.

Measurements behind all of this: `specs/vector-compartments-model.md`.

### Giving existing tenants the extension-function grants (ADR-076)

A tenant provisioned since grants slice 1 gets it at creation. One that was
already serving stops short: `cp-manage project bootstrap` says
`held until its Data API check is live`, and this is how it moves on.

```bash
# The canary: one tenant, then the run stops.
cp-manage extension grants --node node-01
# Check that tenant's Data API refuses an extension function as RPC (403 PT403):
curl -X POST https://<ref>.<domain>/rest/v1/rpc/gen_random_uuid -H "apikey: <publishable key>"
# Then batches, as many runs as it takes.
cp-manage extension grants --node node-01 --batch-size 20
```

**The grants are only safe once the tenant's PostgREST refuses extension
functions as RPC** — before that, customer roles holding `EXECUTE` is ADR-018's
finding, `anon` calling `/rpc/gen_salt`, reopened. Workers are node-local and this
runs on the control plane, so the check is put live through the database. Per
tenant:

1. bootstrap files up to 013 — the check itself;
2. `pgrst.db_pre_request` on the tenant's authenticator, in its database, then a
   config reload notification;
3. **evidence from `pg_stat_activity`**, three seconds later: no PostgREST
   connected (it reads the setting when it starts), or one holding its
   `LISTEN "pgrst"` connection, which is how the reload reaches it;
4. bootstrap 014 and `verify` in one transaction.

Measured on PostgREST 14.17: live 0.18 s after the notification, and 1.1 s after a
killed listener reconnected — PostgREST reloads configuration when its listener
connects.

**A tenant whose worker is connected without a listener stops the run** with
nothing granted: the reload cannot be shown to reach it. Restart that project's
PostgREST unit on its node and re-run. **A tenant whose grants fail verification is
rolled back** and stops the run. Either way nothing after it is touched, and
`extension_grant_upgrades` says which, why, and what the run saw of the worker.

The in-database setting is not carried by `pg_dump`, so a moved or restored tenant
does not have it. It does not need it: the gateway renders `db-pre-request` into
the worker's file for any project at bootstrap 013 or later.

The run takes the extension upgrade's node lock, so it never interleaves with an
`ALTER EXTENSION` — whose trigger is the one 014 replaces.

### What is still open

**Closed by ADR-075** — see "Pinning `vector` and `maludb_core` per node" above:
provisioning installs the pin (pinning slice 2), the upgrade run moves tenants to
it and `cp-manage extension drift` reports lag (slice 3), and CI tests exactly the
newest listed versions (slice 4). What was observed:

Version drift is already observable: the pre-existing `maludb` database has
`vector` 0.8.3 while a database created today gets 0.8.4, because
`CREATE EXTENSION` installs whatever the OS package currently provides. Tenant
databases created at different times will not have identical dependency
versions unless provisioning pins them explicitly. **ADR-075 decides it**
(2026-09-12): `vector` and `maludb_core` are pinned per node from a tested list,
packages held, and a node that disagrees with its pin stops taking work. Tenants
provisioned before a node was pinned keep the versions they were created with
until an upgrade run passes them; `cp-manage extension drift` names them.

The per-project record this section once said was missing exists:
`projects.extension_versions` and `bootstrap_version`, since migration 0005.

## Extensions relevant to Supabase compatibility

**Verified** availability on this host:

| Extension | State | Relevance |
|---|---|---|
| `pgcrypto` | installed | required by `maludb_core`; also required by Supabase Auth |
| `uuid-ossp` | available, not installed | commonly used by Supabase schemas |
| `pg_stat_statements` | available, preloaded, not created | observability |
| `pgaudit` | available, preloaded, not created | audit |
| `pg_partman` | available, not installed | partition maintenance |
| `pg_graphql` | **not available** | Supabase `/graphql/v1` cannot be offered |
| `pg_net` | **not available** | Supabase webhooks depend on it |
| `pg_cron` | **not available** | Supabase scheduled jobs depend on it |
| `pgjwt` | **not available** | some Supabase JWT helpers depend on it |

These absences are compatibility limits that belong in
`specs/compatibility-matrix.yaml` as explicit `intentionally_unsupported` or
`planned` entries rather than being discovered during migration. Adding them is
a separate task from this document.

## Platform implications summary

1. PostgreSQL semantics are stock. The tenancy, governance, and backup designs
   in this repository rest on solid ground.
2. `REVOKE CONNECT ... FROM PUBLIC` is mandatory on every tenant database.
3. Never grant `maludb`, or any `BYPASSRLS` role, to a customer.
4. Per-role resource settings must use `ALTER ROLE ... IN DATABASE`.
5. Budget ~23 MB and ~2.5 s per tenant database for MaluDB itself.
6. Extension upgrades are a per-database fleet operation. The runbook is above: `cp-manage extension upgrade`, a canary then batches (ADR-074).
7. Provisioning must record extension and bootstrap versions per project.
8. MaluDB has its own tenancy model — account/schema-scoped inside one database,
   with `current_account_id`, `malu$object_grant`, and cross-tenant `MALU_ALL_*`
   views. The platform layers database-per-tenant on top. Two different meanings
   of "tenant" now coexist and must be reconciled explicitly; see ADR-013.
9. MaluDB already provides auth tokens, a secret store, a storage adapter
   registry, a REST catalog, and an SSE realtime service. These overlap with
   both the planned control-plane design and the planned Supabase components.
   Overlap is unresolved; see Open questions.

## Open questions raised by this document

Resolved since first writing: `maludb_core` is installed in every tenant
database (ADR-015), and the Supabase role-name collision is settled by ADR-016
with the specification in `specs/tenant-role-model.md`.

Still recorded in `docs/OPEN-QUESTIONS.md`:

- Do platform API keys use MaluDB `auth_token_*`, or a separate control-plane
  implementation?
- Does the platform use MaluDB's in-database secret store for tenant service
  credentials, or an external secret manager?
- Do `maludb-restd` / `maludb-realtimed` have any role in the Supabase-compatible
  data path, or do they remain a parallel MaluDB-native surface?
- How do MaluDB's `current_account_id` tenancy and the platform's
  database-per-tenant tenancy compose inside a single tenant database?
- What is the tenant-fleet extension upgrade procedure?

## Reproducing these findings

```bash
sudo -u postgres psql -d maludb -tAc "SELECT maludb_core.maludb_core_version()"
sudo -u postgres psql -d maludb -c "\dx"
sudo -u postgres psql -d maludb -c \
  "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'maludb%'"
ls /usr/share/postgresql/17/extension/ | grep -c maludb
```

The isolation finding is reproduced by creating a login role with no grants and
connecting it to an unrelated database, before and after
`REVOKE CONNECT ON DATABASE <db> FROM PUBLIC`.
