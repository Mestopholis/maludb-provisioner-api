# Execution Plan: Phase 10 — Storage, and keeping the bytes somewhere the database is not

Status: **COMPLETE** — slices 0 to 7, 2026-08-22 to 2026-08-25, PRs #80 to #88.
A project on any tier creates buckets, uploads, downloads, lists, signs and
removes objects through the official client; an RLS policy on `storage.objects`
decides what a caller may read; one project cannot reach another's objects; the
bytes are in SeaweedFS and demonstrably not in the tenant database; and a
Supabase project that uses Storage is no longer turned away at the door.

Three things this phase produced that are not features:

**Every one of its ten commits carries a `Security-Review:` trailer**, and six
of them found something — fourteen findings in all, twelve fixed in the change
that found them and two recorded and carried deliberately. Phase 09 was the
first phase that could tick this box; this is the second, and the control is
still CI's rather than prose's.

**Three defects were found by driving the real thing, and none of them by a
green suite.** Slice 4 passed a 33-test Python suite with two gateway bugs in it
that only `@supabase/supabase-js` could surface (ADR-062). Slice 5's own
negative cases passed against a dead port until they were pointed at one. Slice
7 found that `cp-manage maintenance run` never passed `config` to `run_all`, so
slice 3's store-side measurement — written specifically because the metadata
figure is customer-forgeable — had never once run outside a test. Each was
invisible to the layer above it.

**The one acceptance-criteria gap is named rather than ticked past.** Storage
policies are enforced and a customer still cannot author one (ADR-061).

Plan written 2026-08-22. **Slice 0 complete**
(2026-08-22): the substrate is measured, the topology is settled as ADR-058, and
`specs/storage-server-model.md` records it. **Slice 1 complete** (2026-08-24):
the tenant `storage` schema exists under platform ownership, upstream's 63
migrations run under a constrained owner, and the one thing that stops them is
recorded rather than worked around. **Slice 2 complete** (2026-08-24): both
ADR-056 ceilings exist, are measured or counted, are visible to a customer, and
reach a project that changes plan. **Slice 3 complete** (2026-08-24): one shared
worker per node serves tenants, its containment is measured from inside the
container, deleted projects lose their objects, and the held-bytes figure is
taken from a source the customer cannot write. **Slice 4 complete**
(2026-08-25): the gateway serves `/storage/v1`, public buckets are reachable
without a key and counted anyway, and both ADR-056 ceilings refuse rather than
merely record. **Slice 5 complete** (2026-08-25): the official client creates
buckets, uploads, downloads, lists, signs and removes over the gateway; an RLS
policy on `storage.objects` decides what it may read; one project cannot reach
another's objects; and the two gateway defects only the real client could have
surfaced are fixed as ADR-062. **Slice 6 complete** (2026-08-25): buckets and
object bytes migrate from Supabase with the customer's own two keys (ADR-063),
the storage blocker is gone, and what does not travel — policies, ownership,
oversize objects — is named rather than dropped. **Slice 7 complete**
(2026-08-25): the criteria are ticked against tests rather than against
recollection, `docs/STORAGE.md` says what was built, slice 4's one carried
item has the caller it was written for, and the phase is closed.

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

**Settled by slice 0 as ADR-058: one shared instance per node.** Measured
105.8 MB for a dedicated instance against ~0.7 MB marginal per tenant shared,
and two independent boundaries rather than one — the host selects the tenant,
and that tenant's own JWT secret must verify. What follows is kept as written,
because it is the reasoning the measurement then confirmed.

As originally posed: A shared instance holds every
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

### Slice 0 — Measure the substrate before any slice commits to it — **COMPLETE**

Findings are in `specs/storage-server-model.md`; the topology decision is
ADR-058. In brief, and in the order they mattered:

- **The image runs on the node CPU profile.** `QEMU Virtual CPU version 2.5+`,
  no AVX/AVX2/BMI2/FMA. No SIGILL — `storage-api` is TypeScript on Node with no
  precompiled NIF in the request path, which is why it survives where the newer
  Realtime image did not. ADR-033's hazard does not repeat. It is **not**
  cleared for imgproxy, so image transformation stays deferred on the original
  grounds.
- **SeaweedFS passes 17/17**, including every operation ADR-055 named as the
  risk: multipart create/upload/complete/abort, SigV4 presigned GET and PUT,
  presign expiry actually enforced, copy-object, conditional GET. No gap that
  would change the provider.
- **Shared beats per-project by two orders of magnitude.** 105.8 MB for a
  dedicated instance against **~0.7 MB marginal per tenant** shared — six
  tenants added 4.1 MB. ADR-058 takes the shared topology, and ADR-034's reason
  for the opposite is confirmed as specific to replication slot names rather
  than general.
- **`DB_INSTALL_ROLES=false` needs exactly one grant.** Upstream's migrations
  run fine and grant table privileges, but leave the schema itself owner-only,
  so every request 403s with `permission denied for schema storage`.
  `GRANT USAGE ON SCHEMA storage TO anon, authenticated, service_role` was the
  entire remedy — smaller than expected, and it is slice 1's recipe.
- **Egress does pass the platform.** A signed URL is a relative path on
  `storage-api`, served with no `Authorization` header and **no redirect** to
  the object store. ADR-056's accounting point is correct.
- **Acceptance criterion 1 is already measured true**: 0 `bytea` columns in
  schema `storage`, 592 kB of metadata, bytes in the object store.
- **ADR-035 holds for Storage.** The container reaches the object store on the
  data address and cannot reach node loopback.

Two findings that change later slices rather than this one:

1. **`X-Forwarded-Host` is the tenant selector**, so the gateway must set it
   authoritatively and strip any client-supplied value. There *is* a second
   boundary — the tenant's own JWT secret is verified, and a token signed for
   one tenant is refused against another with `403 signature verification
   failed` — but slice 4 should be reviewed as though there were not.
2. **The v1.70.6 schema is wider than Storage**: `buckets_vectors`,
   `vector_indexes`, `iceberg_namespaces`, `iceberg_tables`. Slice 1 hardens
   what exists rather than what was expected.

The original step list, kept for the record:

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

### Slice 1 — The tenant's `storage` schema, under platform ownership — **COMPLETE**

Delivered 2026-08-24. Bootstrap `012_storage_schema.sql`, a new per-tenant role
`mldb_<ref>_storage` with its own provisioning step, hardening that re-applies
itself, five new `verify()` outcomes, and `tests/test_object_storage.py`.

What the slice turned on, in the order it mattered:

- **Upstream's migrations do run under a constrained owner — with one change.**
  62 of 63 pass as a `NOSUPERUSER NOBYPASSRLS NOCREATEROLE` role owning nothing
  but the schema. `0011-add-trigger-to-auto-update-updated_at-column.sql` opens
  with an **unqualified** `CREATE OR REPLACE FUNCTION
  update_updated_at_column()` and fails with `permission denied for schema
  public`. Upstream lands it in `storage` only because its own migration 0002
  sets `search_path` on `supabase_storage_admin`, inside the branch this
  platform must turn off.
- **The tempting fix is the dangerous one.** `GRANT CREATE ON SCHEMA public`
  makes the migration pass while dropping a platform function into the one
  schema PostgREST exposes — Phase 00 finding 4 in a new place. The remedy is
  bootstrap 007's: `ALTER ROLE ... IN DATABASE ... SET search_path = storage`.
  With it all 63 pass and `public` gains nothing, asserted as a before/after
  diff because `maludb_core` already puts 373 functions there.
- **The storage role must be a member of the three shared names.**
  `storage-api` does not query as the owner: `scope.js` issues
  `set_config('role', <role from the JWT>, true)` per request. That membership
  is what makes RLS apply at all, and it is ADR-016's permitted direction.
- **RLS is left unforced, deliberately.** Forcing it would deny the owner, and
  with no policies that denies `storage-api`'s own migrations, multipart
  reaping and deletion — the service would not run. Measured: owner sees the
  row, `authenticated` sees none, `service_role` sees it (ADR-041 again).
- **The schema is narrower than slice 0 recorded.** Eight tables, not ten:
  migration 0038 returns early when `storage.multitenant` is true, so the two
  `iceberg_*` tables are a dedicated-mode artefact. ADR-058 takes the shared
  topology, so they never appear.
- **The hardening is a function plus an event trigger, not statements.** The
  objects it hardens do not exist when bootstrap runs — they appear when the
  worker first serves the tenant and again on every upgrade. A one-shot revoke
  would harden an empty schema and be recorded as applied forever, which is
  bootstrap 003's mistake and the reason 005 exists.

Security review found two things and both are recorded rather than fixed
quietly:

1. **An overclaim in the first draft of this slice's own comments.** They said
   the storage credential "cannot read the tenant's application tables". It
   can: the role switches into `anon`/`authenticated`/`service_role`, and
   bootstrap 004 grants those `ALL ON ALL TABLES IN SCHEMA public`. Its reach
   is PostgREST's authenticator's reach. Corrected in the file, the docstring
   and the test, because a wrong comment about a privilege boundary is worse
   than no comment.
2. **A pre-existing role leak.** `jobs._drop_roles` dropped only three of the
   tenant's roles; `executor` and `client` had been left on the cluster by
   every cleanup since Phase 08 slice 2 and Phase 09 slice 2. Fixed with the
   storage role rather than left for a fourth to join them. Worth noting how
   it survived: `test_cleanup_reclaims_an_empty_database_when_explicitly_allowed`
   asserted the dropped set **equalled** the three-role tuple, so the test
   agreed with the bug. `tests/conftest.py::project_factory` had meanwhile
   grown its own drop list for the executor and client, which is where the
   disagreement was visible to anyone reading the two side by side.

The security question the plan asked, answered: **a tenant's own roles cannot
read `storage.objects` rows they do not own** — RLS applies once the role is
switched, and `service_role` is the documented exception it is on Supabase.
**The project admin role cannot grant itself past a bucket policy**, because it
holds no privilege on the schema at all and `CREATE POLICY` needs ownership.

Which produced the one gap this slice did not close, carried to slice 4:
**no customer-reachable role can author a storage policy.** Enforcement works;
authoring does not exist. See below.

### Slice 2 — Two entitlements, and an accounting loop that reaches the node — **COMPLETE**

Delivered 2026-08-24. `object_storage_bytes` and `egress_bytes_per_month` in
`entitlements.Entitlements`, `DEFAULTS` and `specs/plans-and-limits.yaml`;
control-plane migration 0024; new module `object_storage.py`; a
`measure_object_storage` maintenance pass beside the database-storage one; both
ceilings on `GET /v1/projects/{ref}/usage`; and
`tests/test_object_storage_accounting.py`.

Phase 09's opening measurement was the thing to avoid repeating, and it is
asserted **twice** rather than once — the two resources read the plan through
different functions, so inferring the second from the first would have been the
same mistake in a smaller form. A project that upgrades stops being `exceeded`
on the next pass for held bytes, and immediately for egress, with nothing else
done to it.

What the slice decided, beyond the numbers:

- **The two resources are counted differently, on purpose.** Held bytes are
  *measured* by a pass — polling is right for a quantity that is a property of
  the world rather than of a request, it is self-correcting, and a missed pass
  costs accuracy rather than truth. Served bytes are *counted as they pass*,
  because there is nowhere to read them back from afterwards.
- **`record_egress` takes a total, not one response.** The caller is the
  gateway, on the path ADR-026 published a throughput number for, so a write per
  request is not available: slice 4 accumulates in process and flushes, the way
  ADR-030's limiters already work. A test asserts a flush of N equals N flushes
  of one, so the buffering slice 4 adds cannot change the number.
- **`exceeded`, not `restricted`.** Nothing is revoked in the tenant, because
  object bytes arrive through the Storage API and that is where the refusal
  happens. A reader who saw `restricted` on both resources would reasonably
  expect a revoke behind both. Asserted directly: a project pushed over its
  object ceiling has byte-identical table grants afterwards.
- **The egress period is a UTC calendar month, not the billing period.** A free
  project has no subscription and ADR-056 puts this ceiling on free, so aligning
  to a billing period would mean inventing one; and the ceiling is not a charge
  (ADR-050), so there is nothing for it to line up with. A row per period rather
  than a counter that resets, so "what did this project serve last month" has an
  answer and no reset job exists to fail to run.
- **Both are visible on `/usage` now**, before slice 4 refuses at them. ADR-050's
  product point: a ceiling hit with no visible way forward is a churn event
  rather than a saved dollar.

Two things measured rather than assumed:

- **Upstream's `storage.get_size_by_bucket()` is not used**, and the reason is a
  bug in it: it casts each row's size to `int` — four bytes — so a single object
  over 2 GiB overflows the cast and takes the whole aggregate down with it. This
  module reads the same column and casts to `bigint`. A test stores two 3 GiB
  objects.
- **A tenant with no `storage.objects` measures zero rather than raising.** That
  is every project between slice 1 and slice 3, and raising would have failed
  the maintenance pass for the entire fleet in the meantime.

Security review found one thing, and it is the reason this slice's measured
figure is described the way it is:

**A customer who can reach `service_role` can under-report their held bytes,
and re-measuring does not fix it.** `service_role` holds `ALL` on
`storage.objects` and carries `BYPASSRLS`, and `api/tenant_access.py` already
records that a request on an impersonating connection can `SET ROLE
service_role` in one line of its own SQL — a surface ADR-039 puts on every
tier. One `UPDATE` zeroes every recorded size. `anon` and `authenticated`
cannot: same grants, no `BYPASSRLS`, RLS with no policy stops them. Both halves
measured and pinned in the suite.

This is ADR-040's admission in a new place and **worse in one specific way**:
ADR-040's hole is a loop the customer has to keep running, because the next
pass re-revokes. This one re-reads the same forged column forever. What closes
it is a figure taken from the object store, which is not customer-writable and
which nothing can ask for until slice 3 has an endpoint — so it is **carried to
slice 3** rather than fixed here, and the interim figure is documented as the
tenant's claim about itself. Egress is unaffected: it is counted at the gateway
from bytes actually served and never read back from the tenant.

Named and not closed: the measured figure is the tenant's metadata, not a query
against the object store, and the two can drift — an upload that wrote bytes and
failed to commit its row leaves an object nobody is billed for and nobody can
reach. Reconciliation is Phase 11's, with backups and restore. The error is in
the tolerable direction.

### Slice 3 — The storage worker — **COMPLETE**

Delivered 2026-08-24. `scripts/storage-test-cluster.sh` (SeaweedFS on a data
address, the platform bucket, the `pg_hba` line a container needs),
`services/control_plane/storage_workers.py`, `deploy/maludb-storage.service`,
control-plane migration 0025, an object-store client and the two calls the
platform makes with it, a registration step in provisioning, object deletion in
`jobs.cleanup`, and two test modules — `tests/test_object_store.py` (14) and
`tests/test_storage_workers.py` (21).

**The pin moved, and so did its evidence.** Slice 0 measured SeaweedFS 4.44 and
noted it had shipped the same day; this pins **4.41**, three releases back, and
re-ran the bake-off rather than inheriting evidence for a build the platform
does not run. 14/14, including all four operations ADR-055 named as the risk.

**ADR-058's outstanding measurement, taken.** 8 tenants under concurrent load —
400 operations in 5.8 s — moved the instance from 146.8 MB to 161.3 MB: ~1.8 MB
per actively busy tenant on top of ~0.46 MB per registered idle one. Against
105.8 MB for a single dedicated instance the shared topology holds comfortably,
and the number to plan against turns out to be **total concurrency on a node
rather than tenant count**. Recorded in ADR-058, `docs/CAPACITY.md` and the
spec.

Measured end to end, on a real instance: two tenants each create a bucket named
`shared-name` holding `secret.txt` and each read back their own bytes; the
object-store keys confirm ADR-057's `<ref>/<bucket>/<path>/<version>` layout; a
token signed for one tenant reaches nothing of another's; and from inside the
container, the object store is reachable on its data address and
`ECONNREFUSED` on the node's loopback (ADR-035, for a second component).

Three assumptions the spike corrected, none of which reasoning would have
caught:

1. **`POST /tenants/{id}` is insert-only** — it answers 500 on the second
   provisioning run of the same project. `PUT` is upstream's upsert. The module
   said PUT in its docstring and did POST; a test now pins it, because a
   provisioning retry is the ordinary case rather than the exotic one.
2. **`fileSizeLimit: 0` means zero bytes, not unlimited.** Every upload answered
   413. There is no sentinel, so the platform sends a real backstop and the
   plan's ceiling is what a customer meets.
3. **Slice 0's "403 signature verification failed" was reading the body.** The
   HTTP status is 400. With slice 0's own note that the object path masks a
   denial as 404, the honest conclusion is that Storage authentication failures
   cannot be counted from HTTP status codes at all — a monitoring fact, pinned
   in a test.

And two of my own, found by the suite rather than by review: the forwarded-host
pattern was written `[a-z0-9]{4,16}` when project refs are a fixed eight
characters, so the worker would have resolved tenant names no project could
have; and `scripts/storage-test-cluster.sh` had a `pkill` pattern that missed
because of quoting, which let a **stale** server make a failed start report
success — precisely the hazard that script's own header warns about.

**Slice 2 hands this slice a security fix, not only a feature.** The held-bytes
quota is currently measured from `storage.objects.metadata->>'size'`, which a
customer reaching `service_role` can rewrite — and unlike ADR-040's equivalent
hole, re-measuring does not correct it. This is the first slice with an object
store endpoint to ask, so it is the first slice that can measure from a source
the customer cannot write. Take that measurement, and make it the authority
where the two disagree.

Closed. Held bytes are now measured from the object store where one is
configured, and the metadata sum is the fallback for a node with none. The same
forgery slice 2 measured — `service_role` zeroing
`storage.objects.metadata->>'size'` — now changes the metadata figure and leaves
the measured figure and the `exceeded` state intact, asserted directly. An
unreachable store falls back rather than reporting zero, because zero is a claim
and it is the claim that hands a project unlimited storage.

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

**Steps, in the order they will land.**

1. `/storage/v1` leaves `UNIMPLEMENTED_PREFIXES` and gets its own branch in
   `handle`. It is **not** a `Surface`: the four existing ones name a
   per-project port, worker state and activity column, and ADR-058's worker has
   none of the three. Forcing it into that shape would mean columns that exist
   to be ignored — the reason Realtime was kept out of `SURFACES` in Phase 06.
   The upstream is the node's own `config.storage_port`, and the tenant is
   named by `X-Forwarded-Host: <ref>.<gateway_domain>`, which the worker
   matches against `storage_workers.forwarded_host_regexp`. That header is
   already in `UNTRUSTED_INBOUND`, so the client's copy is dropped rather than
   appended to — which is the whole tenancy control on this surface.
2. Registration on demand. Migration 0025 left this explicitly: a project whose
   `storage_registered_at` is NULL "is simply one whose next Storage request
   registers it". `jobs._register_storage_tenant` is the same call, so it moves
   into `storage_workers.ensure_registered` and both callers use it rather than
   the gateway growing a second copy that can drift.

   What this does **not** do is re-register on an upstream `TenantNotFound`,
   which the first draft of this step proposed. Detecting that means reading the
   upstream's response body, and `app.py` opens by saying the gateway does not
   interpret one — a control that depends on parsing somebody else's error
   strings is a control that breaks on an upstream release note. The case it
   would have covered is a worker whose *metadata database* was rebuilt, since
   an ordinary container restart keeps its tenants; `storage_workers.registered_projects`
   exists for that repair and wiring it into the maintenance pass is carried to
   slice 7 rather than done here under the wrong heading.
3. Public buckets. `GET /storage/v1/object/public/<bucket>/<path>` carries no
   key, exactly as `PUBLIC_AUTH_PATHS` carries none: a browser following a link
   sends an `apikey` header for nobody. An exact prefix rather than a wide one,
   for the same reason that list is exact — and read-only, because the
   anonymous surface must not be a write surface.
4. Egress accounting. In-process accumulation flushed on an interval, which is
   what `object_storage.record_egress` was given a batch-total signature for in
   slice 2. A write per response is not available on a path ADR-026 published a
   latency number for.
5. Enforcement, which is what makes slice 2's inert states real. Egress over
   the ceiling answers **429 with `Retry-After` to the month boundary**; a
   project over `object_storage_bytes` answers **413** on upload, which is the
   status upstream already returns for its own file-size limit, so the official
   client's existing error path handles it. Recorded as an ADR: it is
   customer-visible and goes in the compatibility matrix. Reads are not refused
   for a full project — a customer who cannot download their own files to free
   space has no way forward, which is ADR-050's product point.
6. The measurement ADR-056 requires, not an assertion that it is small.
   `scripts/bench-gateway.py` against the storage surface with accounting on
   and off.
7. The gateway's 8 MiB body cap is sized for a PostgREST insert and would have
   capped every upload on the platform. The storage surface gets its own
   ceiling, configuration rather than a constant, defaulting to upstream's
   50 MB — found by implementing rather than by planning, and recorded in the
   compatibility matrix because a client meets it.

**The policy-authoring decision this slice owes.**
`specs/compatibility-matrix.yaml` says slice 4 decides whether the tenant admin
gains membership in the storage role or the platform mediates policy
management. It stays **deferred**, and the entry gains the reasoning rather
than only the deferral: the membership is owner-level bypass of every storage
policy plus write access to the metadata the object store is kept consistent
with, and enforcement already works — authoring is what is missing, and a
platform-mediated surface that validates what it creates is a slice of its own
rather than a line in this one.

**Slice 1 hands this slice a decision.** No customer-reachable role can create
a policy on `storage.objects`: `CREATE POLICY` requires ownership of the table,
the owner is `mldb_<ref>_storage`, and nothing a customer reaches is a member
of it. Supabase's dashboard can do it because its `postgres` role *is* a member
of `supabase_storage_admin`. The MaluDB analogue would grant
`mldb_<ref>_admin` that membership, which is owner-level bypass of every
storage policy plus write access to the metadata the object store is kept
consistent with; the alternative is a platform-mediated policy surface that
validates what it creates. Enforcement already works — that is not what is
missing. Recorded as `storage_policy_authoring` in
`specs/compatibility-matrix.yaml` so it cannot be discovered during slice 5's
compatibility run.

### Slice 5 — Compatibility, driven by the official client — **COMPLETE**

`tests/compat` gains a storage suite driving `@supabase/supabase-js`:
`createBucket`, `upload`, `download`, `remove`, `list`, `createSignedUrl`. Then
the two properties that are the acceptance criteria rather than the feature
list — an RLS policy on `storage.objects` actually gating access, and a project
proved unable to reach another project's objects.

Five `specs/compatibility-matrix.yaml` entries move from `deferred` to
`supported`, each with `verified_by`. Per `AGENTS.md`, no compatibility claim is
made ahead of the test that holds it.

**What it turned out to be, having been done.** Seven entries rather than five —
`list` and `public_urls` were being exercised and were not named — plus
`signed_upload_urls` added as a deferral so that the one Storage call this
platform refuses is a decision on the record rather than a 401 a customer
discovers. And two gateway defects that no Python test could have found, because
a hand-written client sends what its author assumed: the publishable key was
dropped on a surface with no anonymous fallback, and signed URLs required the
API key they exist not to need. Both are ADR-062. See the progress log.

### Slice 6 — The migration path — **COMPLETE**

`services/migrate` gains storage: buckets, object metadata, and object bytes
from Supabase Storage. `rules.py`'s `storage.objects` blocker becomes a
supported migration; `storage.empty_buckets` becomes recreation.
`docs/MIGRATION-FROM-SUPABASE.md` loses "Blocked at launch: Storage (Phase 10)".

ADR-042 constrains this: the customer runs the CLI and the platform never holds
their Supabase credentials. Object bytes move through the customer's machine,
which is slower than a server-side copy and is the arrangement already decided.

### Slice 7 — Close the phase — **COMPLETE**

Acceptance criteria ticked against evidence, `docs/STORAGE.md` rewritten from
"no provider is selected yet" to what was built, `docs/CAPACITY.md` updated with
slice 0's memory figure, the plan moved to `plans/completed/`, and any box the
record does not tick cleanly named rather than ticked.

**What it turned out to be, having been done.** Three of those five were
already true and the check was the work: `docs/CAPACITY.md` gained slice 0's
table in slice 0 and slice 3's load measurement in slice 3, and every one of
the phase's ten commits already carried a `Security-Review:` trailer, so those
boxes are ticked on evidence rather than filled in now.

The other two were not, and neither was a documentation change.

**Slice 4's carried item, done under the right heading.**
`storage_workers.registered_projects` shipped in slice 3 with no caller, and
slice 4 explicitly declined to wire it into the request path — re-registering
on an upstream `TenantNotFound` means reading somebody else's error strings,
and `app.py` opens by saying the gateway does not interpret a response body.
`maintenance.reconcile_storage_tenants` is the caller it was written for: it
asks the worker's admin API a question with a status code for an answer, and
re-registers only what has actually been forgotten. The case is narrow and the
failure is silent, which is what makes it worth a pass — a container restart
keeps its tenants, but a worker whose *multitenant database* was rebuilt has
forgotten every one of them, and those projects then answer
`400 TenantNotFound` to every Storage request with nothing anywhere saying why.

Three things it does deliberately, each of which the obvious implementation
gets wrong. It reads the node's storage root secret rather than ensuring one:
`AUTH_ENCRYPTION_KEY` is derived from that root and decrypts every registered
tenant's connection settings, so a *repair* that minted one would leave the
node's own worker unable to read what it had already written — `node_secret` is
now the read half of `ensure_node_secret` for that reason. It refuses to act
when more than one node has storage-registered projects, because ADR-058 puts
the worker's admin port on loopback and only an operator knows which host this
is; guessing would re-register another node's tenants into this node's worker.
And `tenant_known` discards the response body rather than returning it, because
the admin API answers a presence question with the tenant's whole
configuration — its database URL, carrying a live password, and its JWT signing
secret.

**A defect found by wiring, not by testing.** `cp-manage maintenance run` — the
only production caller of `run_all` — never passed `config`, and `run_all`
defaults it to `None`. `measure_object_storage` treats `None` as "this
deployment has no object store" and falls back to the tenant's own
`storage.objects`, which is precisely the figure slice 3 replaced because a
customer who reaches `service_role` can rewrite it. So the pass ran, recorded a
number and enforced against it, and the trustworthy source was never once
consulted outside a test. Nothing failed, nothing logged, and the slice-3
progress note saying the finding was closed was wrong in production and right
in the suite. Found by threading a second pass through the same argument.

**And the first acceptance criterion gained the half it was missing.** "Object
bytes are outside the tenant Postgres DB" was held by a test asserting the
bytes are in the object store, which does not say they are not *also* in
PostgreSQL. Slice 0 counted `bytea` columns once, by hand, against one build of
one image; an upstream release that began inlining small objects would have
failed nothing. The negative is now asserted after a real upload: no `bytea` or
large-object column in schema `storage`, no large objects, and the schema still
at metadata scale.

## Verification

Ticked at the close against one run, 2026-08-25, with **every** prerequisite
flag set to require rather than skip — `MALUDB_REQUIRE_STORAGE_MIGRATIONS`,
`MALUDB_REQUIRE_OBJECT_STORE`, `MALUDB_REQUIRE_STORAGE_SERVER`,
`MALUDB_REQUIRE_REALTIME_NODE`, `MALUDB_REQUIRE_REALTIME_SERVER` — so an absent
prerequisite would have been a red run rather than a quiet pass. Result:
**1270 passed, 2 skipped, 0 failed**, and both skips named rather than counted:
a live MaluMail send (`MALUMAIL_API` unset), and Phase 06's
`test_the_probe_reports_a_node_without_the_reject_as_unsafe`, which needs a
deliberately **unsafe** cluster (`MALUDB_REALTIME_PERMISSIVE_DSN`) to prove the
ADR-031 probe can report one — "a check that cannot fail has not been tested",
which is the same argument this section makes below about harnesses. Neither is
Phase 10. **No Phase 10 test skipped**, checked rather than inferred:
`test_storage_compat.py` and `test_storage_workers.py` run 43 with none
skipped, and `test_object_storage.py` none.

- [x] Unit/integration tests for schema, entitlements, worker, gateway route —
      `test_object_storage.py` (schema, ownership, hardening, upstream's 63
      migrations under a constrained owner), `test_object_storage_accounting.py`
      (both ceilings), `test_storage_workers.py` (the worker, its containment
      and its isolation claims), `test_gateway_storage.py` (the route),
      `test_object_store.py` (the S3 bake-off), `test_migration_storage.py`
      (slice 6), `test_maintenance.py` (slice 7's reconciliation).
- [x] Compatibility tests using the official `supabase-js` client (slice 5) —
      19 cases in `tests/compat/storage.mjs`, asserted one per behaviour by
      `tests/test_storage_compat.py`.
- [x] Tenant-isolation check: one project cannot reach another's objects —
      two provisioned projects with their own hosts entries, same bucket name
      and same key; plus the same claim below the client in
      `test_storage_workers.py` and `test_object_storage.py`.
- [x] RLS policy on `storage.objects` demonstrably gates access — the same
      object admitted to a signed-in user and refused an anonymous one through
      the same client, refused to a user signed in elsewhere, and hidden from an
      anonymous `list` rather than only from `download`.
- [x] `scripts/export-openapi.py --check` clean after any route change — clean;
      slice 7 changed no route.
- [x] `ruff check .` clean
- [x] Migrations idempotent on re-run — `applied 0 migration(s) (up to date)`.
- [x] `specs/compatibility-matrix.yaml` updated with `verified_by` per feature —
      seven `supported` entries with `verified_by`, three
      `intentional_incompatibility` with `verified_by`, five `deferred` each
      carrying its reasoning.
- [x] Security review recorded as a trailer on every slice — all ten commits of
      the phase, checked by grepping the bodies rather than assumed. Six found
      something; fourteen findings, twelve fixed in the change that found them.

**One box that is ticked and should be read with its qualifier.** The
compatibility and isolation lines are held by a suite whose own negative cases
passed against a dead port until slice 5 pointed them at one, and whose fixture
would have accepted a stale worker until slice 7 did the same. Both are fixed
and both were found by attacking the harness rather than the product. The
honest reading of a green run here is "green, and the harness has now been
checked twice for the specific way it could lie".

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
- 2026-08-24 — **SeaweedFS pinned at 4.41**, not slice 0's 4.44, because 4.44
  was published the day it was measured. The bake-off was re-run rather than
  inherited: a pin whose evidence is for another build is not a pin.
- 2026-08-24 — **boto3 added as a runtime dependency.** ADR-055 makes S3 the
  provider boundary, so the client for it should be the standard one. The
  deciding half is that reaching the store means AWS SigV4 request signing, and
  the repository's own precedent beside `pyjwt` applies word for word: "we wrote
  our own request signing for the component holding every customer's files" is
  not a sentence worth defending for one dependency.
- 2026-08-24 — The storage worker's node-level secrets are **one root on the
  `nodes` row**, with the admin API key, `AUTH_ENCRYPTION_KEY` and the metadata
  password derived from it. Reuse matters more here than for Realtime: that key
  decrypts every registered tenant's connection settings, so regenerating it
  would leave a whole node's tenants unreadable at once rather than one project.
- 2026-08-24 — The storage container drops **every** capability and sets
  `no-new-privileges`, which the Realtime unit cannot. That image needs SETUID
  and SETGID back for a sudo step in its entrypoint; this one has none, so the
  stronger containment is available and taken rather than matched to the weaker
  neighbour for symmetry.
- 2026-08-24 — Egress is counted against a **UTC calendar month**, not the
  subscription's billing period. Free has no subscription and ADR-056 puts the
  ceiling on free; the ceiling is not a charge, so it has nothing to line up
  with. Stored as one row per project per period rather than a counter that
  resets in place.
- 2026-08-24 — The object-storage state is **`exceeded`**, not `restricted`.
  Nothing is revoked in the tenant, and the two words must not imply the same
  mechanism.
- 2026-08-24 — Held bytes are read from the tenant's `storage.objects`
  metadata rather than from the object store. Cheaper, needs no object-store
  call, and is what upstream itself reads — at the cost of a drift that Phase 11
  owns and that under-counts rather than over-charges.
- 2026-08-24 — The `storage` schema is owned by a **new per-tenant role**,
  `mldb_<ref>_storage`, on bootstrap 007's precedent rather than by the
  platform superuser. Upstream's migrations then run as a role that can log in
  to one database and owns one schema, which is what makes
  `DB_INSTALL_ROLES=false` a constraint rather than a label.
- 2026-08-24 — That role's `search_path` is pinned to `storage` **IN DATABASE**,
  and it receives no grant on `public`. Not a preference: without the pin,
  upstream migration 0011 puts an unqualified function in the schema PostgREST
  exposes, and granting `CREATE ON public` to avoid the failure is the worse of
  the two available fixes.
- 2026-08-24 — RLS on the storage tables is left **unforced**. Forcing it denies
  the owner, and with no policies present that denies `storage-api`'s own
  bookkeeping. The control is that the owner is not customer-reachable.
- 2026-08-24 — The tenant admin role gets **no privilege on `storage`**. It is
  service-owned bookkeeping kept consistent with an object store, and a
  customer `DELETE` there orphans bytes nothing will collect. The cost is that
  a customer cannot author a storage policy either, which is slice 4's decision
  and is recorded in the compatibility matrix rather than left to be found.
- 2026-08-22 — **Settled by that measurement as ADR-058: one shared instance
  per node.** The gap was wider than predicted — ~0.7 MB marginal per tenant
  against ~106 MB dedicated — and the worse blast radius is accepted explicitly
  rather than waved through, because two boundaries were verified rather than
  one.

## Progress log

- 2026-08-25 — **Slice 7 complete, and the phase closed.**
  `tasks/PHASE-10-STORAGE.md`'s three criteria ticked against named tests rather
  than against recollection, with the one gap under them — a customer still
  cannot author a storage policy — written into the task file rather than left
  in the matrix for a reader to find. `docs/STORAGE.md` rewritten from "Phase
  10, in progress. Nothing is served yet" to what was built.
  `docs/CAPACITY.md` needed nothing: slice 0's memory table and slice 3's load
  measurement both landed in their own slices, which is where the plan asked
  for them.

  **Two of the seven boxes were not documentation.** Slice 4 carried
  `storage_workers.registered_projects` here rather than wiring it into the
  request path, and it now has the caller it was written for:
  `maintenance.reconcile_storage_tenants` asks the worker's admin API whether it
  still knows a tenant and re-registers only what it has forgotten — the
  container-restart case keeps its tenants, the rebuilt-metadata-database case
  does not, and until now the only symptom of the second was every Storage
  request for those projects answering `400 TenantNotFound` with nothing saying
  why. `node_secret` was split out of `ensure_node_secret` so a repair pass
  cannot mint the root that decrypts every registered tenant's settings, the
  pass refuses to guess which node a loopback admin port belongs to, and
  `tenant_known` throws the response body away because the admin API answers a
  presence question with a live DSN and a signing secret. 8 tests.

  And the first acceptance criterion gained its missing half: "the bytes are in
  the object store" is not "the bytes are not in PostgreSQL", and only the first
  had a test. The second is now asserted after a real upload — no `bytea` or
  large-object column in schema `storage`, no large objects, metadata scale
  intact. Slice 0's hand count of `bytea` columns was one build of one image and
  would not have failed an upstream release that started inlining.

  **The finding, which is slice 3's own and had been believed closed.**
  `cp-manage maintenance run` is the only production caller of
  `maintenance.run_all` and never passed `config`. `run_all` defaults it to
  `None`, and `measure_object_storage` reads `None` as "no object store on this
  deployment" and falls back to the tenant's `storage.objects` — the figure
  slice 3 replaced *because* a customer who can reach `service_role` can rewrite
  it. Every production measurement since slice 3 took the forgeable source; the
  pass ran, recorded and enforced, and nothing failed. The suite never caught it
  because the tests call `measure_object_storage` directly with a config.
  Fixed by passing `settings`, and found only by threading a second pass through
  the same argument — which is the argument for closing a phase by wiring
  something rather than by reading.

  **And a harness fix that is not this phase's, found by running the whole
  suite for the close.** The verification run came back 16 red, every failure
  naming a Data API behaviour — `select`, `insert`, five `rls` cases — and none
  naming a port. The cause was a PostgREST left behind by an earlier
  interrupted run: this run's process died with `bind: resource busy`,
  `workers.wait_until_ready` was satisfied by the orphan, and every case then
  ran against a tenant database that had since been dropped. `wait_until_ready`
  is given a port and nothing else, which is right for the control plane and
  not enough for a fixture that owns the process handle. The compat fixture now
  asserts that the process it started is the process answering, and says so by
  name. Third time this repository has found the same shape: slice 3's stale
  `weed` server, slice 5's negative cases passing against a dead endpoint, and
  this.

  **And a second one, in this phase's own suite, found by running it twice.**
  With the orphan gone the run came back with 21 errors in
  `tests/test_storage_compat.py`, all in fixture setup, naming buckets, uploads
  and isolation and nothing that named a key. The worker was answering
  `ERR_OSSL_BAD_DECRYPT` from `jwks/manager.js`: `AUTH_ENCRYPTION_KEY` is
  derived from the node's storage root, the control-plane database is truncated
  per module so a fresh root is minted on every run, and `maludb_storage_meta`
  is never dropped by anything — `ensure_metadata_database` is idempotent by
  design, because platform code removing a node's tenant registry would be an
  extraordinary thing for it to do. So the fixture inherited JWKS rows
  encrypted under the previous run's key.

  The consequence is worth stating plainly: **these tests passed once per
  metadata database and were red on every run after**, and neither slice 3 nor
  slice 5 could have noticed, because CI's node is new each time and a local
  first run is green. Both storage fixtures now drop the metadata database
  before ensuring it — `scripts/storage-test-cluster.sh`'s own rule, applied to
  the one piece of node state the fixtures were not applying it to. Verified by
  running the two storage modules back to back: 43 passed, then 43 passed.

- 2026-08-25 — **Slice 6 complete.** `services/migrate/storage.py` carries
  buckets and object bytes; `--with-storage` on `apply` runs it; the
  `storage.objects` blocker is gone and `storage.empty_buckets` is gone with it,
  because empty buckets are now recreated rather than warned about.
  `docs/MIGRATION-FROM-SUPABASE.md` loses "Blocked at launch: Storage" and gains
  a section on what the extra credentials are for. 29 tests in
  `tests/test_migration_storage.py`.

  **The decision this slice owed, recorded as ADR-063.** Storage is the first
  thing a migration carries that is not rows, so it is the first that cannot
  reach the destination through the control plane. It takes three credentials,
  all the customer's: the Supabase project URL and service-role key to read, and
  the **destination project's own secret key** to write. The CLI holds a
  platform token that could mint itself the third through
  `POST /v1/projects/{ref}/api-keys`, and deliberately does not — creating a key
  is closer to adding an owner than to changing a setting, and a tool that
  issues one quietly leaves a live credential behind exactly when a run fails
  partway and nobody is looking. All three are environment-only; none has a
  flag.

  **A gap found by looking rather than by failing.** `_POLICIES` filters to the
  customer's own schemas and `storage` is one of Supabase's, so nothing in the
  scanner had ever seen a storage policy. Harmless while objects were a blocker;
  a silent loss the moment they were not, since the files would arrive and the
  rules that governed them would not. There is now a probe, a finding, and a
  summary count — and the finding says which direction it fails in, because with
  no policy RLS denies every role but `service_role`, so what breaks is the
  application and not the privacy of the files.

  **Two findings from the security review, both before merge, and the first is
  slice 4's bug in a new place.** Object keys keep their slashes, because that
  is how Storage represents folders — so a key *is* a path, and `httpx` resolves
  dot segments when it builds a URL. An object named
  `../../../rest/v1/things` would have been uploaded to the Data API rather than
  the Storage surface, carrying the destination project's **secret key**, which
  is `service_role` with no RLS in front of it. The names come from a foreign
  system and a customer whose application accepted user-supplied filenames is
  exactly who runs this. Dot segments are now refused rather than normalised,
  percent-decoded twice first, reported as a failed object so the rest of the
  run continues. Second: a skip line sanitised the object key and printed the
  reason raw — and the reason is an exception message naming the same key, so
  the escape sequence was unescaped one field to the right.

- 2026-08-25 — **Slice 5 complete.** `tests/compat/storage.mjs` drives
  `@supabase/supabase-js` over the gateway against a real `storage-api` and two
  real provisioned tenants (`stcp0001`, `stcp0002`, both with their own hosts
  entries), and `tests/test_storage_compat.py` asserts its 19 cases one per
  behaviour. Seven matrix entries move to `supported` with `verified_by`;
  `signed_upload_urls` is added as `deferred` **by decision** rather than
  arriving by omission. Both acceptance criteria are now held by a test: an RLS
  policy on `storage.objects` admits a signed-in user and refuses an anonymous
  one *for the same object through the same client*, and each project reads back
  its own bytes from a bucket and key of the same name.

  **The slice existed to find what only the real client can find, and it found
  two, both now ADR-062.** First: `supabase-js` sends the project key as
  `Authorization: Bearer <key>`. MaluDB keys are opaque, the gateway dropped the
  header — correct for PostgREST, where the *absence* of a token selects
  `db-anon-role` — and `storage-api` has no such fallback. It reads the bearer,
  fails `verifyJWT` on an empty one, and answers 403 before consulting any
  policy. Every anonymous Storage call on the platform was refused, which is the
  whole free tier and every signed-out visitor of a paid project. A publishable
  key now mints a 60-second `anon` token, exactly as a secret key already minted
  a `service_role` one — and that is what makes bootstrap 012's model real
  rather than theoretical, since it grants `anon` on `storage` and leaves the
  decision to RLS, and a policy can only decide about a role that arrives.
  Second: `createSignedUrl` returns a link with a `token` and no `apikey`,
  because the point of one is that it works alone; the gateway answered 401.
  `GET /storage/v1/object/sign/` joins the public prefix, read-only, with the
  same dot-segment refusal and two new traversal cases. Slice 4 passed a full
  Python suite with both of these in it.

  **A third, found by pointing the finished suite at a dead port.** Five of its
  negative cases — including three isolation claims — passed against nothing at
  all, because `expect(error !== null)` is satisfied by a connection refused. An
  isolation test that proves nothing looks exactly like one that proves
  everything. Every negative case now goes through `assertRefused`, which
  requires a *server* status and fails a transport error explicitly; the count
  passing against a dead endpoint is 0.

  **The CI failure, which was one mistake wearing 21 masks.** All 21 tests in
  the module errored in fixture setup with `relation "storage.objects" does not
  exist`, and nothing else in the run failed. Bootstrap 012 creates the
  `storage` *schema*; upstream creates the *tables*, and
  `DB_MIGRATIONS_STRATEGY` defaults to `on_request` — so a tenant's 63
  migrations run in the preHandler of the first request the worker sees for it,
  and not before. The fixture wrote `CREATE POLICY ON storage.objects` straight
  after bootstrap, against a schema that was still empty. It now makes one real
  request through the gateway first (which is also the registration-on-demand
  path), waits for `storage.objects` with the worker's log in the failure
  message, and only then writes the policy. Only the project that needs a policy
  is warmed; `stcp0002` is still registered by the official client's own first
  call, so the on-demand claim rests on something the harness did not do itself.

  Carried to slice 6 rather than done here: `services/migrate/rules.py` still
  tells a migrating customer "MaluDB has no Storage surface until Phase 10",
  which the matrix now contradicts. That blocker becoming a supported migration
  is slice 6's first line.

- 2026-08-25 — **Slice 4 complete.** `/storage/v1` leaves `UNIMPLEMENTED_PREFIXES`
  and is served from the node's shared worker: prefix stripped, tenant named by
  a forwarded host the gateway sets and the client cannot, registration on
  demand through `storage_workers.ensure_registered` (which provisioning now
  calls too, so there is one copy), public-bucket reads open to a caller with no
  key, egress accumulated in process and flushed in batches, and slice 2's two
  inert states turned into refusals — 429 with `Retry-After` to the month
  boundary, 413 on upload, reads never refused (ADR-060). Policy authoring
  decided and left deferred (ADR-061). ADR-056's measurement taken rather than
  asserted: the accounting is inside this machine's noise at p50 across three
  runs, and the gateway's own +6.3 ms is unmoved. 33 tests in
  `tests/test_gateway_storage.py`.

  **Three findings from the security review, all before merge, and the first is
  the one worth remembering.** `httpx` resolves dot segments when it builds the
  upstream URL, so `/storage/v1/object/public/../files/secret.txt` passed the
  public-bucket check — which is what decides that *no API key is required* —
  and arrived at `storage-api` as `/object/files/secret.txt`, the authenticated
  object endpoint. The gateway authorised one path and forwarded another. Dot
  segments are now refused outright, percent-decoded twice first, and the check
  is made in both the routing and the exemption because a later edit that
  separated them would reopen it silently. Second: the egress refusal named the
  project's usage and plan ceiling to an anonymous reader, which made any public
  URL a usage oracle; the figures now go only to a caller that proved it holds a
  key. Third: the 8 MiB body cap would have capped every upload on the platform
  at 8 MiB against upstream's 50 MB default.

- 2026-08-25 — **Slice 3's CI failures, and what they had in common.** Twelve
  tests failed on PR 84 and none of them on this machine, twice over, for the
  same reason both times: the slice was verified on a node configured by hand
  and CI's node is configured by a script. First `cp_config.load()` in two test
  modules, which requires the deployment's key material — a developer shell
  exports it because the setup instructions say to, CI exports none because the
  suite has `TEST_KEK`; replaced with `storage_env_config()` in conftest, which
  is checked to agree with `load()` on every storage field. Then the worker
  never becoming ready, because `storage-test-cluster.sh` admitted the data
  address in `pg_hba.conf` and assumed the postmaster was already listening on
  it — true of a hand-configured node on `*`, false of `pg_createcluster`'s
  `localhost`. The script now arranges both halves (additively, with the restart
  `listen_addresses` requires), CI asserts the node answers at the data address
  before the suite runs, and the fixture keeps the container's output so the
  next failure of this kind names itself instead of timing out in silence.
- 2026-08-24 — **Slice 3 complete.** The shared worker exists and serves: a node
  preparation script, `storage_workers.py`, a systemd unit, migration 0025,
  registration in provisioning, object deletion in cleanup, and the store-side
  measurement that closes slice 2's finding. SeaweedFS pinned at 4.41 with its
  own bake-off. ADR-058's load measurement taken and recorded. 35 new tests
  across two modules; three upstream assumptions and two of my own corrected by
  running the thing rather than reading about it.
- 2026-08-24 — **Slice 2 complete.** Both ADR-056 ceilings in `entitlements`,
  `DEFAULTS` and the published spec; migration 0024 adding the project columns
  and `project_egress`; `object_storage.py` with measure/classify/evaluate for
  held bytes and an additive monthly counter for served ones; a
  `measure_object_storage` pass with its own cursor so it and the database-
  storage pass can fail independently; both figures on `/usage`. 23 tests in
  `tests/test_object_storage_accounting.py` plus route and pass coverage.
  Nothing refuses anything yet — slice 4 is the enforcement point, and the
  states are inert until it exists.
- 2026-08-24 — **Slice 1 complete.** Bootstrap `012_storage_schema.sql`, the
  `mldb_<ref>_storage` role and its provisioning step (control-plane migration
  0023, status `STORAGE_ROLE_CREATING`), an idempotent
  `maludb_platform.harden_storage_schema()` behind an event trigger, five new
  `verify()` outcomes, and 24 tests in `tests/test_object_storage.py` — three
  of them running upstream's 63 migrations out of the pinned image, gated on
  Podman with `MALUDB_REQUIRE_STORAGE_MIGRATIONS` for CI and a suite banner
  when it is absent. The migrations pass under a constrained owner once the
  role's `search_path` is pinned; without the pin one of them puts a function
  in `public`. `jobs._drop_roles` also stopped leaking `executor` and `client`.
  Storage policy authoring is the one gap left open, and it is written down.
- 2026-08-22 — **Slice 0 complete.** `supabase/storage-api:v1.70.6` and
  SeaweedFS 4.44 measured on the node CPU profile: image runs, 17/17 S3
  operations pass, shared topology beats per-project by two orders of
  magnitude, `DB_INSTALL_ROLES=false` needs exactly one schema grant, egress
  confirmed to pass the platform, ADR-035 containment confirmed for Storage.
  Written up as `specs/storage-server-model.md` and ADR-058; `docs/CAPACITY.md`
  gained the measured figures. No platform code yet — slice 1 is the first.
- 2026-08-22 — Phase 09 verified closed. Canonical docs read, upstream and
  repository measured, three `## Storage` open questions answered by the
  repository owner, plan written. No code yet; slice 0 is next and needs none
  of the code that follows it.
