# Phase 04 — Auth and RLS

## Objective

Provide the initial Supabase-compatible password authentication surface and use Auth JWTs with PostgREST/RLS.

## Scope

- Per-project Auth process/config.
- Tenant `auth` schema bootstrap, per ADR-018 (`search_path`, function ownership).
- Signing/JWKS design.
- Signup/password sign-in.
- Session refresh/user/signout.
- Email integration per ADR-019: per-project SMTP credentials against the MaluDB relay, sender identity, quota entitlements, bounce/complaint handling.
- Unconfirmed-user retention policy.
- RLS integration tests.

## Prerequisites

Phase 00 proved signup, password sign-in, refresh, get-user, signout, and
`auth.uid()`-driven RLS all work with stock GoTrue 2.195.0 — but only with
`MAILER_AUTOCONFIRM=true`. Email is a blocking dependency here, not a follow-up;
see `docs/EMAIL.md`.

## Acceptance criteria

- [ ] User signs up through official client.
- [ ] User signs in and receives valid session.
- [ ] Authenticated request reaches Data API with correct claims.
- [ ] RLS distinguishes two end users.
- [ ] User in project A cannot authenticate/access project B.
- [ ] Key/session rotation design documented.
- [ ] Signup succeeds with `MAILER_AUTOCONFIRM=false` and a real confirmation email delivered through the relay.
- [ ] Password reset completes end to end.
- [ ] A project cannot send using another project's SMTP credentials.
- [ ] Exceeding the plan email quota is rejected at the relay and surfaced as a quota condition, not a generic failure.
- [ ] Suspending a project immediately revokes its ability to send.
- [ ] Hard bounces and complaints reach the control plane and enter the suppression list.
- [ ] A suppressed address is not mailed again by any project.
