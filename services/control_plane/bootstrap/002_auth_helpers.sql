-- Supabase-compatible auth helpers.
--
-- Migrated RLS policies call these by name, so the signatures are fixed by
-- compatibility rather than by preference.
--
-- They read `request.jwt.claims` (plural, JSON), which is what PostgREST 14
-- sets. GoTrue's own migration ships a version reading the legacy
-- `request.jwt.claim.sub`; its final migration coalesces both, but a partially
-- migrated auth schema can leave the legacy-only form in place, and against
-- modern PostgREST that returns NULL and every policy fails closed. Defining
-- them here means the tenant has a correct version from the start.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(auth.jwt() ->> 'sub', '')::uuid $$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT auth.jwt() ->> 'role' $$;

CREATE OR REPLACE FUNCTION auth.email() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT auth.jwt() ->> 'email' $$;
