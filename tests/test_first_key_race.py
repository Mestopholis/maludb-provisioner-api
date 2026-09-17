"""Two processes starting together mint one first key, not two (rehearsal finding 11).

The public and internal listeners are started by one `systemctl` command on a clean install. Both
read an empty `encryption_keys`, both inserted version 1, and one died on the primary key --
recovered by `Restart=`, so nothing was lost, but a crash in a deployment's first seconds reads as
a broken install.

These run against a scratch control-plane database of their own: the suite's own database has keys,
and the case under test is the first second of a database that has none.
"""

from __future__ import annotations

import threading

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from services.control_plane import crypto, migrate
from tests.conftest import DATABASE_URL, TEST_KEK

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="MALUDB_CONTROL_PLANE_DATABASE_URL is unset")

SCRATCH = "maludb_first_key_race_test"


def _dsn(database: str) -> str:
    parts = conninfo_to_dict(DATABASE_URL)
    parts["dbname"] = database
    return make_conninfo(**parts)


@pytest.fixture
def virgin_database():
    """A migrated control-plane database with no keys, dropped afterwards."""
    with psycopg.connect(_dsn("postgres"), autocommit=True) as admin:
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{SCRATCH}"')
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("the control-plane role cannot CREATE DATABASE here")
    migrate.run(_dsn(SCRATCH))
    try:
        yield _dsn(SCRATCH)
    finally:
        with psycopg.connect(_dsn("postgres"), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')


def _load(dsn: str, results: list, index: int) -> None:
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            ring = crypto.KeyRing(TEST_KEK)
            ring.load(conn)
            results[index] = ring
    except Exception as exc:  # noqa: BLE001 - the failure is the finding; report it as one
        results[index] = exc


def test_both_listeners_start_and_share_one_key(virgin_database):
    """Before the fix one of these raised UniqueViolation on encryption_keys_pkey."""
    results = [None, None]
    threads = [threading.Thread(target=_load, args=(virgin_database, results, i)) for i in range(2)]
    barrier = threading.Barrier(2, timeout=10)
    for thread in threads:
        thread.start()
    del barrier
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a listener blocked on the first-key lock"

    for result in results:
        assert not isinstance(result, Exception), f"a listener failed to start: {result!r}"
    with psycopg.connect(virgin_database, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute("SELECT key_version, state FROM encryption_keys").fetchall()
    assert [(r["key_version"], r["state"]) for r in rows] == [(1, "active")], "exactly one first key"
    # Both processes hold the same key, which is what lets either read what the other wrote.
    first, second = results
    secret = b"a value one listener sealed"
    sealed = first.seal(secret, aad=crypto.aad_for("nodes", "admin_ciphertext", "1"))
    assert second.open(sealed, aad=crypto.aad_for("nodes", "admin_ciphertext", "1")) == secret


def test_a_restored_dump_that_lost_its_keys_is_still_refused(virgin_database):
    """ADR-070's guard, which the lock must not have moved out of the way: ciphertext with no key
    is a restore that dropped `encryption_keys`, and minting a fresh one destroys the data."""
    with psycopg.connect(virgin_database, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        # project_email_settings.hook_ciphertext keeps its key version nullable, which is exactly
        # the shape a restore that dropped encryption_keys leaves: ciphertext, and nothing to read it.
        conn.execute("INSERT INTO plans (code, name) VALUES ('race', 'Race') ON CONFLICT DO NOTHING")
        org = conn.execute(
            "INSERT INTO organizations (id, slug, display_name, is_personal) "
            "VALUES (gen_random_uuid(), 'race-org', 'Race', false) RETURNING id").fetchone()["id"]
        project = conn.execute(
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, database_name) "
            "SELECT gen_random_uuid(), %s, 'race0001', 'race', id, 'PROVISIONED', 'mldb_race0001' "
            "FROM plans WHERE code = 'race' RETURNING id", (org,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO project_email_settings (project_id, sender_mode, sender_address, hook_ciphertext, "
            "hook_nonce, hook_key_version) VALUES (%s, 'platform_default', 'noreply@example.com', %s, %s, NULL)",
            (project, b"ciphertext", b"nonce"),
        )

    with psycopg.connect(virgin_database, row_factory=psycopg.rows.dict_row) as conn:
        with pytest.raises(crypto.CryptoError, match="restore|ciphertext"):
            crypto.KeyRing(TEST_KEK).load(conn)
    with psycopg.connect(virgin_database, row_factory=psycopg.rows.dict_row) as conn:
        assert conn.execute("SELECT count(*) AS n FROM encryption_keys").fetchone()["n"] == 0, \
            "no key was minted over a restored database"


def test_the_lock_is_transaction_scoped_so_a_crash_cannot_wedge_a_deployment(virgin_database):
    """A session-scoped lock left by a process that died would block every later start."""
    with psycopg.connect(virgin_database, row_factory=psycopg.rows.dict_row) as conn:
        crypto.KeyRing(TEST_KEK).load(conn)
        held = conn.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        ).fetchone()["n"]
    assert held == 0, "the first-key lock outlived its transaction"
