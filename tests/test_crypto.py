"""Envelope encryption properties required by ADR-023 and docs/TESTING.md."""

from __future__ import annotations

import pytest

from services.control_plane import crypto


class FakeRing(crypto.KeyRing):
    """KeyRing with DEKs supplied directly, so these tests need no database."""

    def __init__(self, kek: bytes, versions: dict[int, bytes], active: int) -> None:
        super().__init__(kek)
        self._deks = dict(versions)
        self._active_version = active


@pytest.fixture
def ring():
    return FakeRing(b"k" * 64, {1: b"d" * 32}, active=1)


def test_round_trip():
    r = FakeRing(b"k" * 64, {1: b"d" * 32}, active=1)
    aad = crypto.aad_for("project_credentials", "ciphertext", "proj-1")
    sealed = r.seal(b"tenant-db-password", aad=aad)
    assert r.open(sealed, aad=aad) == b"tenant-db-password"


def test_ciphertext_moved_between_rows_fails_to_decrypt(ring):
    """The associated-data binding required by ADR-023.

    Without it, an attacker with database write access could move project A's
    encrypted database password into project B's row and have it decrypt.
    """
    sealed = ring.seal(b"project-a-secret", aad=crypto.aad_for("project_credentials", "ciphertext", "project-a"))
    with pytest.raises(crypto.CryptoError, match="failed authentication"):
        ring.open(sealed, aad=crypto.aad_for("project_credentials", "ciphertext", "project-b"))


def test_ciphertext_moved_between_columns_fails_to_decrypt(ring):
    sealed = ring.seal(b"s", aad=crypto.aad_for("project_email_settings", "smtp_ciphertext", "p1"))
    with pytest.raises(crypto.CryptoError):
        ring.open(sealed, aad=crypto.aad_for("project_credentials", "ciphertext", "p1"))


def test_tampered_ciphertext_is_rejected(ring):
    aad = crypto.aad_for("t", "c", "o")
    sealed = ring.seal(b"secret", aad=aad)
    flipped = bytes([sealed.ciphertext[0] ^ 0x01]) + sealed.ciphertext[1:]
    with pytest.raises(crypto.CryptoError):
        ring.open(crypto.SealedValue(flipped, sealed.nonce, sealed.key_version), aad=aad)


def test_nonce_is_unique_per_value(ring):
    aad = crypto.aad_for("t", "c", "o")
    nonces = {ring.seal(b"same plaintext", aad=aad).nonce for _ in range(200)}
    assert len(nonces) == 200


def test_identical_plaintexts_produce_different_ciphertexts(ring):
    aad = crypto.aad_for("t", "c", "o")
    assert ring.seal(b"same", aad=aad).ciphertext != ring.seal(b"same", aad=aad).ciphertext


def test_both_key_versions_readable_during_rotation():
    """Rotation is incremental: old values stay readable while new ones use the new key."""
    ring = FakeRing(b"k" * 64, {1: b"1" * 32, 2: b"2" * 32}, active=1)
    aad = crypto.aad_for("t", "c", "o")
    old = ring.seal(b"encrypted-under-v1", aad=aad)

    ring._active_version = 2  # noqa: SLF001 - simulating a completed rotation
    new = ring.seal(b"encrypted-under-v2", aad=aad)

    assert old.key_version == 1 and new.key_version == 2
    assert ring.open(old, aad=aad) == b"encrypted-under-v1"
    assert ring.open(new, aad=aad) == b"encrypted-under-v2"


def test_unknown_key_version_is_refused(ring):
    aad = crypto.aad_for("t", "c", "o")
    sealed = ring.seal(b"x", aad=aad)
    with pytest.raises(crypto.CryptoError, match="no data encryption key"):
        ring.open(crypto.SealedValue(sealed.ciphertext, sealed.nonce, 99), aad=aad)


def test_wrong_kek_cannot_unwrap():
    """A database dump without the KEK is useless."""
    right = crypto.KeyRing(b"correct-kek-material" * 4)
    wrapped = right._wrap(b"d" * 32)  # noqa: SLF001
    wrong = crypto.KeyRing(b"different-kek-materl" * 4)
    with pytest.raises(crypto.CryptoError, match="cannot unwrap"):
        wrong._unwrap(wrapped)  # noqa: SLF001


def test_key_derivation_normalises_arbitrary_material():
    for material in (b"short", b"k" * 1000, bytes(range(256))):
        assert len(crypto.derive_key(material, info=b"test")) == crypto.KEY_BYTES


def test_key_derivation_is_deterministic_and_info_separated():
    a = crypto.derive_key(b"same-material", info=b"purpose-a")
    b = crypto.derive_key(b"same-material", info=b"purpose-a")
    c = crypto.derive_key(b"same-material", info=b"purpose-b")
    assert a == b
    assert a != c
