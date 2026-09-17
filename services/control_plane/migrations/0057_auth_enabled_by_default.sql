-- ADR-084: Auth is on for every project; its worker still starts only when Auth is used.
--
-- `auth_enabled` defaulted to FALSE (0009) and nothing outside the test suite set it, so the
-- gateway answered 404 on `/auth/v1` for every project a customer created. Supabase clients
-- expect Auth to be there. What ADR-022 protects -- the 17.6 MB worker not running for projects
-- that do not use Auth -- is kept by the gateway starting the worker on the first Auth request
-- and the node's maintenance pass sleeping it when idle (ADR-083), not by this flag.
ALTER TABLE projects ALTER COLUMN auth_enabled SET DEFAULT TRUE;

UPDATE projects SET auth_enabled = TRUE WHERE auth_enabled = FALSE AND deleted_at IS NULL;
