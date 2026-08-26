# Storage

## Status

**Phase 10 complete** (2026-08-22 to 2026-08-25, slices 0 to 7, PRs #81 to #88).
A project on any tier — free included — creates buckets, uploads, downloads,
lists, signs and removes objects through `@supabase/supabase-js` against
`/storage/v1`, an RLS policy on `storage.objects` decides what a caller may
read, and one project cannot reach another's objects. Object bytes live in
SeaweedFS and never in the tenant database.

The execution plan and its evidence are in
`plans/completed/phase-10-storage.md`; what was measured before any of it was
built is in `specs/storage-server-model.md`; the acceptance criteria and the
tests that hold them are in `tasks/PHASE-10-STORAGE.md`.

What is **not** here is listed under [What this does not do](#what-this-does-not-do)
rather than left to be discovered. Two of those are worth knowing before you
read anything else: a customer cannot yet author a storage policy, and
resumable uploads do not exist, so an object larger than the upload ceiling has
no path in.

## The shape of it

Five things, and the seams between them are where the decisions are.

```
supabase-js ──► gateway ──────► storage-api ──┬──► tenant PostgreSQL: schema `storage`
              (routes by         (one shared   │     buckets, object metadata, RLS
               hostname,          container    │
               mints a role       per node)    └──► SeaweedFS: the bytes,
               token, counts                          one platform bucket,
               egress)                                keyed by tenant
```

- **The gateway** resolves the project from the hostname (ADR-008), validates
  the key against it, strips `/storage/v1`, and names the tenant to the worker
  with an `X-Forwarded-Host` **it** sets. That header is in `UNTRUSTED_INBOUND`,
  so a client's own copy is dropped rather than appended to — it is the whole
  tenancy control on this surface.
- **The worker** is upstream `supabase/storage-api`, pinned, `MULTI_TENANT=true`,
  **one shared container per node** (ADR-058), run under rootless Podman by
  `deploy/maludb-storage.service`. Not a template unit: the per-project work is
  registering a tenant, not starting a container.
- **The tenant database** holds buckets and object metadata in a `storage`
  schema owned by `mldb_<ref>_storage`, a platform-internal service role in the
  same class as `mldb_<ref>_auth` (ADR-059). Row-level security is on and
  deliberately **not forced**, so the owner — which is the worker's own
  bookkeeping — is not denied by the customer's policies.
- **The object store** is SeaweedFS (ADR-055), reached over S3 on a data
  address, never loopback (ADR-035).
- **The control plane** provisions the role and schema, registers the tenant,
  measures held bytes, counts egress, and deletes a project's objects when the
  project goes.

## Object bytes are outside the tenant database

Phase 10's first acceptance criterion, and it has two halves that are easy to
conflate. The bytes are in the object store — and they are *not also* in
PostgreSQL. `tests/test_storage_workers.py::test_the_object_keys_are_prefixed_by_tenant_in_one_platform_bucket`
asserts both after a real upload: the key is in SeaweedFS under the tenant's
prefix, schema `storage` has no `bytea` or large-object column to put bytes in,
and it stays at metadata scale.

The negative matters more than it looks. Slice 0 counted `bytea` columns once,
by hand, against one build of one image; an upstream release that started
inlining small objects would not have failed anything.

## Tenancy: one bucket, and isolation that lives in metadata

**One platform bucket holds every tenant's objects** (ADR-057).
`storage-api`'s `STORAGE_S3_BUCKET` is singular, and a customer "bucket" is a
row in that tenant's `storage.buckets`. Keys are prefixed by project ref.

The consequence matters more than the mechanism: **isolation for objects is a
property of the metadata layer and the worker's credential scoping, not of the
object store.** So it is tested as a denial rather than assumed —

- two tenants create a bucket of the *same name* holding a key of the *same
  name*, and each reads back its own bytes
  (`test_storage_workers.py`, and again through the official client in
  `tests/test_storage_compat.py`);
- a token signed for one tenant reaches nothing of another's;
- a client built from one project's key and another project's URL is refused
  401;
- a signed URL issued by one project is not served by another.

This is the risk in this phase most worth a reviewer's attention, and it is the
one the compatibility suite spends two provisioned projects on.

## Authorization

Three credentials reach this surface, and the gateway turns each into something
`storage-api` understands.

| what the client sends | what the worker gets | what decides |
|---|---|---|
| a secret key | a `service_role` token | nothing — `service_role` bypasses RLS |
| a publishable key | a 60-second `anon` token | RLS on `storage.objects` |
| a signed-in user's JWT | that JWT | RLS, as `authenticated` |
| nothing, on a public bucket or a signed URL | no token | the bucket's `public` flag, or the signature |

The publishable-key row is ADR-062 and was a real defect until slice 5 found
it. `supabase-js` sends the project key as `Authorization: Bearer <key>`;
MaluDB keys are opaque, so the gateway dropped the header — correct for
PostgREST, where the *absence* of a token selects `db-anon-role`, and wrong
here, because `storage-api` has no such fallback. It read the empty bearer,
failed `verifyJWT`, and answered 403 before consulting any policy. Every
anonymous Storage call on the platform was refused: the whole free tier and
every signed-out visitor of a paid project. It is also what makes bootstrap
012's model real rather than theoretical — the schema grants `anon` and leaves
the decision to RLS, and a policy can only decide about a role that arrives.

`service_role` bypassing RLS is ADR-041's finding in a new place, and it is why
any customer-influenced role selection is treated as a credential choice rather
than a boundary.

## Limits, and what a full project sees

Storage is available on **every tier including free**, bounded by two hard
ceilings under ADR-050 (ADR-056). Numbers live in
`specs/plans-and-limits.yaml`, never in code.

- **`object_storage_bytes`** — held bytes, measured by a maintenance pass
  **from the object store** rather than from the tenant's own
  `storage.objects`. That is a security property, not an optimisation: a
  customer who can reach `service_role` can rewrite the metadata figure, and
  re-measuring would re-read the same forged column. A node with no object
  store falls back to metadata, which is the only figure available there.
- **`egress_bytes_per_month`** — counted as bytes pass the gateway, in
  `project_egress`, one row per project per UTC calendar month. Accumulated in
  process and flushed in batches, because a write per response is not available
  on a path ADR-026 published a latency number for. It reaches anonymous reads
  of public buckets too, which is where a free project's egress actually goes.

Both are reported on `GET /v1/projects/{ref}/usage`.

What a project over a ceiling meets (ADR-060, and all three are in the
compatibility matrix as intentional incompatibilities, because Supabase bills
the overage instead):

- over egress: **429** with `Retry-After` set to the UTC month boundary;
- over held bytes: **413** on upload — upstream's own status for its file-size
  limit, so an existing client error branch catches it;
- **reads, lists and deletes are never refused.** They are the only way back
  under the ceiling, and a customer who cannot download their own files to free
  space has no way forward. That is ADR-050's product point.

The gateway's own upload ceiling is `MALUDB_STORAGE_MAX_UPLOAD_BYTES`,
defaulting to 50 MiB against Supabase's 50 MB. The gateway's general 8 MiB body
cap is sized for a PostgREST insert and would otherwise have capped every
upload on the platform.

## Migrating from Supabase Storage

`services/migrate` carries buckets, object metadata and object bytes;
`--with-storage` on `apply` runs it. `docs/MIGRATION-FROM-SUPABASE.md` has the
operator's version.

ADR-042 constrains the shape and ADR-063 records what it cost: the customer runs
the CLI and the platform never holds their Supabase credentials, so object bytes
move **through the customer's machine** rather than server to server. It takes
three credentials, all theirs — the Supabase project URL and service-role key to
read, and the **destination project's own secret key** to write. The CLI holds a
platform token that could mint itself that third key and deliberately does not:
creating a key is closer to adding an owner than to changing a setting, and a
tool that issues one quietly leaves a live credential behind exactly when a run
fails partway and nobody is looking.

What does not travel is named rather than dropped: storage policies (there is
nowhere to put them — see below), ownership, and objects over the upload
ceiling. The scanner probes for policies on `storage` and reports which
direction their absence fails in — with no policy, RLS denies every role but
`service_role`, so what breaks is the application rather than the privacy of
the files.

## What this does not do

Each of these is a `deferred` entry in `specs/compatibility-matrix.yaml` with
its reasoning, because a deferral that arrives by omission is a decision nobody
made.

- **A customer cannot author a storage policy** (ADR-061). Policies are
  *enforced*; they cannot be *created* from the customer's side, because
  `CREATE POLICY` requires ownership of `storage.objects` and the owner is a
  platform-internal role. The Supabase analogue — granting the tenant admin
  membership in the storage role — is owner-level bypass of every storage
  policy including the customer's own, plus write access to the metadata that
  says which object bytes belong to whom. The shape that gives authoring safely
  is a mediated surface that validates what it creates, which is a slice rather
  than a grant. Public buckets need no policy and are unaffected.
- **Signed *upload* URLs.** Upstream redeems them on a route its own
  routing places in the group needing no JWT, so honouring them means a *write*
  reachable with no API key. That wants the upload ceiling and the egress meter
  thought through on an unauthenticated path first. Uploading with a key is
  unaffected.
- **Resumable/TUS uploads.** The body is buffered in the gateway, so the answer
  to a much larger ceiling is streaming rather than a bigger number.
- **Image transformation.** Needs a second pinned container, and carries
  ADR-033's no-AVX2 hazard, which slice 0 explicitly did not clear.
- **The S3 protocol endpoint.** A credential and a reachable port, so ADR-039's
  paid line and a decision of its own.

## Why there is no MaluDB provider abstraction

> Storage implementation must not couple the tenant-database lifecycle to one
> specific object-storage vendor.

Met by configuration rather than by code. `storage-api` selects its backend with
`STORAGE_BACKEND=s3|file` and addresses an S3 one with `STORAGE_S3_ENDPOINT`,
`STORAGE_S3_REGION`, `STORAGE_S3_FORCE_PATH_STYLE` and `STORAGE_S3_BUCKET`.

**The platform therefore writes no provider abstraction of its own.** A
MaluDB-side driver interface above an existing one would be owned by us, tested
by us, and would add nothing. `tasks/PHASE-10-STORAGE.md` opens its scope with
"object-store provider abstraction", and the correct implementation of that
bullet is an environment variable and this paragraph.

Two structural facts keep it honest. Provisioning makes no object-store API
call, so a project can be created while the object store is unreachable. And
ADR-035 already forbids the container from reaching node loopback, so the store
is addressed on a data address whether it is one hop away or one datacentre
away — which is what makes ADR-055's exit to dedicated hardware an endpoint
change and a copy, with no platform code touched.

MinIO was the obvious candidate and was rejected: its community edition was
archived on 25 April 2026 — no binaries, no security patches — which is
disqualifying for the component holding every customer's files. Garage is ruled
out by AGPL for a commercial platform; Ceph RGW would be right on a cluster
already running Ceph and is disproportionate otherwise.

## Operating it

- `scripts/storage-test-cluster.sh` builds an object store and prepares the
  node's PostgreSQL for the worker. See `AGENTS.md` for the exports and for what
  skips without them.
- `cp-manage maintenance run` measures held bytes, and re-registers any project
  the node's worker has forgotten. That last one repairs a narrow but silent
  failure: a container restart keeps its tenants, but a worker whose
  *multitenant database* was rebuilt has forgotten all of them, and those
  projects then answer `400 TenantNotFound` to every Storage request with
  nothing anywhere saying why. The worker's admin API is on loopback, so pass
  `--node` when more than one node has storage-registered projects.
- `jobs.cleanup` deletes a project's objects. They live outside the database and
  outside the roles, so dropping both used to leave a deleted project's files in
  the platform bucket indefinitely.
- Node cost is a **fixed** ~120 MB per node rather than a per-project term —
  see `docs/CAPACITY.md`, which also has the load measurement and why the number
  to plan against is total concurrency rather than tenant count.

## Notes for Phase 11

SeaweedFS's Apache 2.0 core covers everything Phase 10 needs. Automatic
erasure-coding repair, EC vacuum, self-healing and point-in-time recovery sit
behind a per-TB Enterprise licence, free under 25 TB for development and test.
Those are durability features, so they belong with backups, restore and PITR
rather than here — recorded so the question arrives as a decision rather than a
discovery.
