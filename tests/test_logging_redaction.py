"""No secret may appear in logs at any level (docs/SECRETS.md, docs/SECURITY.md).

The provisioning failure path is called out specifically: error detail is free
text and a natural place for a connection string to leak.
"""

from __future__ import annotations

import pytest

from services.control_plane.logging import redact


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        (
            "connecting to postgresql://mldb_ab12cd_authenticator:s3cr3tpassword@10.0.0.4/mldb_ab12cd",
            "s3cr3tpassword",
        ),
        ("provisioning failed: password=hunter2 could not connect", "hunter2"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("key rotation used kek=0123456789abcdef0123456789abcdef", "0123456789abcdef"),
        ("client presented mldb_sk_9f8e7d6c5b4a3210", "mldb_sk_9f8e7d6c5b4a3210"),
        ('{"secret": "topsecretvalue"}', "topsecretvalue"),
    ],
)
def test_secrets_are_redacted(raw: str, must_not_contain: str):
    assert must_not_contain not in redact(raw)


def test_redaction_keeps_diagnostic_context():
    """Redaction must not destroy the parts needed to diagnose a failure."""
    out = redact("connecting to postgresql://mldb_ab12cd_auth:pw@10.0.0.4/mldb_ab12cd")
    assert "10.0.0.4" in out
    assert "mldb_ab12cd_auth" in out
    assert "pw@" not in out


def test_ordinary_text_is_untouched():
    message = "project ab12cd reached ACTIVE after 3 attempts"
    assert redact(message) == message
