# Accounts, Organizations, and Control-Plane Identity

Who the *platform's own* users are, how they authenticate, and how project
ownership works.

This is distinct from every credential concept in `docs/AUTH.md`. Those all
concern a customer's application and its end users. This document concerns the
people who log in to MaluDB itself.

See ADR-020 and ADR-021.

## The fifth credential concept

`docs/ARCHITECTURE.md` warns against conflating four credential types. There is
a fifth, previously unmodelled:

| # | Credential | Identifies | Lives in |
|---|---|---|---|
| 1 | Project API key | an application/project component | control plane |
| 2 | End-user access token | a signed-in user of a *customer's* app | tenant `auth` schema |
| 3 | Internal service credential | PostgREST/Auth connecting to a tenant DB | control plane |
| 4 | Direct database credential | a paid customer's SQL client | control plane |
| **5** | **Platform user credential** | **a human who logs in to MaluDB** | **control plane** |

A platform user is never a tenant end user. The two must not share a session,
a token format, or a table.

## Model

```text
user  ──< org_member >──  organization  ──<  project
                                │
                                └──  subscription / billing
```

**Projects belong to organizations, not to users** (ADR-020). Every user gets a
personal organization automatically at signup, so a single developer never sees
organizational concepts, but the ownership edge is an org from the first row
written.

This matters because billing, plan entitlements, and team access all attach to
the owning entity. Retrofitting an organization layer later would mean
migrating the ownership of every existing project and every subscription — the
kind of change that is nearly free now and expensive after launch.

`projects.account_id` becomes `projects.org_id`, with a real foreign key.

### Roles

Organization-scoped, deliberately few:

| Role | Can |
|---|---|
| `owner` | everything, including billing, deleting the org, and transferring ownership |
| `admin` | manage projects, members, and API keys; not billing or org deletion |
| `developer` | create and operate projects; cannot manage members or billing |
| `billing` | manage payment and subscriptions only; no project data access |
| `viewer` | read-only visibility of projects and usage |

Project-scoped roles are a deliberate non-goal for now. The membership table is
shaped so a `project_members` table can be added later without migrating
existing rows.

An organization must always have at least one `owner`. The last owner cannot
leave or be demoted; ownership must be transferred first.

## Authentication

Two credential forms, both distinct from project API keys:

**Browser sessions** for the dashboard. Server-side session records with an
opaque token, so revocation is immediate — a stateless JWT cannot be revoked
before expiry, which is the wrong tradeoff for an account that controls
production databases. Sessions record IP and user agent for the security page
and are individually revocable.

**Personal access tokens** for CLI, CI, and API use. Prefixed for
identification, stored hashed exactly as project API keys are
(`docs/SECURITY.md`), scoped, optionally expiring, and shown once at creation.
A PAT carries the permissions of its user in the organizations that user
belongs to, never more.

Passwords are hashed with a memory-hard function. The control-plane API accepts
both forms via `Authorization: Bearer`; `specs/control-plane-api.yaml` defines
the scheme.

### MFA

TOTP, with recovery codes. Structure it now, and require it for the `owner`
role before billing goes live: an account takeover at owner level means both a
customer data breach and payment fraud. Organizations should be able to require
MFA of all members.

### Sign-in protections

Rate limiting per account and per source, lockout with backoff, notification on
new-device sign-in, and re-authentication before sensitive actions — rotating
keys, changing billing, deleting a project, transferring ownership.

## Invitations

Members join by email invitation: single-use token, expiry, a role fixed at
invitation time, and acceptance restricted to the invited address. Invitations
depend on working transactional email (ADR-019); before that existed this flow
could not have been built at all.

Pending invitations must be listable and revocable, and must not grant access
until accepted.

## Do not dogfood platform identity onto tenant infrastructure — yet

It is tempting to run MaluDB's own accounts on a MaluDB project with GoTrue —
it dogfoods the product and makes a good story.

It also creates a circular dependency: signing in requires a project, and
creating that project requires the control plane, which requires signing in. A
platform incident that takes down tenant Auth would simultaneously lock the
operators out of the tooling needed to fix it.

Recommendation (ADR-021): control-plane identity lives in the control-plane
database, independent of tenant infrastructure. Revisit once the platform is
operationally mature and a break-glass path exists that does not depend on
tenant Auth.

## Lifecycle

**User deletion** must handle sole ownership: a user who is the last owner of
an organization holding projects cannot be deleted until ownership is
transferred or the organization is deleted.

**Organization deletion** cascades to projects, and therefore to customer data.
It must follow the same explicit state checks as project deletion
(`specs/provisioning-state-machine.md`) — never an immediate destructive
action.

**Ownership transfer** must be explicit, audited, and require re-authentication.

## Support access

Staff access to a customer organization must be explicit, time-bounded,
audited, and visible to the customer. It must never be an ambient staff
privilege, and it must be distinguishable from customer action in
`audit_events` — which is why `actor_type` exists there.

## Audit

Record at minimum: sign-in success and failure, MFA enrolment and removal,
session and PAT creation and revocation, membership and role changes,
invitations, ownership transfer, billing changes, and support access.

`audit_events.actor_id` can now reference a real user record.

## Open items

Recorded in `docs/OPEN-QUESTIONS.md`:

- Session lifetime and idle timeout.
- Whether MFA is mandatory for all users or only owners.
- Whether SSO/SAML is needed, and at which plan.
- Whether project-scoped roles are needed before general availability.
- Free-tier limits on organizations and members per account.
- Whether a user may belong to unlimited organizations.
