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

## Security boundary

The control plane may hold privileged credentials needed to provision tenant databases, but those credentials:

- must not be sent to tenant clients;
- must not be logged;
- should be scoped to only the operations needed by provisioning;
- should be stored in a proper secret store/environment mechanism, not source code.

## Availability

A control-plane outage should not unnecessarily take down already-running tenant data APIs. Cache project routing/key metadata so the gateway can continue serving known healthy projects for a bounded period when safe.
