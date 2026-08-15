"""Class A hashing: algorithm by entropy, not importance (ADR-023)."""

from __future__ import annotations

import time

from services.control_plane import hashing

PEPPER = b"p" * 32


def test_password_round_trip():
    stored = hashing.hash_password("correct-horse-battery-staple")
    assert hashing.verify_password(stored, "correct-horse-battery-staple")
    assert not hashing.verify_password(stored, "wrong")


def test_password_hash_is_argon2id():
    assert hashing.hash_password("x" * 16).startswith("$argon2id$")


def test_password_hashes_are_salted():
    assert hashing.hash_password("same password") != hashing.hash_password("same password")


def test_malformed_stored_hash_does_not_raise():
    assert not hashing.verify_password("not-a-hash", "anything")


def test_token_round_trip():
    token = hashing.generate_token("pat", PEPPER)
    assert hashing.verify_token(token.verifier, token.plaintext, PEPPER)
    assert not hashing.verify_token(token.verifier, token.plaintext + "x", PEPPER)


def test_token_verifier_is_not_the_token():
    token = hashing.generate_token("sess", PEPPER)
    assert token.plaintext not in token.verifier


def test_pepper_is_required_to_verify():
    """A database-only compromise must not yield offline-verifiable hashes."""
    token = hashing.generate_token("pat", PEPPER)
    assert not hashing.verify_token(token.verifier, token.plaintext, b"different-pepper" * 2)


def test_tokens_are_unique():
    assert len({hashing.generate_token("pat", PEPPER).plaintext for _ in range(200)}) == 200


def test_token_prefix_round_trips():
    token = hashing.generate_token("pat", PEPPER)
    parsed = hashing.split_token(token.plaintext)
    assert parsed is not None
    kind, prefix = parsed
    assert kind == "pat"
    assert prefix == token.prefix


def test_split_token_rejects_malformed_input():
    for bad in ("", "nope", "mldb_pat", "other_pat_abc123def456", "mldb_pat_short"):
        assert hashing.split_token(bad) is None


def test_token_verification_is_not_memory_hard():
    """ADR-023: a memory-hard function on the verification path would be a
    self-inflicted denial of service, since project API keys are checked on
    every gateway request. This asserts the cost profile, not just the code.
    """
    token = hashing.generate_token("pat", PEPPER)
    start = time.perf_counter()
    for _ in range(1000):
        hashing.verify_token(token.verifier, token.plaintext, PEPPER)
    per_verification_ms = (time.perf_counter() - start)
    # 1000 verifications well under a second; Argon2 would need ~50s.
    assert per_verification_ms < 1.0, f"token verification too slow: {per_verification_ms:.3f}s for 1000"
