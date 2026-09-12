# MaluDB Data-Model Graph Model

What it takes to put `maludb_core`'s data-model graph in front of a customer, and
whether ADR-074's design for doing so works. Deliverable of Phase 12 slice 0.

Status: derived from experiments run 2026-09-12 against `maludb_core` 0.104.0
(and 0.103.0 for the upgrade path), PostgreSQL 17.10 and PostgREST 14.17 on the
development host (6 cores, 3.8 GB RAM). Every tenant measured was provisioned
through the platform's own provisioning module — ADR-016's roles including the
per-project authenticator, executor and client, the ADR-014 lockdown, and the
bootstrap carrying ADR-018's hardening. The harness is
`scripts/spike-datamodel.py`, and it reproduces every table below.

Companion to `plans/active/phase-12-maludb-features.md` and to ADR-074, whose
third decision this document finds **does not work as written**.

## The finding that stops the plan

**A platform wrapper cannot call the data-model facades on the path PostgREST
takes.** Both `maludb_datamodel_refresh` and `maludb_datamodel_describe` call
`maludb_core._memory_schema_assert_manageable`, which ends:

```sql
IF NOT has_schema_privilege(session_user, p_schema, 'CREATE') THEN
    RAISE EXCEPTION 'enable_memory_schema: % lacks CREATE on schema %', session_user, p_schema
```

The check is on **`session_user`, not `current_user`**. A `SECURITY DEFINER`
wrapper changes `current_user` and cannot change `session_user`, and PostgREST
always logs in as the project's authenticator before `SET ROLE`. So on every
request the guard asks whether *the authenticator* can create objects in the
memory schema — and it cannot, correctly.

| Called as | `session_user` / `current_user` | Result |
|---|---|---|
| The platform, directly | `postgres` / `postgres` | works |
| A platform-owned `SECURITY DEFINER` wrapper, from the authenticator after `SET ROLE service_role` | `mldb_<ref>_authenticator` / `service_role` | `enable_memory_schema: mldb_<ref>_authenticator lacks CREATE on schema maludb_memory` |

Every wrapper design over the live facades fails the same way, because the
check sits inside the facade rather than in front of it. Reading the refreshed
graph directly does not route around it either: the facade views chain into
`maludb_core."malu$edge_unified"`, and a non-superuser granted `SELECT` on the
memory schema's `maludb_edge` view gets `permission denied for view
malu$edge_unified`. Reaching that means granting MaluDB's own roles to a
customer, which `docs/MALUDB.md` forbids.

This is arguably an upstream defect. A guard that checks `session_user` inside
`SECURITY DEFINER` functions defeats the reason to use definer rights, and
`current_user` would express the evident intent — that the *caller* may manage
the schema. It is raised upstream rather than worked around here, per the plan's
non-goals. It is not assumed to be fixed.

Options for what Phase 12 does instead are at the end of this document, because
several of the findings between here and there decide which are viable.

## Who can reach the facades — better than ADR-074 feared

ADR-074 says the facades "carry PostgreSQL's default `EXECUTE` grant to
`PUBLIC`" because `enable_memory_schema` creates them at runtime, outside the
extension. **That is wrong.** `enable_memory_schema` writes explicit ACLs: its
functions are executable by the extension's installer and MaluDB's
`maludb_memory_admin`, `maludb_memory_executor` and `maludb_memory_auditor`, and
the schema's `USAGE` goes only to MaluDB roles.

| Role | `USAGE` on memory schema | `EXECUTE` on `describe` |
|---|---|---|
| `anon`, `authenticated`, `service_role` | no | no |
| `mldb_<ref>_authenticator` | no | no |
| `mldb_<ref>_admin` | no | no |
| `mldb_<ref>_executor` (SQL console, every tier) | no | no |
| `mldb_<ref>_client` (direct connection, paid) | no | no |

Privilege functions report inheritance and the customer roles are `NOINHERIT`,
so the check was repeated over **transitive membership**, which is what a
`SET ROLE` could reach: the authenticator reaches `anon`, `authenticated` and
`service_role`; the executor and client reach `mldb_<ref>_admin`; and **no
customer role reaches any `maludb_*` role**. No customer-controlled role can call
a facade directly, over any connection, on any tier — for as long as the rule
never to grant MaluDB's roles to a customer holds.

**The facades run as the node superuser.** They are `SECURITY DEFINER`, and
their owner is the role that installed the extension, which provisioning does
over the node's superuser connection. Whatever exposes them exposes code
running with full rights on the cluster.

## What `describe` discloses — exactly what ADR-074 feared

**`describe` ignores the caller's privileges entirely.** A role with no `SELECT`
on a table — revoked from `PUBLIC`, `anon`, `authenticated` and `service_role`
alike — still gets that table's full structure.

| As a role with no privilege on `public.salaries` | Result |
|---|---|
| `has_table_privilege('public.salaries', 'SELECT')` | `false` |
| `describe('public.salaries')` | every column, type, key and constraint, including `ssn` |
| `describe('auth.users')` | refused: `schema auth is not visible to this tenant` |

The only limit is schema visibility: the memory schema itself, `maludb_core` and
`public`. So `service_role`-only access in ADR-074 is not a cautious default; it
is the whole of the control. **Granting `authenticated` would publish the
structure of every table in `public` to every signed-in end user**, and no
wrapper grant can narrow that — only a wrapper that filters by the caller's own
privileges could. That is the measurement ADR-074 said would decide whether
`authenticated` is ever granted, and it decides against, as the facade stands.

## What enabling touches

**Nothing in `public`.** Inventoried before and after on a bootstrapped tenant:

| | Before → after |
|---|---|
| Relations and functions in `public` | 0 added, 0 removed |
| ACLs on `public`'s functions | 0 changed |
| Objects created in the memory schema | 165 (74 relations, 84 functions) |
| Time | 0.55 s |
| Database size | ~1 MB added to a 23 MB baseline |

ADR-018's hardening of `public` is untouched by enablement. Refresh also leaves
`public` alone — 650 objects before and after, on the large schema below.

## What refresh costs

Refresh introspects the requested schemas with `p_schemas`; the useful call is
`maludb_datamodel_refresh('datamodel', ARRAY['public'])`, because customer tables
live in `public` rather than in the memory schema.

| Schema | Refresh | Nodes / edges |
|---|---|---|
| `public` with no customer tables | 1.22–1.28 s | 334 edges |
| 300 tables, 299 foreign keys, 50 views, 50 functions | 2.54–2.86 s | 400 nodes, 1133 edges |

**The floor is not zero, and ADR-018 is why.** `maludb_core` installs its 373
functions into `public` and cannot be relocated, so every tenant's refresh
introspects them even with no tables of its own. About a second of every refresh
is the platform's, not the customer's.

**Refresh replaces rather than accumulates.** After ~33 refreshes of the large
schema, `malu$svpor_statement` held 933 rows and `malu$svpor_subject` 636 — where
retained history would be tens of thousands — and five further committed
refreshes changed neither. What it does produce is **churn: ~1.2–1.4 MB of dead
rows per refresh** of that schema. Vacuum reclaims it; database size stayed flat
across ten refreshes with `VACUUM` between. Measured inside one transaction, the
same churn looks like permanent linear growth — 26.6 MB across twenty refreshes —
which is a trap for anyone measuring this in a `DO` block.

**So the refresh limit is about CPU and write volume, not storage.** Every refresh
is ~2.5 s of a backend and ~1.3 MB of changed rows, which is WAL, which Phase 11
established is what the backup archive is sized in. The WAL figure is inferred
from the churn, not measured directly.

`malu$embedding_dirty` held 1577 rows that no refresh changed. It is a work queue
for MaluDB's external reindex service, which this platform does not run: a static
backlog, not growth — and a thing vector search will have to decide about.

## What turning it on costs PostgREST

**No restart.** On PostgREST 14.17, against a real tenant:

| Change | Mechanism | Effect |
|---|---|---|
| `db-schemas = "public"` → `"public, maludb"` | rewrite the config file, then `NOTIFY pgrst, 'reload config'` | applied in 0.46 s, same process |
| New functions in the added schema | `NOTIFY pgrst, 'reload schema'` | served in 0.63 s |
| Reads against `public` during a schema reload | — | 86 requests, 0 errors |
| The reverse, back to `"public"` | rewrite, `NOTIFY pgrst, 'reload config'` | `PGRST106` for the removed schema, 0.46 s |

Both reloads are `NOTIFY`s on the tenant database, so the control plane can
trigger them over the connection it already holds, with no signal to a process
on the node. What still has to happen on the node is rewriting the worker's
config file. And disabling is as cheap as enabling, which is what makes a
later "turn it off" slice safe to plan.

Before the first request, PostgREST's initial schema cache load took 3.1 s, and
a request in that window gets `PGRST002`. That is the existing cold start, not
something enablement adds.

## What an extension upgrade does to an enabled schema

**`ALTER EXTENSION maludb_core UPDATE` does not rebuild the facades.**

| Step | Result |
|---|---|
| `CREATE EXTENSION maludb_core VERSION '0.103.0'`, enable | 163 objects; 0 data-model facades — they arrived in 0.104.0 |
| `ALTER EXTENSION maludb_core UPDATE TO '0.104.0'` | extension reports 0.104.0; **the enabled schema still has 0 data-model facades** |
| `enable_memory_schema` again | 165 objects; both facades present |
| Refresh, then `enable_memory_schema` a second time | 335 edges before, 335 after |

So the upgrade procedure must re-run `enable_memory_schema` for every enabled
project, after `ALTER EXTENSION`. It is safe to: re-running is idempotent and
preserves the existing graph. Two consequences follow from how it does it.

- **Re-enabling drops and recreates its own views**, visibly: `drop cascades to
  view maludb_memory.maludb_project` and three more. Anything the platform builds
  with a *tracked* dependency on those objects is cascade-dropped by every
  upgrade. A wrapper written as a `BEGIN ATOMIC` SQL function records such a
  dependency; one with a string body does not. This is a design constraint on any
  wrapper, whichever option below is chosen.
- **A schema's facade version is not self-evident.** No function reports it, and
  a stale schema looks exactly like a current one until something missing is
  called. The platform has to record the version `enable_memory_schema` returned,
  per project, the same way it records `extension_versions`.

## What slice 0 did not measure

- **Schemas far beyond 300 tables.** Refresh went from 1.2 s to 2.7 s between an
  empty `public` and 300 tables; nothing here says how it scales to 5,000.
- **Refresh concurrent with a customer's DDL**, or with a re-enable, which drops
  views the refresh reads.
- **WAL per refresh**, directly. Inferred above from row churn.
- **The other memory-schema facades.** Only the data-model pair was exercised;
  the session-user guard is shared machinery and may block others the same way.
- **Whether the upstream guard is a defect or a deliberate choice.** The case for
  defect is made above; upstream has not been asked.

## Options for delivery, given the guard

Recorded for the owner to decide, since each changes ADR-074 decision 3.

**A. Grant the authenticator `CREATE` on the memory schema.** The guard passes on
every request and the wrappers work as designed, today. But it satisfies a check
by granting what the check exists to withhold: once the authenticator has
`CREATE`, the guard admits `anon` and `authenticated` too, the wrapper `EXECUTE`
grant becomes the only gate in front of superuser-owned code, and the login role
behind every API request gains DDL on a platform schema — unused by request SQL
today, because PostgREST always `SET ROLE`s away from it, but held.

**B. The platform refreshes; customers read a copy.** Refresh runs as the
platform, whose `session_user` passes the guard, as a queued request that is also
the per-plan rate limit. The platform copies the resulting graph — and a
`describe` for each relation — into ordinary tables it owns in the `maludb`
schema, which PostgREST serves to `service_role`. No `SECURITY DEFINER` function
is exposed at all, the superuser-owned facades are never reachable from a
request, and a future `authenticated` grant becomes a row-level-security question
on ordinary tables rather than a disclosure through definer rights. The cost is
that `describe` answers as of the last refresh rather than live, and there is a
copy step to build and keep in step with upgrades.

**C. Mediated execution.** A `/maludb/v1` or RPC path that forwards to the
control plane, which runs the facade over a session whose `session_user` has
`CREATE`. It reopens exactly the routing question ADR-074 declined, and a
mediated session running as a role with `CREATE` on a platform schema, on a
customer's request, is a capability that needs its own review.

**D. Wait for upstream** to check `current_user`. ADR-074's design then works
unchanged. Phase 12 blocks on another project's release, with no date.

This document records the options rather than choosing; the plan's decision log records what the owner
chose, and ADR-074 is amended to match.

## Reproducing

```bash
export MALUDB_NODE_ADMIN_DSN=postgresql://<superuser>@127.0.0.1:5432/postgres
export MALUDB_PLATFORM_OWNER=<superuser>
export MALUDB_POSTGREST_BIN=/usr/local/bin/postgrest
scripts/spike-datamodel.py run       # questions 1-5 and 7; --tables N, --keep
scripts/spike-datamodel.py reload    # question 6
```

Both provision disposable tenants and drop them afterwards. Question 7 needs the
previous extension version installed on the node (`DM_PREVIOUS_VERSION`,
default 0.103.0). Point neither at a node carrying customer data: `run` grants a
probe role `EXECUTE` on superuser-owned code, on purpose.
