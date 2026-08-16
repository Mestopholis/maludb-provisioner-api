# Control Plane

## Responsibilities

The control plane is the product-specific software MaluDB must own.

It manages:

- users/accounts (see `docs/ACCOUNTS.md`);
- organizations, from day one — not deferred (ADR-020);
- organization membership, roles, and invitations;
- platform sessions, personal access tokens, and MFA factors;
- projects;
- plans;
- MaluDB nodes;
- project placement;
- project lifecycle;
- API keys;
- service worker assignments;
- usage;
- billing/subscriptions later;
- backups later;
- audit history.

## Core entities

### Project

Suggested attributes:

- id;
- org_id (ADR-020; replaces the earlier unconstrained account_id);
- project_ref;
- display_name;
- plan_id;
- status;
- node_id;
- database_name;
- created_at;
- upgraded_at;
- suspended_at;
- deleted_at.

### Node

- id;
- hostname;
- internal_address;
- public/direct DB endpoint metadata if applicable;
- state;
- node_pool;
- Postgres/MaluDB version;
- total resources;
- observed resource metrics;
- tenant count;
- last health check.

### API key

- id;
- project_id;
- type: publishable/secret;
- public prefix/identifier;
- verification material;
- created_at;
- revoked_at;
- last_used_at where practical.

### Provisioning job

- id;
- project_id;
- state;
- attempt;
- error_code;
- error_detail;
- started_at;
- updated_at;
- completed_at.

## Two applications (ADR-037, ADR-038)

The control plane serves two applications built from the same routers, on separate listeners:

| | Factory | Mounts | Bound to |
|---|---|---|---|
| **Public** | `create_public_app` | `PUBLIC_ROUTERS` only | a public interface |
| **Internal** | `create_app` | every router, including the private ones | a private interface |

A router reaches the internet by being named in `PUBLIC_ROUTERS` and in no other way, so the failure mode of forgetting is a route that is unreachable from outside rather than one that is reachable and should not be. `tests/test_control_plane_surfaces.py` asserts the public application's served paths against a written-out list rather than against the tuple itself, so moving a router by mistake fails the suite instead of moving the expectation with it.

`specs/control-plane-api.yaml` is generated from the **public** application: it is the contract a customer's client is written against. Internal routes are served on the other listener and deliberately absent from it.

**The public application must not be able to obtain a node's superuser credential.** `nodes.admin_dsn()` unwraps one with the KEK, and the control plane holds the KEK because project credentials need it — so this is a property of what the code can reach rather than of what today's handlers call, and it is asserted from the import graph. Provisioning therefore runs in a worker (ADR-038), and a public route that needs node work changes the ADR before it changes the code.

## Rate limits on the control plane's own routes

Signup is public at launch, so the routes an anonymous caller can reach are throttled in `services/control_plane/ratelimit.py`. Two buckets, counting different things:

- **per source**, on signup and signin, counting attempts and released when a signin succeeds;
- **per account**, on signin, counting *failures* — checked before the password is verified and charged only when it was wrong.

Both are needed: per-source alone does not stop a distributed attempt against one account, and per-account alone lets one host spray many accounts at one attempt each. Counting attempts rather than failures on the account bucket would ration the person it protects, who may sign in from several devices.

State is per process, exactly as ADR-030 records for the gateway: with more than one public process the effective limit is the configured one times the number of processes.

`X-Forwarded-For` is ignored unless `MALUDB_TRUST_FORWARDED_FOR` says a proxy the platform controls rewrites it. A forwarded header nothing strips is attacker-controlled, and a caller that picks the key its attempts are counted against does not have a weaker limit — it has none.

## The plan catalogue is a bring-up step

`plans` is a catalogue an operator populates, not something a migration seeds: the limits live in `entitlements.DEFAULTS` keyed by plan code, and `plans.config_json` exists to *override* them for a particular deployment. Seeding the numbers into the table would put them in two places and make every change to the defaults a migration.

What that means operationally is that a freshly migrated control plane cannot create a project until `cp-manage plans sync` has run — `default_plan` looks for the code `free` and finds nothing, so creation answers 503. The application logs a warning at startup when the catalogue has no default, because an operator should hear it then rather than from the first customer who tries.

`sync` is idempotent, never deletes (a plan that leaves the spec is marked inactive, since projects reference plans), and does not overwrite a deployment's own `config_json` unless asked with `--with-limits`.

## Abuse controls on a public free tier (Phase 07 slice 5)

Signup is public at launch, so three controls sit between an open form and shared nodes.

**A challenge on signup**, required from day one rather than added once abuse appears — by the time farming shows up in the numbers the accounts already exist. `MALUDB_CAPTCHA_SECRET` configures the provider (Cloudflare Turnstile; hCaptcha and reCAPTCHA differ only in the URL). `MALUDB_CAPTCHA_REQUIRED` defaults to on in production and is deliberately separate from having a secret: a deployment that requires a challenge and forgot to configure one **refuses signups** rather than accepting everybody through the development verifier, which says yes to everything.

**The failure mode is the decision, not the provider.** When the challenge service cannot be reached the platform fails *closed* — nobody signs up until it returns. Failing open turns a third party's outage into an unbounded window with no control at all, and that window is exactly when somebody watching for it farms accounts. `MALUDB_CAPTCHA_FAIL_OPEN=1` inverts it, and exists so the choice is made in configuration by somebody who means it rather than by editing a module during an incident.

**A cap on what an organization may accumulate.** `max_projects` is an entitlement like any other — 2 on free — because each project is a database, four roles and a slot on a node whether or not anybody connects to it. Counted per organization, since counting per user would be defeated by an invitation. Deleted projects do not count.

**An audit trail the customer can read**, at `GET /v1/projects/{ref}/audit-events`. Two allowlists rather than one filter: which event types are visible, and which keys of each event's `detail_json` are visible. `detail_json` is free-form and written by several subsystems, so returning the row and redacting what looks sensitive is the wrong way round — the next subsystem to write a node hostname into it would publish it without anybody deciding to. An unclassified event type is invisible, which costs a support question rather than a disclosure.

## Security boundary

The control plane may hold privileged credentials needed to provision tenant databases, but those credentials:

- must not be sent to tenant clients;
- must not be logged;
- should be scoped to only the operations needed by provisioning;
- should be stored in a proper secret store/environment mechanism, not source code.

## Availability

A control-plane outage should not unnecessarily take down already-running tenant data APIs. Cache project routing/key metadata so the gateway can continue serving known healthy projects for a bounded period when safe.
