# MaluDB Platform Agent Instructions

This repository may be developed by multiple human developers using different coding agents, including OpenAI Codex and Claude Code.

The repository is the source of truth. Do not rely on previous chat sessions as authoritative project state.

## Read before significant work

For any substantial implementation task, read:

1. `docs/REQUIREMENTS.md`
2. `docs/ARCHITECTURE.md`
3. `docs/DECISIONS.md`
4. `docs/MALUDB.md` — what MaluDB actually is, and its verified platform constraints
5. the applicable file under `tasks/`
6. any referenced file under `specs/`

For multi-step work, also read `PLANS.md` and create or update an execution plan under `plans/active/`.

## Architectural invariants

Do not violate these without an explicit architecture decision:

- A customer project is a dedicated PostgreSQL/MaluDB database plus constrained roles inside an already-running shared MaluDB database cluster.
- Creating a customer project must not provision a VM or container.
- MaluDB/platform infrastructure owns tenant databases. Customers do not receive PostgreSQL superuser or database-owner privileges.
- Free projects receive no PostgreSQL connection credentials and no reachable database port. A direct connection is a paid capability. SQL executed *by the platform on the project's behalf* is not a direct connection and is available to every tier (ADR-039).
- Free-to-paid upgrades normally retain the same physical tenant database.
- Supabase compatibility is the first public compatibility target.
- MaluDB-specific functionality must extend the Supabase-compatible surface, not silently alter or break it.
- Project API traffic is routed by project identity and validated against project-scoped API keys.
- PostgREST and Auth may begin as per-active-project processes/configurations. Do not replace them with a custom multi-tenant implementation unless a later decision explicitly authorizes it.
- Resource governance is defense in depth: gateway limits plus PostgreSQL/MaluDB limits plus node-level capacity management.
- Customer-controlled extensions and privileged SQL capabilities must be allowlisted.
- Never make an undocumented architectural change merely to simplify an implementation.

## Agent-neutral workflow

- Do not create separate Codex-only and Claude-only architecture.
- The `docs/`, `specs/`, `tasks/`, and `plans/` directories are canonical for every agent.
- `CLAUDE.md` is an adapter, not a second source of truth.
- Record durable decisions in `docs/DECISIONS.md`.
- Record unresolved product/architecture choices in `docs/OPEN-QUESTIONS.md`.
- If implementation discovers that a decision is infeasible, stop that line of implementation, document the conflict, and propose an ADR-style change rather than silently deviating.

## Local development

Control plane: Python 3.12, FastAPI, psycopg3, no ORM (ADR-024). Requires
PostgreSQL 17 and `uv`.

```bash
# 1. Dependencies
uv venv --python 3.12
uv pip install -e ".[dev]"

# 2. Development key material (ADR-023). .dev/ is gitignored -- never commit it.
mkdir -p .dev
openssl rand -hex 32 > .dev/kek
openssl rand -hex 32 > .dev/pepper
chmod 600 .dev/kek .dev/pepper

# 3. Control-plane database. This one is plain PostgreSQL: maludb_core belongs
#    in tenant databases (ADR-015), not here.
sudo -u postgres psql -c "CREATE ROLE cp_dev LOGIN PASSWORD 'devonly'"
sudo -u postgres psql -c "CREATE DATABASE maludb_control_plane_dev OWNER cp_dev"

# 4. Environment
export MALUDB_ENV=development
export MALUDB_CONTROL_PLANE_DATABASE_URL="postgresql://cp_dev:devonly@127.0.0.1:5432/maludb_control_plane_dev"
export MALUDB_KEK_REF=.dev/kek
export MALUDB_TOKEN_PEPPER_REF=.dev/pepper

# 5. Migrate, seed the plan catalogue, and run
.venv/bin/python -m services.control_plane.migrate
.venv/bin/python -m services.control_plane.manage plans sync
.venv/bin/uvicorn --factory services.control_plane.main:create_app --reload --port 8111
```

`plans sync` is not optional and is easy to miss, which is why it is listed
here rather than left to be discovered: **nothing else seeds the `plans`
table.** Without it `models.default_plan` finds no `free` plan, creating a
project answers 503, and the control plane logs a warning at startup saying so.
It writes identity -- the code, the name, whether the plan is offered -- and
leaves the numbers to `entitlements.DEFAULTS`, so re-running it never discards a
deployment's own overrides. `plans list` shows what each plan actually grants.

Billing (Phase 09 slice 4, ADR-049) is off unless configured, and a control
plane without it serves every other route:

```bash
export MALUDB_STRIPE_SECRET_KEY=sk_test_...      # test mode is read from the prefix
export MALUDB_STRIPE_WEBHOOK_SECRET=whsec_...    # the endpoint's signing secret
export MALUDB_BILLING_GRACE_DAYS=14              # ADR-051; optional, this is the default
.venv/bin/python -m services.control_plane.manage billing price set --plan pro --price price_...
```

`billing price set` is the `plans sync` of this feature: **nothing else maps a
plan to a price**, and a plan with no mapping cannot be bought — the checkout
route answers 409 naming it. `cp-manage billing status` says whether the
deployment can take money and what is waiting; `cp-manage billing events` says
what Stripe delivered and what became of each.

The command checks the Stripe **product's** tax code before writing the
mapping and refuses one that is not eligible for Managed Payments. That is not
tidiness: an ineligible product does not fail at checkout, it silently drops
that transaction out of Managed Payments and makes this platform the seller of
record for it. `--unverified` skips the check and says what it costs.

Nothing in the test suite reaches Stripe, so none of these are needed to run
it. What *is* needed for a paid plan to actually take effect is the maintenance
pass: the webhook records what was paid for and
`cp-manage maintenance run` applies it (ADR-053).

Swagger UI is then at `http://127.0.0.1:8111/docs`. It is disabled in
production by default (ADR-024).

That factory builds the **internal** application: every router, including the
ones that must never face the internet. ADR-037 splits the surface in two, and
in production they are separate listeners on separate interfaces:

```bash
# public -- only the routers classified in PUBLIC_ROUTERS
.venv/bin/uvicorn --factory services.control_plane.main:create_public_app --port 8112
```

`specs/control-plane-api.yaml` is generated from the public one, because that is
the contract a customer's client is written against. In development one host can
run both; what must not happen is the internal application on a public
interface, since it serves routes whose only other protection is their own
signature.

## Running the tests

The suite needs two things the development setup above does not provide, and
**silently skips rather than fails without them**.

A scratch control-plane database, separate from the development one. The suite
truncates tables, and it never truncates `encryption_keys` — so a development
database that has been used with your real KEK keeps a data encryption key the
test KEK cannot unwrap. Aim the tests at their own database:

```bash
sudo -u postgres psql -c "CREATE DATABASE maludb_control_plane_test OWNER cp_dev"
```

And a superuser DSN for the node under test, because provisioning creates
databases and roles. Use a disposable cluster — these tests create and drop
`mldb_*` roles and databases:

```bash
export MALUDB_CONTROL_PLANE_DATABASE_URL="postgresql://cp_dev:devonly@127.0.0.1:5432/maludb_control_plane_test"
export MALUDB_NODE_ADMIN_DSN="postgresql://<superuser>:<password>@127.0.0.1:5432/postgres"
export MALUDB_PLATFORM_OWNER=postgres   # role that owns tenant databases
```

Without `MALUDB_NODE_ADMIN_DSN` the run reports **`124 passed, 36 skipped`** in
green, having verified none of Phase 02's security properties — cross-tenant
isolation, `CONNECT` lockdown, per-tenant role privilege limits, or the ADR-018
extension-function revoke. The suite prints a `security properties not
verified` banner when this happens. Do not read a pass past that banner as
evidence that isolation holds.

Some tests need `maludb_core` installed on the cluster and skip without it,
including whether `anon` can reach `gen_salt` — the finding ADR-018 exists for.
CI builds the extension from a pinned upstream commit and sets
`MALUDB_REQUIRE_MALUDB_CORE=1`, which turns an absent extension into a **failed
run** rather than a skipped test. Set it locally too if you want the same
guarantee; leave it unset and you get the banner instead.

The Realtime node assertions need a cluster the development one cannot be:
`wal_level = logical` needs a restart, and the ADR-031 `pg_hba.conf` reject of
physical replication is a file. So they get their own throwaway cluster on
another port, which a script builds and drops:

```bash
sudo apt-get install -y postgresql-17-wal2json   # Postgres Changes decode through it
scripts/realtime-test-cluster.sh          # prints the exports
export MALUDB_REALTIME_NODE_DSN="postgresql://postgres:...@127.0.0.1:5433/postgres"
export MALUDB_REALTIME_DB_HOST=10.90.0.1  # the Realtime data address
export MALUDB_REALTIME_DB_PORT=5433
scripts/realtime-test-cluster.sh --drop   # afterwards
```

The script also builds the **data address**: a private address on an interface
of its own, which the cluster listens on and ADR-031's reject covers. A Realtime
instance is a container with no route to the node's loopback (ADR-035), so
without that address it has no way to reach PostgreSQL at all.

`wal2json` is the one prerequisite here that fails silently rather than loudly.
It is a node requirement, not a convenience — Postgres Changes decode through
that output plugin — and without it a client subscribes successfully and no
event is ever delivered, which arrives as a ten-second timeout naming neither
the plugin nor the package. `cp-manage node realtime-check` reports it, and CI
asserts it when it builds the cluster.

On **PostgreSQL 17.11 and later** the package is not enough: that minor added
`output_plugin_libraries`, which allowlists what a replication connection may
load, and an installed plugin missing from it fails the same silent way. The
script sets it when the version has it — which is why the cluster it builds is
the supported way to run these tests.

Without it, `tests/test_realtime_node.py` skips and the banner says so. What
skips is the assertion that a role holding `REPLICATION` **cannot** take a base
backup of every tenant on the node — the finding ADR-031 exists for — plus the
demonstration that a stalled consumer loses its slot rather than the node losing
its disk. CI builds the cluster and sets `MALUDB_REQUIRE_REALTIME_NODE=1`, which
turns an absent one into a failed run rather than a skipped test.

Never point that variable at a node carrying customer data. A cluster that fails
the check answers a base backup with a readable copy of every database on it.

Running a real Realtime *server* needs Podman and the pinned image, since
upstream ships no binary (ADR-033):

```bash
sudo apt-get install -y podman
podman pull docker.io/supabase/realtime:v2.110.0
```

Without them `tests/test_realtime_server.py` and `tests/test_realtime_compat.py`
skip, and the banner says what that costs: that Postgres Changes are delivered
at all, and that the container cannot reach the node's loopback — where a
tenant's PostgREST answers anonymous reads to anything that can open its port.
CI pulls the image and sets `MALUDB_REQUIRE_REALTIME_SERVER=1`.

Storage (Phase 10) needs the pinned `storage-api` image, and needs it for a
reason that is not obvious: upstream publishes **no release tarball**, so its 63
tenant migrations exist only inside the image. `tests/test_object_storage.py`
takes them out of it and applies them as the constrained owner the platform
provisions.

```bash
podman pull docker.io/supabase/storage-api:v1.70.6
```

Without it three tests skip, and what skips is the claim slice 1 exists to
make: that upstream's migrations complete without the superuser they ask for,
and that they add nothing to `public` — the one schema PostgREST exposes.
Migration 0011 creates a function *unqualified*, so losing the `search_path`
pin does not fail, it silently publishes a platform function on the customer's
Data API. The suite prints a banner when the image is absent. CI pulls it and
sets `MALUDB_REQUIRE_STORAGE_MIGRATIONS=1`, which turns an absent image into a
failed run rather than a skipped test.

Running the storage **worker** needs more than the image: an object store on a
data address, and PostgreSQL reachable at one. A script builds both, the same
way `realtime-test-cluster.sh` does for Realtime:

```bash
scripts/storage-test-cluster.sh          # prints the exports
export MALUDB_STORAGE_DB_HOST=10.91.0.1
export MALUDB_STORAGE_S3_ENDPOINT=http://10.91.0.1:8333
export MALUDB_STORAGE_S3_BUCKET=maludb
export MALUDB_STORAGE_S3_ACCESS_KEY=maludb-platform
export MALUDB_STORAGE_S3_SECRET_KEY=...      # generated; the script prints it
scripts/storage-test-cluster.sh --drop   # afterwards
```

The data address is not a convenience. ADR-035 forbids a rootless Podman
container from reaching node loopback, so the worker addresses both PostgreSQL
and the object store the way it would address them in another datacentre — and
`render_env` refuses a loopback value rather than starting a badly contained
worker. The script also adds the `pg_hba.conf` line that lets the container
authenticate at that address, and removes it on `--drop`.

It arranges the other half of that too, and this one **restarts your node's
PostgreSQL**: `listen_addresses` is postmaster context, so a cluster that
answers only on `localhost` — which is what `pg_createcluster` leaves you, and
what CI had — cannot be talked round with a reload. The change is additive and
a node already on `*` is left alone. This was assumed rather than arranged for
one release, and the failure it produced named nothing useful: the container
was refused, never migrated, and the suite reported `the storage worker never
became ready` a minute later.

Without it, `tests/test_object_store.py` and `tests/test_storage_workers.py`
skip, and the banner says what that costs: the S3 bake-off behind the pinned
SeaweedFS release, and every isolation claim the shared worker makes — that two
tenants using the same bucket and key names read back their own bytes, that a
token signed for one reaches nothing of another's, and that the container cannot
reach the node's loopback. CI builds it and sets
`MALUDB_REQUIRE_OBJECT_STORE=1` and `MALUDB_REQUIRE_STORAGE_SERVER=1`.

Backup (Phase 11) needs `pgbackrest` and a cluster of its own — a third one,
for the same reason Realtime gets one: `archive_mode` is postmaster context and
a pgBackRest stanza owns a whole cluster, so this cannot be done to the cluster
the rest of the suite is using.

```bash
sudo apt-get install -y pgbackrest
scripts/backup-test-cluster.sh            # prints the exports
export MALUDB_BACKUP_NODE_DSN="postgresql://postgres:...@127.0.0.1:5434/postgres"
export MALUDB_BACKUP_STANZA=maludb-bk
export MALUDB_BACKUP_RUN_AS=postgres      # pgBackRest must be the cluster's owner
scripts/backup-test-cluster.sh --drop     # afterwards
scripts/backup-test-cluster.sh --permissive   # WITHOUT the ADR-031 reject
```

`MALUDB_BACKUP_RUN_AS` is not a convenience. pgBackRest reads the data
directory directly and `/etc/pgbackrest.conf` is mode 0600 owned by `postgres`,
so any other user gets `unable to open file '/etc/pgbackrest.conf' for read:
[13] Permission denied` — an error that names a config file and not the actual
cause. Root is not a way round it: a root without `CAP_DAC_OVERRIDE` cannot
read a file it does not own. Become the owner.

Without the cluster, `tests/test_backup.py` skips three tests and the banner
says what that costs — and it is a security property, not a durability
convenience. What skips is the assertion that pgBackRest takes a full backup of
a cluster carrying ADR-031's `host replication all <cidr> reject` **with zero
walsenders**, which is what makes ADR-067 true. If that ever stopped holding,
either the platform could not back up a node or the control that stops one
tenant taking a byte-level copy of every other tenant would have to be
narrowed. CI builds the cluster and sets `MALUDB_REQUIRE_BACKUP_REPO=1`, and
asserts both halves in the build step: that `pg_basebackup` is refused, and
that `pgbackrest check` passes on the same cluster.

The `--permissive` form builds the cluster *without* the reject, following the
Realtime script's precedent, so a check that has never returned unsafe is not
mistaken for a working check.

The compatibility suite additionally needs Node, the official client, and a
hostname that resolves to the gateway — the hostname *is* the routing key
(ADR-008), so a test that bypassed DNS would not exercise it:

```bash
(cd tests/compat && npm install)
echo "127.0.0.1 cmpt0001.maludb.local" | sudo tee -a /etc/hosts
echo "127.0.0.1 rtcp0001.maludb.local" | sudo tee -a /etc/hosts
echo "127.0.0.1 stcp0001.maludb.local" | sudo tee -a /etc/hosts
echo "127.0.0.1 stcp0002.maludb.local" | sudo tee -a /etc/hosts
```

Four entries, and none of them are interchangeable. The Realtime compatibility
test needs a tenant on the prepared cluster while the Phase 03 suite's lives on
the ordinary node, and the hostname *is* the project ref, so they cannot share
one. The Storage suite (`tests/test_storage_compat.py`) needs **two** for a
reason of its own: its acceptance criterion is that one project cannot reach
another's objects, and the hostname is what names the tenant to the shared
worker — so the second project cannot be a variation of the first.

Without them that suite skips, and the banner says what that costs: that an RLS
policy on `storage.objects` gates what the official client can read, and that a
project cannot reach another project's objects. It also needs everything the
storage worker needs, above — it drives a real `storage-api`, not a stub.

It also needs PostgREST on the path, or `MALUDB_POSTGREST_BIN` pointing at it.

The Auth tests need GoTrue — published as `supabase/auth`, still shipping a
binary named `auth` with a `gotrue` symlink beside it. Extract the whole
archive, not just the executable: `gotrue migrate` reads the `migrations`
directory that ships next to it, so a lone binary starts and can never migrate
a tenant.

```bash
curl -sL https://github.com/supabase/auth/releases/download/v2.195.0/auth-v2.195.0-amd64.tar.xz \
  | sudo tar -xJ -C /usr/local/bin
export MALUDB_GOTRUE_BIN=/usr/local/bin/gotrue
```

Checks, all of which CI also runs:

```bash
.venv/bin/ruff check .                                  # lint
.venv/bin/pytest -q                                     # tests
.venv/bin/python scripts/export-openapi.py --check      # OpenAPI drift
.venv/bin/python -m services.control_plane.migrate      # idempotent; re-run is a no-op
```

Run `pytest`, not `python -m pytest`. The latter puts the working directory on
`sys.path` and the former does not, so the two can disagree about imports —
which once produced a green local run and a red CI run. CI invokes bare
`pytest`; match it.

After changing any route, regenerate the contract — CI fails otherwise:

```bash
.venv/bin/python scripts/export-openapi.py
```

Migrations are immutable once applied. The runner rejects a file whose checksum
changed; add a new migration instead of editing an old one.

## Development rules

- Prefer small, reviewable phases over broad rewrites.
- Keep infrastructure behavior configuration-driven.
- Never hard-code production plan limits in application logic.
- Secrets must never be committed to the repository.
- API keys and database passwords must be generated cryptographically.
- Store secret API key material hashed where verification semantics permit.
- Tenant IDs/project refs must be treated as untrusted input.
- SQL identifiers generated from project metadata must be validated and/or safely quoted.
- Provisioning operations must be idempotent or safely retryable.
- Destructive provisioning/cleanup operations must require explicit state checks.

## Compatibility rules

When implementing a Supabase-compatible feature:

- Prefer the official upstream protocol/behavior over creating a MaluDB-specific alternative.
- Add a black-box compatibility test using the official Supabase client when practical.
- Test the same behavior against Supabase and MaluDB where practical.
- Document intentional incompatibilities in `specs/compatibility-matrix.yaml`.
- Do not claim full Supabase compatibility until the matrix and automated tests support the claim.

## Definition of done

A task is not complete until:

- acceptance criteria in the task file are met;
- tests pass;
- **a security review has been done before merge, and recorded** — see below;
- affected docs/specs are updated;
- any new architecture decision is recorded;
- the active execution plan is updated;
- no secrets or environment-specific credentials are committed.

### The security review, and why it is a merge gate

Record the outcome as a trailer on a commit in the change:

```
Security-Review: none
Security-Review: 2 findings, both fixed -- unsanitised name reaching a
                 terminal, truncated snapshot compared as complete
```

`none` is a real answer and the common one. What is not available is silence:
CI's `security-review` job refuses any change touching something other than
`docs/`, `plans/` or `tasks/` without one, and `scripts/require-security-review.sh`
runs the same check locally.

This used to say "consider security/isolation implications", which is what a
checklist says. Twice that was not enough. Phase 07's plan asked for a review
before merge on every slice; four slices merged without one and the catch-up
pass found three issues in shipped code, including a customer able to grant
themselves `direct_database_access`. Phase 08's plan asked again in stronger
words and named slice 1 in advance as not mergeable on a green suite alone;
slice 1 is the one slice of eleven that merged with no review recorded, and its
catch-up pass found ADR-046.

Neither omission was a decision. Both were a control held by prose, checked by
whoever was also doing the work. The trailer moves it somewhere it cannot be
skipped by not thinking about it, and — because it lives in the commit rather
than in a pull request — somewhere an audit can still read it after the branch
is deleted. Finding the Phase 08 gap at all meant grepping commit bodies.

The gate cannot judge a review; nothing in CI reads the code for this. It
enforces that one was recorded. That is the honest limit of it, and it is still
the difference between the two findings above shipping and not.

## Code review rules

Reviewers and agents should pay special attention to:

- cross-tenant data access;
- SQL injection through generated database/role/schema identifiers;
- bypasses of API-only free-tier restrictions;
- API key/project mismatch;
- privilege escalation;
- missing rate/concurrency controls;
- unsafe retry behavior in provisioning;
- secret leakage in logs;
- code that assumes one database per VM;
- code that grants database ownership or superuser privileges to customers.
