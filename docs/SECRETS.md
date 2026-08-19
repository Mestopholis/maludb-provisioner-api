# Secret Storage and Key Management

How every secret the control plane holds is stored, and why the answer is not
the same for all of them.

`docs/SECURITY.md` says "prefer storing non-reversible verification data for
secret keys where possible". That is correct for roughly half the secrets in
this system and actively wrong for the other half. This document draws the line.

See ADR-023.

## Three classes

Classify by one question: **does the platform ever need the plaintext back?**

| Class | Need plaintext back? | Storage | Examples |
|---|---|---|---|
| **A — Verifiers** | No. Compare a presented value against a stored derivative | Hash | project API secret keys, personal access tokens, session tokens, invitation tokens, user passwords |
| **B — Recoverable** | Yes. The platform must reproduce the exact value to use it | Envelope encryption | tenant database passwords, per-project JWT signing keys, SMTP passwords, MFA seeds |
| **C — Ephemeral** | Not stored at all | — | end-user access tokens, one-time codes in flight |

Storing a Class B secret as a hash makes the system unable to function. Storing
a Class A secret encrypted is an unnecessary standing risk: anything the
platform can decrypt, an attacker with platform access can decrypt too.

## Class A — hashing, and why the algorithm differs

Not all Class A secrets take the same hash, and the reason is entropy, not
importance.

| Secret | Algorithm | Why |
|---|---|---|
| User passwords | Argon2id (or scrypt) | Human-chosen, low entropy, dictionary-attackable. Memory-hard cost is the entire defence. |
| Project API secret keys | HMAC-SHA-256 with a server-side pepper | 256-bit CSPRNG values; no feasible search space to slow down. Verified on **every API request** — a memory-hard function here is a self-inflicted denial of service. |
| Personal access tokens | HMAC-SHA-256 with pepper | Same reasoning; high-entropy, machine-generated. |
| Session tokens | HMAC-SHA-256 with pepper | Same. |
| Invitation tokens | HMAC-SHA-256 with pepper | Same. Single-use and short-lived. |

**This is the point most likely to be flagged in review, so it is stated
explicitly**: a fast hash is the correct choice for high-entropy random tokens.
Memory-hard hashing exists to slow down guessing of secrets humans chose. For a
256-bit random token there is nothing to guess. Using bcrypt or Argon2 on the
API-key verification path would add tens of milliseconds to every request in
the gateway hot path for no security gain.

The pepper is a server-side key held outside the database, so a database-only
compromise does not yield offline-verifiable hashes.

### Lookup without a plaintext index

Hashed values cannot be looked up by prefix scan efficiently at request rates.
Every Class A credential therefore carries a **non-secret prefix or identifier**
stored in the clear and indexed — `api_keys.key_identifier`,
`personal_access_tokens.token_prefix`. The request presents the whole token; the
gateway splits off the prefix, fetches the single candidate row, and verifies
the remainder in constant time.

Comparison must be constant-time. `docs/API-GATEWAY.md` already requires the
result to be cached; the cache stores the verification outcome, never the
presented secret.

## Class B — envelope encryption

Recoverable secrets use a two-level key hierarchy:

```text
KEK  (key encryption key)      held outside the database, never in a table
 │
 └── wraps ──> DEK  (data encryption key), versioned, stored wrapped
                │
                └── encrypts ──> secret ciphertext in a column
```

Requirements:

- **AEAD** — AES-256-GCM or XChaCha20-Poly1305. Never a bare block cipher.
- **Per-value nonce**, stored alongside the ciphertext, never reused under a key.
- **Associated data binds the ciphertext to its row.** AAD must include the
  table, column, and owning identifier (project or user). Without this, an
  attacker with write access to the database can move project A's encrypted
  database password into project B's row and have it decrypt successfully.
- **Key version stored per value**, so rotation can proceed incrementally
  rather than as one transaction over every secret.
- The KEK is never persisted in the control-plane database. A dump of that
  database must be useless without it.

Every Class B column therefore carries the triple: `ciphertext`, `nonce`,
`key_version`.

### Rotation

| Event | Work |
|---|---|
| KEK rotation | Re-wrap the DEKs. Cheap — a handful of rows, no secret re-encrypted. |
| DEK rotation | Re-encrypt affected values in batches, incrementing `key_version`. Both versions readable during the roll. |
| Credential rotation | Generate a new secret, reconfigure the consumer, then revoke the old. Never a destructive in-place overwrite. |
| Suspected compromise | Rotate credentials first, keys second. |

Tenant database password rotation has a live-service constraint: the worker
using it must be reconfigured and restarted. It is a provisioning-style
operation with the same idempotency and retry-safety requirements as
`docs/PROVISIONING.md`, not a simple `UPDATE`.

## The credentials that had nowhere to live

Until now the schema had no home for the secrets provisioning actually
generates. The Phase 00 spike wrote them to a file in `/tmp` — acceptable for a
spike, not a design.

Per project, all Class B:

| Secret | Consumer | Why recoverable |
|---|---|---|
| `mldb_<ref>_authenticator` password | PostgREST worker config | must be written into the worker's configuration |
| `mldb_<ref>_auth` password | Auth worker config | same |
| `mldb_<ref>_admin` password | nothing, since ADR-047 | generated at provisioning and never issued; the role is `NOLOGIN` on every tier |
| `mldb_<ref>_executor` password | the platform, to run a customer's SQL (ADR-039) | the console reproduces it on every request |
| `mldb_<ref>_client` password | **the customer**, for paid direct SQL (ADR-047) | it is returned to them on request and rotated on request |
| JWT signing key | **both** PostgREST and Auth | the two must agree; a token signed by one is verified by the other |
| SMTP password | Auth worker config | ADR-019 |
| `mldb_<ref>_replicator` password | Realtime server config | the server must connect as the role to decode; exists only while Realtime is enabled |

**The client password is the only secret in this table that is deliberately
given away**, and that is what makes ADR-047 worth the extra role. Every other
row is a secret the platform holds in order to work; this one is minted to be
put in a customer's application configuration, pasted into their CI, and
eventually leaked by somebody. Because it is its own role, rotating it is a
customer's self-service action rather than a platform outage, and revoking it
when a plan changes is not the same operation as breaking their SQL console.

The `mldb_<ref>_admin` row is the counterpart: a Class B secret with no
consumer at all. It stays generated and stored because ADR-047's client role is
a *member* of the admin role and an operator recovery path may yet need it —
and it is never returned by any route, which is asserted rather than assumed.

The replicator password is the highest-value secret in this table and should be
read that way rather than as one more database password. Within its tenant it is
an **unrestricted reader** — past grants and past row-level security, because
decoding reads WAL and WAL is written before any policy is consulted (ADR-031).
Two consequences: it is revoked and its role dropped when Realtime is disabled,
rather than left dormant, and a shared Realtime server holding many of them is a
concentration held to this document in full, the same way the control plane is.

The JWT signing key is the one that most obviously cannot be hashed: PostgREST
and GoTrue must independently possess the same key material, and the control
plane must supply it to both. `docs/AUTH.md` targets asymmetric/JWKS signing,
which changes what is stored — a private key rather than a shared secret — but
not its class. It remains Class B.

These live in `project_credentials` in `specs/control-plane-schema.sql`.

## Do not use MaluDB's in-database secret store for platform secrets

`maludb_core` ships a secret store — `secret_set`, `secret_get_metadata`,
`secret_revoke`, `__secret_master_key` (`docs/MALUDB.md`). It should not hold
control-plane secrets, for three reasons:

1. **Bootstrap circularity.** The control plane needs a database credential to
   reach any database. Storing that credential in a database it must already
   have reached cannot work.
2. **Boundary violation.** ADR-013 makes the tenant database boundary the
   security boundary. Platform secrets in a MaluDB instance blur exactly the
   line that decision draws.
3. **Blast radius.** A tenant-adjacent store holding platform credentials turns
   any database-level compromise into a platform-level one.

MaluDB's secret store remains a **tenant-facing product feature** — something a
customer can use inside their own project. That is a different thing from the
platform's own key management, and the two must not be merged.

This resolves the open question left by `docs/MALUDB.md`.

## Where the KEK lives

Unresolved, and it is the load-bearing decision. `.env.example` has an empty
`MALUDB_SECRET_STORE=` awaiting it.

The KEK must be available to the control plane at startup, absent from the
control-plane database, absent from the repository, and auditable when
accessed. Candidates for self-hosted Proxmox infrastructure include a secrets
manager such as Vault, systemd credentials, an operator-supplied file with
strict permissions loaded at boot, or a hardware-backed store.

Whatever is chosen, the loading path must be a **narrow interface with one
implementation swappable for another**, so the initial choice does not become
structural. Starting with an operator-supplied file is acceptable for
development; it is not acceptable for production without an explicit decision
recorded here.

Related unresolved question: what happens on control-plane restart if the KEK
source is unavailable. The service must fail closed — refuse to start rather
than run degraded — because a control plane that cannot decrypt cannot safely
provision.

## Logging

`docs/SECURITY.md` already requires redaction of API keys, database passwords,
auth tokens, refresh tokens, and connection strings. Two additions specific to
this design:

- Never log plaintext recovered from Class B storage, including in provisioning
  job error details. `provisioning_jobs.error_detail` is a free-text column and
  a natural place for a connection string to leak.
- Never log the pepper, the KEK, or an unwrapped DEK, including at debug level.

## Required tests

- A Class B value re-encrypted under a new key version decrypts correctly, and
  both versions remain readable mid-rotation.
- A ciphertext moved between rows fails to decrypt (AAD binding).
- API-key verification is constant-time and does not use a memory-hard function.
- A database dump without the KEK yields no usable Class B secret.
- No secret appears in logs at any level, including provisioning failure paths.
- The control plane refuses to start when the KEK source is unavailable.
