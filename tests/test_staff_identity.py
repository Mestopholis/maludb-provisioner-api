"""Platform staff identity (ADR-082 slice 0).

The properties that make a staff account worth having, each tested against a real database:

- **a staff credential and a customer credential are never interchangeable**, in both
  directions, because either failure turns one kind of access into the other;
- **no session without the password and a confirmed second factor together**;
- **a code cannot be replayed**, and guessing locks the account;
- **sessions end** at their absolute lifetime, when idle, on revocation, and when the
  factor is replaced or the password set;
- **the seed is sealed under a staff key that is not the KEK**, bound to its account;
- **nothing staff-side touches customer identity tables**, and listings carry no secret.
"""

from __future__ import annotations

import base64
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from services.control_plane import db, hashing, identity, staff
from tests.conftest import TEST_KEK, TEST_PEPPER, requires_db

pytestmark = requires_db

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAFF_KEY = b"test-staff-key-material-not-the-kek" * 2
PASSWORD = "a-long-staff-password-for-tests"  # noqa: S105 - test fixture
WRONG_PASSWORD = "wrong-wrong-wrong-wrong"  # noqa: S105 - test fixture
OTHER_PASSWORD = "another-long-staff-password"  # noqa: S105 - test fixture
SHORT_PASSWORD = "short"  # noqa: S105 - test fixture
# The real time, to the minute, not a fixed date: sessions are also judged by the database's own
# clock (`list_staff` counts `expires_at > now()`), so a fixed moment made this suite start failing
# eight hours after it -- the session lifetime -- which CI found at 21:39 UTC on the day it was written.
NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def _seed(enrolment: staff.Enrolment) -> bytes:
    secret = enrolment.secret
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def _code(seed: bytes, moment: datetime) -> str:
    return staff.totp(seed, staff.step_at(moment))


@pytest.fixture
def key() -> staff.StaffKey:
    return staff.StaffKey(STAFF_KEY, kek=TEST_KEK)


@pytest.fixture
def enrolled(db_pool, key):
    """A staff account with a confirmed factor, and its seed."""
    with db.connection() as conn:
        account = staff.create(conn, email="Ops@Example.com", password=PASSWORD, display_name="Ops", actor="tester")
        enrolment = staff.enrol(conn, staff=account, staff_key=key, actor="tester")
        seed = _seed(enrolment)
        confirm_at = NOW - timedelta(minutes=5)
        assert staff.confirm_enrolment(conn, staff=account, code=_code(seed, confirm_at), staff_key=key,
                                       actor="tester", now=confirm_at)
    return account, seed


def _sign_in(key, seed, *, moment=NOW, password=PASSWORD, code=None, email="ops@example.com"):
    with db.connection() as conn:
        return staff.sign_in(conn, email=email, password=password, code=code or _code(seed, moment),
                             staff_key=key, pepper=TEST_PEPPER, now=moment)


# -- TOTP ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unix_time", "eight_digits"),
    # RFC 6238 appendix B, SHA-1; a six-digit code is the last six of the eight.
    [(59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"), (2000000000, "69279037")],
)
def test_totp_matches_the_rfc_6238_vectors(unix_time, eight_digits):
    assert staff.totp(b"12345678901234567890", unix_time // 30) == eight_digits[-6:]


def test_a_code_is_accepted_one_step_either_side_and_only_after_the_last_used_step():
    seed = b"12345678901234567890"
    step = staff.step_at(NOW)
    for offset in (-1, 0, 1):
        assert staff.accepted_step(seed, staff.totp(seed, step + offset), now=NOW, after_step=0) == step + offset
    assert staff.accepted_step(seed, staff.totp(seed, step + 2), now=NOW, after_step=0) is None
    assert staff.accepted_step(seed, staff.totp(seed, step), now=NOW, after_step=step) is None
    assert staff.accepted_step(seed, "12345", now=NOW, after_step=0) is None
    assert staff.accepted_step(seed, "abcdef", now=NOW, after_step=0) is None


# -- the staff key ---------------------------------------------------------------


def test_the_staff_key_refuses_kek_material_and_short_material():
    with pytest.raises(staff.StaffError, match="must not be the KEK"):
        staff.StaffKey(TEST_KEK, kek=TEST_KEK)
    with pytest.raises(staff.StaffError, match="32 bytes"):
        staff.StaffKey(b"short")


def test_a_seed_is_bound_to_its_account(key):
    import uuid

    owner, other = uuid.uuid4(), uuid.uuid4()
    ciphertext, nonce = key.seal(b"seed-bytes-for-a-test", staff_id=owner)
    assert key.open(ciphertext, nonce, staff_id=owner) == b"seed-bytes-for-a-test"
    with pytest.raises(staff.StaffError, match="moved from another account"):
        key.open(ciphertext, nonce, staff_id=other)
    with pytest.raises(staff.StaffError):
        staff.StaffKey(b"a-different-staff-key-material-entirely!").open(ciphertext, nonce, staff_id=owner)


# -- signing in ------------------------------------------------------------------


def test_the_password_and_a_confirmed_code_together_give_a_session_that_resolves(enrolled, key):
    account, seed = enrolled
    token = _sign_in(key, seed)
    assert token and token.startswith("mldb_staff_")
    with db.connection() as conn:
        principal = staff.resolve(conn, presented=token, pepper=TEST_PEPPER, now=NOW + timedelta(minutes=1))
        stored = db.one(conn, "SELECT token_hash FROM staff_sessions")
    assert principal and principal.staff.id == account.id and principal.staff.email == "ops@example.com"
    assert token not in str(stored), "the session token is stored as a verifier only"


def test_no_session_without_a_confirmed_factor(db_pool, key):
    with db.connection() as conn:
        account = staff.create(conn, email="new@example.com", password=PASSWORD, display_name=None, actor="t")
    assert _sign_in(key, b"x" * 20, email="new@example.com", code="000000") is None, "no factor at all"
    with db.connection() as conn:
        seed = _seed(staff.enrol(conn, staff=account, staff_key=key, actor="t"))
    assert _sign_in(key, seed, email="new@example.com") is None, "a factor never confirmed"


def test_a_wrong_password_or_a_wrong_code_is_refused(enrolled, key):
    _, seed = enrolled
    assert _sign_in(key, seed, password=WRONG_PASSWORD) is None
    wrong = f"{(int(_code(seed, NOW)) + 1) % 1000000:06d}"
    assert _sign_in(key, seed, code=wrong) is None
    assert _sign_in(key, seed, email="nobody@example.com") is None


def test_a_code_cannot_be_used_twice(enrolled, key):
    _, seed = enrolled
    code = _code(seed, NOW)
    assert _sign_in(key, seed, code=code)
    assert _sign_in(key, seed, code=code, moment=NOW + timedelta(seconds=5)) is None
    later = NOW + timedelta(seconds=30)
    assert _sign_in(key, seed, moment=later), "the next step's code is fine"


def test_failures_lock_the_account_and_the_lock_passes(enrolled, key):
    _, seed = enrolled
    for _ in range(staff.MAX_FAILED_SIGNINS):
        assert _sign_in(key, seed, password=WRONG_PASSWORD) is None
    assert _sign_in(key, seed) is None, "locked: even the right password and code are refused"
    after = NOW + staff.LOCKOUT + timedelta(minutes=1)
    assert _sign_in(key, seed, moment=after), "the lock expires"
    with db.connection() as conn:
        row = db.one(conn, "SELECT failed_signins, locked_until FROM staff_users")
    assert row["failed_signins"] == 0 and row["locked_until"] is None


# -- staff and customer credentials never cross ---------------------------------


def test_a_staff_token_is_not_a_customer_credential_and_the_reverse(enrolled, key):
    _, seed = enrolled
    staff_token = _sign_in(key, seed)
    with db.connection() as conn:
        user, _ = identity.create_user_with_personal_org(conn, email="ops@example.com", password=PASSWORD)
        customer_session = identity.create_session(conn, user_id=user.id, pepper=TEST_PEPPER)
        customer_pat = identity.create_pat(conn, user_id=user.id, name="t", pepper=TEST_PEPPER)
        conn.commit()

        assert identity.resolve_principal(conn, presented=staff_token, pepper=TEST_PEPPER) is None
        for customer in (customer_session, customer_pat):
            assert staff.resolve(conn, presented=customer, pepper=TEST_PEPPER, now=NOW) is None
            assert staff.sign_out(conn, presented=customer, pepper=TEST_PEPPER) is False
    assert _sign_in(key, seed, email="ops@example.com", moment=NOW + timedelta(seconds=30)), (
        "a customer account with the same address changes nothing on the staff side"
    )


def test_a_forged_staff_token_with_a_customer_verifier_does_not_resolve(enrolled, key, db_pool):
    """The kinds differ in the text, so the verifiers differ too; a relabelled token is just unknown."""
    with db.connection() as conn:
        user, _ = identity.create_user_with_personal_org(conn, email="c@example.com", password=PASSWORD)
        customer_session = identity.create_session(conn, user_id=user.id, pepper=TEST_PEPPER)
        conn.commit()
        relabelled = customer_session.replace("mldb_sess_", "mldb_staff_", 1)
        assert staff.resolve(conn, presented=relabelled, pepper=TEST_PEPPER, now=NOW) is None


# -- sessions end ------------------------------------------------------------------


def test_a_session_ends_when_idle_and_at_its_lifetime(enrolled, key):
    _, seed = enrolled
    token = _sign_in(key, seed)
    with db.connection() as conn:
        assert staff.resolve(conn, presented=token, pepper=TEST_PEPPER, now=NOW + staff.IDLE_TIMEOUT
                             + timedelta(seconds=1)) is None, "idle"
    token = _sign_in(key, seed, moment=NOW + timedelta(seconds=30))
    moment = NOW
    with db.connection() as conn:
        while moment < NOW + staff.SESSION_LIFETIME - timedelta(minutes=20):
            moment += timedelta(minutes=20)
            assert staff.resolve(conn, presented=token, pepper=TEST_PEPPER, now=moment), moment
        assert staff.resolve(conn, presented=token, pepper=TEST_PEPPER,
                             now=NOW + staff.SESSION_LIFETIME + timedelta(minutes=1)) is None, "lifetime"


@pytest.mark.parametrize("change", ["revoke", "enrol", "password", "sign_out"])
def test_revoking_re_enrolling_or_setting_the_password_ends_sessions(enrolled, key, change):
    account, seed = enrolled
    token = _sign_in(key, seed)
    with db.connection() as conn:
        if change == "revoke":
            staff.revoke(conn, staff=account, actor="tester")
        elif change == "enrol":
            staff.enrol(conn, staff=account, staff_key=key, actor="tester")
        elif change == "password":
            staff.set_password(conn, staff=account, password=OTHER_PASSWORD, actor="tester")
        else:
            assert staff.sign_out(conn, presented=token, pepper=TEST_PEPPER)
        conn.commit()
        assert staff.resolve(conn, presented=token, pepper=TEST_PEPPER, now=NOW + timedelta(minutes=1)) is None


def test_a_revoked_account_cannot_sign_in_and_its_address_can_be_reused(enrolled, key):
    account, seed = enrolled
    with db.connection() as conn:
        staff.revoke(conn, staff=account, actor="tester")
    assert _sign_in(key, seed) is None
    with db.connection() as conn:
        again = staff.create(conn, email="ops@example.com", password=PASSWORD, display_name=None, actor="t")
        assert again.id != account.id
        with pytest.raises(staff.StaffError, match="already has an active"):
            staff.create(conn, email="ops@example.com", password=PASSWORD, display_name=None, actor="t")


def test_short_passwords_are_refused(db_pool):
    with db.connection() as conn, pytest.raises(staff.StaffError, match="at least"):
        staff.create(conn, email="s@example.com", password=SHORT_PASSWORD, display_name=None, actor="t")


# -- audit and listings --------------------------------------------------------------


def test_account_changes_and_sign_ins_are_audited_as_staff(enrolled, key):
    _, seed = enrolled
    _sign_in(key, seed)
    _sign_in(key, seed, password=WRONG_PASSWORD, moment=NOW + timedelta(seconds=30))
    with db.connection() as conn:
        rows = db.query(conn, "SELECT actor_type, actor_id, event_type, detail_json FROM audit_events ORDER BY id")
    events = [r["event_type"] for r in rows]
    for expected in ("staff.created", "staff.enrolled", "staff.factor_confirmed", "staff.signin",
                     "staff.signin_failed"):
        assert expected in events
    assert {r["actor_type"] for r in rows} == {"staff"}
    assert rows[0]["actor_id"] == "tester", "an account change names the OS account that ran it"
    for row in rows:
        text = str(row["detail_json"])
        assert PASSWORD not in text and "mldb_staff_" not in text and "seed" not in text


def test_the_listing_carries_no_secret(enrolled, key):
    _, seed = enrolled
    _sign_in(key, seed)
    with db.connection() as conn:
        rows = staff.list_staff(conn)
    assert len(rows) == 1 and rows[0]["live_sessions"] == 1 and rows[0]["factor_confirmed_at"] is not None
    for field in rows[0]:
        assert not re.search(r"hash|seed|cipher|nonce|token", field), field


# -- structure -----------------------------------------------------------------------


def test_staff_code_and_schema_do_not_touch_customer_identity_tables():
    module = (ROOT / "services" / "control_plane" / "staff.py").read_text()
    migration = (ROOT / "services" / "control_plane" / "migrations" / "0051_staff_identity.sql").read_text()
    sql = " ".join(re.findall(r'"""(.*?)"""|"([^"]*)"', module, re.S).__str__().split())
    for table in ("users", "user_sessions", "personal_access_tokens", "org_members", "organizations",
                  "user_mfa_factors", "encryption_keys"):
        assert not re.search(rf"\b(FROM|JOIN|INTO|UPDATE|REFERENCES)\s+{table}\b", sql, re.I), table
        assert not re.search(rf"REFERENCES\s+{table}\b", migration, re.I), table
    assert "KeyRing" not in module, "the staff side never loads the KEK's key ring"


def test_the_staff_token_kind_is_registered_for_log_redaction():
    assert "staff" in hashing.TOKEN_KINDS


# -- cp-manage staff -----------------------------------------------------------------


def test_cp_manage_staff_create_enrol_list_and_revoke(db_pool, monkeypatch, tmp_path, capsys):
    """The commands an operator runs, with the prompts answered: the account signs in only once confirmed."""
    import getpass

    from services.control_plane import manage

    key_file = tmp_path / "staff-key"
    key_file.write_bytes(STAFF_KEY)
    key_file.chmod(0o600)
    kek_file = tmp_path / "kek"
    kek_file.write_bytes(TEST_KEK)
    kek_file.chmod(0o600)
    monkeypatch.setenv("MALUDB_STAFF_KEY_REF", str(key_file))
    monkeypatch.setenv("MALUDB_KEK_REF", str(kek_file))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr(manage.db, "init_pool", lambda url: None)
    monkeypatch.setattr(manage.db, "close_pool", lambda: None)

    printed_secret = {}

    def answer(prompt=""):
        out = capsys.readouterr().out
        match = re.search(r"secret: ([A-Z2-7]+)", out)
        if match:
            printed_secret["seed"] = base64.b32decode(match.group(1) + "=" * (-len(match.group(1)) % 8))
        return staff.totp(printed_secret["seed"], staff.step_at(datetime.now(UTC)))

    monkeypatch.setattr("builtins.input", answer)
    assert manage.main(["staff", "create", "--email", "cli@example.com", "--name", "CLI"]) == 0
    assert "confirmed: cli@example.com can sign in" in capsys.readouterr().out

    assert manage.main(["staff", "list"]) == 0
    listing = capsys.readouterr().out
    assert "cli@example.com" in listing and "confirmed" in listing

    # Same material as the KEK is refused before anything is written.
    monkeypatch.setenv("MALUDB_STAFF_KEY_REF", str(kek_file))
    assert manage.main(["staff", "enrol", "--email", "cli@example.com"]) == 1
    assert "must not be the KEK" in capsys.readouterr().err
    monkeypatch.setenv("MALUDB_STAFF_KEY_REF", str(key_file))

    assert manage.main(["staff", "revoke", "--email", "cli@example.com"]) == 0
    with db.connection() as conn:
        assert staff.find_active(conn, "cli@example.com") is None
        actors = {r["actor_id"] for r in db.query(
            conn, "SELECT actor_id FROM audit_events WHERE event_type LIKE 'staff.%%'")}
    assert actors and all(a for a in actors)
