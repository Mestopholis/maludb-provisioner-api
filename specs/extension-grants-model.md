# Extension function grants — what a PostgREST pre-request check can and cannot do

Grants slice 0 of `plans/active/extension-function-grants.md` (ADR-076).
Measured 2026-09-12/13. Reproduce with `scripts/spike-extension-grants.py run`.

## Where it was measured

The development node's test cluster (PostgreSQL 17.10, `maludb_core` 0.104.0,
`vector` 0.8.4) and PostgREST 14.17. One throwaway tenant, provisioned through
`provisioning` and `tenant_bootstrap` with direct access enabled, plus
`uuid-ossp` installed after bootstrap. PostgREST ran from the platform's own
`workers.render_config` output, with `db-pre-request` appended for the check.

ADR-076 decision 4 was applied **by hand, on that tenant only**: `EXECUTE` on every
non-`maludb_core` extension function (386) granted to `anon`, `authenticated`,
`service_role` and the tenant's `admin`, `client` and `executor`. `public` then
held 344 distinct extension function names.

Customer objects: a `docs` table with `id uuid DEFAULT uuid_generate_v4()` and a
`vector(3)` column; `match_documents(query vector(3), k int)` ordering by `<=>`;
`request_settings()` returning what the request can see; `bump()` calling
`nextval` on a sequence; and `similarity(t text)`, sharing a name with pg_trgm's
`similarity(text, text)`.

## Findings

### 1. With `EXECUTE` restored, the Supabase patterns work

| As | Request | Result |
|---|---|---|
| `anon` | `POST /rpc/match_documents` (vector `<=>`) | 200, rows |
| `authenticated` | `POST /rpc/match_documents` | 200, rows |
| `authenticated` | `POST /docs`, `id` from `uuid_generate_v4()` | 201 |

Each of these is refused on a tenant provisioned today (pinning slice 0, finding 7).

### 2. What PostgREST exposes once `anon` can execute — before any check

- **OpenAPI:** 22 RPC paths, **19 of them extension function names**, identical
  for `anon`, `authenticated` and `service_role`: `armor`, `dearmor`,
  `gen_random_uuid`, `gen_salt`, `pgp_armor_headers`, `pgp_key_id`, `show_limit`,
  `show_trgm`, `similarity`, `uuid_generate_v1`, `uuid_generate_v1mc`,
  `uuid_generate_v3`, `uuid_generate_v4`, `uuid_generate_v5`, `uuid_nil`,
  `uuid_ns_dns`, `uuid_ns_oid`, `uuid_ns_url`, `uuid_ns_x500`. The other 325 names
  take unnamed arguments PostgREST cannot bind.
- **Reachable over `/rpc` as `anon`: 14.** Twelve returned 200 — `gen_salt` among
  them, answering `$2a$06$…` to a text body of `bf`, which is ADR-018's finding
  exactly. Two more, `dearmor` and `pgp_armor_headers`, returned 500 with
  `39000 Corrupt ascii-armor`: they ran, on bad input. 674 attempts were 404.

The count grows with every extension a customer installs under ADR-045 that has
callable shapes; 19 is the provisioning set plus `uuid-ossp`.

### 3. The pre-request function sees what it needs

`request.path` and `request.method` are set before the pre-request function runs:
`/rpc/request_settings` for both `POST` and `GET`. `request.headers` carries no
`content-profile` for a default-schema request.

### 4. The check refuses before the function runs, and nothing else changes

With `db-pre-request = "maludb_platform.refuse_extension_rpc"` in the file and
the function raising `PT403`:

| Request | Result |
|---|---|
| `/rpc/gen_salt` as `anon`, text body | 403 `PT403` function gen_salt is not available over the Data API |
| `/rpc/gen_salt` as `service_role` | 403, same |
| `/rpc/dearmor` as `anon` | 403, same |
| every name reachable in finding 2 | refused; none callable |
| customer `match_documents` as `anon` | 200 |
| `GET /docs` as `anon` | 200 |
| OpenAPI root as `anon` | 200 |

**The refused function's body does not run.** A check variant that also refused
`bump`: the sequence stood at 1 before the refused call and at 1 after; the
control call without the refusal returned 2 and moved it to 2. Sequences are not
transactional, so a body that ran and was rolled back would still have moved it.

**Error shape.** `PT403` gives 403 with PostgREST's standard error body. `RAISE
SQLSTATE 'PGRST'` with a JSON message and `{"status": 404}` detail gives a 404
with a platform code (`MLDB404`). Either is a clean 4xx naming nothing internal.

**Cost:** median `/rpc` latency 3.33 ms without the check, 3.87 ms with — about
half a millisecond, on RPC requests; other paths return at the first comparison.

### 5. Matching "every function of that name" leaves a bypass; "any" closes it

ADR-076 decision 2 as written refused a name only when **every** function of it
is extension-owned, so a customer's own function sharing the name stayed
callable. Measured consequence: after the tenant admin creates
`public.gen_salt(integer)` and grants it to `anon`:

| Rule | `/rpc/gen_salt`, text `bf` | `/rpc/gen_salt`, `{"n": 1}` |
|---|---|---|
| every | **200 `$2a$06$…`** — pgcrypto's `gen_salt(text)` | 200 `mine` |
| any | 403 | 403 |

Under "every", defining any function that shares a name re-exposes the
extension's overloads of that name on the customer's own Data API — `crypt()` at
a high cost included, on a shared node. **Decided after this measurement: "any"**
(ADR-076 decision 2 as amended). Its cost is also measured: the customer's own
`similarity(t)` is refused with the same 403, and renaming it is the remedy.
pg_trgm's two-argument `similarity` was never reachable (404 `PGRST202`).

### 6. The customer cannot remove the check

As `mldb_<ref>_executor` and as `mldb_<ref>_client`, each in `SET ROLE
mldb_<ref>_admin` — the path the SQL console and direct connections take — all 16
attempts were refused with `42501`:

| Attempt | Refusal |
|---|---|
| `ALTER ROLE <authenticator> IN DATABASE … SET pgrst.db_pre_request` | permission denied to alter role |
| `ALTER ROLE <authenticator> SET pgrst.db_pre_request` | permission denied to alter role |
| `ALTER DATABASE … SET pgrst.db_pre_request` | must be owner of database |
| `CREATE OR REPLACE`, `DROP`, `RENAME` the check | permission denied for schema maludb_platform |
| `REVOKE EXECUTE` on the check from `anon` | permission denied for schema maludb_platform |
| `CREATE FUNCTION` in `maludb_platform` | permission denied for schema maludb_platform |

The check still refused `gen_salt` afterwards.

### 7. In-database configuration overrides the file

Set **as superuser**, `ALTER ROLE <authenticator> IN DATABASE … SET
pgrst.db_pre_request = ''` followed by a config reload **disabled the check**:
`/rpc/gen_salt` answered 200 with a salt. `RESET` restored the refusal.

The customer cannot do this (finding 6), but the platform can by accident: ADR-074
already writes `pgrst.db_schemas` into exactly that place for the data-model
graph. So `tenant_bootstrap.verify`, and the fleet run's per-tenant check, must
assert **no in-database `pgrst.db_pre_request` for the authenticator**, and no
code path may write one.

## What this settles, and changes

- **No stop condition is met.** The check refuses before the call runs, as a
  clean 4xx, and the customer cannot remove it.
- **ADR-076 decision 2 is amended to "any"** (finding 5).
- **ADR-076 decision 3 is closed: the 19 are accepted** (finding 2) — each listed
  path answers 403 with a message saying why.
- **Grants slice 1** verifies the absence of an in-database pre-request override
  (finding 7), refuses with `PT403` (403, PostgREST's standard body, honest about
  a listed path that exists but is not served), and tells customers that an RPC
  sharing an extension function's name is refused.
