"""What the operator console's database role may read and write (ADR-082 slice 2).

An allowlist, as for the memory worker (`memory_worker_grants`) and unlike the
gateway's denylist: the console's database access is one module per surface --
`staff` for sign-in, and the report modules slice 3 adds -- and
`tests/test_admin_grants.py` runs the console as this role end to end, so a grant that
is too narrow fails in the suite rather than in production.

**What is absent is the point.** Not a single encrypted column, password or token
verifier on the customer side; no INSERT on `staff_users` and no write to a staff
password, status or seed, so a compromised console cannot make itself a staff account
or re-enrol one -- that is `cp-manage staff`, run as the control plane's own role.

Rows are narrowed in migration 0052: the console inserts only staff audit events.
Applied by `cp-manage admin-console grant` to the group role. Every statement is
absolute, so re-running it after a migration re-establishes the model.
"""

from __future__ import annotations

from psycopg import sql

GROUP_ROLE = "cp_admin_console"

READS = {
    # Sign-in and session resolution (`staff.sign_in`, `staff.resolve`, `staff.sign_out`).
    # The password hash and sealed seed are read to verify, as the ADR accepts: the
    # console verifies staff credentials, it does not hold customer ones.
    "staff_users": ("id", "email", "display_name", "status", "password_hash", "failed_signins", "locked_until"),
    "staff_mfa_factors": ("staff_id", "seed_ciphertext", "seed_nonce", "confirmed_at", "last_used_step"),
    "staff_sessions": ("id", "staff_id", "token_hash", "expires_at", "revoked_at", "last_seen_at"),
    # The gateway's own-node policy on `audit_events` names no role, so it is evaluated
    # for this one too and reads these with the caller's privilege (see 0046's note).
    "nodes": ("id", "gateway_role",
              # Slice 3c, capacity (`node_capacity.capacity_of`). Rows through migration 0055.
              "name", "node_pool", "status", "capacity_json", "metrics_json", "last_health_at", "created_at"),
    "node_extension_pins": ("node_id", "extension", "version", "set_by", "set_at"),
    "provisioning_jobs": ("project_id", "attempt", "error_code", "state", "updated_at"),
    # Slice 3a, sales and customers (`admin_reports`). Rows through migration 0053's policies.
    "projects": ("id", "node_id", "org_id", "project_ref", "display_name", "plan_id", "status", "created_at",
                 "deleted_at", "database_bytes", "object_bytes", "storage_state", "object_storage_state",
                 "database_measured_at", "object_measured_at",
                 # Slice 3c: warm workers and Realtime for capacity; provisioning timing.
                 "worker_state", "auth_worker_state", "realtime_enabled", "requested_at", "failed_at",
                 "retry_after"),
    # Slice 3b, usage. Rows through migration 0054's policies. Never `email_events.recipient_hash`.
    "project_egress": ("project_id", "period_start", "bytes"),
    "email_events": ("project_id", "event_type", "occurred_at"),
    # `config_json` for the plan's ceilings (slice 3b), resolved by `entitlements`.
    "plans": ("id", "code", "config_json"),
    "subscriptions": ("id", "org_id", "project_id", "plan_code", "state", "state_since", "state_as_of",
                      "period_start", "period_end", "created_at", "provider_subscription_id",
                      "provider_customer_id", "reconciled_state", "reconciled_plan_code"),
    "billing_events": ("event_id", "event_type", "livemode", "event_at", "received_at", "outcome", "note",
                       "project_id"),
    "organizations": ("id", "display_name", "slug", "is_personal", "created_at", "deleted_at"),
    "org_members": ("org_id", "user_id", "role", "created_at"),
    # Never `password_hash` (FORBIDDEN_COLUMNS): who a customer is, not how they sign in.
    "users": ("id", "email", "display_name", "status", "created_at", "last_login_at", "email_verified_at",
              "deleted_at"),
}

UPDATES = {
    "staff_users": ("failed_signins", "locked_until", "last_signin_at"),
    "staff_mfa_factors": ("last_used_step",),
    "staff_sessions": ("last_seen_at", "revoked_at"),
}

INSERTS = {
    "staff_sessions": ("id", "staff_id", "token_hash", "ip_address", "user_agent", "created_at", "last_seen_at",
                       "expires_at"),
    # `org_id` for `staff.view` (slice 3a). Migration 0052 admits staff events with no project only.
    "audit_events": ("actor_type", "actor_id", "org_id", "event_type", "detail_json"),
}

# Asked of the catalogue by `violations`, at console startup and in preflight. Every
# column `crypto` seals, and every verifier on the customer side.
FORBIDDEN_COLUMNS = (
    ("nodes", "admin_ciphertext"),
    ("nodes", "storage_secret_ciphertext"),
    ("project_credentials", "ciphertext"),
    ("api_keys", "ciphertext"),
    ("api_keys", "verification_data"),
    ("project_email_settings", "malumail_ciphertext"),
    ("project_email_settings", "hook_ciphertext"),
    ("project_provider_keys", "ciphertext"),
    ("user_mfa_factors", "ciphertext"),
    ("users", "password_hash"),
    ("user_sessions", "token_hash"),
    ("personal_access_tokens", "verification_data"),
    ("org_invitations", "token_hash"),
    ("encryption_keys", "wrapped_dek"),
    # A customer's end user's address, hashed. Counting sends needs no address.
    ("email_events", "recipient_hash"),
    # Where a node is: nothing the console reports needs an address to reach it.
    ("nodes", "hostname"),
    ("nodes", "internal_host"),
    # Free text that can quote a node's own error messages; the code is enough to triage.
    ("provisioning_jobs", "error_detail"),
)

# Writes that would let a compromised console make or remake a staff credential.
FORBIDDEN_WRITES = (
    ("staff_users", "INSERT", None),
    ("staff_users", "UPDATE", "password_hash"),
    ("staff_users", "UPDATE", "status"),
    ("staff_users", "UPDATE", "email"),
    ("staff_mfa_factors", "INSERT", None),
    ("staff_mfa_factors", "UPDATE", "seed_ciphertext"),
    ("staff_mfa_factors", "UPDATE", "confirmed_at"),
    ("staff_mfa_factors", "DELETE", None),
    ("staff_sessions", "DELETE", None),
    ("audit_events", "UPDATE", None),
    ("audit_events", "DELETE", None),
)


def statements(role: str = GROUP_ROLE) -> list[sql.Composed]:
    r = sql.Identifier(role)
    out = [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(r),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(r),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r),
    ]
    for verb, model in (("SELECT", READS), ("UPDATE", UPDATES), ("INSERT", INSERTS)):
        for table, columns in model.items():
            out.append(sql.SQL("GRANT {} ({}) ON TABLE {} TO {}").format(
                sql.SQL(verb), sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table), r))
    return out


def revocations(role: str = GROUP_ROLE) -> list[sql.Composed]:
    r = sql.Identifier(role)
    return [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(r),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(r),
        sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(r),
    ]


def _first(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def violations(conn, role: str) -> list[str]:
    """What `role` can reach that the console must not, from the catalogue."""
    found = []
    for table, column in FORBIDDEN_COLUMNS:
        row = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AND has_column_privilege(%s, %s, %s, 'SELECT') AS yes",
            (table, role, table, column),
        ).fetchone()
        if _first(row):
            found.append(f"read {table}.{column}")
    for table, verb, column in FORBIDDEN_WRITES:
        if column is None:
            row = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL AND has_table_privilege(%s, %s, %s) AS yes",
                (table, role, table, verb),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL AND has_column_privilege(%s, %s, %s, %s) AS yes",
                (table, role, table, column, verb),
            ).fetchone()
        if _first(row):
            found.append(f"{verb.lower()} {table}{'.' + column if column else ''}")
    superuser = conn.execute(
        "SELECT rolsuper OR rolbypassrls AS yes FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if superuser is not None and _first(superuser):
        found.append("superuser or BYPASSRLS")
    return found


def overlaps(conn, group: str = GROUP_ROLE) -> list[str]:
    """Members of the console's group that are also another narrowed component.

    Refused: a role holding two models holds the union, and the console's reach plus a
    gateway's own-node policies or a memory worker's credential reads is wider than
    either was reviewed as.
    """
    rows = conn.execute(
        """
        SELECT m.rolname AS name
          FROM pg_catalog.pg_roles g
          JOIN pg_catalog.pg_roles m ON m.oid <> g.oid AND pg_catalog.pg_has_role(m.oid, g.oid, 'MEMBER')
         WHERE g.rolname = %s
           AND (m.rolname IN (SELECT gateway_role FROM public.nodes WHERE gateway_role IS NOT NULL)
             OR m.rolname IN (SELECT health_reporter_role FROM public.nodes WHERE health_reporter_role IS NOT NULL)
             OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles o
                         WHERE o.rolname IN ('cp_memory_worker', 'cp_memory_embedder')
                           AND pg_catalog.pg_has_role(m.oid, o.oid, 'MEMBER')))
        """,
        (group,),
    ).fetchall()
    return [_first(r) for r in rows]


__all__ = ["FORBIDDEN_COLUMNS", "FORBIDDEN_WRITES", "GROUP_ROLE", "INSERTS", "READS", "UPDATES", "overlaps",
           "revocations", "statements", "violations"]
