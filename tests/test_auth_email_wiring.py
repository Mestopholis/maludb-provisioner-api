"""Auth email reaches the platform's hook (free slice 2).

Two gaps closed, both silent before: a project a customer created had no email settings (only
`cp-manage project email` made them), and no Auth worker the gateway started was given the
Send Email Hook at all -- so GoTrue accepted signups and sent nothing. Held here:

- the first Auth start creates `platform_default` settings with a hook secret, and every later
  start reuses that secret; an existing row, including a customer's `custom_domain`, is untouched;
- the rendered GoTrue environment names the control plane's hook for this project, and a body
  signed with the rendered secret verifies against the secret the hook route loads;
- production refuses to configure Auth with no hook, before touching the project;
- the base URL is refused unless it is a bare http(s) origin; preflight fails production without
  a platform sender.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import time
import uuid

import pytest

from services.control_plane import auth_workers, crypto, db, identity, mail, preflight, provisioning
from services.control_plane import config as config_module
from tests.conftest import TEST_CREDENTIAL, requires_db

pytestmark = requires_db

HOOK = auth_workers.EmailHook(base_url="http://10.0.0.10:8111", sender_address="noreply@maludb.test")


@pytest.fixture
def project(db_pool, key_ring):
    """A provisioned-looking project with the credentials `settings_for` reads back. No node needed."""
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(conn, email="m@example.com", password=TEST_CREDENTIAL)
        plan = db.one(conn, "INSERT INTO plans (code, name) VALUES ('free', 'Developer') ON CONFLICT (code) "
                            "DO UPDATE SET name = EXCLUDED.name RETURNING id")["id"]
        project_id = uuid.uuid4()
        db.execute(conn, "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, database_name, "
                         "auth_port) VALUES (%s, %s, 'mailwire', 'Mail Wire', %s, 'ACTIVE', 'mldb_mailwire', 29999)",
                   (project_id, org, plan))
        provisioning.store_credential(conn, project_id=project_id, credential_type="db_auth",
                                      role_name="mldb_mailwire_auth", secret="auth-" + "x" * 20, key_ring=key_ring)
        conn.commit()
    return project_id


def _settings(key_ring, project_id, **kwargs):
    with db.connection() as conn:
        return auth_workers.settings_for(conn, project_id=project_id, key_ring=key_ring,
                                         gateway_domain="test.maludb.org", **kwargs)


def test_the_first_auth_start_creates_platform_default_settings_and_later_ones_reuse_the_secret(key_ring, project):
    first = _settings(key_ring, project, email=HOOK)
    second = _settings(key_ring, project, email=HOOK)
    assert first.send_email_hook_uri == "http://10.0.0.10:8111/internal/hooks/email/mailwire"
    assert first.send_email_hook_secret.startswith("v1,whsec_")
    assert second.send_email_hook_secret == first.send_email_hook_secret, "a restart must not rotate the secret"
    with db.connection() as conn:
        rows = db.query(conn, "SELECT sender_mode, sender_address, sender_name FROM project_email_settings")
    assert rows == [{"sender_mode": "platform_default", "sender_address": "noreply@maludb.test",
                     "sender_name": "Mail Wire"}]


def test_an_existing_custom_domain_row_is_kept(key_ring, project):
    with db.connection() as conn:
        sealed = key_ring.seal(b"v1,whsec_" + base64.b64encode(b"k" * 32),
                               aad=crypto.aad_for("project_email_settings", "hook", str(project)))
        customer_key = key_ring.seal(b"mm_customer_key",
                                     aad=crypto.aad_for("project_email_settings", "malumail", str(project)))
        db.execute(conn, "INSERT INTO project_email_settings (project_id, sender_mode, sender_address, sender_name, "
                         "hook_ciphertext, hook_nonce, hook_key_version, malumail_ciphertext, malumail_nonce, "
                         "malumail_key_version) VALUES (%s, 'custom_domain', 'hello@customer.example', 'Customer', "
                         "%s, %s, %s, %s, %s, %s)",
                   (project, sealed.ciphertext, sealed.nonce, sealed.key_version,
                    customer_key.ciphertext, customer_key.nonce, customer_key.key_version))
        conn.commit()
    settings = _settings(key_ring, project, email=HOOK)
    assert settings.send_email_hook_secret == "v1,whsec_" + base64.b64encode(b"k" * 32).decode()
    with db.connection() as conn:
        row = db.one(conn, "SELECT sender_mode, sender_address FROM project_email_settings")
    assert row == {"sender_mode": "custom_domain", "sender_address": "hello@customer.example"}


def test_gotrue_is_told_to_call_the_hook_and_its_signature_verifies_where_the_hook_checks_it(key_ring, project):
    settings = _settings(key_ring, project, email=HOOK)
    env = auth_workers.render_env(settings)
    assert 'GOTRUE_HOOK_SEND_EMAIL_ENABLED="true"' in env
    assert 'GOTRUE_HOOK_SEND_EMAIL_URI="http://10.0.0.10:8111/internal/hooks/email/mailwire"' in env
    assert 'GOTRUE_MAILER_AUTOCONFIRM="false"' in env

    # Sign the way GoTrue does (Standard Webhooks), with the secret GoTrue was given...
    body, webhook_id, timestamp = b'{"user":{"email":"a@b.example"}}', "msg_1", str(int(time.time()))
    key = base64.b64decode(settings.send_email_hook_secret.split("whsec_", 1)[1])
    signature = base64.b64encode(hmac.new(key, f"{webhook_id}.{timestamp}.".encode() + body,
                                          hashlib.sha256).digest()).decode()
    # ...and verify against the secret the hook route loads for this project.
    cfg = dataclasses.replace(_config(), malumail_api_key="mm_test")
    with db.connection() as conn:
        loaded = mail.load_config(conn, "mailwire", key_ring=key_ring, settings=cfg)
    mail.verify_signature(secret=loaded.hook_secret, webhook_id=webhook_id, timestamp=timestamp, body=body,
                          signature_header=f"v1,{signature}")


def test_production_refuses_auth_with_no_hook_before_touching_the_project(key_ring, project):
    with pytest.raises(auth_workers.AuthWorkerError, match="send no confirmation"):
        _settings(key_ring, project, email=None, require_email=True)
    with db.connection() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM project_email_settings")["n"] == 0


def test_without_a_hook_outside_production_nothing_is_configured(key_ring, project):
    settings = _settings(key_ring, project)
    assert settings.send_email_hook_uri is None and "HOOK_SEND_EMAIL" not in auth_workers.render_env(settings)


def test_the_hook_comes_from_configuration_only_when_both_halves_are_set():
    cfg = _config()
    assert auth_workers.email_hook_from(cfg) is None
    assert auth_workers.email_hook_from(dataclasses.replace(cfg, email_hook_base_url="http://h:8111")) is None
    both = dataclasses.replace(cfg, email_hook_base_url="http://h:8111", platform_email_from="noreply@x.test")
    assert auth_workers.email_hook_from(both) == auth_workers.EmailHook("http://h:8111", "noreply@x.test")


@pytest.mark.parametrize("value", ["ftp://h:8111", "http://h:8111/internal", "10.0.0.10:8111", "http://:8111",
                                   "http://h:8111?x=1"])
def test_the_hook_base_url_must_be_a_bare_origin(value):
    with pytest.raises(config_module.ConfigError, match="MALUDB_EMAIL_HOOK_BASE_URL"):
        config_module._hook_base_url(value)  # noqa: SLF001
    assert config_module._hook_base_url(" http://10.0.0.10:8111/ ") == "http://10.0.0.10:8111"  # noqa: SLF001


def _config(**overrides):
    base = config_module.Config(environment="production", database_url="postgresql://x/y", gateway_domain="e.com",
                                database_domain="db.e.com", docs_enabled=False, kek=b"k" * 32, token_pepper=b"p" * 32)
    return dataclasses.replace(base, **overrides)


def test_preflight_fails_production_without_a_platform_sender(db_pool):  # noqa: ARG001
    def check(cfg):
        with db.connection() as conn:
            return next(c for c in preflight.run(conn, cfg).checks if c.name == "email")
    missing = check(_config())
    assert not missing.ok and not missing.advisory and "MALUMAIL_API" in missing.detail
    assert check(_config(environment="development")).advisory
    assert check(_config(malumail_api_key="mm_x", platform_email_from="noreply@x.test")).ok
