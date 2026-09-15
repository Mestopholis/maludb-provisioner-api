"""What the memory worker's database role may read and write (ADR-079, memory slice 5c).

The memory worker holds the KEK: it opens each project's sealed memory writer
password and the customer's provider keys. Decision 6 promises that a compromised
worker reaches memory, not the fleet -- and while it connected as the control
plane's own role, that was true only of the code it runs. The same process could
have opened every node's superuser DSN (`nodes.admin_*`) and every tenant's
database password and JWT signing key (`project_credentials`).

**An allowlist, unlike the gateway's denylist** (`gateway_grants`). The gateway
reaches the database through half the control plane, so enumerating its reads
would be a guess. The worker's reads are three modules -- `memory_worker`,
`provisioning.load_credential`, `provider_keys.load_key` -- plus the key ring, and
`tests/test_memory_worker_grants.py` runs the worker as this role end to end, so
an enumeration that is too narrow fails in the suite rather than in production.

**Columns here, rows in migration 0046.** A column grant cannot say "only
`db_memwriter` credentials"; the `memory_worker_reach` policies do, keyed on
membership of `cp_memory_worker` rather than on anything the process says about
itself.

Applied by `cp-manage memory-worker grant` to that group role. Every statement is
absolute, so re-running it after a migration re-establishes the model.
"""

from __future__ import annotations

from psycopg import sql

GROUP_ROLE = "cp_memory_worker"

# Read by the worker, column by column. What is absent is the point: no
# `nodes.admin_*`, no JWT or database secrets on `projects`, no `api_keys`, users,
# sessions, tokens, billing or audit.
READS = {
    "projects": ("id", "project_ref", "database_name", "status", "node_id", "plan_id", "deleted_at"),
    # `gateway_role` because the gateway's own-node policies name no role, so they
    # are evaluated for this one too, and `gateway_node_id()` reads the column with
    # the caller's privilege. Without it every such table answers "permission
    # denied" instead of the rows this role's own policies admit. Role names are
    # world-readable in `pg_roles` already.
    "nodes": ("id", "internal_host", "gateway_role"),
    "plans": ("id", "code", "config_json"),
    "encryption_keys": ("key_version", "wrapped_dek", "state"),
    "project_credentials": ("project_id", "credential_type", "ciphertext", "nonce", "key_version", "revoked_at"),
    "project_provider_keys": ("project_id", "provider", "ciphertext", "nonce", "key_version", "revoked_at"),
    "memory_spaces": ("id", "project_id", "name", "schema_name", "state", "item_count", "extraction_provider",
                      "extraction_model", "embedding_provider", "embedding_model"),
    "memory_ingests": ("id", "project_id", "space_id", "kind", "state", "item_count", "items_json", "results_json",
                       "written", "failed", "detail", "requested_at", "started_at", "completed_at", "heartbeat_at"),
}

# Written by the worker. Never `INSERT` or `DELETE` anywhere: the gateway queues
# ingests and the provisioner builds spaces; the worker only moves a request along
# and counts what it stored.
WRITES = {
    "memory_spaces": ("item_count",),
    "memory_ingests": ("state", "items_json", "results_json", "written", "failed", "detail", "started_at",
                       "completed_at", "heartbeat_at"),
}

# What a correctly granted role must not be able to read, asked of the catalogue by
# the worker at startup and by `deploy preflight`. The first is ADR-072's finding;
# the rest are the secrets a worker holding the KEK would otherwise open.
FORBIDDEN_COLUMNS = (
    ("nodes", "admin_ciphertext"),
    ("project_credentials", "role_name"),
)
FORBIDDEN_TABLES = ("api_keys", "users", "user_sessions", "personal_access_tokens", "audit_events", "subscriptions")


# ADR-079 memory slice 6a: the query embedder, a listening process on the same host.
# It authenticates the customer's own secret key and embeds one query with the
# space's model -- so it reads key hashes and provider keys, and no memory writer
# credential, no ingest, no plan. Its own group, so neither process carries the
# other's reach.
EMBEDDER_GROUP_ROLE = "cp_memory_embedder"
EMBEDDER_READS = {
    "projects": ("id", "project_ref", "status", "node_id", "deleted_at", "plan_id"),
    # The project's request rate, held again here: the route is reachable without the gateway.
    "plans": ("id", "code", "config_json"),
    # The gateway's own-node policies name no role and read this column for every caller.
    "nodes": ("id", "gateway_role"),
    "encryption_keys": ("key_version", "wrapped_dek", "state"),
    "api_keys": ("id", "project_id", "key_type", "key_identifier", "verification_data", "revoked_at",
                 "last_used_at"),
    "project_provider_keys": ("project_id", "provider", "ciphertext", "nonce", "key_version", "revoked_at"),
    "memory_spaces": ("id", "project_id", "name", "state", "embedding_provider", "embedding_model"),
}
# `api_keys.authenticate` records use, at most once per resolution window.
EMBEDDER_WRITES = {"api_keys": ("last_used_at",)}
EMBEDDER_FORBIDDEN_COLUMNS = (
    ("nodes", "admin_ciphertext"),
    ("project_credentials", "ciphertext"),
    ("api_keys", "ciphertext"),
)
EMBEDDER_FORBIDDEN_TABLES = ("users", "user_sessions", "personal_access_tokens", "audit_events", "subscriptions",
                             "memory_ingests")


def statements(role: str = GROUP_ROLE, *, reads: dict | None = None, writes: dict | None = None) -> list[sql.Composed]:
    reads = READS if reads is None else reads
    writes = WRITES if writes is None else writes
    r = sql.Identifier(role)
    out = [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(r),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(r),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r),
    ]
    for table, columns in reads.items():
        out.append(sql.SQL("GRANT SELECT ({}) ON TABLE {} TO {}").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table), r))
    for table, columns in writes.items():
        out.append(sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table), r))
    return out


def revocations(role: str = GROUP_ROLE) -> list[sql.Composed]:
    r = sql.Identifier(role)
    return [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(r),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(r),
        sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(r),
    ]


def _first(row):
    """The first column, whichever row factory the connection uses."""
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def violations(conn, role: str, *, columns=None, tables=None) -> list[str]:
    """What `role` can reach that it must not, from the catalogue. The worker's list by default."""
    found = []
    for table, column in (FORBIDDEN_COLUMNS if columns is None else columns):
        row = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AND has_column_privilege(%s, %s, %s, 'SELECT') AS yes",
            (table, role, table, column),
        ).fetchone()
        if _first(row):
            found.append(f"{table}.{column}")
    for table in (FORBIDDEN_TABLES if tables is None else tables):
        row = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AND has_table_privilege(%s, %s, 'SELECT') AS yes",
            (table, role, table),
        ).fetchone()
        if _first(row):
            found.append(table)
    return found


def embedder_violations(conn, role: str = EMBEDDER_GROUP_ROLE) -> list[str]:
    return violations(conn, role, columns=EMBEDDER_FORBIDDEN_COLUMNS, tables=EMBEDDER_FORBIDDEN_TABLES)


def gateway_members(conn, group: str = GROUP_ROLE) -> list[str]:
    """Gateway roles that are also memory workers.

    Refused wherever it is checked. A gateway role's own-node policy admits every
    `project_credentials` row of its node, and the worker's column grants include
    their ciphertext -- so a role holding both reads that node's tenant database
    passwords and signing keys, which neither role can alone.
    """
    rows = conn.execute(
        "SELECT n.gateway_role FROM nodes n "
        "  JOIN pg_catalog.pg_roles gw ON gw.rolname = n.gateway_role "
        "  JOIN pg_catalog.pg_roles g ON g.rolname = %s "
        " WHERE pg_catalog.pg_has_role(gw.oid, g.oid, 'MEMBER')",
        (group,),
    ).fetchall()
    return [_first(r) for r in rows]


__all__ = [
    "EMBEDDER_GROUP_ROLE", "EMBEDDER_READS", "EMBEDDER_WRITES", "GROUP_ROLE", "READS", "WRITES",
    "embedder_violations", "gateway_members", "revocations", "statements", "violations",
]
