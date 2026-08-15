-- Bookkeeping for tenant bootstrap, inside the tenant database.
--
-- Tracked here as well as on projects.bootstrap_version: the tenant database
-- is the thing being migrated, and it must be able to answer "what version am
-- I" without the control plane. A restore into temporary infrastructure
-- (docs/BACKUP-RECOVERY.md) has no control plane at all.
--
-- Deliberately NOT in `public`: PostgREST exposes public, and platform
-- bookkeeping is not customer API surface.

CREATE SCHEMA IF NOT EXISTS maludb_platform;

REVOKE ALL ON SCHEMA maludb_platform FROM PUBLIC;

CREATE TABLE IF NOT EXISTS maludb_platform.bootstrap_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
