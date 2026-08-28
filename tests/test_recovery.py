"""The control plane's own recovery (Phase 11 slice 5, ADR-070).

Slices 1 to 4 make a *node* recoverable. This covers the thing that administers
nodes, and the failure it exists to prevent was measured before the guard
existed rather than reasoned about:

**A control plane restored from a dump that omitted `encryption_keys` started up
successfully.** `KeyRing.load` found no keys, minted a fresh version 1, marked it
active and returned. The service was healthy by every check it had, every
ciphertext in the restored database was permanently undecryptable, and the new
key occupied the version the real keys needed — so the silent success destroyed
the recovery path as well as the data. The failure only surfaced later, one
secret at a time, as `ciphertext failed authentication`.

Two claims are therefore under test and they are different in kind. A dump that
cannot restore a working platform is **not a backup**, whatever its exit code.
And a restore is not verified by the control plane *starting* — it is verified by
opening something.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - building a restore fixture
import uuid

import psycopg
import pytest

from services.control_plane import crypto, db, recovery
from tests.conftest import TEST_KEK, requires_db

pytestmark = [requires_db]


# --------------------------------------------------------------------------
# The guard on minting a first key
# --------------------------------------------------------------------------


def _as_a_keyless_restore_would_leave_it(conn) -> None:
    """Reproduce the state a keyless restore actually produces.

    Not `DELETE FROM encryption_keys` -- the schema's foreign keys forbid that on
    a live database, which is a real defence and worth not pretending away. What
    a restore leaves is different and worse: the dump could not recreate those
    constraints (psql prints an ERROR for each, and carries on unless
    ON_ERROR_STOP is set), so the restored database holds every secret, has no
    keys, and is *missing the foreign keys that would have objected*.

    So the constraints are dropped first, exactly as the failed restore did.
    """
    db.execute(conn, "ALTER TABLE nodes DROP CONSTRAINT IF EXISTS nodes_admin_key_version_fkey")
    db.execute(
        conn,
        "ALTER TABLE nodes DROP CONSTRAINT IF EXISTS nodes_storage_secret_key_version_fkey",
    )
    db.execute(
        conn,
        "ALTER TABLE project_credentials "
        "  DROP CONSTRAINT IF EXISTS project_credentials_key_version_fkey",
    )
    db.execute(conn, "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_key_version_fkey")
    db.execute(conn, "DELETE FROM encryption_keys")
    conn.commit()


def test_a_virgin_database_still_bootstraps(db_pool):
    """The guard must not break a new deployment.

    A control plane with no keys *and no secrets* is a fresh install, and
    minting the first key is exactly right there. If this ever fails, the guard
    has been written too broadly and every new deployment is bricked.
    """
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM encryption_keys")
        conn.commit()
        crypto.KeyRing(TEST_KEK).load(conn)
        row = db.one(conn, "SELECT key_version, state FROM encryption_keys")
    assert row["key_version"] == 1 and row["state"] == "active"


def test_a_database_holding_secrets_but_no_keys_is_refused(db_pool, node_with_secret):
    """The measured failure, now a refusal.

    Before ADR-070 this path succeeded and made the loss permanent.
    """
    with db.connection() as conn:
        _as_a_keyless_restore_would_leave_it(conn)
        with pytest.raises(crypto.CryptoError) as raised:
            crypto.KeyRing(TEST_KEK).load(conn)

        message = str(raised.value)
        assert "nodes.admin_ciphertext" in message, "the refusal must name what it protected"
        assert "ADR-070" in message

        # And nothing was written. A guard that refused *after* minting would
        # have destroyed the recovery path while reporting an error.
        assert db.one(conn, "SELECT count(*) AS n FROM encryption_keys")["n"] == 0


def test_the_refusal_names_every_kind_of_secret_it_found(db_pool, node_with_secret):
    """So an operator learns the blast radius from the error, not from a document."""
    with db.connection() as conn:
        project_id = _a_project(conn)
        _a_credential(conn, project_id)
        _as_a_keyless_restore_would_leave_it(conn)
        with pytest.raises(crypto.CryptoError) as raised:
            crypto.KeyRing(TEST_KEK).load(conn)
    message = str(raised.value)
    assert "nodes.admin_ciphertext" in message
    assert "project_credentials.ciphertext" in message


def test_the_guard_and_the_break_glass_list_cover_the_same_columns():
    """Two lists that must agree, asserted rather than maintained by hope.

    `crypto` checks these columns to decide whether to refuse; `recovery` lists
    them to say what losing each one costs. Whoever adds the next encrypted
    column has to update both, and this is what tells them.
    """
    import inspect

    source = inspect.getsource(crypto.KeyRing._refuse_if_secrets_exist)
    for table, column, _ in recovery.CLASS_B_COLUMNS:
        assert f'"{table}"' in source and f'"{column}"' in source, (
            f"{table}.{column} is in recovery.CLASS_B_COLUMNS but the crypto guard "
            "does not check it, so a restore that lost the keys would be waved through"
        )


def test_every_break_glass_entry_says_what_is_lost():
    """A classification with an empty cell is one nobody can act on."""
    for name, consequence in recovery.break_glass():
        assert len(consequence) > 40, f"{name} has no usable consequence recorded"


# --------------------------------------------------------------------------
# A dump that cannot restore a platform is not a backup
# --------------------------------------------------------------------------


def test_a_dump_without_key_material_is_refused(tmp_path):
    """`--exclude-table-data=encryption_keys` exits 0 and produces this.

    The check reads the COPY blocks rather than trusting the exit code, because
    the whole failure mode is a dump that succeeded and is missing one table.
    """
    path = tmp_path / "no-keys.sql"
    path.write_text(
        "COPY public.nodes (id, admin_ciphertext) FROM stdin;\n"
        "1\t\\\\x00\n"
        "\\.\n"
    )
    report = recovery.inspect_dump(str(path))
    assert report.key_rows == 0
    assert not report.ok
    assert "no `encryption_keys` rows" in report.problems()[0]
    assert "nodes" in report.problems()[0], "the refusal should say what is at risk"


def test_a_dump_with_key_material_passes(tmp_path):
    path = tmp_path / "keys.sql"
    path.write_text(
        "COPY public.encryption_keys (key_version, wrapped_dek) FROM stdin;\n"
        "1\t\\\\x00\n"
        "\\.\n"
    )
    report = recovery.inspect_dump(str(path))
    assert report.key_rows == 1
    assert report.ok
    assert report.problems() == []


def test_a_backup_always_says_the_kek_is_not_in_it(tmp_path):
    """The dependency that makes this file insufficient on its own (ADR-023).

    Stated on every backup rather than in a runbook, because the moment somebody
    needs to know is the moment they are holding only this file.
    """
    path = tmp_path / "keys.sql"
    path.write_text("COPY public.encryption_keys (a) FROM stdin;\n1\n\\.\n")
    report = recovery.inspect_dump(str(path))
    assert any("KEK is NOT in this file" in note for note in report.notes())


def test_an_unreadable_dump_is_an_error_not_an_empty_one(tmp_path):
    report = recovery.inspect_dump(str(tmp_path / "does-not-exist.sql"))
    assert report.error is not None
    assert not report.ok


def test_a_real_dump_is_written_private(tmp_path):
    """It carries every node's admin DSN. Ciphertext, and still not world-readable."""
    path = tmp_path / "cp.sql"
    report = recovery.dump(
        os.environ["MALUDB_CONTROL_PLANE_DATABASE_URL"], path=str(path)
    )
    assert report.error is None, report.error
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600", (
        "the dump holds every node's credential and was left group- or world-readable"
    )


def test_a_password_never_reaches_a_command_line():
    """`/proc/<pid>/cmdline` is world-readable; `/proc/<pid>/environ` is not.

    The DSN this module hands to `pg_dump` opens the database holding every
    node's admin credential. For as long as that process ran, a password in argv
    was readable by every local account out of the process table.
    """
    safe, env = recovery.split_password("postgresql://u:sekrit@h:5432/db")
    assert "sekrit" not in safe
    assert env["PGPASSWORD"] == "sekrit"
    assert "dbname=db" in safe and "user=u" in safe, "the rest of the DSN must survive"


def test_a_dsn_with_no_password_needs_no_environment_override():
    """Peer or trust authentication is normal for a local control plane."""
    safe, env = recovery.split_password("postgresql:///maludb_control_plane")
    assert "PGPASSWORD" not in env or env.get("PGPASSWORD") == os.environ.get("PGPASSWORD")
    assert "maludb_control_plane" in safe


# --------------------------------------------------------------------------
# A restore is verified by opening something
# --------------------------------------------------------------------------


def test_a_restore_is_not_verified_by_a_control_plane_that_holds_nothing(db_pool):
    """The vacuous pass this phase keeps finding, refused here explicitly.

    A control plane with no secrets *and* no ability to decrypt is
    indistinguishable from a healthy one if the only question asked is "did
    anything fail". This asserts the distinction the dataclass draws.
    """
    with db.connection() as conn:
        proof = recovery.verify_restore(conn, kek=TEST_KEK)
    assert proof.ok, "an empty control plane is consistent, just not evidence of much"
    assert proof.unwrapped == {}


def test_a_restore_with_the_wrong_kek_is_refused(db_pool, node_with_secret):
    """Total rather than per-row, and it must be reported as such."""
    with db.connection() as conn:
        proof = recovery.verify_restore(conn, kek=b"a-different-kek-entirely" * 2)
    assert not proof.ok
    assert proof.error is not None
    assert "KEK" in proof.error


def test_a_restore_unwraps_a_node_admin_credential(db_pool, node_with_secret):
    """The acceptance criterion's first half: the key material survived."""
    with db.connection() as conn:
        proof = recovery.verify_restore(conn, kek=TEST_KEK)
    assert proof.ok, proof.failures
    assert proof.unwrapped.get("nodes.admin_ciphertext") == 1
    assert proof.encrypted_values.get("nodes.admin_ciphertext") == 1


def test_a_node_that_is_down_is_not_reported_as_a_key_failure(db_pool, node_with_secret):
    """A down node and lost key material are different incidents.

    Conflating them would send an operator hunting for a KEK problem during an
    outage that has nothing to do with one.
    """
    def refuse(dsn):
        raise psycopg.OperationalError("connection refused")

    with db.connection() as conn:
        proof = recovery.verify_restore(conn, kek=TEST_KEK, reach_nodes=True, connect=refuse)

    assert proof.ok, "a down node must not make key material look broken"
    assert proof.failures == []
    assert proof.nodes_unreachable and "OperationalError" in proof.nodes_unreachable[0]
    assert proof.nodes_reached == []


def test_reaching_a_node_is_what_proves_administration(db_pool, node_with_secret):
    """The acceptance criterion's second half.

    Decrypting a credential proves the key material survived; connecting proves
    the credential is still true. They are different claims and the report keeps
    them apart.
    """
    opened = []

    class _Conn:
        def close(self):
            opened.append("closed")

    with db.connection() as conn:
        proof = recovery.verify_restore(
            conn, kek=TEST_KEK, reach_nodes=True, connect=lambda dsn: _Conn()
        )

    assert proof.nodes_reached == ["rec-node"]
    assert opened == ["closed"], "the verification leaked a node connection"


def test_a_credential_plaintext_never_reaches_a_failure_message(db_pool, node_with_secret):
    """`docs/SECRETS.md` forbids logging anything recovered from Class B storage.

    The failure path records an exception *type*, never a message, because a
    psycopg error can carry the DSN it failed on.
    """
    with db.connection() as conn:
        project_id = _a_project(conn)
        _a_credential(conn, project_id, corrupt=True)
        conn.commit()
        proof = recovery.verify_restore(conn, kek=TEST_KEK)

    assert proof.failures, "a corrupt credential should have been reported"
    for failure in proof.failures:
        assert "CryptoError" in failure or "Error" in failure
        assert "postgresql://" not in failure and "password" not in failure.lower()


# --------------------------------------------------------------------------
# The whole cycle, against real databases
# --------------------------------------------------------------------------


def test_a_dump_restored_into_a_fresh_database_can_still_decrypt(db_pool, node_with_secret, tmp_path):
    """Dump, restore elsewhere, and open a credential from the copy.

    The acceptance criterion end to end, using real `pg_dump` and `psql` against
    a real second database rather than a re-read of the same one -- a restore
    that verified itself against its own source would prove nothing.
    """
    superuser = os.environ.get("MALUDB_NODE_ADMIN_DSN", "").strip()
    if not superuser:
        pytest.skip("MALUDB_NODE_ADMIN_DSN is needed to create the restore target database")

    source = os.environ["MALUDB_CONTROL_PLANE_DATABASE_URL"]
    path = tmp_path / "cycle.sql"
    report = recovery.dump(source, path=str(path))
    assert report.ok, report.problems()
    assert report.key_rows >= 1

    target = f"maludb_cp_cycle_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(superuser, autocommit=True)
    try:
        with admin.cursor() as cur:
            # Owned by the role the dump's objects belong to, so the restore
            # does not need superuser -- only creating the database does.
            cur.execute(f'CREATE DATABASE "{target}" OWNER {_owner_of(source)}')
    finally:
        admin.close()

    restored_dsn = _swap_database(source, target)
    try:
        safe_dsn, env = recovery.split_password(restored_dsn)
        loaded = subprocess.run(  # noqa: S603 - argv list, no shell
            ["psql", "--quiet", "--no-psqlrc", "-v", "ON_ERROR_STOP=1",  # noqa: S607 - suite PATH
             "-f", str(path), safe_dsn],
            capture_output=True, text=True, timeout=300, check=False, env=env,
        )
        assert loaded.returncode == 0, loaded.stderr[-400:]

        with psycopg.connect(restored_dsn) as copy:
            proof = recovery.verify_restore(copy, kek=TEST_KEK)

        assert proof.ok, f"{proof.error} {proof.failures}"
        assert proof.unwrapped.get("nodes.admin_ciphertext") == 1, (
            "the restored copy could not open a node credential, which is the "
            "whole acceptance criterion"
        )
    finally:
        admin = psycopg.connect(superuser, autocommit=True)
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
        finally:
            admin.close()


def test_a_restore_that_lost_the_keys_is_caught_before_it_is_used(db_pool, node_with_secret, tmp_path):
    """The measured failure, reproduced through the real tooling.

    A dump taken with `--exclude-table-data=encryption_keys` -- which exits 0 --
    is refused by `inspect_dump`, and if it were restored anyway the key ring
    refuses to mint over it. Two independent guards, because the first one is
    only run by whoever remembers to run it.
    """
    source = os.environ["MALUDB_CONTROL_PLANE_DATABASE_URL"]
    path = tmp_path / "keyless.sql"
    safe_dsn, env = recovery.split_password(source)
    taken = subprocess.run(  # noqa: S603 - argv list, no shell
        ["pg_dump", "--format=plain", "--no-owner",  # noqa: S607 - suite PATH
         "--exclude-table-data=encryption_keys", "--file", str(path), safe_dsn],
        capture_output=True, text=True, timeout=300, check=False, env=env,
    )
    assert taken.returncode == 0, "pg_dump refused; the premise of this test is that it does not"

    report = recovery.inspect_dump(str(path))
    assert report.key_rows == 0
    assert not report.ok, "a dump that cannot restore a working platform passed as a backup"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _owner_of(dsn: str) -> str:
    """The role the control-plane DSN connects as, quoted for a CREATE DATABASE."""
    user = psycopg.conninfo.conninfo_to_dict(dsn).get("user") or "postgres"
    if not user.replace("_", "").isalnum():
        raise AssertionError(f"unusable role name {user!r}")
    return f'"{user}"'


def _swap_database(dsn: str, database: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    parsed["dbname"] = database
    return psycopg.conninfo.make_conninfo(**parsed)


def _a_project(conn) -> uuid.UUID:
    org = db.one(
        conn,
        "INSERT INTO organizations (id, slug, display_name) VALUES (%s,%s,'R') RETURNING id",
        (uuid.uuid4(), f"rec-{uuid.uuid4().hex[:8]}"),
    )["id"]
    plan = db.one(
        conn,
        "INSERT INTO plans (code,name) VALUES (%s,'R') RETURNING id",
        (f"rec-{uuid.uuid4().hex[:8]}",),
    )["id"]
    project_id = uuid.uuid4()
    db.execute(
        conn,
        "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
        "VALUES (%s,%s,%s,'r',%s,'PROVISIONED')",
        (project_id, org, uuid.uuid4().hex[:8], plan),
    )
    return project_id


def _a_credential(conn, project_id: uuid.UUID, *, corrupt: bool = False) -> None:
    key_ring = crypto.KeyRing(TEST_KEK)
    key_ring.load(conn)
    sealed = key_ring.seal(
        b"a-tenant-password",
        aad=crypto.aad_for("project_credentials", "ciphertext", f"{project_id}:db_password"),
    )
    ciphertext = b"\x00" * len(sealed.ciphertext) if corrupt else sealed.ciphertext
    db.execute(
        conn,
        "INSERT INTO project_credentials (id, project_id, credential_type, role_name, "
        "ciphertext, nonce, key_version) VALUES (%s,%s,'db_password','r',%s,%s,%s)",
        (uuid.uuid4(), project_id, ciphertext, sealed.nonce, sealed.key_version),
    )
    conn.commit()


@pytest.fixture
def node_with_secret(db_pool):
    """A node whose admin DSN is sealed with the suite's KEK."""
    with db.connection() as conn:
        key_ring = crypto.KeyRing(TEST_KEK)
        key_ring.load(conn)
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status) "
            "VALUES ('rec-node','rec.example','rec.internal','shared','active') RETURNING id",
        )["id"]
        sealed = key_ring.seal(
            b"postgresql://postgres:secret@127.0.0.1:5432/postgres",
            aad=crypto.aad_for("nodes", "admin_ciphertext", str(node_id)),
        )
        db.execute(
            conn,
            "UPDATE nodes SET admin_ciphertext = %s, admin_nonce = %s, admin_key_version = %s "
            " WHERE id = %s",
            (sealed.ciphertext, sealed.nonce, sealed.key_version, node_id),
        )
        conn.commit()
    return node_id
