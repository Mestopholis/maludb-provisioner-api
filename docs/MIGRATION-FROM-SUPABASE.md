# Migration from Supabase

## Product goal

Migration is a first-class product capability, even if it is not the first implementation phase.

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

## Migration domains

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
- supported extensions;
- publications/replication configuration as applicable.

### Auth

- users;
- identities;
- compatible password hashes where possible;
- metadata;
- confirmation state;
- provider configuration where supported.

### Storage

- buckets;
- object metadata;
- object bytes;
- policies.

### Realtime

- required publications/configuration.

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

## Cutover

Zero/minimal-downtime migration is a later objective. Initial migration may require a controlled write freeze during final sync/cutover.

Do not claim zero-downtime until implemented and tested.
