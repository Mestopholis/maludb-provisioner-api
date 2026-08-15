"""The hook endpoint, driven by a real GoTrue.

Everything in the path is real except MaluMail itself, which is stubbed because
it charges quota and puts mail in real inboxes. What that leaves under test is
the part we wrote and the part most likely to be wrong: that GoTrue's signature
verifies against a secret we generated and stored, that the payload we receive
composes into a working link, and that suppression, quota and suspension are
enforced before anything is sent.

Slice 2's compatibility suite proved a stub upstream can hide a defect that only
a real one surfaces -- it forwarded `/rest/v1` verbatim and PostgREST answered
PGRST125. The same argument applies here, which is why GoTrue is real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid

import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from services.control_plane import auth_workers, crypto, db, identity, mail
from services.control_plane.main import create_app
from tests.conftest import TEST_CREDENTIAL, TEST_PEPPER, requires_db
from tests.test_provisioning import ADMIN_DSN, _tenant_admin_dsn

GOTRUE_BIN = os.environ.get("MALUDB_GOTRUE_BIN", "gotrue")
requires_gotrue = pytest.mark.skipif(
    shutil.which(GOTRUE_BIN) is None and not os.path.exists(GOTRUE_BIN),
    reason="GoTrue binary not available",
)
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

pytestmark = [requires_db]

HOOK_PORT = 28210
GOTRUE_PORT = 28211


class _Recorder:
    """Stands in for MaluMail. Records what would have been sent."""

    sent: list = []

    def __init__(self, api_key, **kwargs):
        self.api_key = api_key

    def send(self, *, sender, sender_name, to, message):
        _Recorder.sent.append(
            {"api_key": self.api_key, "from": sender, "to": to,
             "subject": message.subject, "text": message.text}
        )
        return {"status": "sent", "accepted": [to], "rejected": []}


@pytest.fixture
def email_project(db_pool, key_ring, app_config):
    """A project configured to send on the platform's account."""

    def make(ref: str, *, sender_mode="platform_default", status="ACTIVE",
             suspended=False, daily_limit=None) -> tuple[uuid.UUID, str]:
        project_id = uuid.uuid4()
        secret = mail.generate_hook_secret()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            limits = {} if daily_limit is None else {"limits": {"emails_per_day": daily_limit}}
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name,config_json) VALUES (%s,'Test',%s) "
                "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
                (f"plan-{ref}", psycopg.types.json.Jsonb(limits)),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, "
                "database_name) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (project_id, org, ref, "Acme", plan, status, f"mldb_{ref}"),
            )
            sealed = key_ring.seal(
                secret.encode(),
                aad=crypto.aad_for("project_email_settings", "hook", str(project_id)),
            )
            mm = key_ring.seal(
                b"mm_" + b"c" * 48,
                aad=crypto.aad_for("project_email_settings", "malumail", str(project_id)),
            )
            db.execute(
                conn,
                "INSERT INTO project_email_settings (project_id, sender_mode, sender_address, "
                "sender_name, hook_ciphertext, hook_nonce, hook_key_version, "
                "malumail_ciphertext, malumail_nonce, malumail_key_version, sending_suspended_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (project_id, sender_mode, "noreply@maludb.org", "MaluDB",
                 sealed.ciphertext, sealed.nonce, sealed.key_version,
                 mm.ciphertext if sender_mode == "custom_domain" else None,
                 mm.nonce if sender_mode == "custom_domain" else None,
                 mm.key_version if sender_mode == "custom_domain" else None,
                 "now()" if suspended else None),
            )
            if suspended:
                db.execute(
                    conn,
                    "UPDATE project_email_settings SET sending_suspended_at = now() "
                    "WHERE project_id = %s", (project_id,),
                )
            conn.commit()
        return project_id, secret

    return make


@pytest.fixture
def hook_client(app_config, db_pool, monkeypatch):
    _Recorder.sent = []
    monkeypatch.setattr(mail, "MaluMail", _Recorder)
    cfg = app_config
    object.__setattr__(cfg, "malumail_api_key", "mm_" + "p" * 48)
    monkeypatch.setattr("services.control_plane.config.load", lambda: cfg)
    with TestClient(create_app(cfg)) as client:
        yield client


def _post(client, ref: str, secret: str, payload: dict, **overrides):
    body = json.dumps(payload).encode()
    wid = overrides.get("webhook_id", "msg_" + uuid.uuid4().hex[:8])
    ts = overrides.get("timestamp", str(int(time.time())))
    import base64
    import hashlib
    import hmac

    key = base64.b64decode(secret.split("whsec_", 1)[1])
    sig = base64.b64encode(
        hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    headers = {
        "webhook-id": wid,
        "webhook-timestamp": ts,
        "webhook-signature": overrides.get("signature", f"v1,{sig}"),
        "content-type": "application/json",
    }
    return client.post(f"/internal/hooks/email/{ref}", content=body, headers=headers)


def _payload(address="user@example.com", action="signup"):
    return {
        "user": {"email": address},
        "email_data": {
            "email_action_type": action,
            "token_hash": "pkce_" + uuid.uuid4().hex,
            "token": "123456",
            "site_url": "https://eh000001.maludb.local/auth/v1",
            "redirect_to": "https://app.example.com",
        },
        "metadata": {},
    }


# -- the happy path --------------------------------------------------------


def test_a_signed_hook_call_sends_one_message(hook_client, email_project):
    _, secret = email_project("eh000001")
    response = _post(hook_client, "eh000001", secret, _payload())
    assert response.status_code == 200, response.text
    assert len(_Recorder.sent) == 1
    sent = _Recorder.sent[0]
    assert sent["to"] == "user@example.com"
    assert sent["from"] == "noreply@maludb.org"
    assert "/auth/v1/verify?" in sent["text"], "the link must come back through the gateway"


def test_platform_default_uses_the_platform_key_not_a_tenant_one(hook_client, email_project):
    _, secret = email_project("eh000002", sender_mode="platform_default")
    _post(hook_client, "eh000002", secret, _payload())
    assert _Recorder.sent[0]["api_key"] == "mm_" + "p" * 48


def test_custom_domain_uses_the_customers_own_key(hook_client, email_project):
    """ADR-029: on custom_domain the send is billed to and limited by the
    customer's own MaluMail account."""
    _, secret = email_project("eh000003", sender_mode="custom_domain")
    _post(hook_client, "eh000003", secret, _payload())
    assert _Recorder.sent[0]["api_key"] == "mm_" + "c" * 48


# -- authentication --------------------------------------------------------


def test_an_unsigned_call_sends_nothing(hook_client, email_project):
    """The signature is the only thing identifying the caller. Without this,
    anyone reaching the endpoint could put a live link in any inbox."""
    email_project("eh000004")
    response = hook_client.post(
        "/internal/hooks/email/eh000004", content=json.dumps(_payload()).encode()
    )
    assert response.status_code == 401
    assert _Recorder.sent == []


def test_a_call_signed_with_another_projects_secret_is_refused(hook_client, email_project):
    email_project("eh000005")
    _, other_secret = email_project("eh000006")
    response = _post(hook_client, "eh000005", other_secret, _payload())
    assert response.status_code == 401
    assert _Recorder.sent == []


def test_a_replayed_call_is_refused(hook_client, email_project):
    _, secret = email_project("eh000007")
    stale = str(int(time.time()) - mail.TIMESTAMP_TOLERANCE_SECONDS - 60)
    response = _post(hook_client, "eh000007", secret, _payload(), timestamp=stale)
    assert response.status_code == 401
    assert _Recorder.sent == []


def test_an_unknown_project_is_indistinguishable_from_a_bad_signature(hook_client, email_project):
    """Security review finding. `load_config` ran before `verify_signature`, so
    an unauthenticated caller got 403 for a project that exists and 401 for one
    that does not -- an oracle for which refs have email configured, and for
    which are suspended. The gateway went to some length to make every refusal
    identical for exactly this reason."""
    _, secret = email_project("eh00000j")
    unknown = _post(hook_client, "eh00000zz", secret, _payload())
    bad_signature = _post(hook_client, "eh00000j", mail.generate_hook_secret(), _payload())
    assert unknown.status_code == bad_signature.status_code == 401
    assert unknown.json() == bad_signature.json()


def test_suspension_is_not_disclosed_before_authentication(hook_client, email_project):
    """A suspended project must look like any other refusal to a caller that
    cannot sign, or suspension becomes queryable."""
    email_project("eh00000k", suspended=True)
    response = _post(hook_client, "eh00000k", mail.generate_hook_secret(), _payload())
    assert response.status_code == 401, "suspension leaked to an unauthenticated caller"


def test_a_refusal_does_not_describe_the_project(hook_client, email_project):
    """An error naming the sender mode or the configuration would describe a
    project's setup to whoever managed to reach the endpoint."""
    email_project("eh000008")
    body = _post(hook_client, "eh000008", mail.generate_hook_secret(), _payload()).json()
    assert "noreply@maludb.org" not in json.dumps(body)
    assert "platform_default" not in json.dumps(body)


# -- the three acceptance criteria -----------------------------------------


def test_a_suspended_project_stops_sending(hook_client, email_project):
    """ADR-029: the MaluMail key on custom_domain belongs to the customer and
    cannot be revoked by us, so the guarantee is that nothing the platform
    originates sends -- which is all of Auth mail."""
    _, secret = email_project("eh000009", suspended=True)
    response = _post(hook_client, "eh000009", secret, _payload())
    assert response.status_code == 403
    assert _Recorder.sent == []


def test_a_deleted_or_unprovisioned_project_cannot_send(hook_client, email_project):
    _, secret = email_project("eh00000a", status="FAILED")
    response = _post(hook_client, "eh00000a", secret, _payload())
    assert response.status_code == 403
    assert _Recorder.sent == []


def test_exceeding_the_entitlement_is_a_quota_condition_not_a_generic_failure(
    hook_client, email_project
):
    """429 so GoTrue backs off and the condition is legible as a quota
    condition, which is what the acceptance criterion asks for."""
    _, secret = email_project("eh00000b", daily_limit=2)
    for _ in range(2):
        assert _post(hook_client, "eh00000b", secret, _payload()).status_code == 200
    third = _post(hook_client, "eh00000b", secret, _payload())
    assert third.status_code == 429
    assert len(_Recorder.sent) == 2, "a send happened past the entitlement"


def test_the_quota_is_checked_before_the_send_not_after(hook_client, email_project):
    """On platform_default the allowance is shared between every project using
    it, so discovering the limit from MaluMail's 429 would let one project
    spend another's."""
    _, secret = email_project("eh00000c", daily_limit=0)
    response = _post(hook_client, "eh00000c", secret, _payload())
    assert response.status_code == 429
    assert _Recorder.sent == [], "MaluMail was called despite no entitlement"


def test_a_custom_domain_project_is_not_metered_by_us(hook_client, email_project):
    """Their MaluMail plan governs; a MaluDB-side limit would be a second,
    independent cap on an account we neither meter nor bill."""
    _, secret = email_project("eh00000d", sender_mode="custom_domain", daily_limit=0)
    assert _post(hook_client, "eh00000d", secret, _payload()).status_code == 200
    assert len(_Recorder.sent) == 1


def test_a_project_cannot_send_attributed_to_another(hook_client, email_project):
    """The reworded criterion: the message goes out as the project whose secret
    signed the request, never another."""
    a_id, a_secret = email_project("eh00000e")
    email_project("eh00000f")
    _post(hook_client, "eh00000e", a_secret, _payload())
    with db.connection() as conn:
        rows = db.query(
            conn, "SELECT project_id FROM email_events WHERE event_type = 'sent'"
        )
    assert [r["project_id"] for r in rows] == [a_id]


# -- suppression -----------------------------------------------------------


def test_a_suppressed_recipient_is_not_mailed(hook_client, email_project):
    _, secret = email_project("eh00000g")
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO email_suppressions (recipient_hash, reason) VALUES (%s,'hard_bounce')",
            (mail.recipient_hash("gone@example.com", pepper=TEST_PEPPER),),
        )
        conn.commit()
    response = _post(hook_client, "eh00000g", secret, _payload(address="gone@example.com"))
    assert response.status_code == 502
    assert _Recorder.sent == []


def test_no_end_user_address_is_stored(hook_client, email_project):
    """Migration 0003's rule, asserted rather than assumed: the control plane
    must not accumulate a list of every end user of every tenant."""
    _, secret = email_project("eh00000h")
    _post(hook_client, "eh00000h", secret, _payload(address="private@example.com"))
    with db.connection() as conn:
        rows = db.query(conn, "SELECT recipient_hash, detail_json FROM email_events")
    assert rows
    dumped = json.dumps([{"h": bytes(r["recipient_hash"]).hex(), "d": r["detail_json"]} for r in rows])
    assert "private@example.com" not in dumped


# -- end to end with a real GoTrue ----------------------------------------


@requires_node
@requires_gotrue
def test_a_real_gotrue_signup_reaches_the_hook(email_project, key_ring, app_config, monkeypatch):
    """The whole path except MaluMail: GoTrue renders nothing, posts a signed
    payload to a running control plane, and the platform composes the message.

    A stub GoTrue would prove only that our own signing matches our own
    verification. This proves it matches *GoTrue's*.
    """
    ref = "eh00000i"
    project_id, secret = email_project(ref)
    _Recorder.sent = []
    monkeypatch.setattr(mail, "MaluMail", _Recorder)
    object.__setattr__(app_config, "malumail_api_key", "mm_" + "p" * 48)
    monkeypatch.setattr("services.control_plane.config.load", lambda: app_config)

    server = uvicorn.Server(
        uvicorn.Config(create_app(app_config), host="127.0.0.1", port=HOOK_PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    db_name = f"mldb_{ref}"
    auth_role, auth_pw = f"{db_name}_auth", "gotrue-probe-password"  # noqa: S105
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.execute(f'DROP ROLE IF EXISTS "{auth_role}"')
        admin.execute(f"CREATE ROLE \"{auth_role}\" LOGIN PASSWORD '{auth_pw}'")
        admin.execute(f'CREATE DATABASE "{db_name}"')
    with psycopg.connect(_tenant_admin_dsn(db_name)) as tenant:
        tenant.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{auth_role}"')
        for path in ("002_auth_helpers.sql", "007_auth_role_ownership.sql"):
            tenant.execute(open(f"services/control_plane/bootstrap/{path}").read())
        tenant.commit()

    settings = auth_workers.AuthSettings(
        project_ref=ref, database=db_name, auth_role=auth_role, auth_password=auth_pw,
        jwt_secret="e2e-jwt-secret-long-enough-for-hs256-000",  # noqa: S106
        port=GOTRUE_PORT,
        site_url=f"http://{ref}.maludb.local",
        external_url=f"http://127.0.0.1:{GOTRUE_PORT}",
        autoconfirm=False,
        send_email_hook_uri=f"http://127.0.0.1:{HOOK_PORT}/internal/hooks/email/{ref}",
        send_email_hook_secret=secret,
    )
    auth_workers.migrate(settings, binary=GOTRUE_BIN)
    env = dict(os.environ)
    for line in auth_workers.render_env(settings).splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k] = v.strip('"')
    gotrue = subprocess.Popen(  # noqa: S603 - fixed binary, generated environment
        [GOTRUE_BIN], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        auth_workers.wait_until_ready(GOTRUE_PORT, timeout=30)
        import httpx

        result = httpx.post(
            f"http://127.0.0.1:{GOTRUE_PORT}/signup",
            json={"email": "e2e@example.com", "password": "correct-horse-battery-staple"},
            timeout=20,
        )
        assert result.status_code == 200, result.text

        # Follow the link the platform composed. This is the assertion that
        # matters most in this file: `verification_url` is ours, GoTrue never
        # sees it before a user clicks it, and a wrong shape would 404 for every
        # signup on the platform while every other test still passed.
        assert _Recorder.sent, "no hook call, so no link to follow"
        link = next(
            line.strip() for line in _Recorder.sent[0]["text"].splitlines()
            if line.strip().startswith("http")
        )
        followed = httpx.get(link, follow_redirects=False, timeout=20)
        assert followed.status_code in (301, 302, 303), (
            f"the composed link did not verify: {followed.status_code} {followed.text[:200]}"
        )
        assert "error" not in (followed.headers.get("location") or ""), followed.headers.get("location")

        # And the user is actually confirmed, which is what the acceptance
        # criterion means by a confirmation email that works.
        with psycopg.connect(_tenant_admin_dsn(db_name)) as tenant, tenant.cursor() as cur:
            cur.execute(
                "SELECT confirmed_at IS NOT NULL FROM auth.users WHERE email = %s",
                ("e2e@example.com",),
            )
            assert cur.fetchone()[0] is True, "following the link did not confirm the user"
    finally:
        gotrue.terminate()
        gotrue.wait(timeout=10)
        server.should_exit = True
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            admin.execute(f'DROP ROLE IF EXISTS "{auth_role}"')
        # The app's shutdown closes the shared pool this test still needs. Both
        # ran in one process, which production never does.
        db.close_pool()
        db.init_pool(app_config.database_url)

    assert len(_Recorder.sent) == 1, "GoTrue did not reach the hook, or the signature was refused"
    sent = _Recorder.sent[0]
    assert sent["to"] == "e2e@example.com"
    assert sent["subject"] == "Confirm your email address"
    assert "/verify?" in sent["text"] and "token=" in sent["text"]

    with db.connection() as conn:
        row = db.one(
            conn, "SELECT count(*) AS n FROM email_events WHERE project_id = %s "
            "AND event_type = 'sent'", (project_id,)
        )
    assert row["n"] == 1
