# Product Vision

## What MaluDB Platform is

MaluDB Platform is a managed production database platform built around MaluDB, a PostgreSQL-compatible memory database with additional functionality.

The commercial wedge is Supabase compatibility:

> Make it easy for a Supabase application to move to MaluDB for production without forcing the developer to rewrite the application first.

The long-term differentiation is MaluDB itself.

## Product sequence

### Stage 1 — Supabase-compatible foundation

Support the most important Supabase-compatible interfaces needed by production applications.

### Stage 2 — Migration product

Make moving an existing Supabase project to MaluDB predictable, testable, and low-friction.

### Stage 3 — MaluDB-native advantage

Expose MaluDB memory/database functionality that applications can adopt incrementally after migration.

## Core promise

For supported features, the target migration experience is:

1. migrate database/auth/storage state as applicable;
2. switch project URL and API key;
3. run compatibility validation;
4. continue operating the application;
5. optionally adopt MaluDB-specific features.

## What this product is not

- It is not a VM-per-customer hosting platform.
- It is not a container-per-customer database platform.
- It is not initially a ground-up reimplementation of PostgREST, Supabase Auth, or Realtime.
- It is not allowed to sacrifice compatibility merely to expose a MaluDB feature faster.
