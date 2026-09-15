"""Structured logging with correlation IDs and secret redaction.

docs/OBSERVABILITY.md requires every log line carry safe correlation
identifiers and never a secret. docs/SECRETS.md adds that recovered plaintext
must not appear even in provisioning failure paths, which are the most likely
place for a connection string to leak.

Redaction here is a backstop, not a licence to log secrets deliberately.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

from services.control_plane import hashing

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
project_ref_var: ContextVar[str | None] = ContextVar("project_ref", default=None)

REDACTED = "[REDACTED]"

# Ordered most-specific first. Each pattern keeps the identifying prefix where
# one exists so logs stay diagnosable without exposing material.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # postgres://user:password@host -> keep user and host, drop password
    (re.compile(r"(?P<scheme>\w+://)(?P<user>[^:/@\s]+):(?P<pw>[^@\s]+)@"), rf"\g<scheme>\g<user>:{REDACTED}@"),
    # password=... / "secret": "..." in connection strings, dicts, or JSON.
    # The optional quote after the key name is what catches JSON-encoded logs,
    # which is how most structured output actually reaches a log line.
    (
        re.compile(r"(?i)\b(password|passwd|pwd|secret|pepper|kek)[\"']?\s*[=:]\s*[\"']?[^\s,'\"}\]]+"),
        rf"\1={REDACTED}",
    ),
    # Credentials in a URL's query string. A websocket cannot send headers from
    # a browser, so Realtime clients put the project key in `apikey=`; email
    # links and signed Storage URLs carry `token=`. Server access logs print
    # the whole URL. The parameter name is kept so the line still says what
    # was presented.
    (
        re.compile(
            r"(?i)([?&](?:apikey|api_key|token|token_hash|access_token|refresh_token"
            r"|x-amz-signature|x-amz-credential)=)[^&\s\"']+"
        ),
        rf"\1{REDACTED}",
    ),
    # bearer tokens and JWTs
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"), REDACTED),
    # Prefixed platform tokens: mldb_<kind>_<prefix><secret>. The kinds are
    # enumerated from `hashing.TOKEN_KINDS` rather than approximated by a word
    # pattern, for two reasons found the hard way: a length-bounded class
    # silently missed `pwreset` (a one-hour account takeover) while looking
    # exhaustive, and it also matched things that are not tokens at all --
    # `mldb_gw_node0001` is a gateway role an operator needs to see. Tenant
    # roles (`mldb_<8-character ref>_authenticator`) are safe either way, since
    # no kind is eight characters.
    #
    # The secret is `token_urlsafe`, so it contains `-` and `_`: a class
    # without them redacts the first few characters and leaves the rest of the
    # key in the line.
    (
        re.compile(
            r"\bmldb_(?:"
            + "|".join(re.escape(k) for k in sorted(hashing.TOKEN_KINDS, key=len, reverse=True))
            + r")_[A-Za-z0-9_\-]{8,}"
        ),
        REDACTED,
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if request_id := request_id_var.get():
            payload["request_id"] = request_id
        if project_ref := project_ref_var.get():
            payload["project_ref"] = project_ref
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = redact(value) if isinstance(value, str) else value
        return json.dumps(payload, separators=(",", ":"))


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
