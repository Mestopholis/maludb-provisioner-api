"""Configuration must fail closed (ADR-023) and gate docs routes (ADR-024)."""

from __future__ import annotations

import pytest

from services.control_plane import config as config_module


@pytest.fixture
def key_files(tmp_path):
    kek = tmp_path / "kek"
    pepper = tmp_path / "pepper"
    kek.write_bytes(b"k" * 64)
    pepper.write_bytes(b"p" * 64)
    kek.chmod(0o600)
    pepper.chmod(0o600)
    return kek, pepper


def _env(monkeypatch, kek, pepper, **overrides):
    monkeypatch.setenv("MALUDB_CONTROL_PLANE_DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("MALUDB_KEK_REF", str(kek))
    monkeypatch.setenv("MALUDB_TOKEN_PEPPER_REF", str(pepper))
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)


def test_loads_with_valid_environment(monkeypatch, key_files):
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper, MALUDB_ENV="development")
    cfg = config_module.load()
    assert cfg.environment == "development"
    assert cfg.kek == b"k" * 64


def test_fails_closed_when_kek_missing(monkeypatch, key_files):
    """ADR-023: refuse to start rather than run degraded."""
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper)
    monkeypatch.delenv("MALUDB_KEK_REF")
    with pytest.raises(config_module.ConfigError, match="MALUDB_KEK_REF"):
        config_module.load()


def test_fails_closed_when_kek_file_absent(monkeypatch, key_files, tmp_path):
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper, MALUDB_KEK_REF=str(tmp_path / "nope"))
    with pytest.raises(config_module.ConfigError, match="no file at"):
        config_module.load()


def test_rejects_world_readable_key_material(monkeypatch, key_files):
    kek, pepper = key_files
    kek.chmod(0o644)
    _env(monkeypatch, kek, pepper)
    with pytest.raises(config_module.ConfigError, match="accessible"):
        config_module.load()


def test_rejects_short_key_material(monkeypatch, key_files, tmp_path):
    kek, pepper = key_files
    short = tmp_path / "short"
    short.write_bytes(b"tooshort")
    short.chmod(0o600)
    _env(monkeypatch, kek, pepper, MALUDB_KEK_REF=str(short))
    with pytest.raises(config_module.ConfigError, match="at least 32"):
        config_module.load()


def test_rejects_unknown_environment(monkeypatch, key_files):
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper, MALUDB_ENV="prod")
    with pytest.raises(config_module.ConfigError, match="MALUDB_ENV"):
        config_module.load()


def test_docs_disabled_by_default_in_production(monkeypatch, key_files):
    """ADR-024: production must not publish a map of the admin surface."""
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper, MALUDB_ENV="production")
    assert config_module.load().docs_enabled is False


def test_docs_enabled_by_default_in_development(monkeypatch, key_files):
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper, MALUDB_ENV="development")
    assert config_module.load().docs_enabled is True


def test_config_repr_does_not_leak_key_material(monkeypatch, key_files):
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper)
    cfg = config_module.load()
    rendered = repr(cfg)
    assert "kkkk" not in rendered
    assert "pppp" not in rendered


def test_config_repr_does_not_leak_the_database_password(monkeypatch, key_files):
    """Regression: repr rendered database_url in full while suppressing key material."""
    kek, pepper = key_files
    _env(monkeypatch, kek, pepper)
    monkeypatch.setenv("MALUDB_CONTROL_PLANE_DATABASE_URL", "postgresql://u:sup3rs3cret@127.0.0.1:5432/db")
    cfg = config_module.load()
    assert "sup3rs3cret" not in repr(cfg)
    assert "sup3rs3cret" not in str(cfg)


def test_safe_dsn_keeps_diagnostics_without_credentials():
    dsn = "postgresql://cp_user:sup3rs3cret@db.internal:5433/control_plane"
    safe = config_module.redacted_dsn(dsn)
    assert "sup3rs3cret" not in safe
    # still usable for diagnosing which database was targeted
    assert "db.internal" in safe
    assert "5433" in safe
    assert "control_plane" in safe
    assert "cp_user" in safe


def test_safe_dsn_handles_a_dsn_without_credentials():
    assert "localhost" in config_module.redacted_dsn("postgresql://localhost/db")
