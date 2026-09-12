# Extension pinning — what moving `vector` under live tenants actually does

Pinning slice 0 of `plans/active/phase-12-extension-pinning.md` (ADR-075).
Measured 2026-09-12. Reproduce with `scripts/spike-extension-pinning.py`; its
docstring has the exact invocation.

## Where it was measured

A disposable container, because the measurement replaces the `vector` package
under connected tenants and that must never be done to a shared cluster:
`docker.io/library/postgres:17` — PostgreSQL 17.11 (Debian trixie, pgdg) —
with `postgresql-17-pgvector` 0.8.4-1.pgdg13+1 and `maludb_core` built from
`MALUDB_CORE_REF` (`b4d6d52`, extension 0.104.0). Two tenants were provisioned
through `services/control_plane/provisioning.py` and `tenant_bootstrap`, so
ADR-016's roles and ADR-018's hardening trigger are the platform's own. Each
carried 20,000 rows of `vector(64)` with an HNSW (`vector_l2_ops`) and an
IVFFlat (`vector_cosine_ops`, 100 lists) index. The package moved to
0.8.6-1.pgdg13+1 with `apt-get install` while sessions were open.

Debian trixie rather than the Ubuntu noble nodes run: the same upstream pgvector
source, a different build. Nothing below depends on the distribution.

## Findings

### 1. `vector`'s point releases change nothing in SQL

Every upgrade script from 0.8.0 to 0.8.6 is the 153-byte header and no
statements. Across pgvector's history, 12 of 40 steps carry DDL; the most recent
is 0.7.4 → 0.8.0 (four `CREATE FUNCTION`, four `CREATE CAST`). Everything
0.8.1–0.8.6 changed — including 0.8.3's "possible index corruption with HNSW
vacuuming" and 0.8.4's HNSW vacuum errors — is in `vector.so`.

**So `extversion` is a label, and the package is the upgrade.** A tenant whose
`extversion` says 0.8.4 on a node with 0.8.6 installed is running 0.8.6's code
the moment its backend loads the library. ADR-075 decision 1 — pin what the node
runs, not what provisioning writes — is confirmed more strongly than it was
argued: a provisioning-only pin would have pinned nothing at all.

### 2. A package swap does not reach sessions that are already open

| Session | `vector.so` mapped |
|---|---|
| opened before the swap | `/usr/lib/postgresql/17/lib/vector.so (deleted)` |
| opened after the swap | `/usr/lib/postgresql/17/lib/vector.so` |

Read from `/proc/<pid>/maps` through `pg_read_file`. `dpkg` replaces the file;
a backend that already loaded it keeps the old inode mapped until it
disconnects. Both kinds of session answered HNSW queries.

**Consequence: a node is not "at the pin" when the package is installed.** It is
at the pin when every backend that loaded the old library has gone — and
PostgREST, Realtime and the storage worker hold pooled connections for as long
as they run. `default_version` cannot see this. The same `/proc` read can: a
backend mapping a `(deleted)` `vector.so` is still running the replaced code.

### 3. Tenants keep serving before any `ALTER EXTENSION`

After the swap, with `extversion` still 0.8.4: HNSW and IVFFlat queries planned
their indexes and returned rows, in old and new sessions. `maludb_core`
0.104.0 answered `maludb_core_version()` and its own HNSW index
(`maludb_core."malu$vector_demo_embedding_hnsw"`) planned and served. Three
`maludb_core` functions use vector operators in their bodies.

ADR-075 decision 3's "a lagging tenant keeps serving" was stated from pgvector's
policy; it is now observed.

### 4. `ALTER EXTENSION vector UPDATE` is safe inside one transaction per tenant

With a reader (HNSW `ORDER BY … LIMIT 10`) and a writer (`INSERT`) running
against the tenant throughout, and the updating transaction held open ~1 s
after the statement:

| | Run 2 | Run 3 |
|---|---|---|
| `ALTER EXTENSION` | 511 ms | 356 ms |
| sessions waiting on a lock while it was open | 0 | 0 |
| reader: queries / worst latency | 1,589 / 67 ms | 1,595 / 27 ms |
| writer: inserts / worst latency | 252 / 71 ms | 659 / 31 ms |
| errors | none | none |
| indexes rebuilt (relfilenode changed) | none | none |
| indexes invalid | none | none |

Locks held by the updating transaction: `AccessShareLock` on catalogue relations
(`pg_proc`, `pg_depend`, `pg_namespace`, `pg_authid`, `pg_roles`,
`pg_db_role_setting` and their indexes), `RowExclusiveLock` on `pg_proc`'s TOAST
table, and its own transaction and virtual xid. Nothing on a user table.
`tenant_bootstrap.verify` passes afterwards.

**The time is ADR-018's trigger, not the update.** The same empty step:

| Database | Time |
|---|---|
| plain database, `maludb_core` installed, no trigger | 5.8 ms |
| bootstrapped tenant | 395 ms |
| same tenant, `maludb_harden_extensions` disabled | 8.3 ms |

`harden_extension_functions()` re-revokes every extension-owned function in
`public` on every extension DDL — the `RowExclusiveLock` on `pg_proc`'s TOAST is
its `REVOKE`s rewriting ACLs. At ~0.4–0.5 s a tenant, a 200-tenant node's run
is on the order of 100 s. **The plan's stop condition is not met**: the update
blocks no reader or writer and touches no index.

### 5. ADR-018's trigger covers a function a `vector` update adds

No real 0.8.x step adds one, so a synthetic step (`0.8.6` → `0.8.6spike`) was
installed into the node's extension directory, creating
`public.pin_spike_added(vector)`. After `ALTER EXTENSION`: an extension member;
`EXECUTE` refused to `anon`, `authenticated` and `service_role`;
`tenant_bootstrap.verify` passes. The step was removed afterwards.

### 6. A dump carries no extension version — so a move or restore installs the target's

`pg_dump` of a tenant emits:

```
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS maludb_core WITH SCHEMA maludb_core;
```

Moves (`tenant_movement.py`: `pg_dump` on the source, `pg_restore` on the
target) and restores (`restore.py`: `pg_dump` from the scratch cluster,
`pg_restore` into the live node) both take this path, so a tenant arrives at
**whatever `default_version` the receiving node provides.**

Measured in the direction that matters: a tenant at 0.8.6, dumped, the package
downgraded to 0.8.4, restored into a new database —

- it **arrived at `extversion` 0.8.4**, a silent downgrade; its HNSW index
  served and no index was invalid;
- `pg_restore` reported `errors ignored on restore: 1` — the dump's `REVOKE` on a
  function that exists at the source's version and not the target's. The
  function was synthetic here; the class is real for any step that adds DDL.

And the database left behind: under the 0.8.4 library its catalogue still said
the newer version, it still served, and `ALTER EXTENSION vector UPDATE TO
'0.8.4'` failed — `has no update path`. **A node's package can be downgraded; its
tenants' catalogues cannot follow.**

### 7. Outside pinning: no customer role can use any extension function in `public`

Found while checking what Q5's `service_role` result meant. In a tenant the
platform provisions:

| As | `ORDER BY embedding <-> '[…]'::vector(64)` |
|---|---|
| `anon`, `authenticated`, `service_role` | `permission denied for function vector` |
| `mldb_<ref>_admin`, `_client`, `_executor` | `permission denied for function vector` |

0 of `vector`'s 118 functions are executable by `service_role`. The same holds
for other extensions' functions: `similarity()` (pg_trgm), `crypt()` and
`digest()` (pgcrypto) are refused to `authenticated`. Core functions such as
`gen_random_uuid()` are not affected.

The cause is exact. `bootstrap/003_extension_hardening.sql` and `005`'s trigger
revoke `EXECUTE` on every extension-owned function in `public` **from
`PUBLIC`**, then from `anon` and `authenticated`. Every other role held
`EXECUTE` only through `PUBLIC`'s default grant, and nothing grants it back —
searched: no `GRANT EXECUTE` on extension functions anywhere in the control
plane or bootstrap. ADR-018 named `anon` and `authenticated` as the exposure;
revoking from `PUBLIC` was necessary to close it and took every other role with
it.

**This is not a pinning finding and is not fixed here.** It changes the ADR-018
hardening posture, which is a decision rather than a slice, so it is recorded in
`docs/OPEN-QUESTIONS.md` for the owner. It does two things worth stating now:
it **blocks vector search** as a feature until decided, and it means a
Supabase-style application — a `vector` column queried by distance, a
`match_documents` RPC, `crypt()` in a migrated schema — **fails on this platform
today**, which `specs/compatibility-matrix.yaml` does not yet say.

## What this changes in the plan

- **Pinning slice 1's node check** reports backends still mapping a replaced
  library (finding 2), and a node is recorded as at its pin only when none are.
  The runbook gains a step that cycles the node's workers after a package
  install.
- **A move between nodes with different pins is refused**, not only a move onto
  a mismatched node (finding 6). Equal pins, because a lower target silently
  downgrades and a higher one is a tenant arriving ahead of the run.
- **A pin never moves down on a node with tenants** (finding 6): their
  catalogues have no path back.
- **Pinning slice 3's per-tenant `ALTER`** is confirmed as one transaction per
  tenant (finding 4); its time budget is the hardening trigger's, ~0.5 s.
- **The drift report says what lag means**: for a step with no DDL, a lagging
  `extversion` is bookkeeping, not old code (finding 1). Steps with DDL are
  where it is not.
