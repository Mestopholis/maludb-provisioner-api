# Supabase Compatibility Tests

This directory will contain black-box tests using official Supabase client libraries where practical.

The suite should support target configuration such as:

```text
TARGET=supabase
TARGET=maludb
```

Test code must never commit live credentials.

The compatibility matrix is `specs/compatibility-matrix.yaml`.
