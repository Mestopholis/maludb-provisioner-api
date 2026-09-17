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
from psycopg.rows import tuple_row

# The columns that make a node's superuser DSN recoverable. `admin_dsn()` reads
# all three; without them the KEK on the node opens nothing at the fleet level.
NODE_ADMIN_COLUMNS = ("admin_ciphertext", "admin_nonce", "admin_key_version")

# What the gateway legitimately needs from `nodes`.
#
# `id` is a row lock while it allocates a worker port (`workers.py`, "SELECT id
# FROM nodes WHERE id = %s FOR UPDATE"). PostgreSQL requires UPDATE privilege on
# at least one column for a FOR UPDATE lock, so the lock is paid for with the
# narrowest column that is not a secret rather than with the table.
#
# `gateway_role` is the row narrowing (ADR-072 point 2). The policies resolve
# `current_user` through `public.gateway_node_id()`, which reads this column, and
# a policy expression runs with the privileges of the role running the query
# rather than the table owner's -- so without the grant every query the gateway
# makes fails with "permission denied for table nodes" instead of returning its
# own rows. It is not a secret in any case: role names are already world-readable
# in `pg_roles`.
# `storage_secret_*` is the node's own object-storage root, which
# `storage_workers.ensure_node_secret` reads on the request that registers a
# project with the shared worker -- a gateway path. Granting it was unsafe while
# `nodes` had no row policy, because a column grant covers every row; with the
# policy from migration 0031 the gateway reads these for its own node and no
# other. Without it, a correctly narrowed gateway answers 500 on every Storage
# request, which is a narrowing that breaks the job and would be reverted.
NODE_READABLE_COLUMNS = (
    "id",
    "gateway_role",
    "storage_secret_ciphertext",
    "storage_secret_nonce",
    "storage_secret_key_version",
)

# Deliberately not the storage secret. Reading an existing root is a gateway
# path; *sealing a new one* is node preparation, and an internet-facing process
# that can write it can also, on a node where it is absent, mint a root the
# running container does not hold -- which is the failure `ensure_node_secret`
# already warns about, arriving from a new direction. The provisioner creates
# it; this reads it.
NODE_LOCKABLE_COLUMNS = ("last_health_at",)

# Tables the gateway may not read or write at all.
#
# `project_provider_keys` (ADR-079): customers' own model keys, which nothing on the
# request path needs.
#
# The staff tables (ADR-082): the gateway's reach is defined by subtraction from
# `ALL TABLES`, so a staff table left off this list would let an internet-facing
# process on a node insert a staff account or a staff session -- operator access,
# from the one component every tenant's traffic passes through.
#
# `maintenance_runs` (ADR-083): preflight reads it to decide whether the control plane's
# maintenance pass is running, so a gateway that could insert there could make a stopped
# pass -- purchases not applied, storage not enforced -- look healthy. The node half records
# in `node_maintenance_runs`, under the gateway's own-node policy.
UNREACHABLE_TABLES = ("project_provider_keys", "staff_users", "staff_mfa_factors", "staff_sessions",
                      "maintenance_runs")

# Tables the gateway may read for its own node and must never write.
#
# `node_extension_pins` decides, with the check recorded on `nodes`, whether a
# node takes new projects, restores and moves (ADR-075). The gateway cannot
# write the check -- `nodes` is column-granted above -- but the denylist's
# default would let it write its own node's pins, and a gateway on a node whose
# packages had moved could then pin the version it happens to run and put its
# node back into placement. Found in pinning slice 1's security review.
READ_ONLY_TABLES = ("node_extension_pins",)


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
    # ADR-079 memory slice 4. Customers' own provider API keys: nothing on the
    # request path needs one, and a node holding the KEK (ADR-072) is reason to
    # keep the ciphertext off it, not to hand it over.
    for table in UNREACHABLE_TABLES:
        out.append(sql.SQL("REVOKE ALL ON TABLE {table} FROM {role}").format(table=sql.Identifier(table), role=r))
    for table in READ_ONLY_TABLES:
        out.append(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE {table} FROM {role}").format(
                table=sql.Identifier(table), role=r
            )
        )
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


# The second half of ADR-072, and the reason it is a query rather than a
# configuration check: a gateway role that maps to no node fails *closed* -- the
# policies compare against NULL, nothing matches, and the process serves 404s for
# every tenant on the machine. That is the right direction to fail in and a
# miserable thing to diagnose from the outside, so it is asked at startup.
NODE_IDENTITY_SQL = "SELECT public.gateway_node_id()"


def node_identity(conn) -> int | None:
    """The node this connection's role serves, or None if it maps to none.

    A tuple cursor on purpose: the gateway calls this with a pooled connection, and
    the pool hands out `dict_row` connections. Indexing that row by position raised
    KeyError and stopped every production gateway at startup -- found by the
    deployment rehearsal, because only a production gateway asks.
    """
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(NODE_IDENTITY_SQL)
        row = cur.fetchone()
    return None if row is None else row[0]

