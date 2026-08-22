# Execution Plan: Phase 10 — Storage, and keeping the bytes somewhere the database is not

Status: **NOT STARTED** — plan written 2026-08-22, no code yet.

Human owner: repository owner
Agent: Claude Code
Branch: `plan/phase-10-storage` for this document, then `feat/phase-10-slice-*`, one per slice
Related task: `tasks/PHASE-10-STORAGE.md`
Dependencies: Phase 09 complete (merged 2026-08-21, PRs #70–#79). Nothing in
Phase 10 depends on billing beyond the entitlement machinery Phase 05 built and
Phase 09 made reconcilable — which matters, because slice 2 adds two
entitlements and they must reach the node rather than only the row. That is the
lesson Phase 09 opened with, and the reason it is named here.

## Objective

Give a project buckets and objects through the official Supabase Storage
surface, with the bytes outside PostgreSQL, one tenant unable to reach
another's objects, and no coupling between the tenant-database lifecycle and
whichever object store is behind it.

## The three questions this phase was blocked on, and their answers

`docs/OPEN-QUESTIONS.md` carried a `## Storage` section of three bullets since
Phase 01. All three were answered 2026-08-22 by the repository owner and are
recorded as ADR-055 to ADR-057. They are restated here because the slices below
assume them.

**Object-storage provider — SeaweedFS.** MinIO was the obvious candidate and is
the wrong one: its community edition went to maintenance in December 2025 and
was archived 25 April 2026, with the admin console stripped, binary
distribution stopped and no security patches. Unpatched is disqualifying for
the component holding every customer's files. SeaweedFS is Apache 2.0 (not
AGPL, which matters for a commercial platform and rules out Garage on the same
grounds), actively developed, and its S3 gateway is a single Go binary.

**Where the bytes live — the existing Proxmox hardware, for now, with a stated
exit.** Not a separate storage box yet. This is affordable precisely because
the platform never learns that the object store is local: ADR-035 already
forbids a Podman container from reaching node loopback, so `storage-api` must
address SeaweedFS on a data address whether it is one hop away or one
datacenter away. Moving to a dedicated box later is an endpoint change and a
copy, with no platform code touched. Recorded as ADR-055, with the exit written
down rather than assumed.

**Egress model — hard ceilings on every tier, including free.** Two new
entitlements, `object_storage_bytes` and `egress_bytes_per_month`, enforced at
the point of use exactly as ADR-050 requires: refused when exceeded, never
converted into a charge, never reported to any provider. Free projects get
Storage, because Storage over the gateway is API access and therefore inside
ADR-005 rather than against it — and because `services/migrate/rules.py` turns
away every Supabase project that uses Storage today, which is a migration story
this phase exists to fix.

## Scope

- Object-store provider abstraction (which is upstream's, not ours — see below).
- Buckets and object metadata in the tenant database.
- Upload, download, delete.
- Signed URLs.
- Authorization/RLS integration.
- Migration path from Supabase Storage.

## Non-goals

Each of these gets a `specs/compatibility-matrix.yaml` entry saying it is
deferred and why. Silence is what makes a gap into a surprise.

- **Resumable/TUS uploads.** The omission a migrating customer is most likely
  to hit, and deferred deliberately rather than overlooked: it adds upload
  state that survives across requests, with its own failure modes, to a phase
  that is already introducing object storage to the platform.
- **Image transformation.** Needs a second pinned container (imgproxy) per
  node, and carries a specific ADR-033 hazard — these nodes are QEMU vCPUs with
  no AVX2, which is exactly what killed the newer Realtime image with SIGILL
  inside a precompiled Rust NIF. imgproxy leans on libvips. It would need
  measuring on real node hardware before it could be promised, and that
  measurement is not this phase's.
- **The S3 protocol endpoint** (`S3_PROTOCOL_ACCESS_KEY_*`), which lets a
  customer point an S3 client at their project directly. It is a credential and
  a reachable endpoint, which is ADR-039's paid line almost word for word, and
  deserves that decision explicitly rather than by inheritance.
- **Overage billing of storage or egress.** ADR-050 settled this for the
  platform and it is not reopened per-resource.
- **Moving the object store to dedicated hardware.** ADR-055 records the exit;
  taking it is an operations task, not a phase.

## What is already true, measured before planning

Measured 2026-08-22 against the repository at `9740a98` and against upstream.

### Upstream ships no binary, so this is ADR-033's pattern and not ADR-027's

`supabase/storage` releases carry exactly two assets — `api.json` and
`api-admin.json` — across v1.70.4, v1.70.5 and v1.70.6 (19–21 August 2026).
There is no tarball and no binary. PostgREST and GoTrue both publish
executables and are therefore plain systemd units under ADR-027; Realtime does
not and became a pinned rootless-Podman container under ADR-033. Storage is in
the second group, and not by preference: `AGENTS.md` prefers upstream's own
artefact, and upstream's own artefact is an image.

`supabase/storage-api:v1.70.6` is published for amd64 and arm64 at roughly
244 MB compressed. That is the pin candidate; slice 0 confirms it runs on the
node CPU profile before anything depends on it.

### The provider abstraction already exists, and it is upstream's

`docs/STORAGE.md` requires that the implementation "must not couple the
tenant-database lifecycle to one specific object-storage vendor". Upstream
selects its backend with `STORAGE_BACKEND=s3|file` and addresses an S3 one with
`STORAGE_S3_ENDPOINT`, `STORAGE_S3_REGION`, `STORAGE_S3_FORCE_PATH_STYLE` and
`STORAGE_S3_BUCKET`.

So the requirement is met by configuration, and **the platform should write no
provider abstraction of its own.** A MaluDB-side driver interface over S3 would
be a second abstraction above an existing one, owned by us, tested by us, and
adding nothing. This is worth stating because the task file's first scope bullet
reads "object-store provider abstraction", and the correct implementation of
that bullet is an environment variable and a document saying so.

### `STORAGE_S3_BUCKET` is singular, which answers the tenancy question

Upstream names one bucket for the whole deployment. A customer's "bucket" is a
row in the tenant database's `storage.buckets`, and an object is a key prefixed
by tenant and bucket inside the single platform bucket. That is not a
constraint this platform is choosing; it is the design of the product being
made compatible with, and inventing a bucket-per-project scheme on top would be
a MaluDB-specific alternative to an upstream behaviour, which `AGENTS.md`
forbids. Recorded as ADR-057.

The consequence that matters for review: **tenant isolation for objects is not
provided by the object store.** One S3 bucket holds every tenant's bytes, and
the only things standing between tenants are the key prefix and the tenant
database rows that map a request to it. Acceptance criterion 3 — cross-project
object access denied — is therefore a property of the metadata layer and the
worker's credential scoping, and must be tested as one. It is the highest-value
security review in this phase.

### `MULTI_TENANT` exists, and Realtime's reason for per-project does not apply here

ADR-034 forced one Realtime instance per project, and the reason was specific:
`SLOT_NAME_SUFFIX` is a server-level variable and PostgreSQL replication slot
names are cluster-unique, so one server can serve one tenant per cluster. That
cost ~146 MB per project against 31.8 MB for an entire warm project.

`storage-api` has `MULTI_TENANT=true`, `DATABASE_MULTITENANT_URL`,
`SERVER_ADMIN_API_KEYS` and `REQUEST_X_FORWARDED_HOST_REGEXP` — tenant resolved
per request from the forwarded host, with each tenant's DSN held in a
multitenant database. There is no cluster-unique resource in that design and
therefore no known reason it cannot serve many tenants on one cluster.

**This is the single largest open technical question in the phase and slice 0
exists to settle it**, because the two answers differ by an order of magnitude
in density and by a great deal in blast radius. A shared instance holds every
tenant's DSN in one process; a per-project instance holds one. ADR-034 accepted
per-project partly because it preferred the smaller blast radius and could
afford it. Whether the same trade is right here depends on a number nobody has
measured yet.

### Upstream's migrations want a superuser, and cannot have one

`.env.sample` carries `DB_INSTALL_ROLES=true`, `DB_SUPER_USER=postgres`,
`DB_ANON_ROLE=anon`, `DB_SERVICE_ROLE=service_role`,
`DB_AUTHENTICATED_ROLE=authenticated` and `DB_ALLOW_MIGRATION_REFRESH=true`.

Every one of those collides with something already decided. ADR-004 says the
platform retains database ownership and customers get no superuser. ADR-016 says
the Supabase role names are shared cluster-wide and each project gets its own
authenticator — so a component that thinks it may create `anon` is a component
that thinks it is alone on the cluster. This is the same shape as the GoTrue
work in Phase 04 and the ADR-018 hardening, and slice 1 is where it is resolved:
`DB_INSTALL_ROLES=false`, roles created by the platform beforehand, schema owned
by the platform, migrations run under a constrained role.

### RLS on `storage.objects` is load-bearing, and `service_role` bypasses it

`services/migrate/source.py:248` records the measurement from Phase 08:
Supabase enables RLS on `storage.objects` and `storage.buckets` by design,
owned by `supabase_storage_admin`, because storage policies *are* RLS policies.
So this phase cannot harden by turning RLS off, and cannot leave authorization
to the worker.

ADR-041's general rule applies directly and should be quoted in the slice that
implements this: on a surface where the caller influences which role is used,
the role named in a request selects a credential and nothing else. It is not a
permission boundary. `service_role` bypasses RLS by design, so any path that
lets a customer cause `storage-api` to act as `service_role` for a bucket they
do not own defeats every policy at once.

### The seams for this phase already exist

- `services/gateway/app.py:154` — `UNIMPLEMENTED_PREFIXES = ("/realtime/v1",
  "/storage/v1")`. The route is reserved and answers 404 today, deliberately, so
  that a client calling it gets a comprehensible answer. Slice 4 removes the
  second entry.
- `services/migrate/rules.py:206` — `_storage()` already emits a
  `storage.objects` blocker naming Phase 10 by number, read from the
  compatibility matrix, plus a `storage.empty_buckets` warning. Slice 6 turns
  the blocker into a migration.
- `specs/compatibility-matrix.yaml:133` — `buckets`, `upload`, `download`,
  `delete` and `signed_urls` are all `{status: deferred, phase: 10}`. Five
  entries to move, with `verified_by` pointing at real tests.
- `services/control_plane/bootstrap/` runs 001–011 in order; the tenant's
  `storage` schema is 012.
- `jobs.py` `STEPS` and `_validate` are where provisioning gains a storage
  step, next to `_apply_realtime_plan`.

### A naming collision that will mislead a reader if it is not decided now

`services/control_plane/storage.py` and `tests/test_storage.py` already exist
and are about **database** storage accounting — `pg_database_size`, the quota
state machine, and the ADR-040/041 write restriction. They have nothing to do
with object storage.

New modules are therefore `object_storage.py` and `tests/test_object_storage.py`,
and no function in this phase is called `measure`, `restrict` or `release`
without a qualifier. This is trivial to decide now and expensive to fix once
six slices have imported the wrong thing.

### Egress leaves through the gateway regardless of where the bytes are

Supabase serves a signed URL from `storage-api` rather than redirecting to the
object store, so object bytes reach an end user through the platform. If that
holds — **slice 0 confirms it rather than assuming it** — then two things
follow. Egress accounting belongs at the gateway, where the bytes actually pass.
And the object store's own transfer allowance is close to irrelevant next to the
node's bandwidth, which simplifies ADR-055's hosting decision considerably.

## Implementation steps

Eight slices. Each is a branch, a pull request, and a `Security-Review:`
trailer that CI will not let it merge without.

### Slice 0 — Measure the substrate before any slice commits to it

No platform code. A spike plus `specs/storage-server-model.md`, in the shape of
`specs/realtime-server-model.md`, and the ADRs this plan names.

1. Run `supabase/storage-api:v1.70.6` under rootless Podman against SeaweedFS
   and a real tenant database. **Confirm it boots on the node CPU profile** —
   QEMU vCPU, SSE4.2 and `popcnt`, no AVX/AVX2/BMI2/FMA. ADR-033 exists because
   a newer Realtime image died with SIGILL on exactly this, and the cost of
   finding out in slice 3 is a phase built on an image that cannot run.
2. Exercise the S3 surface `storage-api` actually requires against SeaweedFS:
   AWS SigV4, multipart upload create/complete/abort, copy-object, presigned
   URLs, and ETag/conditional request handling. Nothing found in the survey
   tests SeaweedFS against `storage-api` specifically, so this is the bake-off.
   A gap here is a provider decision, not a workaround.
3. **Settle shared versus per-project.** Stand up `MULTI_TENANT=true` against
   two tenants on one cluster and confirm each sees only its own objects; then
   stand up two single-tenant instances. Record memory per instance under cgroup
   accounting, as ADR-034 did, so `docs/CAPACITY.md` gains a real number rather
   than an estimate.
4. Measure what `DB_INSTALL_ROLES=false` requires to already exist, by running
   upstream's migrations against a tenant provisioned by this platform and
   reading what fails.
5. Confirm whether a signed URL is proxied by `storage-api` or redirects to the
   object store. Slice 4's egress accounting depends on the answer.
6. Write **ADR-058** — the container runtime and the topology — plus whatever
   else slice 0 discovers. ADR-055 to ADR-057 are already recorded, because
   they answer the owner's open questions; this one is deliberately not, because
   its topology half is a measurement rather than a preference.

Exit: `specs/storage-server-model.md` states the topology, the pin, the CPU
finding, the memory figure, and the S3 feature matrix, each with a measurement
behind it.

### Slice 1 — The tenant's `storage` schema, under platform ownership

Bootstrap `012_storage_schema.sql`. Create `storage` owned by the platform,
create nothing named `anon`/`authenticated`/`service_role`, run upstream's
migrations with `DB_INSTALL_ROLES=false`, and harden the result the way
bootstrap 011 hardens every schema. RLS stays on, because it is the
authorization mechanism rather than an obstacle to it.

Security review focus: whether a tenant's roles can read `storage.objects` rows
they do not own, and whether the project admin role can grant itself past a
bucket policy. ADR-040's admission — a table owner holds `GRANT OPTION`
implicitly — applies here too, and the answer needs to be written down whichever
way it falls.

### Slice 2 — Two entitlements, and an accounting loop that reaches the node

`object_storage_bytes` and `egress_bytes_per_month` into
`entitlements.Entitlements`, `DEFAULTS`, `specs/plans-and-limits.yaml` and a
control-plane migration. New module `object_storage.py`, following the
measure/classify/enforce shape `storage.py` already has, with a maintenance-pass
hook next to the database-storage one.

Phase 09's opening measurement is the thing to avoid repeating: an entitlement
that is applied once at provisioning and never re-applied is an entitlement a
plan change never reaches. Both of these are re-evaluated per pass or per
request, and the plan-change path is asserted in tests, not assumed.

### Slice 3 — The storage worker

Per-project or shared, as slice 0 determined. Pinned image, rootless Podman,
systemd, and ADR-035's containment applied without exception: a data address
rather than loopback, `--cap-drop` to the minimum the image genuinely needs,
and a reachable set of exactly PostgreSQL and the SeaweedFS endpoint. Port
allocation, config rendering with `render_env`'s refusal of unusable values,
start/stop, and the `jobs.py` provisioning step beside `_apply_realtime_plan`.

**`jobs.cleanup` must delete the project's objects.** Today it drops the
database and the roles. Objects live outside both, so without this a deleted
project's customer files persist indefinitely in the platform bucket — a data
retention problem first and a cost problem second. This is the slice where that
is easy and every later slice where it is not.

### Slice 4 — The gateway serves `/storage/v1`

`/storage/v1` leaves `UNIMPLEMENTED_PREFIXES`. Project resolution, key
validation, the hostname/key match, plan rate and concurrency rules, and
wake-on-request all already exist and apply unchanged. What is new is egress
accounting on a path that ADR-026 measured for throughput — so the accounting
must be cheap, and the cost of it measured against that number rather than
asserted to be small.

Anonymous access to public buckets is the free-tier egress vector and is
enforced here.

### Slice 5 — Compatibility, driven by the official client

`tests/compat` gains a storage suite driving `@supabase/supabase-js`:
`createBucket`, `upload`, `download`, `remove`, `list`, `createSignedUrl`. Then
the two properties that are the acceptance criteria rather than the feature
list — an RLS policy on `storage.objects` actually gating access, and a project
proved unable to reach another project's objects.

Five `specs/compatibility-matrix.yaml` entries move from `deferred` to
`supported`, each with `verified_by`. Per `AGENTS.md`, no compatibility claim is
made ahead of the test that holds it.

### Slice 6 — The migration path

`services/migrate` gains storage: buckets, object metadata, and object bytes
from Supabase Storage. `rules.py`'s `storage.objects` blocker becomes a
supported migration; `storage.empty_buckets` becomes recreation.
`docs/MIGRATION-FROM-SUPABASE.md` loses "Blocked at launch: Storage (Phase 10)".

ADR-042 constrains this: the customer runs the CLI and the platform never holds
their Supabase credentials. Object bytes move through the customer's machine,
which is slower than a server-side copy and is the arrangement already decided.

### Slice 7 — Close the phase

Acceptance criteria ticked against evidence, `docs/STORAGE.md` rewritten from
"no provider is selected yet" to what was built, `docs/CAPACITY.md` updated with
slice 0's memory figure, the plan moved to `plans/completed/`, and any box the
record does not tick cleanly named rather than ticked.

## Verification

- [ ] Unit/integration tests for schema, entitlements, worker, gateway route
- [ ] Compatibility tests using the official `supabase-js` client (slice 5)
- [ ] Tenant-isolation check: one project cannot reach another's objects
- [ ] RLS policy on `storage.objects` demonstrably gates access
- [ ] `scripts/export-openapi.py --check` clean after any route change
- [ ] `ruff check .` clean
- [ ] Migrations idempotent on re-run
- [ ] `specs/compatibility-matrix.yaml` updated with `verified_by` per feature
- [ ] Security review recorded as a trailer on every slice

A note on what a green suite is worth here. `AGENTS.md` documents that the
suite skips rather than fails without its prerequisites, and Phase 06 added a
banner for exactly this. Storage adds another: without Podman, the pinned image
and a reachable SeaweedFS endpoint, the storage tests will skip, and what skips
is every isolation property in this phase. Slice 3 adds
`MALUDB_REQUIRE_STORAGE_SERVER=1` for CI, matching
`MALUDB_REQUIRE_REALTIME_SERVER`, so an absent one is a failed run rather than a
quiet pass.

## Risks

- **The image does not run on node hardware.** ADR-033's precedent is exact and
  recent. Mitigated by making it the first thing slice 0 measures; if it fails,
  the phase stops and becomes a decision rather than a workaround.
- **SeaweedFS's S3 gateway is missing something `storage-api` needs.** Multipart
  and SigV4 presigning are the likely candidates. Mitigated by the slice 0
  bake-off. A gap changes the provider, and because the boundary is S3 that
  change is configuration — which is the argument for the boundary being there.
- **One bucket holds every tenant's bytes.** Isolation is a property of metadata
  and credential scoping, not of the store. Mitigated by making it the named
  focus of slices 1 and 5 and by testing the denial directly. This is the risk
  most worth a reviewer's attention in the whole phase.
- **Density.** If slice 0 says per-project, Storage becomes the second expensive
  per-project container after Realtime, and `docs/CAPACITY.md`'s model needs
  both terms. Unlike Realtime this is bounded by memory rather than by
  PostgreSQL's replication slot budget, so it degrades gracefully rather than
  hitting a hard ceiling at four projects per node.
- **Egress accounting on the hot path.** ADR-026 published a throughput number
  for the gateway and it is the platform's claim about itself. Mitigated by
  measuring the regression rather than reasoning about it.
- **`service_role` defeats storage policies.** ADR-041's finding, in a new
  place. Mitigated by treating any customer-influenced role selection as a
  credential choice rather than a boundary, and by saying so in the code where
  the choice is made.
- **Orphaned objects after project deletion.** A retention problem that grows
  silently and is not visible in any test that does not look for it. Mitigated
  by putting it in slice 3 rather than in a cleanup pass later.
- **SeaweedFS's paid tier covers durability, not features.** Everything Phase 10
  needs is Apache 2.0. Automatic erasure-coding repair, EC vacuum, self-healing
  and point-in-time recovery sit behind a per-TB Enterprise licence, free under
  25 TB for dev and test. That is a **Phase 11** input, where backups, restore
  and PITR already live, and it is recorded here so it arrives as a decision
  rather than a discovery.

## Decision log

- 2026-08-22 — Phase 09 confirmed closed and merged at `9740a98` (PR #79)
  before Phase 10 planning began.
- 2026-08-22 — MinIO rejected. Community edition archived 25 April 2026, no
  binaries, no security patches. Recommended and then withdrawn during planning;
  recorded because the withdrawal is the useful part.
- 2026-08-22 — SeaweedFS selected. Apache 2.0 rather than AGPL, actively
  developed, single-binary S3 gateway. Garage rejected on the AGPL grounds for
  a commercial platform; Ceph RGW rejected as disproportionate absent an
  existing Ceph cluster; RustFS noted as viable but least proven.
- 2026-08-22 — Object store runs on existing Proxmox hardware initially, not a
  separate box, with the exit to dedicated hardware stated in ADR-055 and made
  cheap by ADR-035's existing prohibition on container-to-loopback.
- 2026-08-22 — `STORAGE_BACKEND=file` rejected as the starting point. It is a
  different code path rather than a different address, so it would make the
  eventual move a backend swap during a live data migration and would leave the
  provider-abstraction criterion untested.
- 2026-08-22 — Storage available on every tier including free, bounded by hard
  ceilings under ADR-050. Storage over the gateway is API access and therefore
  inside ADR-005.
- 2026-08-22 — Scope held to the task file's six features. Resumable uploads,
  image transformation and the S3 protocol endpoint deferred with matrix
  entries.
- 2026-08-22 — No MaluDB-side provider abstraction. The task file's
  "object-store provider abstraction" is satisfied by `STORAGE_BACKEND` and
  `STORAGE_S3_ENDPOINT`, and a second abstraction above upstream's would add
  maintenance and nothing else.
- 2026-08-22 — New code is `object_storage.py`, because `storage.py` is
  database storage accounting and the collision would mislead every later
  reader.
- 2026-08-22 — Shared versus per-project topology deliberately **not** decided
  in this plan. ADR-034's reason for per-project does not apply to
  `storage-api`, and the alternative differs by an order of magnitude in
  density. It is slice 0's measurement.

## Progress log

- 2026-08-22 — Phase 09 verified closed. Canonical docs read, upstream and
  repository measured, three `## Storage` open questions answered by the
  repository owner, plan written. No code yet; slice 0 is next and needs none
  of the code that follows it.
