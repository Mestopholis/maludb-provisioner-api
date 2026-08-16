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
- unsafe filesystem/server-program execution;
- the `REPLICATION` attribute.

`REPLICATION` is last because it is the least obvious. It is not a database-scoped privilege and the ADR-014 `CONNECT` lockdown does not constrain it: a non-superuser holding it took a byte-level copy of **every database on the cluster** through `pg_basebackup`, including one it was explicitly denied `CONNECT` on (`specs/realtime-replication-model.md`, R6). Realtime needs it and no lesser privilege substitutes, so the platform issues it to a dedicated per-tenant replicator role and contains it at the node with a `pg_hba.conf` reject of physical replication (ADR-031). Granting it to a customer-reachable role — the tenant admin or the authenticator — hands that customer every tenant on the node.

Note also that a replication consumer reads every table in its own database past grants and row-level security, because logical decoding reads WAL and WAL is written before any policy is consulted. Its credential is correspondingly high-value.

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
- Treat publishable keys as public identifiers with limited privilege; security must still rely on RLS/user authorization.

Storage depends on whether the platform must recover the plaintext (ADR-023, `docs/SECRETS.md`):

- **Verifiers** — API keys, personal access tokens, session and invitation tokens, user passwords — are stored **hashed**. High-entropy machine-generated tokens use HMAC-SHA-256 with a server-side pepper; human-chosen passwords use Argon2id. Do not use a memory-hard function on the API-key verification path, which runs on every request.
- **Recoverable secrets** — tenant database passwords, per-project JWT signing keys, SMTP passwords, MFA seeds — must be **envelope encrypted**, never hashed. The platform has to reproduce them exactly to configure workers.

"Prefer non-reversible storage" is therefore not a universal rule. Applying it to a recoverable secret breaks provisioning.

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
