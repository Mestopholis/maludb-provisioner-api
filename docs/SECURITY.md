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
