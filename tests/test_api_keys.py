"""Project API keys.

The property this module exists for is ADR-008's: a key is accepted only for
the project it belongs to. Phase 03 makes that the difference between a public
HTTP request reaching its own tenant and reaching someone else's, so it is
tested directly and from both directions rather than inferred from the fact
that a project id is compared somewhere.
"""

from __future__ import annotations

import uuid

import pytest

from services.control_plane import api_keys, crypto, db, identity
from tests.conftest import TEST_CREDENTIAL, TEST_PEPPER, requires_db

pytestmark = [requires_db]


@pytest.fixture
def project_pair(db_pool, key_ring):
    """Two provisioned projects in different organizations."""

    def make(ref: str) -> uuid.UUID:
        project_id = uuid.uuid4()
        with db.connection() as conn:
            _, org = identity.create_user_with_personal_org(
                conn, email=f"{ref}@example.com", password=TEST_CREDENTIAL
            )
            plan = db.one(
                conn,
                "INSERT INTO plans (code,name) VALUES (%s,'Test') "
                "ON CONFLICT (code) DO UPDATE SET name='Test' RETURNING id",
                (f"plan-{ref}",),
            )["id"]
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
                "VALUES (%s,%s,%s,%s,%s,'PROVISIONED')",
                (project_id, org, ref, ref, plan),
            )
            conn.commit()
        return project_id

    return make("ak000001"), make("ak000002")


# -- ADR-008: the control this module exists for --------------------------


def test_a_key_is_rejected_against_another_project(project_pair, key_ring):
    """The cross-tenant control. A valid key for A presented against B must be
    refused -- otherwise any key on the platform reads any tenant."""
    a, b = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()

        assert api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER
        ) is not None, "the key does not work for its own project"

        assert api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=b, pepper=TEST_PEPPER
        ) is None, "a key for project A authenticated against project B"


def test_authenticate_requires_a_project(project_pair):
    """Signature-level: there is no way to resolve a key without naming the
    project you expect, so the comparison cannot be forgotten by a caller."""
    import inspect

    signature = inspect.signature(api_keys.authenticate)
    project = signature.parameters["project_id"]
    assert project.kind is inspect.Parameter.KEYWORD_ONLY
    assert project.default is inspect.Parameter.empty, "project_id must be required"


def test_a_mismatch_is_indistinguishable_from_an_unknown_key(project_pair):
    """Both answer None. A distinguishable failure tells an attacker which
    project refs and keys exist."""
    a, b = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()
        mismatch = api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=b, pepper=TEST_PEPPER
        )
        unknown = api_keys.authenticate(
            conn, presented="mldb_secret_deadbeefnotarealkeyatall", project_id=b, pepper=TEST_PEPPER
        )
    assert mismatch is unknown is None


# -- storage discipline (ADR-023) -----------------------------------------


def test_a_secret_key_is_not_recoverable(project_pair, key_ring):
    """Class A. The row must not carry anything that yields the key back."""
    a, _ = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()
        row = db.one(
            conn,
            "SELECT verification_data, ciphertext, nonce, key_version FROM api_keys WHERE id = %s",
            (issued.id,),
        )
    assert row["ciphertext"] is None and row["nonce"] is None and row["key_version"] is None
    assert issued.plaintext not in row["verification_data"]


def test_a_publishable_key_is_recoverable_for_display(project_pair, key_ring):
    """Class B. A dashboard has to show this one again next month."""
    a, _ = project_pair
    with db.connection() as conn:
        issued = api_keys.create(
            conn, project_id=a, key_type=api_keys.PUBLISHABLE, pepper=TEST_PEPPER, key_ring=key_ring
        )
        conn.commit()
        recovered = api_keys.reveal_publishable(
            conn, key_id=issued.id, project_id=a, key_ring=key_ring
        )
    assert recovered == issued.plaintext


def test_a_publishable_key_cannot_be_revealed_to_another_project(project_pair, key_ring):
    a, b = project_pair
    with db.connection() as conn:
        issued = api_keys.create(
            conn, project_id=a, key_type=api_keys.PUBLISHABLE, pepper=TEST_PEPPER, key_ring=key_ring
        )
        conn.commit()
        with pytest.raises(api_keys.ApiKeyError):
            api_keys.reveal_publishable(conn, key_id=issued.id, project_id=b, key_ring=key_ring)


def test_ciphertext_moved_to_another_projects_row_fails_to_decrypt(project_pair, key_ring):
    """The AAD binding, tested rather than assumed. Copying the ciphertext into
    another project's row must fail closed, not authorise there."""
    a, b = project_pair
    with db.connection() as conn:
        mine = api_keys.create(
            conn, project_id=a, key_type=api_keys.PUBLISHABLE, pepper=TEST_PEPPER, key_ring=key_ring
        )
        theirs = api_keys.create(
            conn, project_id=b, key_type=api_keys.PUBLISHABLE, pepper=TEST_PEPPER, key_ring=key_ring
        )
        conn.commit()
        db.execute(
            conn,
            "UPDATE api_keys SET ciphertext = (SELECT ciphertext FROM api_keys WHERE id = %s), "
            "nonce = (SELECT nonce FROM api_keys WHERE id = %s) WHERE id = %s",
            (mine.id, mine.id, theirs.id),
        )
        conn.commit()
        with pytest.raises(crypto.CryptoError):
            api_keys.reveal_publishable(conn, key_id=theirs.id, project_id=b, key_ring=key_ring)


def test_a_secret_key_row_cannot_carry_ciphertext(project_pair, key_ring):
    """The classification is a database invariant, not a convention -- a Class A
    secret stored Class B would not be visible in review of the calling code."""
    a, _ = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()
        with pytest.raises(Exception, match="api_keys_recoverability_check"):
            db.execute(
                conn,
                "UPDATE api_keys SET ciphertext = %s, nonce = %s, key_version = 1 WHERE id = %s",
                (b"x", b"y", issued.id),
            )
        conn.rollback()


def test_a_publishable_key_needs_the_key_ring(project_pair):
    a, _ = project_pair
    with db.connection() as conn, pytest.raises(api_keys.ApiKeyError, match="key ring"):
        api_keys.create(conn, project_id=a, key_type=api_keys.PUBLISHABLE, pepper=TEST_PEPPER)


# -- format (ADR-028) ------------------------------------------------------


def test_keys_carry_the_maludb_prefix(project_pair, key_ring):
    """A leaked key should be attributable at a glance and matchable by a
    secret-scanning rule."""
    a, _ = project_pair
    with db.connection() as conn:
        secret = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        publishable = api_keys.create(
            conn, project_id=a, key_type=api_keys.PUBLISHABLE, pepper=TEST_PEPPER, key_ring=key_ring
        )
    assert secret.plaintext.startswith("mldb_secret_")
    assert publishable.plaintext.startswith("mldb_publishable_")


def test_two_keys_are_never_equal(project_pair, key_ring):
    a, _ = project_pair
    with db.connection() as conn:
        minted = {
            api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER).plaintext
            for _ in range(20)
        }
    assert len(minted) == 20


# -- lifecycle -------------------------------------------------------------


def test_a_revoked_key_stops_authenticating(project_pair):
    a, _ = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()
        assert api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER
        ) is not None
        assert api_keys.revoke(conn, key_id=issued.id, project_id=a) is True
        conn.commit()
        assert api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER
        ) is None


def test_one_project_cannot_revoke_anothers_key(project_pair):
    a, b = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()
        assert api_keys.revoke(conn, key_id=issued.id, project_id=b) is False
        conn.commit()
        assert api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER
        ) is not None


def test_rotation_does_not_require_an_outage(project_pair):
    """Both keys live at once, deliberately: a one-live-key-per-type constraint
    would make every rotation a window where the old key is dead and the new one
    is not yet deployed."""
    a, _ = project_pair
    with db.connection() as conn:
        old = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        new = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()
        for key in (old, new):
            assert api_keys.authenticate(
                conn, presented=key.plaintext, project_id=a, pepper=TEST_PEPPER
            ) is not None
        api_keys.revoke(conn, key_id=old.id, project_id=a)
        conn.commit()
        assert api_keys.authenticate(
            conn, presented=new.plaintext, project_id=a, pepper=TEST_PEPPER
        ) is not None


def test_a_key_for_a_project_that_is_not_serving_is_rejected(project_pair):
    """A suspended or half-provisioned project must not answer API traffic."""
    a, _ = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        db.execute(conn, "UPDATE projects SET status = 'SUSPENDED' WHERE id = %s", (a,))
        conn.commit()
        assert api_keys.authenticate(
            conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER
        ) is None


def test_listing_keys_never_exposes_secret_material(project_pair, key_ring):
    a, _ = project_pair
    with db.connection() as conn:
        secret = api_keys.create(
            conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER, name="ci"
        )
        conn.commit()
        listed = api_keys.list_for_project(conn, project_id=a)

    assert [row["name"] for row in listed] == ["ci"]
    rendered = str(listed)
    assert secret.plaintext not in rendered
    assert "verification_data" not in rendered
    assert "ciphertext" not in rendered


def test_a_malformed_key_is_refused_without_touching_the_database(project_pair):
    a, _ = project_pair
    with db.connection() as conn:
        for junk in ("", "not-a-key", "mldb_", "mldb_secret_", "sb_secret_abc", "mldb_pat_abcdefgh"):
            assert api_keys.authenticate(
                conn, presented=junk, project_id=a, pepper=TEST_PEPPER
            ) is None


# -- last_used_at ----------------------------------------------------------


def test_last_used_is_recorded_but_not_on_every_request(project_pair):
    """At gateway volume an unconditional update makes every read a write to one
    hot row, serialising a project's concurrent requests behind a row lock."""
    a, _ = project_pair
    with db.connection() as conn:
        issued = api_keys.create(conn, project_id=a, key_type=api_keys.SECRET, pepper=TEST_PEPPER)
        conn.commit()

        api_keys.authenticate(conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER)
        conn.commit()
        first = db.one(conn, "SELECT last_used_at FROM api_keys WHERE id = %s", (issued.id,))
        assert first["last_used_at"] is not None, "use was never recorded"

        for _ in range(5):
            api_keys.authenticate(conn, presented=issued.plaintext, project_id=a, pepper=TEST_PEPPER)
        conn.commit()
        later = db.one(conn, "SELECT last_used_at FROM api_keys WHERE id = %s", (issued.id,))

    assert later["last_used_at"] == first["last_used_at"], "every request rewrote the row"
