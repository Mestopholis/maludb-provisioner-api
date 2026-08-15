# Email

Transactional email is a hard dependency of Supabase-compatible Auth, not an
optional integration. This document defines what the platform requires, what
the MaluDB mail relay (`malumail`) must provide, and the product decisions that
follow.

Verified 2026-08-15 against Supabase Auth (GoTrue) 2.195.0. See
`tasks/PHASE-00-FEASIBILITY.md` and ADR-019.

## Why this is load-bearing

Three Auth flows have no non-email alternative:

| Flow | Endpoint | Without email |
|---|---|---|
| Signup confirmation | `POST /signup` | user created but unconfirmed; sign-in refused |
| Password reset | `POST /recover` | unrecoverable account, and free tier has no direct SQL to fix it |
| Magic link / OTP | `POST /otp` | flow cannot complete at all |

Password reset is the operationally severe one: without it, a locked-out end
user of a customer's application is unrecoverable through any supported
interface.

## Verified GoTrue behavior

**With no SMTP configured, GoTrue fails silently.** Signup returns HTTP 200,
stamps `confirmation_sent_at` as though mail was sent, leaves `confirmed_at`
NULL, creates the user row, and logs nothing. Sign-in then fails with
`400 email_not_confirmed`. `POST /recover` and `POST /otp` both return 200
having done nothing, and `/otp` creates a user record for an address that
receives nothing. The platform gets no signal at any point; the failure appears
only as a tenant's end users being unable to log in.

**With SMTP configured, failures are loud and transactional.** Pointing GoTrue
at an unreachable relay produced:

```text
HTTP 500  {"error_code":"unexpected_failure","msg":"Error sending confirmation email"}
log       level=error  error="dial tcp 127.0.0.1:2525: connect: connection refused"
```

and, importantly, **no user row was created** — the mail failure rolled the
signup back. Configuring a working relay therefore fixes both the silent-failure
mode and the orphaned-unconfirmed-user problem in one step.

The dangling-user concern applies only to the unconfigured case.

## Transport

GoTrue speaks **SMTP**. The 2.195.0 binary embeds a `net/smtp` client
(PLAIN and CRAM-MD5); no HTTP send-email hook was found in this build.

Decision: **`malumail` must expose an authenticated SMTP submission endpoint.**
Adding an SMTP frontend to the relay is substantially less work than shimming
an HTTP API into every per-project Auth worker, and it keeps stock upstream
GoTrue — required by the compatibility rule in `AGENTS.md` that upstream
software be preferred over MaluDB-specific alternatives.

Confirmed working configuration keys:

```text
GOTRUE_SMTP_HOST          GOTRUE_SMTP_USER          GOTRUE_SMTP_ADMIN_EMAIL
GOTRUE_SMTP_PORT          GOTRUE_SMTP_PASS          GOTRUE_SMTP_SENDER_NAME
```

Because each active project has its own Auth process (ADR-007), each project
can hold its own SMTP credentials with no additional machinery.

## Requirements on `malumail`

### R1 — Authenticated SMTP submission

Submission on 587 with STARTTLS (or 465 implicit TLS), PLAIN auth over TLS.
Must not be an open relay: unauthenticated submission refused, and the relay
reachable only from platform networks.

### R2 — Per-project credentials

Every project gets its own SMTP username and generated password. This is the
key benefit of owning the relay: it makes the relay the enforcement point for
attribution, quota, and revocation, rather than trusting per-worker config.

Credentials must be revocable immediately on project suspend or delete.

### R3 — Per-project quota enforcement at the relay

The relay enforces send quotas per credential, from plan entitlements. Gateway
and worker limits are advisory by comparison; the relay is the only place a
count is authoritative. Exceeding quota must return a distinguishable SMTP
error so the control plane can surface it as a quota condition rather than a
generic failure.

New projects start under a low warm-up cap that rises with account age and
verification, to blunt drive-by abuse.

### R4 — Per-project sender identity with DKIM

Two tiers:

- **Default**: platform-owned sender on a dedicated subdomain, per project —
  `noreply@<project-ref>.mail.maludb.com` or equivalent. Use a subdomain
  reserved for transactional auth mail so a blocklisting cannot affect
  corporate or marketing mail on the apex domain.
- **Custom domain** (paid): the customer's own sending domain, gated on DNS
  verification. The relay signs DKIM per domain and the control plane tracks
  verification state.

### R5 — Separate IP pools for free and paid

Mirror the node-pool model in `docs/ARCHITECTURE.md`. Free-tier sending must
not share sending IPs with paid-tier sending, so free-tier abuse cannot damage
paid deliverability.

This is the counterweight to owning the relay: a self-operated relay means the
platform owns its IP reputation outright, with no shared-pool cushion and no
vendor to escalate a blocklisting to. Pool separation and R3 are what make that
risk manageable.

### R6 — Bounce and complaint feedback

Relay acceptance is not delivery. The relay must report asynchronous outcomes —
hard bounce, soft bounce, complaint — back to the control plane by webhook,
with the project and recipient attributed.

Required handling:

- hard bounce → mark the address undeliverable, stop retrying;
- complaint → suppress the address and flag the project for review;
- global suppression list, consulted before every send, so a hard-bounced or
  complaining address is never mailed again by any project.

### R7 — Per-project observability

Sent, delivered, bounced, complained, and quota-rejected counts per project, so
they can drive the dashboard, plan design, and abuse detection.
`docs/OBSERVABILITY.md` has no email dimension today; it needs one.

## Product decisions

### Email confirmation is on by default

With a working relay there is no reason to run `GOTRUE_MAILER_AUTOCONFIRM=true`
in production. Autoconfirm accepts any address without proving control of it,
which permits account squatting on other people's addresses.

`GOTRUE_MAILER_ALLOW_UNVERIFIED_EMAIL_SIGN_INS` exists in the binary and is a
legitimate choice for a deliberately email-free tier — but it must be an
explicit, documented per-project entitlement, never a default.

### Unconfirmed user retention

Even with a working relay, users who never confirm accumulate and hold the
`UNIQUE` constraint on `auth.users.email`, which can block the legitimate owner
of an address from signing up later. A retention policy is required: purge
unconfirmed users after a configured interval. The interval is a plan/product
decision, not a technical one.

### Email is a metered resource

Email volume belongs in `specs/plans-and-limits.yaml` alongside API and storage
limits, and in the same entitlement model — never as a hard-coded per-tier
check (`docs/BILLING-AND-PLANS.md`).

## Abuse

A free tier that can send mail from platform-controlled domains is an
attractive spam and phishing vector, and the damage lands on shared
infrastructure. Controls, in order of effectiveness:

1. relay-enforced per-project quotas with new-project warm-up caps (R3);
2. free/paid IP pool separation (R5);
3. complaint-rate monitoring per project, with automatic suspension of sending
   — *sending only*, never automatic data destruction, per
   `docs/RESOURCE-GOVERNANCE.md`;
4. signup velocity limits per account and per source;
5. content and volume anomaly detection at the relay.

This belongs in the abuse/AUP work that `docs/OPEN-QUESTIONS.md` still lists as
unresolved.

## Migration

Migrating a Supabase project brings users whose confirmation state must be
preserved — a confirmed Supabase user must not be forced to re-confirm on
MaluDB. And password reset must work from the moment of cutover, or the
customer's users are stranded on an application they cannot recover access to.
See `docs/MIGRATION-FROM-SUPABASE.md`.

## Open items

- Exact quota values per plan.
- Unconfirmed-user retention interval.
- Whether custom sending domains are a paid feature or available on all tiers.
- Template customization: platform-default templates, per-project overrides, or
  both.
- Whether the relay or the control plane owns the global suppression list.
