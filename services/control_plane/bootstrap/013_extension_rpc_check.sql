-- ADR-076 decision 2: keep extension functions off the Data API with a
-- PostgREST pre-request check, rather than by withholding EXECUTE.
--
-- On its own this file changes nothing a customer can observe. It creates the
-- check; a worker only names it in `db-pre-request` once this version is
-- recorded against the project (`workers.start_worker`), and the grants that
-- make the check necessary are bootstrap 014, which `tenant_bootstrap.apply`
-- holds back until the check is live. Split this way because either order in a
-- single file is wrong on a serving tenant: a config naming a function that
-- does not exist breaks every request, and grants without the check hand `anon`
-- /rpc/gen_salt again.
--
-- Measured in grants slice 0 (`specs/extension-grants-model.md`):
--
-- * PostgREST 14.17 sets `request.path` before the pre-request function runs,
--   and a raised PT403 answers 403 before the RPC's body executes -- a
--   non-transactional sequence the refused function would have advanced did
--   not move.
-- * **Any**, not every. Refusing a name only when every function of it is an
--   extension's let a customer-created `public.gen_salt(integer)` make
--   pgcrypto's `gen_salt(text)` callable again. So a name is refused if any
--   function of it, in the schema the request targets, belongs to an extension.
--   A customer RPC named like one is refused too, and renamed.
--
-- Its own schema rather than `maludb_platform`: the request roles must hold
-- USAGE on wherever the check lives, and `maludb_platform` holds the bootstrap
-- ledger, which is asserted unreachable by them. `maludb_guard` holds this one
-- function and nothing else, which a test lists.

-- The tenant admin holds CREATE ON DATABASE (bootstrap 010), so on a tenant
-- provisioned before this file a customer could already own a schema by this
-- name -- and the owner of a schema can drop and replace what is in it. Refused
-- by name rather than left to `CREATE SCHEMA`'s own error, so the failure says
-- what to do about it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'maludb_guard') THEN
        RAISE EXCEPTION 'a schema named maludb_guard already exists in %; it is reserved for '
                        'the platform''s Data API check (ADR-076) and must be renamed or dropped '
                        'before bootstrap can continue', current_database();
    END IF;
END $$;

CREATE SCHEMA maludb_guard;
REVOKE ALL ON SCHEMA maludb_guard FROM PUBLIC;
GRANT USAGE ON SCHEMA maludb_guard TO anon, authenticated, service_role;

-- SECURITY INVOKER: it reads only pg_proc, pg_namespace and pg_depend, which
-- every role can read, and a definer function reachable by anon is a larger
-- thing to get wrong than this needs. search_path pinned so nothing in the body
-- resolves through a schema the tenant controls.
CREATE FUNCTION maludb_guard.refuse_extension_rpc()
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    path    text := current_setting('request.path', true);
    headers jsonb;
    target  text;
    fn      text;
BEGIN
    IF path IS NULL OR left(path, 5) <> '/rpc/' THEN
        RETURN;
    END IF;

    -- `request.path` is the path as the client sent it; PostgREST decodes it
    -- before resolving the function. Found in the grants slice 1 security
    -- review: `/rpc/gen%5Fsalt` answered pgcrypto's salt past a check that
    -- compared the raw text. So the name is percent-decoded here the same way,
    -- and slashes around it dropped. A path that does not decode to valid UTF-8
    -- cannot name a function, and is refused rather than waved through.
    BEGIN
        fn := convert_from(
            (SELECT string_agg(
                        CASE WHEN m[1] IS NOT NULL THEN decode(m[1], 'hex')
                             ELSE convert_to(m[2], 'UTF8') END,
                        ''::bytea ORDER BY ord)
               FROM regexp_matches(substr(path, 6), '%([0-9A-Fa-f]{2})|([^%]+|%)', 'g')
                    WITH ORDINALITY AS t(m, ord)),
            'UTF8');
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'this request path cannot be checked'
            USING ERRCODE = 'PT403';
    END;
    fn := btrim(coalesce(fn, ''), '/');

    -- PostgREST resolves an RPC in the schema the request names, defaulting to
    -- the first exposed schema. Checked in that schema and in `public`, so a
    -- profile header cannot steer the lookup somewhere empty while the call
    -- lands on `public`.
    headers := nullif(current_setting('request.headers', true), '')::jsonb;
    target := coalesce(headers ->> 'content-profile', headers ->> 'accept-profile', 'public');

    IF EXISTS (
        SELECT 1
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_depend d ON d.classid = 'pg_proc'::regclass
                          AND d.objid = p.oid
                          AND d.deptype = 'e'
         WHERE p.proname = fn
           AND n.nspname IN ('public', target)
    ) THEN
        RAISE EXCEPTION 'function % is not available over the Data API', fn
            USING ERRCODE = 'PT403',
                  HINT = 'Functions installed by an extension can be used from SQL but are not '
                         'exposed as RPC; a function of your own with the same name must be renamed.';
    END IF;
END $$;

REVOKE ALL ON FUNCTION maludb_guard.refuse_extension_rpc() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION maludb_guard.refuse_extension_rpc() TO anon, authenticated, service_role;
