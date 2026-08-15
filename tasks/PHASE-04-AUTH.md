# Phase 04 — Auth and RLS

## Objective

Provide the initial Supabase-compatible password authentication surface and use Auth JWTs with PostgREST/RLS.

## Scope

- Per-project Auth process/config.
- Tenant `auth` schema bootstrap, per ADR-018 (`search_path`, function ownership).
- Signing/JWKS design.
- Signup/password sign-in.
- Session refresh/user/signout.
- Email integration per ADR-019 as amended by ADR-029: GoTrue's Send Email Hook to the MaluMail REST API, two sender modes, sender identity, quota entitlements, bounce/complaint handling.
- Unconfirmed-user retention policy.
- RLS integration tests.

## Prerequisites

Phase 00 proved signup, password sign-in, refresh, get-user, signout, and
`auth.uid()`-driven RLS all work with stock GoTrue 2.195.0 — but only with
`MAILER_AUTOCONFIRM=true`. Email is a blocking dependency here, not a follow-up;
see `docs/EMAIL.md`.

## Carried from Phase 03

- **GoTrue must reuse the project's existing JWT signing secret.** Phase 02 recorded the
  signing key as landing here; Phase 03 slice 2 provisioned it early because PostgREST needs
  it to start. It is stored as `project_credentials.credential_type = 'jwt_signing'`,
  envelope encrypted. Generating a second one would produce a project whose own Auth tokens
  its own Data API rejects, which is the failure this note exists to prevent.
- **`/auth/v1` is routed but unimplemented.** The gateway answers 404 with "this API surface
  is not available yet" (`services/gateway/app.py`). Implementing Auth means adding the
  prefix to the routing table and starting a per-project worker, not inventing routing.
- **The gateway forwards an end-user JWT untouched**, because verifying it is PostgREST's
  job. Phase 04 is what starts producing those tokens; the forwarding path is already tested.
- A dedicated provisioning superuser would be cleaner than reusing `postgres`. Carried since
  Phase 02.
- `maludb_core` hard-codes `public.gen_random_bytes`, which is why extensions cannot be
  relocated to their own schema (ADR-018). Still to be raised upstream; it is the root cause
  the revoke works around.

## Acceptance criteria

- [ ] User signs up through official client.
- [ ] User signs in and receives valid session.
- [ ] Authenticated request reaches Data API with correct claims.
- [ ] RLS distinguishes two end users.
- [ ] User in project A cannot authenticate/access project B.
- [ ] Key/session rotation design documented.
- [ ] Signup succeeds with `MAILER_AUTOCONFIRM=false` and a real confirmation email delivered through the relay.
- [ ] Password reset completes end to end.
- [ ] A project cannot send mail attributed to another project. Reworded from "another
      project's SMTP credentials" per ADR-029: on `platform_default` there are no
      per-project credentials to misuse, so the property is that the hook sends as the
      project whose secret signed the request; on `custom_domain` it is additionally that
      one project's MaluMail key is never used for another.
- [ ] Exceeding the email quota is surfaced as a quota condition, not a generic failure.
      On `platform_default` the platform enforces the plan entitlement *before* calling,
      because the account's allowance is shared; on `custom_domain` MaluMail enforces it
      and the `429` is surfaced rather than swallowed.
- [ ] Suspending a project immediately stops it sending. Reworded from "revokes its
      ability to send" per ADR-029: on `custom_domain` the key belongs to the customer
      and the platform cannot revoke it, so the guarantee is that nothing the platform
      originates will send — which is all of Auth mail.
- [ ] Hard bounces and complaints reach the control plane and enter the suppression list.
- [ ] A suppressed address is not mailed again by any project.
