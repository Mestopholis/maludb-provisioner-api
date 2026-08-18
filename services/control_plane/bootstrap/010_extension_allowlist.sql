-- ADR-045: a customer may install an allowlisted extension themselves.
--
-- **The mechanism is not the one ADR-045 described, and the difference is the
-- point.** That ADR said "a SECURITY DEFINER installer that checks the
-- allowlist" -- a function a customer would call instead of writing
-- `CREATE EXTENSION`. Measured 2026-08-18: that defeats the purpose. The whole
-- motivation is that a migrated Supabase schema opens with
--
--     create extension if not exists "uuid-ossp";
--
-- and an installer function only helps if somebody rewrites that line, which
-- means a migration cannot apply a customer's own SQL unaltered. So the check
-- moved to where the DDL already is.
--
-- Two halves, and neither works alone:
--
-- 1. `GRANT CREATE ON DATABASE` to the tenant admin. PostgreSQL 13+ lets a
--    non-superuser holding that install an extension marked `trusted`, and
--    refuses an untrusted one on its own -- verified: `citext` installed,
--    `postgres_fdw` answered "Must be superuser to create this extension".
--    That is upstream's judgement doing the coarse filtering, which criterion 1
--    of specs/extension-allowlist.yaml says is stronger than ours.
-- 2. This event trigger narrows `trusted` to *our* allowlist. `trusted` is set
--    by whoever packaged the extension, so a node with an unreviewed trusted
--    package would otherwise widen what tenants may install without anybody
--    deciding.
--
-- Aborting at `ddl_command_end` rolls the install back -- the same property
-- bootstrap 005 already depends on, verified again here: after a refused
-- `CREATE EXTENSION hstore`, `pg_extension` held no row for it.
--
-- The grant also gives the admin role `CREATE SCHEMA`, which is deliberate:
-- migrated projects have their own schemas, and slice 6 has to recreate them.

CREATE TABLE IF NOT EXISTS maludb_platform.allowed_extensions (
    name       TEXT PRIMARY KEY,
    synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bookkeeping, not customer surface. The schema is already REVOKE ALL FROM
-- PUBLIC (bootstrap 001); this says so about the table too, so a later GRANT on
-- the schema does not quietly hand a customer the list they are checked
-- against. The SECURITY DEFINER function below reads it regardless of this.
REVOKE ALL ON maludb_platform.allowed_extensions FROM PUBLIC;

CREATE OR REPLACE FUNCTION maludb_platform.enforce_extension_allowlist()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    offending text;
BEGIN
    -- **`session_user`, not `current_user`.** Inside a SECURITY DEFINER
    -- function `current_user` is the function's owner -- a superuser, since
    -- bootstrap runs as one -- so a `rolsuper(current_user)` test is
    -- unconditionally true and this trigger would never refuse anything.
    -- Measured 2026-08-18: written that way, a non-allowlisted extension
    -- installed cleanly and the check looked like it was working.
    --
    -- `session_user` is the role that authenticated: the platform's superuser
    -- when provisioning installs maludb_core, and `mldb_<ref>_executor` when a
    -- customer's console statement does. A customer cannot make it a superuser.
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = session_user) THEN
        RETURN;
    END IF;

    -- Joined on `objid` rather than compared against `object_identity`.
    -- `object_identity` is *quoted* where the name needs it, so it renders
    -- `uuid-ossp` as `"uuid-ossp"` and never matches the allowlist -- which
    -- would have refused precisely the extension ADR-045 exists for. Measured
    -- the same day, by running the ADR's own example line.
    --
    -- **The closure, not just the extension named in the command.**
    -- `CREATE EXTENSION x CASCADE` installs x's dependencies and reports only
    -- *x* to this trigger -- measured during the slice 6a security review, with
    -- a synthetic trusted pair: the allowlisted parent installed and dragged an
    -- explicitly refused dependency in with it, whose install script runs as the
    -- bootstrap superuser. Walking `pg_depend` from the new extension catches
    -- them. Criterion 5 of specs/extension-allowlist.yaml keeps this from ever
    -- refusing a legitimate install, by requiring an entry's dependencies to be
    -- listed as well; if the two ever disagree, failing closed is the right way
    -- round.
    WITH RECURSIVE installed AS (
        SELECT c.objid
          FROM pg_event_trigger_ddl_commands() c
         WHERE c.command_tag = 'CREATE EXTENSION'
        UNION
        SELECT d.refobjid
          FROM pg_depend d
          JOIN installed i ON d.objid = i.objid
         WHERE d.classid = 'pg_extension'::regclass
           AND d.refclassid = 'pg_extension'::regclass
    )
    SELECT e.extname INTO offending
      FROM installed
      JOIN pg_extension e ON e.oid = installed.objid
     WHERE NOT EXISTS (
             SELECT 1 FROM maludb_platform.allowed_extensions a WHERE a.name = e.extname
           )
     LIMIT 1;

    IF offending IS NOT NULL THEN
        -- Named, because a refusal a customer cannot understand becomes the
        -- support ticket ADR-045 chose self-service to avoid.
        RAISE EXCEPTION
            'extension "%" is not on this platform''s allowlist', offending
            USING HINT = 'See specs/extension-allowlist.yaml. Ask for a review if you need it.',
                  ERRCODE = 'insufficient_privilege';
    END IF;
END $$;

REVOKE ALL ON FUNCTION maludb_platform.enforce_extension_allowlist() FROM PUBLIC;

-- Fires before `maludb_harden_extensions` (005), which matters: event triggers
-- run in name order, and `maludb_allowlist_...` sorts before `maludb_harden_...`.
-- Refusing an extension outright is the cheaper answer than hardening it first.
DROP EVENT TRIGGER IF EXISTS maludb_allowlist_extensions;
CREATE EVENT TRIGGER maludb_allowlist_extensions
    ON ddl_command_end
    WHEN TAG IN ('CREATE EXTENSION')
    EXECUTE FUNCTION maludb_platform.enforce_extension_allowlist();

DO $$
DECLARE
    admin_role text;
BEGIN
    -- Derived as bootstrap 007 and 008 do: TenantNames builds the database as
    -- mldb_<ref> and the admin role as <database>_admin, from a ref validated
    -- against a strict alphabet.
    admin_role := current_database() || '_admin';

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = admin_role) THEN
        RAISE EXCEPTION 'no admin role % for this tenant', admin_role;
    END IF;

    -- CREATE on the database: what makes a trusted extension installable at
    -- all, and what lets a migrated project recreate its own schemas. Not
    -- ownership of the database, which stays with the platform (ADR-004).
    EXECUTE format('GRANT CREATE ON DATABASE %I TO %I', current_database(), admin_role);
END $$;
