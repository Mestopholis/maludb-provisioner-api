# Storage

## Status

Deferred from the first compatibility milestone.

## Design direction

Keep object bytes in object storage, not large byte payloads inside tenant database tables.

Potential providers to evaluate later:

- S3-compatible service;
- Cloudflare R2;
- MinIO;
- Ceph/object storage;
- other operationally appropriate provider.

No provider is selected yet.

## Compatibility goals

Eventually support the Supabase Storage client surface needed by migrated applications:

- buckets;
- upload;
- download;
- delete;
- signed URLs;
- RLS-compatible authorization;
- storage metadata.

## Requirement

Storage implementation must not couple the tenant-database lifecycle to one specific object-storage vendor.
