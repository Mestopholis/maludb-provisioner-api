# Migration from Supabase

## Product goal

Migration is a first-class product capability, even if it is not the first implementation phase.

**It is a CLI the customer runs** (ADR-042). The tool reads their Supabase
project with their own credentials, from their own machine, and writes to their
MaluDB project through the same public API a dashboard would call. The platform
never holds a Supabase credential: a dashboard-driven scanner would mean the
control plane storing a third party's production secret, with a blast radius on
somebody else's platform and a revocation path we do not own. Dashboard-driven
migration is deferred rather than refused; `docs/OPEN-QUESTIONS.md` carries what
it would need first.

Target experience:

```text
Analyze Supabase project
        |
        v
Compatibility report
        |
        v
Migrate schema/data/configuration
        |
        v
Validate MaluDB project
        |
        v
Switch project URL/key
```

## What the first launch covers

ADR-043: **exactly the surfaces `specs/compatibility-matrix.yaml` marks
`supported`** — the database, email/password Auth users and identities, and
Realtime Postgres Changes. Everything else is a scanner *blocker* that names the
phase which will carry it, because a migration that silently moved something the
platform cannot serve would be claiming compatibility the tests do not support,
in the one place a customer cannot check it: their own cutover.

Blocked at launch: Storage (Phase 10), OAuth/magic link/MFA/SSO identities,
Realtime broadcast and presence, Edge Functions, and any extension the allowlist
does not carry.

The matrix is the authority rather than this list. When a surface is promoted to
`supported`, migration scope grows with it.

## Migration domains

The domains below are the eventual shape. What the *first* launch carries is the
section above.

### Database

- schemas;
- tables/data;
- sequences;
- constraints;
- indexes;
- views;
- functions;
- triggers;
- RLS policies;
- supported extensions, from `specs/extension-allowlist.yaml` (ADR-045) —
  a customer may install those themselves, so a migrated schema's
  `create extension if not exists "uuid-ossp"` succeeds rather than needing an
  operator mid-cutover. Implemented in Phase 08 slice 6a as a grant plus an
  event trigger rather than the installer function the ADR first described,
  because an installer only helps if something rewrites that line — see the
  ADR-045 amendment. An allowlisted extension PostgreSQL does not mark
  `trusted` (`vector`) is installed by the platform at provisioning instead;
- publications/replication configuration as applicable.

### Auth

- users;
- identities;
- compatible password hashes where possible;
- metadata;
- confirmation state;
- provider configuration where supported.

### Storage

- buckets;
- object metadata;
- object bytes;
- policies.

### Realtime

- required publications/configuration.

## Running the scanner

Phase 08 slice 5. `maludb-migrate scan` reads the source project read-only and
reports what would stop a migration. It needs no MaluDB project, so it can be
run before a customer has one.

```bash
# The environment variable rather than --source-dsn: an argument is visible in
# `ps` and lands in shell history, and this is a production credential.
export MALUDB_SOURCE_DSN='postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres'
maludb-migrate scan                 # for a person
maludb-migrate scan --format json   # for a runbook or a pipeline
```

Exit codes, so this can gate a deployment script: **0** migratable, **1** a
blocker was found, **2** the tool could not run. The middle one is the point —
a scan that could not read part of the project exits 1 rather than 0, because a
scan that did not run must not look like a scan that found nothing.

The connection string is used to open a connection and for nothing else. It is
never written to the report, an error, or a log line, and the whole read runs in
a `READ ONLY REPEATABLE READ` transaction: "source is not modified unexpectedly"
is an acceptance criterion, and `tests/test_migration_scanner.py` proves it by
making the scan try to write.

## Applying a schema

Phase 08 slice 6b. `maludb-migrate apply` scans first, refuses if anything would
block the migration, and then applies the source's allowlisted extensions and
its schemas to a MaluDB project.

```bash
export MALUDB_SOURCE_DSN='postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres'
export MALUDB_TOKEN='<a personal access token from the dashboard>'
maludb-migrate apply --project-ref abcd1234 --dry-run   # see what would be sent
maludb-migrate apply --project-ref abcd1234
```

Add `--with-data` once you have frozen writes on the source, and the same
command copies the rows. Without it the destination gets the structure and none
of the data, and the tool says so rather than implying a finished migration.

**Freeze writes on the source before copying.** MaluDB cannot do it for you —
the source is Supabase — and rows written during the copy are exactly the ones
that quietly go missing. The tool compares row counts per table afterwards and
refuses to report success if any differ, which is the only check available after
the fact (ADR-044).

Three things about the data path worth knowing:

- **Values travel as their own text representation**, out through each type's
  output function and back in through its input function — which is what `COPY`
  does. The obvious JSON-shaped alternative silently turns a `jsonb` column
  holding the JSON value `null` into SQL `NULL`, because both render as `null`.
- **Tables are copied parents-first**, computed from the source's foreign keys.
  The alternative, disabling triggers, needs privileges a tenant does not have.
- **Sequences are advanced** past the migrated rows. Without that the
  application's next insert collides with a row the migration just wrote — a bug
  that only appears under production traffic.
- **Your triggers are turned off for the copy and back on afterwards.** Migrated
  rows are not application writes: an `updated_at` trigger would rewrite every
  migrated timestamp to the migration's own clock, and a filter trigger would
  drop rows. Foreign keys are still enforced throughout.
- **The source connection must be able to read every row.** The tool sets
  `row_security = off`, so a role that row-level security would filter fails
  loudly instead of copying a subset whose counts look correct. Use the
  `postgres` role from your Supabase project settings.

Three things worth knowing about how it works:

- **`pg_dump` writes the DDL.** Reconstructing it from catalogues means
  re-deriving dependency order, defaults, identity columns and policy
  expressions — work PostgreSQL already does correctly. So `pg_dump` must be on
  the path at or above the source server's major version, which the tool checks
  *before* the write freeze rather than discovering during it.
- **Function permissions are carried; ownership is not.** `pg_dump
  --no-privileges` drops `REVOKE`s along with `GRANT`s, and a newly created
  PostgreSQL function is `EXECUTE` to `PUBLIC` — so a locked-down `SECURITY
  DEFINER` helper would arrive callable by `anon` over your Data API. The tool
  re-applies the source's own function permissions, and the scanner names every
  `SECURITY DEFINER` function so you can check the ones that matter.
- **Ownership and table grants are not carried.** `--no-owner --no-privileges`: the
  source's objects belong to Supabase's `postgres` role and carry its grants,
  and the destination's posture is the platform's own (ADR-018, bootstrap 004
  and 008). RLS policies *are* carried — they are not privileges in `pg_dump`'s
  sense — and they work unmodified because the role names are the same
  (ADR-016).
- **It goes through the public API**, in as few multi-statement requests as the
  size cap allows, with the plan's rate limit waited out rather than failed on.
  The CLI has no privileged path: it is a client of the same route a dashboard
  would call, which is what keeps ADR-039's ceiling meaningful.

## Compatibility scanner

Before migrating, report:

- supported features;
- unsupported extensions;
- incompatible SQL;
- unsupported Auth providers/features;
- Storage usage;
- Realtime usage;
- estimated data size;
- blockers/warnings.

Two severities and the distinction is load-bearing (ADR-043): a **blocker**
means the migration will not complete correctly and must not be attempted; a
**warning** is something to know before committing to a maintenance window. A
blocker names the phase that will carry the surface, so the answer is a date
rather than a refusal.

Three things the scan reports that it *cannot* see, rather than omitting:

- **Edge Functions** live in Supabase's platform, not the database.
- **Realtime broadcast and presence** are client-side and leave no catalogue
  trace.
- **Anything the supplied credential could not read** — which is a blocker, not
  a shrug, because what it could not read may be what would have blocked the
  migration.

## Cutover

ADR-044: the initial migration requires a **controlled write freeze** on the
source during final sync and cutover, and the expected window is **published as
a function of data size**, measured by the Phase 08 validation runs rather than
estimated. "Expect some downtime" is not something a customer can schedule a
maintenance window around; a figure nobody measured would be worse.

**The platform cannot enforce the freeze.** The source is Supabase, so stopping
writes to it is the customer's action in their own application. A migration
where writes continued produces a destination quietly missing rows, which is the
worst failure this capability can have — so validation compares row counts and
the report names any table that moved.

Zero/minimal-downtime migration is a later objective. Do not claim zero-downtime
until implemented and tested.
