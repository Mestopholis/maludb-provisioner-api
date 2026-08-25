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
`supported`** — the database, email/password Auth users and identities,
Realtime Postgres Changes, and — since Phase 10 slice 6 — Storage buckets and
object bytes. Everything else is a scanner *blocker* that names the phase which
will carry it, because a migration that silently moved something the platform
cannot serve would be claiming compatibility the tests do not support, in the
one place a customer cannot check it: their own cutover.

Blocked at launch: OAuth/magic link/MFA/SSO identities, Realtime broadcast and
presence, Edge Functions, and any extension the allowlist does not carry.

Carried with a caveat rather than blocked: **Storage policies**. MaluDB enforces
row-level security on `storage.objects`, but no customer-reachable role can
create a policy there (ADR-061), so your objects arrive and the rules that
governed them do not. The scan names each one. This fails *closed* — with no
policy, every role but `service_role` is denied — so what breaks is your
application's access, not the privacy of your files.

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

Phase 10 slice 6, ADR-063.

- buckets, including empty ones and their public flag, size limit and MIME
  restrictions where the source records them;
- object metadata — the key, content type and cache-control header;
- object bytes.

Not carried, each reported rather than silently dropped:

- **policies** — see above;
- **`owner_id` and the original timestamps**. The Storage API has no way to
  accept them: an uploaded object is owned by the token that uploaded it and is
  created now. Preserving them would mean writing `storage.objects` directly,
  which is the metadata the object store is kept consistent with;
- **objects larger than the destination's upload ceiling** (50 MiB by default).
  These are named and skipped *before* being downloaded, so an oversize file
  costs you a line in the report rather than a transfer that fails at the end.

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

## Migrating Auth users

Phase 08 slice 7. Add `--with-auth` and the source's **email and password**
users come across, bcrypt hashes included — so a migrated user signs in with the
password they already had, rather than discovering at cutover that they must
reset.

```bash
maludb-migrate apply --project-ref abcd1234 --with-auth --with-data
```

Two things shape how this works, and neither is a detail:

- **It does not go through the SQL console.** A tenant's `auth` schema is owned
  by the project's auth role, and the console's role is denied both `SELECT` and
  `INSERT` on `auth.users` — measured. Granting it access was considered and
  rejected: it would put every end user's bcrypt hash within reach of anyone
  with console access, on every tier, permanently, in exchange for a one-off
  operation. So the platform holds the credential and
  `POST /v1/projects/{ref}/auth/import` takes **JSON rows, never SQL**,
  composing every statement itself from an allowlist of columns.
- **In-flight secrets are not carried.** `confirmation_token`, `recovery_token`,
  the `email_change_*` tokens and `reauthentication_token` belong to the
  platform that minted them; carrying them would let a Supabase-issued token be
  redeemed against MaluDB. A user midway through a password reset starts it
  again.

**A project whose tables live in `public` migrates.** That sounds like nothing;
it did not work until slice 8. `pg_dump` emits `CREATE SCHEMA "public"` and every
MaluDB project already has one, so the migration stopped on its first statement
-- for every real Supabase project, since that is where Supabase puts your
tables. It survived two slices of testing because the tests all used a schema
named `app`. The schema statement is now tolerant of one that exists.

**A comment on the `public` schema is not carried**, and it is the only thing
dropped. `COMMENT ON SCHEMA` requires ownership of the schema, and your
project's `public` is the platform's -- on MaluDB and on Supabase alike.
pg_dump's text for it is `'standard public schema'`. Comments on your own
schemas, tables and columns are carried normally.

**`role` and `aud` are set by the platform, not carried.** They decide which
database role a GoTrue token maps to, and `service_role` bypasses row-level
security — so importing them would let whoever runs the migration choose that.
Every imported user arrives as `authenticated`, which is what GoTrue's own
signup writes. Importing users is an owner or admin action for the same reason.

`raw_app_meta_data` *is* carried, because it is your data and your policies
already trusted it — but the tool reports how many imported users carry a
`role`-shaped key in it, since Supabase-style policies often read
`auth.jwt() -> 'app_metadata' ->> 'role'`.

External-provider identities are not imported (ADR-043) and the scanner reports
them as a blocker beforehand. The import names any column this platform's GoTrue
does not have, rather than discarding it silently — the two sides pin different
versions.

## Migrating Storage

Phase 10 slice 6, ADR-063. Add `--with-storage` and your buckets and their
object bytes come across.

This is the one part of a migration that needs credentials the rest of it does
not, and the reason is where the bytes are. Buckets and the object list are rows
in your Supabase database, which the tool already reads. The **files themselves**
are in Supabase's object store, and MaluDB's are in the node's — neither is
reachable with a platform token.

```bash
export MALUDB_SOURCE_STORAGE_URL='https://<ref>.supabase.co'
export MALUDB_SOURCE_SERVICE_KEY='<your Supabase service-role key>'
export MALUDB_PROJECT_KEY='<the destination project's secret key>'

maludb-migrate apply --project-ref abcd1234 --with-storage --with-data \
                     --receipt cutover.json
```

All three are environment variables and none has a command-line flag, unlike
`--source-dsn`. A DSN is scoped to one database; a service-role key is your
entire Supabase project and a secret key is the destination's data API with no
row-level security in front of it. Neither belongs in `ps` output or your shell
history.

**None of these keys is created for you.** The tool holds a platform token that
could issue itself a destination key, and deliberately does not: creating one is
closer to adding an owner than to changing a setting, and a migration that
issues a credential quietly leaves a live one behind whenever it fails
partway — which is exactly when nobody is looking.

**The bytes travel through your machine**, source to here to destination. That
is slower than a server-side copy between two object stores and is the same
arrangement as every other part of a migration (ADR-042): the platform never
holds your Supabase credentials.

What to expect while it runs:

- Buckets are created first, including empty ones — an empty bucket is
  configuration your application expects to find.
- Objects are copied one at a time and the run reports progress every hundred.
- **A single file that fails does not stop the run.** It is recorded and the
  copy continues, because the alternative is discovering your broken files one
  re-run at a time inside a write freeze.
- **A run that lost anything exits non-zero** and says so in plain words. These
  are your files; an exit code of 0 is a script's permission to cut over.
- With `--receipt`, every object that did not arrive is written to a sidecar
  file — `cutover.storage.json` for the example above — because the terminal
  shows the first twenty and you may have five hundred.
- Re-running is safe. Buckets that exist are left alone and objects are
  overwritten, so an interrupted migration is finished by running it again.

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

### The measured window

Slice 8 measured it, so the figure ADR-044 promised now exists:

| | |
|---|---|
| Copy | **≈1.9 MiB/s**, ≈12,000 rows/s |
| Verify, row counts | ≈700 MiB/s |
| Verify, `--digest` | ≈120 MiB/s |

Measured on 1,000,000 rows / 160.5 MiB with source and destination on one host.
Roughly **nine minutes per GiB**, so 10 GiB is an hour and a half.

**That rate is the price of having no privileged path.** Rows move as multi-row
`INSERT` statements through the same public SQL route a dashboard uses, because
the alternative -- `COPY` over a direct connection -- needs a privilege the
console does not have and that the free tier has no connection for at all. It is
a deliberate trade and it is a slow one, and a customer with tens of gigabytes
deserves to hear that before they book an outage rather than during it.

`scan --throughput-mb-per-s` takes a rate you measured yourself; without one the
report says the window is not measured rather than printing a plausible number.

### Verifying afterwards

`maludb-migrate verify` compares the two databases once the copy is done, and
`docs/CUTOVER-RUNBOOK.md` is the sequence to run it in.

Two things it is worth knowing it does *not* do by default:

- **It cannot see a broken freeze without `apply --receipt`.** The receipt
  records what each table held when it was copied. Without it, a source that
  gained rows and a copy that fell short are the same arithmetic, and they have
  opposite remedies. The report says which question it answered.
- **Row counts do not catch a table whose rows arrived and were changed on the
  way.** Measured: two databases identical except for one rewritten timestamp
  compare equal on `count(*)`. `--digest` compares content, at roughly a sixth
  of the speed, inside your freeze window.

It also checks sequences, because a table whose rows all arrived and whose `id`
sequence never advanced verifies clean on every other measure and fails on the
customer's first insert.
