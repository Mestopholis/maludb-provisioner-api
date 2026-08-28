# Storage Server Model

What it takes to run upstream `supabase/storage-api` against MaluDB's tenancy,
and what it costs. Deliverable of Phase 10 slice 0.

Status: derived from experiments run 2026-08-22 against
`supabase/storage-api:v1.70.6` and SeaweedFS 4.44, on PostgreSQL 17 and on the
node CPU profile described below. Every claim here was measured on a running
server. Objects were written through `storage-api`'s own HTTP surface and read
back out of the object store by key.

Companion to `docs/STORAGE.md`, which records the decisions (ADR-055 to
ADR-057), and to `specs/realtime-server-model.md`, whose topology finding is the
one this document deliberately contradicts.

## The finding that decides the topology

ADR-034 forced **one Realtime instance per project**, and the reason was
specific rather than general: `SLOT_NAME_SUFFIX` is a server-level environment
variable and PostgreSQL replication slot names are cluster-unique, so one
Realtime server can serve one tenant per cluster. That cost ~146 MB per project.

**`storage-api` has no equivalent cluster-unique resource**, and the difference
is measurable rather than argued.

| | dedicated instance | shared instance |
|---|---|---|
| Instance, cgroup accounting | **105.8 MB** | 115.7 MB at 2 tenants |
| | | **119.8 MB at 8 tenants** |
| Marginal cost per tenant | ~106 MB | **~0.7 MB** |
| PSS of the process tree | 129.7 MB | 143.1 MB at 8 tenants |

Six tenants added 4.1 MB. Against ADR-022's 31.8 MB for an entire warm project
and ADR-034's 146 MB for one Realtime instance, a dedicated `storage-api` per
project would be **the most expensive thing a project could enable**, and a
shared one is close to free per tenant.

**Recommendation: one shared multi-tenant instance per node**, which is the
opposite of ADR-034's answer for Realtime and is not in tension with it — the
constraint that forced ADR-034 does not exist here.

The measured figures are for **registered, idle** tenants: schema migrated,
pool established, no sustained traffic. The number that would change this
conclusion is per-tenant connection pooling under load, bounded by
`DATABASE_MAX_CONNECTIONS` with `DATABASE_FREE_POOL_AFTER_INACTIVITY`
releasing idle pools. That is a load measurement and slice 3 should take it
before the topology is committed to in code.

## The node CPU profile, and ADR-033's hazard

ADR-033 pinned Realtime to v2.110.0 because a newer image died with SIGILL
inside a precompiled Rust NIF built for a CPU baseline these nodes do not meet.
That hazard was checked first here, because it is the one that stops a phase.

Measured on `QEMU Virtual CPU version 2.5+` — `sse4_2` and `popcnt`, **no AVX,
AVX2, BMI2 or FMA**, the same profile ADR-033 documents:

```
$ podman run --rm supabase/storage-api:v1.70.6 node -e 'console.log(process.version)'
v24.19.0
```

The full server starts, completes its upgrade steps, and serves. **No SIGILL,
no native-module fault.** `storage-api` is TypeScript on Node with no
precompiled NIF in the request path, which is why it survives where the newer
Realtime image did not.

This does **not** clear image transformation. `IMAGE_TRANSFORMATION_ENABLED`
requires imgproxy, a separate Go binary over libvips, and nothing here measured
it. Phase 10 defers it, and the deferral should stand until imgproxy is
measured on this profile.

## Pin

`docker.io/supabase/storage-api:v1.70.6`, published 21 August 2026, amd64 and
arm64, ~244 MB. Pinned in every environment, matching the treatment of
`maludb_core`, PostgREST, GoTrue and the Realtime image.

Upstream publishes **no release binary** — `supabase/storage` releases carry
only `api.json` and `api-admin.json`. So this is a container under ADR-033's
pattern rather than a systemd unit under ADR-027's, and not by preference.

SeaweedFS 4.44 was the object store under test. It is a single Go binary and
runs on the same CPU profile. Note that 4.44 was released the same day it was
tested; slice 3 should pin a release with some age on it rather than tracking
latest.

## What the platform must do that upstream would otherwise do itself

`DB_INSTALL_ROLES=true` is upstream's default and is unavailable here: it makes
the service create `anon`, `authenticated` and `service_role` and act as
`DB_SUPER_USER`. ADR-004 gives customers no superuser, and ADR-016 makes those
three names **shared cluster-wide**, so a component that believes it may create
them is a component that believes it is alone on the cluster.

Measured with `DB_INSTALL_ROLES=false`:

- **The service runs its own migrations successfully** and creates the full
  `storage` schema without needing the roles installed. Boot is clean.
- **Every request then fails** with `permission denied for schema storage`,
  reported to the client as `403 AccessDenied`.
- The cause is narrow and worth stating exactly: upstream's migrations **do**
  grant table privileges to the three role names, but the schema itself is
  left `(null = owner only)`. The missing grant is schema-level `USAGE`.

```sql
GRANT USAGE ON SCHEMA storage TO anon, authenticated, service_role;
```

That single statement was the entire remedy. With it, bucket creation, upload,
download, signed URLs and listing all returned 200 on the first attempt.

Slice 1 owns issuing that grant from a platform bootstrap file rather than by
hand, and owns deciding schema ownership — under `DB_INSTALL_ROLES=false` the
schema and all its tables are owned by the migrating role.

### RLS is already on, and is not forced

Measured on a freshly migrated tenant:

```
buckets  rls=true  forced=false
objects  rls=true  forced=false
```

This matches what Phase 08 recorded at `services/migrate/source.py:248` and is
the mechanism storage policies are built from, so this phase cannot harden by
turning it off. `forced=false` matters: **the table owner bypasses RLS**, which
is ADR-040's admission in a new place and something slice 1 must decide
deliberately rather than inherit.

### The v1.70.6 schema is wider than Storage

A migrated tenant carries ten tables, not the two a reader would expect:

```
buckets  buckets_analytics  buckets_vectors  iceberg_namespaces
iceberg_tables  migrations  objects  s3_multipart_uploads
s3_multipart_uploads_parts  vector_indexes
```

`buckets_vectors`, `vector_indexes` and the two `iceberg_*` tables are surface
Phase 10 does not use and does not expose. They are named here because slice 1
hardens what exists rather than what was expected, and because an unexamined
table in a tenant database is exactly the kind of thing ADR-018 exists for.

## Object layout, and where isolation actually comes from

One platform bucket (ADR-057). Measured key layout, identical in both
topologies:

```
<tenant_id>/<bucket_name>/<object_path>/<version_uuid>

aaaaaaaa/shared-name/secret.txt/558ad701-835d-4830-86b8-9853b23af56f
bbbbbbbb/shared-name/secret.txt/b14a6c1b-2fe4-47a7-99ee-6edc0d92c4f3
```

Two tenants created a bucket of the **same name** holding a key of the **same
name**, and each read back its own bytes. The prefix that separates them is
`tenant_id`, which comes from server configuration in dedicated mode and from
the resolved tenant in shared mode — **in neither case from the request body or
path.**

### Acceptance criterion 1, measured

Object bytes are outside tenant PostgreSQL:

- `bytea` columns in schema `storage`: **0**
- `storage` schema on disk after upload: **592 kB** (metadata and indexes)
- the object row records `size` and `mimetype`; the bytes are in the object
  store, retrievable by the key above.

## Tenant resolution in shared mode, and the two boundaries

`REQUEST_X_FORWARDED_HOST_REGEXP` extracts the tenant from `X-Forwarded-Host`
and looks its configuration up in `DATABASE_MULTITENANT_URL`. Registration is
through an admin API on `SERVER_ADMIN_PORT`, authenticated with an **`apikey`
header** — not `Authorization`, which answers 401 and is worth writing down
because it costs an hour to discover.

The question that decides whether shared mode is safe is whether
`X-Forwarded-Host` is the *only* thing selecting a tenant. It is not. Measured
with two tenants holding different `jwtSecret` values:

| request | result |
|---|---|
| A's token, A's host | `200`, A's bytes |
| B's token, B's host | `200`, B's bytes |
| garbage token, B's host | `403 JWS Protected Header is invalid` |
| **A's token, B's host** | **`403 signature verification failed`** |
| unregistered host | `400 TenantNotFound` |
| host not matching the regexp | `400 Invalid tenant id` |

So there are **two independent boundaries**: the host selects the tenant, and
the tenant's own JWT secret must verify. A stolen or forged host header alone
does not reach another tenant's data.

**This does not make the header safe to accept from a client.** It is the
tenant selector, and the gateway must set it authoritatively and strip any
client-supplied value. A client able to set `X-Forwarded-Host` chooses which
tenant's configuration and database pool a request is evaluated against, which
is a denial-of-service and information-disclosure surface even where the JWT
check holds. Slice 4 owns this, and it should be reviewed as if the JWT check
were absent.

### An error-surface inconsistency, recorded because monitoring depends on it

The same cross-tenant attempt reports differently depending on the route:

- `GET /bucket` with a foreign token → `403 signature verification failed`
- `GET /object/<bucket>/<key>` with a foreign token → `404 NoSuchBucket`

Both deny. But the object path masks an authentication failure as a
not-found, so a monitor counting auth failures will not see cross-tenant
attempts on the busiest route in the service. Not a vulnerability; a gap in
what is observable, and cheaper to know about now than to discover during an
incident.

## SeaweedFS against what `storage-api` actually requires

Not a general S3 conformance run. These are the operations upstream's own code
paths depend on, taken from `.env.sample` and the Supabase S3-compatibility
documentation. All exercised with SigV4 and path-style addressing.

| operation | result |
|---|---|
| create bucket, list buckets | pass |
| put / get / head object | pass, ETag matches MD5 |
| conditional GET (`If-None-Match`) | pass, `304` |
| copy object | pass |
| list objects v2 | pass |
| **multipart create / upload / complete** | pass, 5 MiB + tail assembled |
| multipart abort | pass |
| list multipart uploads | pass |
| **presigned URL (GET)** | pass, `200` |
| **presigned URL (PUT)** | pass, read back |
| **presigned URL expiry enforced** | pass, `403` after expiry |
| range GET | pass |
| delete object | pass |
| object tagging | pass |

**17/17.** The operations in bold are the ones ADR-055 named as the
compatibility risk, and they are the ones that pass. No gap was found that
would change the provider.

## Egress passes through the platform

ADR-056 assumes egress is accountable at the gateway. Measured rather than
assumed, which slice 0 was asked to do:

```
POST /object/sign/<bucket>/<key>
  -> {"signedURL": "/object/sign/<bucket>/<key>?token=eyJhbGciOiJIUzI1NiJ9..."}
```

The signed URL is a **relative path on `storage-api`**, and fetching it with no
`Authorization` header returns `200` with the object body and an `etag`. It is
**not** a redirect to the object store, and no object-store credential is
exposed to the client.

Therefore object egress leaves through `storage-api` and, in the deployed
topology, through the gateway. ADR-056's accounting point is correct, and the
object store's own transfer allowance is close to irrelevant next to the node's
bandwidth.

## ADR-035 containment, confirmed for Storage

ADR-035 was measured for Realtime. It holds identically here, and Storage
depends on it for the same reason:

```
container -> http://10.90.0.1:8333   reached, status 403   (S3 refusing an unsigned request)
container -> http://127.0.0.1:8333   fetch failed          (refused, as required)
```

The container reaches the object store on the data address and cannot reach
node loopback — where a tenant's PostgREST answers anonymous reads to anything
that can open its port. This is what makes ADR-055's "start local, separate
later" cheap rather than merely intended: even a co-located object store is
addressed as though remote, because it cannot be addressed any other way.

## What slice 0 did not measure

Named rather than left silent.

- **Per-tenant cost under sustained load.** All density figures are idle
  tenants. Connection-pool behaviour is the term that could move them.
- **imgproxy on this CPU profile.** Image transformation stays deferred.
- **A non-superuser migration role.** `DB_INSTALL_ROLES=false` was measured
  with the migrating role as superuser. Whether upstream's migrations complete
  under a constrained owner is slice 1's question, and it is the one most
  likely to produce an unwelcome surprise. **Answered by slice 1 — see below.**
- **Anything about RLS policy behaviour.** RLS is on; whether a customer's
  policies gate access the way Supabase's do is slice 5's compatibility work.
- **SeaweedFS durability.** Replication, erasure coding and failure modes were
  not exercised. That is Phase 11's subject.

## Slice 1: the migrations under a constrained owner

Measured 2026-08-24 against the same pinned image, applying all 63 files in
`/app/migrations/tenant` as a role created
`NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION`, owning nothing
but the `storage` schema, holding `CONNECT` on one database and no grant on
`public`. The settings are the ones
`dist/internal/database/migrations/migrate.js` sets before each file.

**62 of 63 pass untouched. One does not, and it is the finding.**

`0011-add-trigger-to-auto-update-updated_at-column.sql` fails with `permission
denied for schema public`. It opens with an **unqualified** `CREATE OR REPLACE
FUNCTION update_updated_at_column()`. Upstream lands it in `storage` because
its own migration 0002 issues `ALTER USER supabase_storage_admin SET
search_path = "storage"` — inside the `DB_INSTALL_ROLES=true` branch this
platform must turn off. Left at the default `"$user", public`, it aims at
`public`, the one schema PostgREST exposes.

The remedy is bootstrap 007's, not a grant:

```sql
ALTER ROLE <storage role> IN DATABASE <db> SET search_path = storage
```

With it, **all 63 pass and `public` gains nothing** — measured as a diff of
relations and functions before and after, because a node carrying `maludb_core`
already has 373 functions there.

Granting `CREATE ON SCHEMA public` instead would have made the migration pass
while dropping a platform function into the customer's Data API namespace. That
is Phase 00 finding 4 in a new place, and it is the reason
`tests/test_object_storage.py` asserts "the migrations succeeded **and** public
is unchanged" rather than only the first half.

### What else the run established

- **Ownership.** Every table upstream creates is owned by the storage role.
  Schema and tables alike; nothing lands on the platform superuser.
- **The schema is narrower in multi-tenant mode than slice 0 recorded.** Eight
  tables, not ten: migration 0038 returns early when `storage.multitenant` is
  true, so `iceberg_namespaces` and `iceberg_tables` are never created. ADR-058
  takes that topology, so those two are a dedicated-mode artefact.
  `buckets_vectors` and `vector_indexes` **are** created, and upstream grants
  `SELECT` on both to all three shared roles.
- **`storage.migrations` is owner-only** without intervention.
- **Two grants are made to whatever role is named `postgres`**, unconditionally:
  migration 0046 grants `ALL ... WITH GRANT OPTION` on `buckets` and `objects`
  to `storage.super_user`, and 0049 does the same to the literal name
  `postgres` if the role exists. On a MaluDB node `postgres` is the platform
  owner and a superuser, so this confers nothing it did not already hold. It is
  recorded because it is a hard-coded role name in a migration, and a
  deployment that ever gives that name to something else inherits a grant it
  did not choose.

### Role switching is how RLS applies at all

`dist/internal/database/postgres/scope.js` issues
`set_config('role', <role from the JWT>, true)` per request — a `SET LOCAL
ROLE` — alongside `request.jwt.claims`. So the storage role must be a member of
`anon`, `authenticated` and `service_role`, and ADR-016's permitted direction
covers it: the shared names are granted *to* the per-tenant role.

Measured on a migrated tenant holding one object and no policies:

| queried as | rows |
|---|---|
| the owner, no role switch | 1 |
| `authenticated` | 0 |
| `anon` | 0 |
| `service_role` | 1 |

The owner bypass is `relforcerowsecurity = false`, and slice 1 kept it
deliberately. Forcing RLS would deny the owner too, and with no policies that
denies `storage-api`'s own bookkeeping — migrations, multipart reaping,
deletion. The service would not run. The control is therefore that the owning
role is not customer-reachable, not that the owner is filtered.

With a policy added, `authenticated` sees its own row and not another
subject's, so the mechanism a migrated application depends on works.

### The gap slice 1 found and did not close

**No customer-reachable role can create a policy on `storage.objects`.**
`CREATE POLICY` requires ownership of the table; privileges are not enough.
Supabase's dashboard can do it because its `postgres` role is a member of
`supabase_storage_admin`. Here the owner is `mldb_<ref>_storage` and nothing a
customer reaches is a member of it — and the storage role itself has no `USAGE`
on `auth`, so even it cannot compile the `auth.uid()` call that essentially
every Supabase storage policy makes.

Enforcement works; authoring does not exist yet. The MaluDB analogue of
Supabase's arrangement would grant `mldb_<ref>_admin` membership in the storage
role, which is owner-level bypass of every storage policy plus write access to
metadata the object store is kept consistent with. That belongs to the slice
that serves the Storage API. Recorded in `specs/compatibility-matrix.yaml` as
`storage_policy_authoring` and carried to slice 4 in the plan.

## Slice 3: the worker, and the measurement ADR-058 was waiting for

Measured 2026-08-24 against `supabase/storage-api:v1.70.6` and **SeaweedFS
4.41** — a different build from slice 0's, deliberately: slice 0 noted that 4.44
had been published the same day it tested it and asked slice 3 to pin something
with age. Pinning a different build makes slice 0's bake-off evidence for
something the platform does not run, so the bake-off was re-run.
`tests/test_object_store.py` is that re-run: **14/14**, including all four
operations ADR-055 named as the provider risk — multipart create/upload/
complete, presigned GET, presigned PUT, and presign expiry actually enforced.

### Per-tenant cost under load, which ADR-058 left open

ADR-058 recorded its density figures as **idle** tenants and said the number
that could move the conclusion was per-tenant connection pooling under load, to
be measured by slice 3 "before the topology is committed to in code". Measured
on one shared instance, cgroup accounting:

| | container | cgroup |
|---|---|---|
| idle, 0 tenants | 143.1 MB | 156.9 MB |
| 1 registered tenant | 144.7 MB | 158.6 MB |
| 4 registered tenants | 146.2 MB | 160.1 MB |
| 8 registered tenants | 146.8 MB | 160.7 MB |
| **8 tenants under concurrent load** | **161.3 MB** | 175.6 MB |
| after the load stopped | 161.3 MB | 175.6 MB |

Load was 8 tenants in parallel, 25 upload-and-download pairs each: **400
successful operations in 5.8 s**, 69 ops/s on a development box.

Two things follow.

- **Registration is nearly free**: ~0.46 MB marginal per idle tenant, which
  confirms slice 0's ~0.7 MB on a second build.
- **Active traffic costs ~1.8 MB per concurrently loaded tenant**, and the
  memory does not come back promptly when the traffic stops — pools stay warm,
  which is what `DATABASE_FREE_POOL_AFTER_INACTIVITY` releases over a longer
  window than this measurement covers.

**The load term does not move ADR-058's decision.** Eight simultaneously busy
tenants cost about 18 MB above idle, against 105.8 MB for a *single* dedicated
instance. The shared topology is still two orders of magnitude cheaper at the
density this platform is built for, and the figure to watch is total concurrency
on a node rather than tenant count.

### What the end-to-end run established

- **The arrangement works.** One shared instance, `MULTI_TENANT=true`, boots in
  ~8 s, migrates its own multitenant database and serves. Two tenants each
  create a bucket named `shared-name` holding a key named `secret.txt`, and each
  reads back its own bytes.
- **ADR-057's key layout, read out of the store rather than inferred**:
  `<project_ref>/<bucket>/<path>/<version>`. Object bytes are outside
  PostgreSQL, which is Phase 10's first acceptance criterion, measured a second
  way.
- **ADR-035 holds for Storage, from inside the container**: it reaches the
  object store on the data address and gets `ECONNREFUSED` for the same store
  addressed on the node's loopback.
- **A token signed for one tenant reaches nothing of another's.**

### Three corrections to what was assumed

1. **`POST /tenants/{id}` is insert-only.** It answers `500` with a primary key
   violation the second time, which makes it wrong for the ordinary case — a
   provisioning retry. `PUT` calls upstream's `upsertTenantAndGenerateJwk` and
   is what the platform uses.
2. **`fileSizeLimit: 0` is a limit of zero bytes, not "unlimited".** Every
   upload answers `413 EntityTooLarge`. There is no sentinel for unlimited, so
   the platform sends a real number and the plan's ceiling is what a customer
   actually meets.
3. **Slice 0's "403 signature verification failed" was reading the body.** The
   **HTTP status is 400**; only the JSON body carries `403`. Together with slice
   0's own note that the object path masks a denial as `404`, the conclusion is
   that Storage authentication failures cannot be counted from HTTP status codes
   at all. That is a monitoring fact, and it is better known now than during an
   incident.

### The measurement slice 2 was waiting for

Slice 2 measured that a customer who can reach `service_role` can rewrite
`storage.objects.metadata->>'size'` and take a 900 MB project to a measured
zero — and that, unlike ADR-040's equivalent admission, re-measuring does not
correct it, because it re-reads the same forged column.

**Closed here.** Where a node has an object store configured, held bytes are
measured from the store, which has no surface a customer can reach; the metadata
sum is the fallback for a node that has none. Asserted directly: the same
forgery, run against a project whose bytes are in the store, changes the
metadata figure to zero and leaves the measured figure — and the `exceeded`
state — intact. An unreachable store falls back rather than reporting zero,
because zero is a claim, and it is the claim that hands a project unlimited
storage.

## Slice 4: durability, and what the two data sets actually agree about

Phase 11 slice 4. Measured against the store this platform runs, on the fixture
`scripts/storage-test-cluster.sh` builds.

### The join, re-measured because a wrong assumption was cheap to hold

The key layout above (`<tenant_id>/<bucket_name>/<object_path>/<version_uuid>`)
was measured in Phase 10. What slice 4 needed and Phase 10 did not record is
what happens to the **old** key when an object changes:

| | key | row |
|---|---|---|
| first upload | `.../f.txt/9f85471c-…` | version `9f85471c-…` |
| overwrite | `.../f.txt/b7886fee-…` **only** | version `b7886fee-…` |
| delete | none | none |

**An overwrite replaces the key.** The previous version's bytes are removed, so
the bucket holds no population of superseded versions, and a reconciliation can
be an exact set difference on the full key. This was worth measuring: a pass
built on the assumption that old versions accumulate would have compared on
`<ref>/<bucket>/<name>` and reported every overwritten object on the platform as
orphaned.

It also settles what object PITR would cost. There is no version history in the
bucket to recover *from* — recovering an object to a point in time would require
turning versioning on, which is a decision this phase does not take.

### Incomplete multipart uploads are invisible

Measured with a real 5 MiB part, uploaded and never completed:

| | |
|---|---|
| `ListObjectsV2` under the prefix | **0 keys** |
| `ListMultipartUploads` under the prefix | **1 upload** |
| fields returned per upload | **`Key`, `UploadId`** |

Two consequences, and the second is the sharper one.

**The bytes are held and counted by nothing.** `object_storage.measure_store_bytes`
lists objects, so the store-side measurement misses them; the quota reads the
tenant's metadata, so that misses them too. A project can hold storage that
neither figure shows.

**They cannot be aged.** The S3 API specifies `Initiated` on a
`ListMultipartUploads` entry and this store does not return it, so an upload
abandoned last week is indistinguishable from one three seconds old. Aborting a
live multipart upload destroys a customer's file mid-write, so the reconciliation
reports them and never touches them. `tests/test_reconcile.py` asserts the
absence of `Initiated` directly, so that if the store ever starts returning one
the decision is revisited rather than silently left in place.

### Durability: one copy

The store as Phase 10 shipped it and as the test fixture builds it runs at
SeaweedFS's default:

```
replication = 000     # datacentre, rack, server copies IN ADDITION to the original
```

**One copy of every customer object.** A single disk loss loses customer files
outright, and unlike the tenant database there is no backup of them to restore
from — slices 1 to 3 cover PostgreSQL and nothing covers the bucket. ADR-069
records the decision; `cp-manage storage durability` reports it, and fails in
production.

The free Apache core does provide replication: `-replication=001` and friends
are core options, and a second copy costs disk rather than a licence. What is
behind the Enterprise line — SeaweedFS says so in its own startup banner — is
**automatic** repair: "data recovery, self-healing storage, customizable erasure
coding, EC vacuum and repair". So a replica that is lost is replaced by an
operator, not by the store, and that is the gap a deployment plans around rather
than a reason to keep one copy.
