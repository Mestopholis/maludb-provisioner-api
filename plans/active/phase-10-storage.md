# Execution Plan: Phase 10 — Storage, and keeping the bytes somewhere the database is not

Status: **IN PROGRESS** — plan written 2026-08-22. **Slice 0 complete**
(2026-08-22): the substrate is measured, the topology is settled as ADR-058, and
`specs/storage-server-model.md` records it. **Slice 1 complete** (2026-08-24):
the tenant `storage` schema exists under platform ownership, upstream's 63
migrations run under a constrained owner, and the one thing that stops them is
recorded rather than worked around. **Slice 2 complete** (2026-08-24): both
ADR-056 ceilings exist, are measured or counted, are visible to a customer, and
reach a project that changes plan. **Slice 3 complete** (2026-08-24): one shared
worker per node serves tenants, its containment is measured from inside the
container, deleted projects lose their objects, and the held-bytes figure is
taken from a source the customer cannot write. Slice 4 is next.

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
