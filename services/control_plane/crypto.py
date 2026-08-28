"""Envelope encryption for Class B (recoverable) secrets.

ADR-023 and docs/SECRETS.md. Class B secrets are values the platform must
reproduce exactly -- tenant database passwords, per-project JWT signing keys,
SMTP passwords, MFA seeds. They are encrypted, never hashed, because hashing
them would make the platform unable to configure the workers that need them.

    KEK (outside the database, from config)
     |
     +-- wraps --> DEK (versioned, stored wrapped in encryption_keys)
                    |
                    +-- encrypts --> ciphertext in a column

Three properties the ADR requires, all enforced here:

- AEAD (AES-256-GCM), never a bare cipher;
- a fresh random nonce per value, never reused under a key;
- associated data binding each ciphertext to its table, column and owning row,
  so a ciphertext moved between rows fails to decrypt rather than silently
  authenticating as the wrong tenant's secret.

The KEK never enters the database. A dump of the control-plane database is
useless without it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from services.control_plane import db

ALGORITHM = "aes-256-gcm"
KEY_BYTES = 32
NONCE_BYTES = 12


class CryptoError(RuntimeError):
    """Raised when key material or a ciphertext cannot be used."""


def derive_key(material: bytes, *, info: bytes) -> bytes:
    """Derive a fixed-length key from arbitrary configured material.

    Operators supply key material as a file whose contents we do not control --
    hex, base64 or raw bytes of any length. HKDF normalises that to exactly 32
    bytes without assuming an encoding, so a differently-formatted KEK file
    cannot silently produce a short or malformed key.
    """
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info).derive(material)


def aad_for(table: str, column: str, owner_id: str) -> bytes:
    """Associated data binding a ciphertext to exactly one row and column.

    Without this, an attacker with database write access could move project A's
    encrypted database password into project B's row and have it decrypt
    cleanly. With it, decryption of a relocated ciphertext fails.
    """
    return f"{table}:{column}:{owner_id}".encode()


@dataclass(frozen=True)
class SealedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: int


class KeyRing:
    """Holds the KEK and caches unwrapped DEKs by version."""

    def __init__(self, kek_material: bytes) -> None:
        # A distinct info string keeps this derivation separate from any other
        # use of the same configured material.
        self._kek = derive_key(kek_material, info=b"maludb-control-plane-kek-v1")
        self._deks: dict[int, bytes] = {}
        self._active_version: int | None = None

    # -- DEK lifecycle ----------------------------------------------------

    def _wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(self._kek).encrypt(nonce, dek, b"maludb-dek-wrap")

    def _unwrap(self, wrapped: bytes) -> bytes:
        nonce, blob = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            return AESGCM(self._kek).decrypt(nonce, blob, b"maludb-dek-wrap")
        except InvalidTag as exc:
            raise CryptoError(
                "cannot unwrap a data encryption key with the configured KEK. "
                "Either the KEK is wrong or the stored key is corrupt; refusing to continue."
            ) from exc

    def load(self, conn: psycopg.Connection) -> None:
        """Load every stored DEK, creating the first one only on a virgin database."""
        rows = db.query(conn, "SELECT key_version, wrapped_dek, state FROM encryption_keys ORDER BY key_version")
        if not rows:
            # Phase 11 slice 5, ADR-070. Minting a first key is correct on a new
            # deployment and catastrophic on a restored one, and the two are
            # indistinguishable from `encryption_keys` alone -- both are empty.
            #
            # Measured before this guard existed. The schema does defend itself
            # once: `nodes.admin_key_version` and `project_credentials.key_version`
            # are foreign keys into this table, so a dump that omitted it fails
            # to restore those constraints and psql prints an ERROR for each.
            #
            # But `ON_ERROR_STOP` is opt-in, and a restore that does not set it
            # -- the default, and what any `psql -f dump.sql` in a script does --
            # keeps going. What it leaves is a database carrying every secret,
            # missing those foreign keys, and holding no keys at all. Then this
            # ran: it minted a fresh version 1, marked it active, and returned
            # **successfully**. Every ciphertext in that database was permanently
            # undecryptable from that moment, and the failure surfaced later and
            # one secret at a time as "ciphertext failed authentication". Worse,
            # the new row occupies version 1, so the real keys can no longer be
            # re-imported without a collision: the silent success destroys the
            # recovery path as well as the data.
            #
            # So this is the second line, and it exists because the first one is
            # a constraint error in a log that a scripted restore discards.
            #
            # The discriminator is not the key table. It is whether anything in
            # this database is already encrypted.
            self._refuse_if_secrets_exist(conn)
            self._create_first(conn)
            rows = db.query(conn, "SELECT key_version, wrapped_dek, state FROM encryption_keys ORDER BY key_version")

        for row in rows:
            self._deks[row["key_version"]] = self._unwrap(bytes(row["wrapped_dek"]))
            if row["state"] == "active":
                self._active_version = row["key_version"]

        if self._active_version is None:
            raise CryptoError("no active data encryption key; refusing to continue")

    def _refuse_if_secrets_exist(self, conn: psycopg.Connection) -> None:
        """Fail closed when the database holds ciphertext but no key to read it.

        That state has exactly one innocent explanation -- a brand-new
        deployment, which has no ciphertext either -- and one dangerous one: a
        restore that lost `encryption_keys`. Since the dangerous reading is
        unrecoverable and the safe reading costs one query at startup, this
        refuses and says what to do about it.

        Every Class B column in the schema is checked rather than a
        representative one. A restore can lose the key table while keeping any
        subset of the rest, and a guard that only looked at `nodes` would wave
        through a control plane whose customers' credentials had just been
        orphaned.
        """
        holders = [
            ("nodes", "admin_ciphertext"),
            ("nodes", "storage_secret_ciphertext"),
            ("project_credentials", "ciphertext"),
            ("api_keys", "ciphertext"),
            ("project_email_settings", "malumail_ciphertext"),
            ("project_email_settings", "hook_ciphertext"),
            ("user_mfa_factors", "ciphertext"),
        ]
        found: list[str] = []
        for table, column in holders:
            # A column that does not exist yet is not a finding: this runs on a
            # database the migrations may not have finished building.
            exists = db.one(
                conn,
                "SELECT 1 AS present FROM information_schema.columns "
                " WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                (table, column),
            )
            if not exists:
                continue
            row = db.one(
                conn,
                f"SELECT count(*) AS n FROM {table} WHERE {column} IS NOT NULL",  # noqa: S608 - fixed literals above
            )
            if row and row["n"]:
                found.append(f"{table}.{column} ({row['n']})")

        if not found:
            return

        raise CryptoError(
            "this database holds encrypted values but no data encryption keys: "
            + ", ".join(found)
            + ". Refusing to mint a new key, because doing so would make every one of "
            "those values permanently undecryptable and would occupy the version the "
            "real keys need. This is what a control-plane restore that lost the "
            "`encryption_keys` table looks like -- restore that table from the same "
            "backup and start again (ADR-070; docs/BACKUP-RECOVERY.md)."
        )

    def _create_first(self, conn: psycopg.Connection) -> None:
        dek = os.urandom(KEY_BYTES)
        db.execute(
            conn,
            """
            INSERT INTO encryption_keys (key_version, wrapped_dek, algorithm, kek_identifier, state)
            VALUES (%s, %s, %s, %s, 'active')
            """,
            (1, self._wrap(dek), ALGORITHM, "config:MALUDB_KEK_REF"),
        )
        conn.commit()

    def rotate(self, conn: psycopg.Connection) -> int:
        """Introduce a new active DEK; previous versions stay readable.

        Rotation is incremental by design (ADR-023): existing values keep their
        key_version and are re-encrypted in batches, so a roll never has to be
        one transaction over every secret.
        """
        next_version = max(self._deks, default=0) + 1
        dek = os.urandom(KEY_BYTES)
        with conn.transaction():
            db.execute(conn, "UPDATE encryption_keys SET state = 'retiring' WHERE state = 'active'")
            db.execute(
                conn,
                """
                INSERT INTO encryption_keys (key_version, wrapped_dek, algorithm, kek_identifier, state)
                VALUES (%s, %s, %s, %s, 'active')
                """,
                (next_version, self._wrap(dek), ALGORITHM, "config:MALUDB_KEK_REF"),
            )
        self._deks[next_version] = dek
        self._active_version = next_version
        return next_version

    # -- value encryption -------------------------------------------------

    def seal(self, plaintext: bytes, *, aad: bytes) -> SealedValue:
        if self._active_version is None:
            raise CryptoError("key ring not loaded")
        nonce = os.urandom(NONCE_BYTES)
        dek = self._deks[self._active_version]
        return SealedValue(
            ciphertext=AESGCM(dek).encrypt(nonce, plaintext, aad),
            nonce=nonce,
            key_version=self._active_version,
        )

    def open(self, sealed: SealedValue, *, aad: bytes) -> bytes:
        dek = self._deks.get(sealed.key_version)
        if dek is None:
            raise CryptoError(f"no data encryption key for version {sealed.key_version}")
        try:
            return AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad)
        except InvalidTag as exc:
            # Either the ciphertext was tampered with, or it was moved to a
            # different row and the associated data no longer matches.
            raise CryptoError("ciphertext failed authentication") from exc
