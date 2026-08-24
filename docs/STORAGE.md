# Storage

## Status

**Phase 10, planned 2026-08-22 — not yet built.** See
`plans/active/phase-10-storage.md` for the execution plan and
`tasks/PHASE-10-STORAGE.md` for the acceptance criteria.

Deferred from the first compatibility milestone, and deferred deliberately:
`services/migrate/rules.py` turns away every Supabase project that uses Storage
today, and says so by name.

## Design direction

Object bytes live in object storage, never as large byte payloads inside tenant
database tables. That is Phase 10's first acceptance criterion.

**The object store is SeaweedFS** (ADR-055), reached over the S3 API. Apache
2.0 rather than AGPL, actively developed, and a single Go binary for the S3
gateway. MinIO was the obvious candidate and was rejected: its community
edition was archived on 25 April 2026 — no binaries, no security patches —
which is disqualifying for the component holding every customer's files. Garage
is ruled out by AGPL for a commercial platform; Ceph RGW would be right on a
cluster already running Ceph and is disproportionate otherwise.

Bytes live on the existing Proxmox hardware initially, not on a dedicated
storage box. The exit to dedicated hardware is stated in ADR-055 and stays
cheap for a structural reason rather than a hopeful one: ADR-035 already
forbids a rootless Podman container from reaching node loopback, so
`storage-api` addresses the object store on a data address whether it is one
hop away or one datacenter away. Moving it is an endpoint change and a copy,
with no platform code touched.

**Tenancy is one platform bucket, keyed by tenant** (ADR-057). `storage-api`'s
`STORAGE_S3_BUCKET` is singular; a customer "bucket" is a row in the tenant
database's `storage.buckets`. The consequence matters more than the mechanism:
tenant isolation for objects is a property of the metadata layer and the
worker's credential scoping, **not of the object store**, and has to be tested
as a denial rather than assumed.

## Requirement, and how it is met

> Storage implementation must not couple the tenant-database lifecycle to one
> specific object-storage vendor.

This is met by configuration rather than by code. `storage-api` selects its
backend with `STORAGE_BACKEND=s3|file` and addresses an S3 one with
`STORAGE_S3_ENDPOINT`, `STORAGE_S3_REGION`, `STORAGE_S3_FORCE_PATH_STYLE` and
`STORAGE_S3_BUCKET`.

**The platform therefore writes no provider abstraction of its own.** A
MaluDB-side driver interface above an existing one would be owned by us, tested
by us, and would add nothing. `tasks/PHASE-10-STORAGE.md` opens its scope with
"object-store provider abstraction", and the correct implementation of that
bullet is an environment variable and this paragraph.

Provisioning makes no object-store API call, which is what actually keeps the
tenant lifecycle decoupled: a project can be created while the object store is
unreachable.

## Compatibility goals

Phase 10 targets exactly the task file's list. Each is `deferred` in
`specs/compatibility-matrix.yaml` today and moves to `supported` only with a
`verified_by` test behind it — `AGENTS.md` does not permit the claim ahead of
the test.

- buckets;
- upload;
- download;
- delete;
- signed URLs;
- RLS-compatible authorization;
- storage metadata.

Deferred past Phase 10, with matrix entries saying so rather than silence:
resumable/TUS uploads, image transformation (which needs a second pinned
container and carries ADR-033's no-AVX2 hazard), and the S3 protocol endpoint
(a credential and a reachable port, so ADR-039's paid line and its own
decision).

## Limits

Storage is available on every tier including free, bounded by hard ceilings
under ADR-050 (ADR-056): `object_storage_bytes` and `egress_bytes_per_month`,
enforced at the point of use, never converted into a charge and never reported
to any provider. Numbers live in `specs/plans-and-limits.yaml`, not in code.

Egress is accounted at the gateway, because that is where the bytes pass:
Supabase serves a signed URL through `storage-api` rather than redirecting to
the object store. Phase 10 slice 0 confirms that before slice 4 depends on it —
if a signed URL turns out to redirect, egress leaves without passing the gateway
and this paragraph is wrong rather than merely imprecise.

## Notes for Phase 11

SeaweedFS's Apache 2.0 core covers everything Phase 10 needs. Automatic
erasure-coding repair, EC vacuum, self-healing and point-in-time recovery sit
behind a per-TB Enterprise licence, free under 25 TB for development and test.
Those are durability features, so they belong with backups, restore and PITR
rather than here — recorded so the question arrives as a decision rather than a
discovery.
