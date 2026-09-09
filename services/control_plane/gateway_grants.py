"""What the gateway's database role may and may not do (ADR-072).

The gateway is an internet-facing process that runs on a node and holds the
KEK, because waking a sleeping worker means decrypting that project's password
and verifying a tenant's JWT means its signing key. That much is load-bearing
and ADR-072 keeps it.

What ADR-072 removes is the *fleet* from its reach. `nodes.admin_dsn()` needs a
control-plane connection, a loaded key ring, a `node_id` and an AAD derived from
that id. A gateway holding the control plane's own database credentials has all
four, so one compromised public listener yielded the PostgreSQL superuser DSN of
every node on the platform. ADR-038 forbids exactly that and enforces it with an
import-graph test over the control plane's public routers; the gateway is a
second internet-facing application and that test never saw it.

**The model is a denylist, deliberately.** An allowlist of the tables the
gateway needs would be the tighter statement and the wrong one to write today:
the gateway reaches the database through `workers`, `auth_workers`,
`realtime_workers`, `storage_workers`, `object_storage`, `entitlements` and
`provisioning` as well as its own modules, so an enumeration would be a guess,
and a guess that is too narrow fails at runtime on a path a test did not cover.
What is enumerated instead is the thing that must not be reachable, which is
short, stable and exactly what the finding was about.

This lives in code rather than in a migration because applying it needs the role
to exist, and creating a role needs privileges the control-plane role does not
have -- it owns the schema, it is not a superuser. So an operator creates the
login role (a documented superuser step) and `cp-manage gateway grant` applies
this model to it. `tests/test_gateway_grants.py` asserts the result against a
real cluster rather than trusting the statements to be right.
"""

from __future__ import annotations

from psycopg import sql

# The columns that make a node's superuser DSN recoverable. `admin_dsn()` reads
# all three; without them the KEK on the node opens nothing at the fleet level.
NODE_ADMIN_COLUMNS = ("admin_ciphertext", "admin_nonce", "admin_key_version")

# The only thing the gateway legitimately needs from `nodes`: a row lock while
# it allocates a worker port (`workers.py`, "SELECT id FROM nodes WHERE id = %s
# FOR UPDATE"). PostgreSQL requires UPDATE privilege on at least one column for
# a FOR UPDATE lock, so the lock is paid for with the narrowest column that is
# not a secret rather than with the table.
NODE_READABLE_COLUMNS = ("id",)
NODE_LOCKABLE_COLUMNS = ("last_health_at",)


def statements(role: str) -> list[sql.Composed]:
    """The full permission model for `role`, as ordered statements.

    Idempotent: every grant is absolute rather than additive, so re-running this
    after a schema change re-establishes the model rather than layering on it.
    """
    r = sql.Identifier(role)
    out: list[sql.Composed] = [
        # Start from what the control plane has, because the gateway's reach is
        # defined by subtraction. `ALL TABLES` is evaluated now, so this command
        # is re-run after a migration adds a table -- which `deploy preflight`
        # checks by looking for the revoke below rather than by trusting anyone
        # to remember.
        sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=r),
        sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
        ).format(role=r),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}").format(role=r),
        # Then take the fleet back out. Table-level SELECT cannot be narrowed by
        # a column-level REVOKE -- in PostgreSQL a table-level grant covers every
        # column and a subsequent column REVOKE is a no-op -- so `nodes` has to
        # lose the table grant entirely and be re-granted by column.
        sql.SQL("REVOKE ALL ON TABLE nodes FROM {role}").format(role=r),
        sql.SQL("GRANT SELECT ({cols}) ON TABLE nodes TO {role}").format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in NODE_READABLE_COLUMNS), role=r
        ),
        sql.SQL("GRANT UPDATE ({cols}) ON TABLE nodes TO {role}").format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in NODE_LOCKABLE_COLUMNS), role=r
        ),
    ]
    return out


def revocations(role: str) -> list[sql.Composed]:
    """Undo `statements`. Used by tests to drop the role afterwards.

    Explicit rather than `DROP OWNED BY`, which is documented to revoke
    privileges granted to a role but did not clear these -- leaving `DROP ROLE`
    to fail on "privileges for table ..." for every table in the schema.
    """
    r = sql.Identifier(role)
    return [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}").format(role=r),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}").format(role=r),
        sql.SQL("REVOKE ALL ON TABLE nodes FROM {role}").format(role=r),
        sql.SQL("REVOKE ALL ON SCHEMA public FROM {role}").format(role=r),
    ]


def probe_sql(column: str = NODE_ADMIN_COLUMNS[0]) -> sql.Composed:
    """A statement that must fail for a correctly-granted gateway role."""
    return sql.SQL("SELECT {col} FROM nodes LIMIT 1").format(col=sql.Identifier(column))
