# Execution Plan: Operator console (ADR-082)

Status: IN PROGRESS — slice 2 in review  
Human owner: Joseph Lehman  
Agent: Claude Code  
Branch: one per slice, `feat/operator-console-<slice>`  
Related task: none yet — operator tooling; ADR-082  
Dependencies: none in code. Deployment needs an operator VPN reaching the control-plane host.

## Objective

A read-only web console for platform staff — sales, usage, abuse, capacity, provisioning
failures — on its own private listener, with staff accounts that are not customer accounts.

## Scope

- Staff principal: tables, password + mandatory TOTP, server-side sessions, `cp-manage staff`.
- `create_admin_app()` with `ADMIN_ROUTERS` under `/admin/v1`, its own unit and listener.
- A narrowed control-plane database role and a staff key; no KEK in the admin process.
- Read-only report routes over existing functions, and an admin frontend in the same visual
  language as the customer console.
- `staff.view` audit on views of one organization.

## Non-goals

- Any write action (suspend, move, plan change, retry). Each needs an ADR-082 amendment.
- Support access to customer content.
- Customer MFA (can reuse the TOTP verifier later; not decided here).
- An external identity provider.

## Implementation steps (slices, each its own PR)

0. **Staff identity.** Migration: `staff_users`, `staff_sessions`, `staff_mfa_factors`
   (seed encrypted under the staff key). `cp-manage staff create|enrol|revoke|list`.
   Password hashing as ADR-023; TOTP (RFC 6238, ±1 step, replay refused per step).
   Sessions 8 h absolute, 30 min idle. Tests: customer session refused by staff auth and the
   reverse; no staff session without a verified code; replayed code refused.
1. **Admin application and listener.** `create_admin_app()`, `ADMIN_ROUTERS`, sign-in/out
   routes, `deploy/maludb-control-plane-admin.service`, `MALUDB_ADMIN_BIND`,
   `MALUDB_STAFF_KEY_REF`. Surface test (admin routes only on admin app; none on public or
   internal). Import-graph test (no `admin_dsn`, `KeyRing`, provisioning). Preflight: bind is
   private; staff key differs from KEK.
2. **Narrowed database role.** `cp-manage admin-console grant`: the staff tables it signs in with,
   nothing on encrypted or verifier columns; preflight asks the catalogue. Report reads are added
   to the allowlist by slice 3, route by route, each proven by running that route as the role.
3. **Reports.** Routes over `billing.events`, subscriptions by plan/state, storage/egress/email
   per project against ceilings, `abuse_report.report`, capacity and node health, failed
   provisioning jobs. `staff.view` audit on per-organization views. Contract tests: no
   credential or ciphertext field in any response.
4. **Admin frontend.** Static `admin/` served by the admin listener: overview stat cards, sales,
   usage table, abuse queue, nodes, one organization's records. Chromium-verified.
5. **Deployment.** `docs/DEPLOYMENT.md` section, rehearsal on 10.120.0.173 behind the operator VPN.

## Verification

- [ ] Unit/integration tests per slice, including the surface and import-graph tests
- [ ] Tenant-isolation: no admin response carries customer content or secrets
- [ ] Preflight checks for bind address, key separation, role narrowing
- [ ] Browser walk-through of the admin frontend
- [ ] `docs/CONTROL-PLANE.md`, `docs/DEPLOYMENT.md`, `docs/SECURITY.md` updated
- [ ] Security review recorded on every slice

## Risks

- **A staff session reaching a customer route, or the reverse.** Separate tables and token
  formats, and tests in both directions in slice 0.
- **The admin process quietly loading the KEK** because a report helper imports something that
  does. The import-graph test is the control; a report that genuinely needs a secret is an ADR
  change, not a workaround.
- **Reports leaking content by accident** (a `detail_json`, an error message). Response
  contract tests scan for ciphertext, DSNs, key prefixes and SQL.
- **TOTP seed loss** with the staff key: re-enrolment only; documented as break-glass.

## Decision log

- 2026-09-16 — ADR-082 accepted: own staff accounts + TOTP, private listener via VPN, customers
  see only content access, one staff role.

## Progress log

- 2026-09-16 — Plan written.
- 2026-09-16 — Slice 0 built (`feat/operator-console-0-staff-identity`): migration 0051
  (`staff_users`, `staff_mfa_factors`, `staff_sessions`), `services/control_plane/staff.py`
  (Argon2id password + RFC 6238 TOTP checked together; replay guard by step; 5 failures lock
  15 min; sessions 8 h / 30 min idle; token kind `staff`), `StaffKey` refusing KEK material,
  `cp-manage staff create|enrol|password|revoke|list`, `tests/test_staff_identity.py` (26).
  **Security review finding, fixed:** the gateway role's grants are `ALL TABLES` minus a list,
  so the new staff tables would have been writable by a re-granted gateway (operator access
  from the node). Added to `gateway_grants.UNREACHABLE_TABLES`; preflight now checks any
  privilege, not only SELECT; tests for both, the gateway one confirmed to fail without the fix.
- 2026-09-16 — Slice 1 built (`feat/operator-console-1-admin-app`): `admin_main.create_admin_app`
  with `ADMIN_ROUTERS` (health + `/admin/v1/session` sign-in, who-am-I, sign-out);
  `config.AdminConfig`/`load_admin` (own DSN `MALUDB_ADMIN_DATABASE_URL`, staff key, no KEK or
  pepper); staff session tokens peppered from the staff key (`StaffKey.session_pepper`) so the
  console holds one secret; HttpOnly SameSite=Strict Secure cookie on `/admin`; `X-MaluDB-Staff`
  header on state changes; uniform 401; per-client sign-in limit; no-store/DENY/no-referrer
  headers. `deploy/maludb-control-plane-admin.service` (own user, own env file, only the
  `staff-key` credential, port 8113) and `admin-console.env.example`; preflight `operator console`
  check (private bind, staff key != KEK). Tests: `test_admin_app.py`, unit and preflight additions.
  Not deployable until slice 2 gives it a database role; DEPLOYMENT.md §1.7 says so.
- 2026-09-16 — Slice 2 built (`feat/operator-console-2-db-role`): migration 0052
  (`is_admin_console()`, `admin_console_staff_events` INSERT policy on `audit_events`: staff events
  only, no project, no customer actor), `admin_grants` (column allowlist for sign-in; forbidden
  reads of every sealed column and customer verifier; forbidden writes that would make or remake a
  staff credential; superuser/BYPASSRLS), `cp-manage admin-console grant` (refuses a group holding a
  gateway, reporter or memory role), console startup refusal in production, preflight
  `operator console role`. `tests/test_admin_grants.py` runs sign-in end to end as the role in
  production mode. Found by that run: `overlaps()` cannot work from inside the console (row
  policy on `nodes` hides every row), so startup checks only what `pg_roles` answers; and the
  gateway's audit policy needs `nodes(id, gateway_role)` and `projects(id, node_id)` readable,
  confirmed by removing them. Report-table reads deferred to slice 3 (plan step 2 amended).
