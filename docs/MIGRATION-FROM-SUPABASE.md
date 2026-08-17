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
  operator mid-cutover;
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
