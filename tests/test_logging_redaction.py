"""No secret may appear in logs at any level (docs/SECRETS.md, docs/SECURITY.md).

The provisioning failure path is called out specifically: error detail is free
text and a natural place for a connection string to leak.
"""

from __future__ import annotations

import pytest

from services.control_plane import hashing
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
        # A real kind: `mldb_sk_` was invented for this test and no key has
        # ever had that shape, so it asserted nothing about a real credential.
        ("client presented mldb_secret_9f8e7d6c5b4a3210", "mldb_secret_9f8e7d6c5b4a3210"),
        ('{"secret": "topsecretvalue"}', "topsecretvalue"),
    ],
)
def test_secrets_are_redacted(raw: str, must_not_contain: str):
    assert must_not_contain not in redact(raw)


@pytest.mark.parametrize("kind", hashing.TOKEN_KINDS)
def test_a_real_platform_token_is_redacted_whole(kind: str):
    """Minted by the real generator rather than written out by hand, and
    parametrised over the registry rather than a list written beside it.

    The case above uses `mldb_sk_`, a format no key has ever had. The pattern
    it passed missed `publishable` entirely and stopped a secret key at its
    first `-` or `_`; the list that replaced it named five of the six kinds,
    omitting `pwreset` -- an hour-long account takeover -- while looking
    exhaustive. `generate_token` now refuses a kind this cannot see.
    """
    for _ in range(50):  # token_urlsafe: most draws contain `-` or `_`
        plaintext = hashing.generate_token(kind, b"pepper").plaintext
        out = redact(f"client presented {plaintext} and was refused")
        secret_part = plaintext[len(f"mldb_{kind}_") + hashing.PREFIX_BYTES:]
        assert plaintext not in out
        assert secret_part[-12:] not in out
        assert out == "client presented [REDACTED] and was refused"


def test_a_key_in_a_websocket_access_line_is_redacted():
    """The line uvicorn wrote to the rehearsal node's journal, with the key
    that was in it replaced by a freshly minted one."""
    plaintext = hashing.generate_token("publishable", b"pepper").plaintext
    line = f'10.120.0.1:33218 - "WebSocket /realtime/v1/websocket?apikey={plaintext}&vsn=1.0.0" 403'
    out = redact(line)
    assert plaintext not in out
    assert plaintext[20:] not in out
    assert "apikey=" in out and "vsn=1.0.0" in out and "403" in out


@pytest.mark.parametrize("name", ["apikey", "token", "access_token", "refresh_token"])
def test_credential_query_parameters_are_redacted(name: str):
    out = redact(f'"GET /auth/v1/verify?type=signup&{name}=abc123opaque-VALUE_x&redirect_to=/ HTTP/1.1" 303')
    assert "abc123opaque" not in out
    assert "VALUE_x" not in out
    assert "type=signup" in out and "redirect_to=/" in out


def test_main_does_not_let_uvicorn_replace_the_logging_config(monkeypatch):
    """uvicorn's default config replaces the handlers `build()` installed, and
    its own print without redaction. This asserts the kwarg only; the test
    below asserts what the kwarg buys."""
    import uvicorn

    from services.gateway import main as gateway_main

    seen = {}
    monkeypatch.setattr(gateway_main, "build", lambda: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    gateway_main.main(["8110"])
    assert "log_config" in seen and seen["log_config"] is None


def test_a_uvicorn_access_line_is_redacted_once_configured(capsys):
    """End to end through the real handler: uvicorn's own logger, the line it
    actually writes, and the formatter `configure()` installs."""
    import logging as stdlib_logging

    from services.control_plane import logging as cp_logging

    key = hashing.generate_token("publishable", b"pepper").plaintext
    # The state `log_config=None` leaves behind: uvicorn's loggers untouched,
    # so a record walks up to the root handler `configure()` installed. Set
    # explicitly because other tests here build a `uvicorn.Config`, and its
    # `configure_logging()` is process-wide: it gives each of these a handler
    # of its own and `propagate = False` -- including the *parent* `uvicorn`
    # logger, where propagation then stops -- and with `log_level="error"`, as
    # those tests pass, a level that drops this record before any handler sees
    # it. Whichever test ran first would otherwise decide the result here.
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    root_handlers = stdlib_logging.getLogger().handlers[:]
    saved = {
        name: (lg.handlers[:], lg.propagate, lg.level)
        for name, lg in ((n, stdlib_logging.getLogger(n)) for n in names)
    }
    try:
        for name in names:
            lg = stdlib_logging.getLogger(name)
            lg.handlers[:] = []
            lg.propagate = True
            lg.setLevel(stdlib_logging.NOTSET)
        cp_logging.configure()
        stdlib_logging.getLogger("uvicorn.access").info(
            '%s - "WebSocket %s" 403', "10.120.0.1:33218", f"/realtime/v1/websocket?apikey={key}"
        )
    finally:
        stdlib_logging.getLogger().handlers[:] = root_handlers
        for name, (handlers, propagate, level) in saved.items():
            lg = stdlib_logging.getLogger(name)
            lg.handlers[:] = handlers
            lg.propagate = propagate
            lg.setLevel(level)

    written = capsys.readouterr().out
    assert "WebSocket" in written, "the line never reached the configured handler"
    assert key not in written
    assert key[20:] not in written


def test_redaction_keeps_diagnostic_context():
    """Redaction must not destroy the parts needed to diagnose a failure."""
    out = redact("connecting to postgresql://mldb_ab12cd_auth:pw@10.0.0.4/mldb_ab12cd")
    assert "10.0.0.4" in out
    assert "mldb_ab12cd_auth" in out
    assert "pw@" not in out


def test_ordinary_text_is_untouched():
    message = "project ab12cd reached ACTIVE after 3 attempts"
    assert redact(message) == message
