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
  likely to produce an unwelcome surprise.
- **Anything about RLS policy behaviour.** RLS is on; whether a customer's
  policies gate access the way Supabase's do is slice 5's compatibility work.
- **SeaweedFS durability.** Replication, erasure coding and failure modes were
  not exercised. That is Phase 11's subject.
