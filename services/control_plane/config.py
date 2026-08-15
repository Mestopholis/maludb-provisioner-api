"""Environment-aware configuration.

Plain module reading os.environ with explicit validation, matching the house
style in maludb-python-api-server. No settings framework.

Two rules from the ADRs are enforced here rather than left to deployment:

- ADR-024: FastAPI's documentation routes are configuration-gated, so they are
  never publicly reachable in production.
- ADR-023: the control plane fails closed when key material is unavailable. It
  refuses to start rather than running degraded, because a control plane that
  cannot decrypt cannot safely provision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ENVIRONMENTS = ("development", "test", "staging", "production")


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but unset")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_secret_file(ref: str, label: str) -> bytes:
    """Load key material from a file reference.

    ADR-023 requires the loading path be a narrow interface with one
    implementation swappable for another. This file backend is the development
    implementation; a manager such as Vault replaces this function alone.
    """
    path = Path(ref)
    if not path.is_file():
        raise ConfigError(f"{label}: no file at {ref}")
    mode = path.stat().st_mode & 0o077
    if mode:
        raise ConfigError(f"{label}: {ref} is group/world accessible (mode {oct(mode)}); chmod 600 it")
    material = path.read_bytes().strip()
    if len(material) < 32:
        raise ConfigError(f"{label}: {ref} holds {len(material)} bytes, need at least 32")
    return material


def redacted_dsn(dsn: str) -> str:
    """Render a connection string safe to log: host and database, no credentials."""
    parsed = urlsplit(dsn)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "?"
    user = f"{parsed.username}@" if parsed.username else ""
    return f"{parsed.scheme}://{user}{host}{port}/{database}"


@dataclass(frozen=True)
class Config:
    environment: str
    # repr=False on every field carrying credentials. database_url embeds a
    # password, so it is suppressed alongside the key material -- a security
    # review found it rendered in full while kek and token_pepper did not.
    database_url: str = field(repr=False)
    gateway_domain: str
    docs_enabled: bool
    kek: bytes = field(repr=False)
    token_pepper: bytes = field(repr=False)
    # The platform's own MaluMail key, used for every project on
    # sender_mode = 'platform_default' (ADR-029). repr=False for the same
    # reason as the rest: it sends mail as our verified domain.
    #
    # Optional rather than required, because a control plane that cannot send
    # email should still provision, serve and validate projects. The failure
    # surfaces where it matters -- a project on platform_default gets a refusal
    # naming the missing key, rather than the process refusing to start.
    malumail_api_key: str | None = field(default=None, repr=False)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def safe_database_dsn(self) -> str:
        """Credential-free form of database_url, for logs and diagnostics."""
        return redacted_dsn(self.database_url)


def load() -> Config:
    """Build configuration from the environment, or fail closed."""
    environment = os.environ.get("MALUDB_ENV", "development").strip() or "development"
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"MALUDB_ENV must be one of {', '.join(ENVIRONMENTS)}, got {environment!r}")

    # ADR-024: docs default on outside production, off in production. An
    # explicit override is honoured so staging can expose them deliberately.
    docs_enabled = _flag("MALUDB_DOCS_ENABLED", default=environment != "production")

    return Config(
        environment=environment,
        database_url=_require("MALUDB_CONTROL_PLANE_DATABASE_URL"),
        gateway_domain=os.environ.get("MALUDB_GATEWAY_DOMAIN", "maludb.local").strip(),
        docs_enabled=docs_enabled,
        kek=_read_secret_file(_require("MALUDB_KEK_REF"), "MALUDB_KEK_REF"),
        token_pepper=_read_secret_file(_require("MALUDB_TOKEN_PEPPER_REF"), "MALUDB_TOKEN_PEPPER_REF"),
        malumail_api_key=(os.environ.get("MALUMAIL_API", "").strip() or None),
    )
