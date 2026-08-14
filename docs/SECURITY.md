# Security

## Primary threat: cross-tenant access

Every design/code review must assume tenant-controlled inputs may be malicious.

## Database privileges

Customers must not receive:

- superuser;
- database ownership;
- arbitrary `CREATEROLE`;
- arbitrary `CREATEDB`;
- cluster configuration privileges;
- arbitrary extension installation;
- unsafe filesystem/server-program execution.

## Extensions

Maintain an allowlist.

An extension should be reviewed for:

- superuser requirements;
- background workers;
- filesystem/network access;
- shared-memory behavior;
- ability to bypass tenant boundaries;
- resource-consumption implications.

## API keys/secrets

- Generate cryptographically.
- Prefix/types may be identifiable without exposing secret material.
- Do not log full secret keys.
- Support revocation/rotation.
- Prefer storing non-reversible verification data for secret keys where possible.
- Treat publishable keys as public identifiers with limited privilege; security must still rely on RLS/user authorization.

## Provisioning

Privileged provisioning credentials must be:

- server-side only;
- secret-managed;
- never returned to users;
- minimally scoped;
- rotated.

## Network

- MaluDB node administrative interfaces should not be internet exposed.
- Internal PostgREST/Auth worker ports should not be publicly reachable.
- Direct DB access for paid users should use an intentional public/pool endpoint with TLS and firewall rules.
- Free projects have no direct DB endpoint.

## Logging

Redact:

- API keys;
- database passwords;
- Auth tokens;
- refresh tokens;
- connection strings containing credentials.

## Audit

Record security-relevant control-plane changes:

- key creation/revocation;
- project suspension/resume;
- plan change;
- direct DB credential reset;
- extension enablement;
- privileged support/admin actions.
