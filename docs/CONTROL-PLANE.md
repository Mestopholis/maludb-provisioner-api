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

## Security boundary

The control plane may hold privileged credentials needed to provision tenant databases, but those credentials:

- must not be sent to tenant clients;
- must not be logged;
- should be scoped to only the operations needed by provisioning;
- should be stored in a proper secret store/environment mechanism, not source code.

## Availability

A control-plane outage should not unnecessarily take down already-running tenant data APIs. Cache project routing/key metadata so the gateway can continue serving known healthy projects for a bounded period when safe.
