"""The control plane's own backup, and proving a restored one still works.

Phase 11 slice 5. Slices 1 to 4 make a *node* recoverable. This makes the thing
that administers nodes recoverable, which is the higher-consequence half and was
in no phase's scope bullets until this plan added it.

## Recovery needs two artefacts, and neither is sufficient

ADR-023 keeps the KEK out of the control-plane database on purpose: "a dump of
it must be useless alone". That property is exactly right and it has a
consequence nobody had written down — **a backup of the control-plane database
is not a backup of the control plane.** Restoring it gives a database full of
ciphertext and no way to read any of it. The KEK is a second artefact, stored
somewhere else by design, and recovery needs both.

So the two failure modes are symmetric and both are total:

- **The dump without the KEK.** Every node admin DSN, every project credential,
  every MFA seed is ciphertext nobody can open. The platform cannot administer a
  single node it still owns.
- **The KEK without the dump.** Nothing to decrypt.

## The failure this module exists to prevent

Measured, and it takes two failures rather than one — which is why it survived
this long unnoticed.

The schema defends itself once. `nodes.admin_key_version` and
`project_credentials.key_version` are foreign keys into `encryption_keys`, so a
dump that omitted that table cannot restore those constraints, and psql prints an
ERROR for each one.

**`ON_ERROR_STOP` is opt-in.** A restore that does not set it — the default, and
what any `psql -f dump.sql` in a recovery script does — prints those errors and
carries on. What it leaves is a database holding every secret, missing those
foreign keys, and containing no keys at all.

Then the control plane started, and *that* is where it became unrecoverable:
`KeyRing.load` found no keys, minted a fresh version 1, marked it active, and
returned **successfully**. The service was healthy by every check it had. Every
ciphertext in the restored database was permanently undecryptable from that
moment, and the failure surfaced later, one secret at a time, as `ciphertext
failed authentication` when somebody tried to administer a node. The new key also
occupied version 1, so the real keys could no longer be re-imported without a
collision — the silent success destroyed the recovery path as well as the data.

Both halves are addressed. The runbook requires `ON_ERROR_STOP=1`, so the
constraint errors stop the restore where they happen; and `crypto.KeyRing.load`
refuses to mint over a database that already holds ciphertext, because a runbook
is a thing people follow under pressure at three in the morning.

Two things follow, and they are the whole design here. **A dump that does not
contain key material is not a backup**, and `verify_dump` refuses it rather than
recording it. And **a restore is not verified by the control plane starting**;
it is verified by unwrapping something, which is what `verify_restore` does.

## What this deliberately does not do

It does not decide where the KEK lives. That is `docs/OPEN-QUESTIONS.md`'s
load-bearing open question and a production deployment decision, not a slice's
to take. What this does is make the *dependency* explicit and checkable: the
report says which artefacts a recovery needs and refuses to call a backup
complete without them.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess  # noqa: S404 - pg_dump is a command; there is no library
from dataclasses import dataclass, field

import psycopg

from services.control_plane import crypto, db

log = logging.getLogger("maludb.recovery")


class RecoveryError(RuntimeError):
    """The control plane could not be backed up, or a restore could not be proved."""


# Every Class B column in the schema, as (table, column, what losing it costs).
# Enumerated rather than discovered, because the point of the list is the third
# element: a query can find the columns, but only a person can say what a
# customer loses when one becomes unreadable.
#
# `crypto.KeyRing._refuse_if_secrets_exist` checks the same set from the other
# direction. Two lists that must agree is a smell, so the test suite asserts
# they do rather than leaving it to whoever adds the next encrypted column.
CLASS_B_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "nodes",
        "admin_ciphertext",
        "the superuser DSN for a node. Regenerable by an operator with access to the "
        "node: reset the role's password and re-record it. Costs a touch of every node, "
        "loses no customer data.",
    ),
    (
        "nodes",
        "storage_secret_ciphertext",
        "the object store's credential. Regenerable from the object store's own "
        "configuration. Loses no customer data.",
    ),
    (
        "project_credentials",
        "ciphertext",
        "per-project database passwords AND per-project JWT signing keys. The passwords "
        "are resettable. **The signing keys are not**: every access token and refresh "
        "token ever issued to a tenant's end users stops verifying, so every end user of "
        "every project is signed out and must authenticate again. Nothing is corrupted "
        "and nothing is unrecoverable, but this is the one that a customer notices.",
    ),
    (
        "api_keys",
        "ciphertext",
        "the publishable key, which ADR-023 stores recoverably because a dashboard must "
        "be able to show it again. Losing it does not disable the key -- verification is "
        "Class A and independent -- it means the dashboard can no longer display it, and "
        "the fix is to issue a new one, which invalidates whatever is in a customer's "
        "deployed client bundle.",
    ),
    (
        "project_email_settings",
        "malumail_ciphertext",
        "the project's SMTP credential. Customer-supplied for a custom sending domain, so "
        "the platform cannot regenerate it: the customer re-enters it. Until they do, that "
        "project sends no email.",
    ),
    (
        "project_email_settings",
        "hook_ciphertext",
        "the auth hook secret. Same shape as the SMTP credential: customer-supplied, "
        "re-entered by the customer.",
    ),
    (
        "user_mfa_factors",
        "ciphertext",
        "TOTP seeds for platform users. **Unrecoverable.** Every user with MFA must "
        "re-enrol, which needs an account-recovery path that does not itself depend on "
        "MFA -- so this is the row that decides whether losing the KEK locks the operators "
        "out of their own dashboard.",
    ),
)


def split_password(dsn: str) -> tuple[str, dict[str, str]]:
    """A DSN safe to put in argv, and the environment carrying its password.

    A connection string with a password in it must never reach a command line.
    `/proc/<pid>/cmdline` is world-readable on Linux, so for as long as `pg_dump`
    runs, every local account can read the control-plane superuser's password out
    of the process table -- and this is the credential that opens the database
    holding every node's admin DSN. `/proc/<pid>/environ` is 0600, which is why
    libpq offers `PGPASSWORD` at all.

    Returns the DSN with the password removed and an environment overlay to pass
    to the child. Both halves are needed: dropping the password without supplying
    it elsewhere turns a working backup into a prompt nobody answers.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    password = parsed.pop("password", None)
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = str(password)
    return psycopg.conninfo.make_conninfo(**parsed), env


@dataclass
class DumpReport:
    """A control-plane dump, and whether it is worth having."""

    path: str
    bytes_written: int = 0
    # Rows of `encryption_keys` the dump carries. Zero means the dump cannot
    # restore a usable control plane, whatever else it contains.
    key_rows: int = 0
    tables_with_secrets: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.key_rows > 0

    def problems(self) -> list[str]:
        if self.error:
            return [self.error]
        if self.key_rows == 0:
            holders = ", ".join(f"{t} ({n})" for t, n in self.tables_with_secrets.items())
            return [
                "this dump carries no `encryption_keys` rows"
                + (f" but does carry ciphertext in {holders}" if holders else "")
                + ". Restoring it produces a control plane that cannot decrypt anything it "
                "holds, and -- before the ADR-070 guard -- one that would start up anyway "
                "and mint a key that made the loss permanent"
            ]
        return []

    def notes(self) -> list[str]:
        return [
            "the KEK is NOT in this file and must not be (ADR-023). Recovery needs both "
            "this dump and the KEK, stored separately and both surviving; either alone "
            "restores nothing"
        ]


_IDENT = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def checked_identifier(name: str) -> str:
    """A table or column name that is safe to interpolate.

    Everything passed here is a literal from `CLASS_B_COLUMNS`, so this can
    never fire in practice -- which is the reason to have it. The next person to
    add an encrypted column will add a literal too, and the check is what makes
    that stay true rather than depending on them noticing.
    """
    if not _IDENT.match(name):
        raise RecoveryError(f"unusable identifier {name!r}")
    return name


def count_encrypted_values(conn: psycopg.Connection) -> dict[str, int]:
    """How many encrypted values this control plane holds, per column.

    Skips columns the schema does not have yet, so it runs on a partially
    migrated database without pretending the absent ones are empty.
    """
    counts: dict[str, int] = {}
    for table, column, _ in CLASS_B_COLUMNS:
        checked_identifier(table)
        checked_identifier(column)
        present = db.one(
            conn,
            "SELECT 1 AS present FROM information_schema.columns "
            " WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
            (table, column),
        )
        if not present:
            continue
        row = db.one(
            conn,
            f"SELECT count(*) AS n FROM {table} WHERE {column} IS NOT NULL",  # noqa: S608 - checked above
        )
        if row and row["n"]:
            counts[f"{table}.{column}"] = int(row["n"])
    return counts


def dump(
    dsn: str,
    *,
    path: str,
    pg_dump_bin: str = "pg_dump",
    timeout: int = 900,
) -> DumpReport:
    """Take a logical backup of the control-plane database.

    `pg_dump` rather than a physical backup, and that is a real choice rather
    than the easy one. The control plane is a single small database on ordinary
    PostgreSQL -- not a node -- so a physical backup would mean turning on
    archiving and owning a stanza for a cluster whose whole content restores in
    seconds from a file that can be copied anywhere. What a logical dump gives up
    is point-in-time recovery between dumps, and what that costs is stated in
    `docs/BACKUP-RECOVERY.md` rather than left for an operator to discover: the
    RPO is the dump interval.

    The dump is written 0600. It contains every node's admin DSN as ciphertext,
    which is useless without the KEK by design -- but "useless without a second
    artefact" is a reason for care, not a reason for a world-readable file.
    """
    report = DumpReport(path=path)
    # The password goes in the environment, never in argv. See `split_password`.
    safe_dsn, env = split_password(dsn)
    argv = [pg_dump_bin, "--format=plain", "--no-owner", "--file", path, safe_dsn]
    try:
        # 0600 before the child writes, so the file is never briefly readable.
        # `pg_dump` creates it if absent and does not widen an existing mode.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.error = f"pg_dump could not be run ({type(exc).__name__})"
        return report

    if proc.returncode != 0:
        # stderr can name the database and host; it does not carry the password,
        # which libpq keeps out of its messages.
        report.error = f"pg_dump exited {proc.returncode}: {(proc.stderr or '').strip()[-300:]}"
        return report

    try:
        report.bytes_written = os.path.getsize(path)
    except OSError:
        report.error = "pg_dump reported success and wrote no file"
        return report

    inspected = inspect_dump(path)
    report.key_rows = inspected.key_rows
    report.tables_with_secrets = inspected.tables_with_secrets
    return report


def inspect_dump(path: str) -> DumpReport:
    """Read a plain-format dump and say whether it could restore a working platform.

    Parses the `COPY` blocks rather than trusting the file's size or `pg_dump`'s
    exit code, because the failure being guarded against is a dump that
    succeeded and is missing the one table that matters -- `--exclude-table` and
    `--exclude-table-data` both produce exactly that, silently and with exit 0.
    """
    report = DumpReport(path=path)
    secret_tables = {table for table, _, _ in CLASS_B_COLUMNS}
    current: str | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if current is not None:
                    if line.startswith("\\."):
                        current = None
                    elif current == "encryption_keys":
                        report.key_rows += 1
                    else:
                        report.tables_with_secrets[current] = (
                            report.tables_with_secrets.get(current, 0) + 1
                        )
                    continue
                if line.startswith("COPY "):
                    # `COPY public.encryption_keys (...) FROM stdin;`
                    name = line.split()[1].split("(")[0].strip()
                    bare = name.split(".")[-1].strip('"')
                    if bare == "encryption_keys" or bare in secret_tables:
                        current = bare
    except OSError as exc:
        report.error = f"the dump could not be read ({type(exc).__name__})"
    return report


@dataclass
class RestoreProof:
    """Whether a restored control plane can actually be used.

    "The service started" is not this. Before ADR-070's guard a control plane
    restored without its keys started perfectly well and could administer
    nothing, so the only honest verification is to open something.
    """

    keys_loaded: int = 0
    encrypted_values: dict[str, int] = field(default_factory=dict)
    unwrapped: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    # Nodes whose admin DSN was not merely decrypted but used to open a
    # connection. The acceptance criterion names this specifically: a restored
    # control plane administering a node that was never lost.
    nodes_reached: list[str] = field(default_factory=list)
    nodes_unreachable: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.error or self.failures:
            return False
        # Nothing unwrapped and nothing to unwrap is a vacuous pass, and this is
        # the shape of every false success in this phase. A control plane with no
        # secrets is a control plane that has provisioned nothing.
        return bool(self.unwrapped) or not self.encrypted_values

    def summary(self) -> list[str]:
        lines = [f"{self.keys_loaded} data encryption key(s) loaded"]
        for name, count in sorted(self.unwrapped.items()):
            held = self.encrypted_values.get(name, 0)
            lines.append(f"  {name}: unwrapped {count} of {held}")
        if self.nodes_reached:
            lines.append(
                f"  administered {len(self.nodes_reached)} node(s) with the recovered "
                f"credential: {', '.join(self.nodes_reached)}"
            )
        return lines


def verify_restore(
    conn: psycopg.Connection,
    *,
    kek: bytes,
    reach_nodes: bool = False,
    sample: int = 3,
    connect=None,
) -> RestoreProof:
    """Prove a restored control plane can read what it holds.

    Loads the key ring against the supplied KEK and opens a sample of every kind
    of encrypted value. A sample rather than all of them: the failure this
    detects is "the wrong KEK" or "no keys", which is total rather than
    per-row -- a control plane that can open three node credentials can open
    three hundred, and a verification that took an hour on a large deployment
    would be one nobody runs during an incident.

    With `reach_nodes`, the recovered DSN is used to open an actual connection.
    That is the acceptance criterion in full: decrypting a credential proves the
    key material survived, and connecting proves the credential is still true.
    """
    from psycopg.rows import dict_row

    from services.control_plane import nodes as nodes_module

    # Every other module here is handed a pooled connection, which the pool
    # configures with `dict_row`. This one is different by design: it is aimed at
    # a *restored* database, so the natural caller -- an operator at a prompt, a
    # recovery script -- opens it with a bare `psycopg.connect` and gets tuples.
    # `db.one` then raises `TypeError: tuple indices must be integers`, which is
    # a confusing thing to meet while establishing whether a recovery worked. Set
    # it rather than requiring every caller to know.
    conn.row_factory = dict_row

    proof = RestoreProof()
    key_ring = crypto.KeyRing(kek)
    try:
        key_ring.load(conn)
    except crypto.CryptoError as exc:
        proof.error = str(exc)
        return proof

    row = db.one(conn, "SELECT count(*) AS n FROM encryption_keys")
    proof.keys_loaded = int(row["n"]) if row else 0
    proof.encrypted_values = count_encrypted_values(conn)

    # Node admin DSNs: the credential the platform needs to administer anything.
    node_rows = db.query(
        conn,
        "SELECT id, name FROM nodes WHERE admin_ciphertext IS NOT NULL ORDER BY name LIMIT %s",
        (sample,),
    )
    for node in node_rows:
        try:
            dsn = nodes_module.admin_dsn(conn, node_id=node["id"], key_ring=key_ring)
        except Exception as exc:  # noqa: BLE001 - recorded as a failure, not raised
            proof.failures.append(
                f"nodes.admin_ciphertext for {node['name']}: {type(exc).__name__}"
            )
            continue
        proof.unwrapped["nodes.admin_ciphertext"] = (
            proof.unwrapped.get("nodes.admin_ciphertext", 0) + 1
        )
        if not reach_nodes:
            continue
        opener = connect or psycopg.connect
        try:
            node_conn = opener(dsn)
        except Exception as exc:  # noqa: BLE001 - a node being down is not a key failure
            proof.nodes_unreachable.append(f"{node['name']} ({type(exc).__name__})")
            continue
        try:
            proof.nodes_reached.append(node["name"])
        finally:
            node_conn.close()

    # Project credentials: the ones whose loss a customer notices.
    creds = db.query(
        conn,
        "SELECT id, project_id, credential_type FROM project_credentials "
        " WHERE ciphertext IS NOT NULL ORDER BY id LIMIT %s",
        (sample,),
    )
    for cred in creds:
        try:
            _open_credential(conn, key_ring, cred["id"])
        except Exception as exc:  # noqa: BLE001 - recorded
            proof.failures.append(
                f"project_credentials.ciphertext id={cred['id']}: {type(exc).__name__}"
            )
            continue
        proof.unwrapped["project_credentials.ciphertext"] = (
            proof.unwrapped.get("project_credentials.ciphertext", 0) + 1
        )

    return proof


def _open_credential(conn: psycopg.Connection, key_ring: crypto.KeyRing, credential_id) -> bytes:
    """Decrypt one stored credential, and never return it anywhere it can be logged.

    The plaintext is returned so the caller can prove it worked and is
    immediately discarded by every caller here. `docs/SECRETS.md` forbids logging
    anything recovered from Class B storage, including in error details, so the
    exception handlers above record a type name and not a message.
    """
    row = db.one(
        conn,
        "SELECT project_id, credential_type, ciphertext, nonce, key_version "
        "  FROM project_credentials WHERE id = %s",
        (credential_id,),
    )
    if row is None:
        raise RecoveryError("no such credential")
    sealed = crypto.SealedValue(
        ciphertext=bytes(row["ciphertext"]),
        nonce=bytes(row["nonce"]),
        key_version=row["key_version"],
    )
    # The associated data must match what `provisioning` sealed with, exactly:
    # column name in the middle, and the owner is the project and credential
    # type joined. Guessing it wrong does not fail loudly in an obvious way --
    # AEAD reports it as `ciphertext failed authentication`, indistinguishable
    # from a wrong KEK, which would make this verifier report a healthy restore
    # as broken. Read from `provisioning.py` rather than inferred.
    return key_ring.open(
        sealed,
        aad=crypto.aad_for(
            "project_credentials",
            "ciphertext",
            f"{row['project_id']}:{row['credential_type']}",
        ),
    )


def break_glass() -> list[tuple[str, str]]:
    """What is lost, and what is merely regenerable, if the KEK is gone.

    `docs/OPEN-QUESTIONS.md` has carried "break-glass procedure if the KEK is
    lost: which secrets are regenerable by re-provisioning and which represent
    unrecoverable state" since Phase 01. This is the answer, in the code rather
    than only in prose so the CLI can print it during the incident where it is
    needed and nobody is reading documentation.
    """
    return [(f"{table}.{column}", consequence) for table, column, consequence in CLASS_B_COLUMNS]


__all__ = [
    "CLASS_B_COLUMNS",
    "DumpReport",
    "RecoveryError",
    "RestoreProof",
    "break_glass",
    "checked_identifier",
    "count_encrypted_values",
    "dump",
    "split_password",
    "inspect_dump",
    "verify_restore",
]
