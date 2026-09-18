"""Customers' own model provider API keys (ADR-079 decisions 4 and 5, memory slice 4).

A customer's secret, held to the platform's own standard: sealed under the KEK and
bound to its project and provider, write-only through the API, one live key per
provider, never logged or audited in full, and out of the gateway's reach
(`tests/test_gateway_grants.py`).
"""

from __future__ import annotations

import logging

import pytest

from services.control_plane import crypto, db, provider_keys
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_maludb_jobs import _headers, _member

pytestmark = requires_db

KEYS = "/v1/projects/{ref}/maludb/memory/provider-keys"
KEY = "sk-test-" + "A1b2C3d4" * 5 + "wxyz"  # noqa: S105 - test fixture, not a real key
OTHER = "sk-test-" + "Z9y8X7w6" * 5 + "qrst"  # noqa: S105 - test fixture, not a real key


def _set(project_id, key_ring, provider="openai", api_key=KEY):
    with db.connection() as conn:
        info = provider_keys.set_key(conn, project_id=project_id, provider=provider, api_key=api_key,
                                     key_ring=key_ring, actor_user_id=None)
        conn.commit()
    return info


def _load(project_id, key_ring, provider="openai"):
    with db.connection() as conn:
        return provider_keys.load_key(conn, project_id=project_id, provider=provider, key_ring=key_ring)


# -- the store ---------------------------------------------------------------


def test_a_key_is_sealed_and_only_the_loader_opens_it(placed_project, key_ring):
    project_id = placed_project("mpk00001")
    info = _set(project_id, key_ring)
    assert info.hint == KEY[-4:]
    with db.connection() as conn:
        stored = db.one(conn, "SELECT ciphertext, key_hint FROM project_provider_keys WHERE project_id = %s",
                        (project_id,))
        listed = provider_keys.list_keys(conn, project_id=project_id)
    assert KEY.encode() not in bytes(stored["ciphertext"])
    assert [(k.provider, k.hint) for k in listed] == [("openai", KEY[-4:])]
    assert _load(project_id, key_ring) == KEY


def test_replacing_a_key_destroys_the_one_it_replaces(placed_project, key_ring):
    """ADR-088. This used to keep the old row with `revoked_at` set, on `project_credentials`'
    model -- but a platform credential authenticates a role the platform can drop, and a provider
    key is the customer's credential at a third party that goes on working there. A customer
    rotating monthly left a year of live-at-the-provider keys sealed in the database and in every
    dump of it, and rotation was the last way that happened (10e closed removal and deletion).
    Nothing read them: every query in the module filters `revoked_at IS NULL`.
    """
    project_id = placed_project("mpk00002")
    _set(project_id, key_ring)
    _set(project_id, key_ring, api_key=OTHER)
    with db.connection() as conn:
        rows = db.query(conn, "SELECT key_hint, revoked_at FROM project_provider_keys "
                              " WHERE project_id = %s ORDER BY created_at", (project_id,))
        sets = db.query(
            conn,
            "SELECT detail_json FROM audit_events WHERE project_id = %s AND event_type = %s "
            " ORDER BY id", (project_id, provider_keys.AUDIT_SET),
        )
    assert len(rows) == 1 and rows[0]["key_hint"] == OTHER[-4:], "one row, and it is the live key"
    assert rows[0]["revoked_at"] is None
    assert _load(project_id, key_ring) == OTHER
    assert "superseded" not in sets[0]["detail_json"], "the first set replaced nothing"
    assert sets[1]["detail_json"]["superseded"] == 1, "the trail says a key was replaced, not which"


def test_a_ciphertext_moved_to_another_project_or_provider_does_not_open(placed_project, key_ring):
    """AAD binds each ciphertext to its project and provider."""
    source = placed_project("mpk00003")
    target = placed_project("mpk00004")
    _set(source, key_ring)
    _set(target, key_ring, provider="anthropic", api_key=OTHER)
    with db.connection() as conn:
        db.execute(conn, "UPDATE project_provider_keys t SET ciphertext = s.ciphertext, nonce = s.nonce, "
                         "key_version = s.key_version FROM project_provider_keys s "
                         "WHERE s.project_id = %s AND t.project_id = %s", (source, target))
        db.execute(conn, "UPDATE project_provider_keys SET provider = 'voyage' WHERE project_id = %s", (source,))
        conn.commit()
    with pytest.raises(crypto.CryptoError):
        _load(target, key_ring, provider="anthropic")
    with pytest.raises(crypto.CryptoError):
        _load(source, key_ring, provider="voyage")


@pytest.mark.parametrize("bad", ["", "short", "has spaces in the middle of the key", '"quoted-key-value-123456"',
                                 "x" * 513, "{\"key\": \"sk-abcdefghijklmnopqrst\"}"])
def test_what_is_plainly_not_a_key_is_refused_without_echoing_it(placed_project, key_ring, bad):
    project_id = placed_project("mpk00005")
    with pytest.raises(provider_keys.ProviderKeyError) as refused:
        _set(project_id, key_ring, api_key=bad)
    assert refused.value.status == 422
    if len(bad) > 4:
        assert bad not in str(refused.value)


def test_an_unknown_provider_is_refused(placed_project, key_ring):
    project_id = placed_project("mpk00006")
    with pytest.raises(provider_keys.ProviderKeyError) as refused:
        _set(project_id, key_ring, provider="example")
    assert refused.value.status == 404


# -- the routes --------------------------------------------------------------


def test_a_manager_sets_a_key_and_no_route_ever_returns_it(client, placed_project, caplog):
    placed_project("mpr00001")
    manager = _headers(client, "mpr00001")
    with caplog.at_level(logging.DEBUG):
        put = client.put(f"{KEYS.format(ref='mpr00001')}/openai", json={"api_key": KEY}, headers=manager)
        listed = client.get(KEYS.format(ref="mpr00001"), headers=manager)
        audit = client.get("/v1/projects/mpr00001/audit-events", headers=manager)
    assert put.status_code == 200, put.text
    assert put.json() == {"provider": "openai", "hint": KEY[-4:], "created_at": put.json()["created_at"]}
    assert listed.status_code == 200 and listed.json()["providers"] == ["openai", "anthropic", "voyage"]
    for response in (put, listed, audit):
        assert KEY not in response.text
    assert KEY not in caplog.text, "a provider key reached a log line"
    events = [e for e in audit.json() if e["event_type"] == provider_keys.AUDIT_SET]
    assert events and events[0]["detail"] == {"provider": "openai", "hint": KEY[-4:]}


def test_a_developer_can_see_which_keys_exist_but_cannot_set_or_remove_one(client, placed_project, key_ring):
    project_id = placed_project("mpr00002")
    _set(project_id, key_ring)
    developer = _member(client, "mpr00002", email="mp-dev@example.com", role="developer")
    assert client.get(KEYS.format(ref="mpr00002"), headers=developer).json()["keys"][0]["hint"] == KEY[-4:]
    assert client.put(f"{KEYS.format(ref='mpr00002')}/openai", json={"api_key": OTHER},
                      headers=developer).status_code == 403
    assert client.delete(f"{KEYS.format(ref='mpr00002')}/openai", headers=developer).status_code == 403
    assert _load(project_id, key_ring) == KEY


def test_removing_a_key_destroys_it_and_a_second_removal_is_404(client, placed_project, key_ring):
    """Removal deletes the row. It used to set `revoked_at` and keep the ciphertext, so "remove"
    meant "stop using, still hold": the customer's key at the provider stayed sealed in the control
    plane and in every dump of it, after they had told the platform to get rid of it. Nothing read
    those rows -- every query here filters `revoked_at IS NULL` -- so the retention had no purpose,
    and the history a person needs is the audit event, which carries the provider and the hint.
    """
    project_id = placed_project("mpr00003")
    _set(project_id, key_ring)
    manager = _headers(client, "mpr00003")
    assert client.delete(f"{KEYS.format(ref='mpr00003')}/openai", headers=manager).status_code == 204
    assert _load(project_id, key_ring) is None
    with db.connection() as conn:
        left = db.query(conn, "SELECT revoked_at FROM project_provider_keys "
                              " WHERE project_id = %s AND provider = 'openai'", (project_id,))
        removed = db.query(
            conn,
            "SELECT detail_json FROM audit_events WHERE project_id = %s AND event_type = %s",
            (project_id, provider_keys.AUDIT_REMOVED),
        )
    assert left == [], "a revoked row is still the customer's key, sealed and recoverable"
    assert len(removed) == 1 and removed[0]["detail_json"]["provider"] == "openai", (
        "the removal is still on the record, with the hint and without the key"
    )
    assert client.delete(f"{KEYS.format(ref='mpr00003')}/openai", headers=manager).status_code == 404


def test_a_non_member_learns_nothing(client, placed_project):
    placed_project("mpr00004")
    client.post("/v1/auth/signup", json={"email": "mp-out@example.com", "password": TEST_CREDENTIAL})
    token = client.post("/v1/auth/signin", json={"email": "mp-out@example.com", "password": TEST_CREDENTIAL}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert client.get(KEYS.format(ref="mpr00004"), headers=headers).status_code == 404
    put = client.put(f"{KEYS.format(ref='mpr00004')}/openai", json={"api_key": KEY}, headers=headers)
    assert put.status_code == 404


def test_a_rejected_key_is_not_echoed_in_the_validation_error(client, placed_project):
    placed_project("mpr00005")
    bad = "not a key but a secret sentence with spaces"
    response = client.put(f"{KEYS.format(ref='mpr00005')}/openai", json={"api_key": bad},
                          headers=_headers(client, "mpr00005"))
    assert response.status_code == 422
    assert bad not in response.text
