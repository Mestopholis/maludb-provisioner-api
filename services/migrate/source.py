"""Reading a Supabase project, without changing it (Phase 08 slice 5).

This module runs on the **customer's machine**, against the customer's
**production** database, using a credential this platform never sees (ADR-042).
Three things follow, and they are stricter than anything in the control plane:

- **The transaction is read-only and repeatable-read.** Every statement here is
  a constant in this file and nothing takes caller-supplied SQL, so `READ ONLY`
  is a genuine backstop rather than the claim ADR-040 showed it not to be
  against submitted text. `tasks/PHASE-08-SUPABASE-MIGRATION.md` makes "source
  is not modified unexpectedly" an acceptance criterion; this is how it is met,
  and a future edit that adds a write fails loudly against the customer's own
  database rather than quietly succeeding.
- **The DSN never appears anywhere but the connection.** Not in the report, not
  in an error, not in a log line. A connection failure reports its `sqlstate`
  and nothing else, for the same reason `sql_console` does: psycopg's connection
  errors can echo the conninfo, and this one is a production credential for
  somebody else's platform.
- **A probe that cannot read something reports that it could not**, rather than
  reporting zero. A Supabase connection string does not always reach
  `auth.users` or `storage.objects`, and "0 users" and "could not see the users"
  lead a customer to opposite decisions about their cutover. Same rule as
  `api/usage.py`: null means unknown, not none.

The scanner reads the source *by catalogue*, not by trying the migration. It is
the step that runs before a customer commits to a maintenance window, so it must
be quick and it must not need write access to anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10

# Schemas Supabase creates that are the *platform's* rather than the customer's.
# Objects here are not migrated: the destination builds its own equivalents
# during provisioning, and copying Supabase's would overwrite them.
SUPABASE_SCHEMAS = frozenset(
    {
        "auth", "storage", "realtime", "supabase_functions", "supabase_migrations",
        "graphql", "graphql_public", "extensions", "pgbouncer", "vault", "net",
        "pgsodium", "pgsodium_masks", "cron", "_realtime", "_analytics",
    }
)

_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})


class SourceError(RuntimeError):
    """The source could not be read. Never carries the DSN."""


@dataclass
class Probe:
    """One thing the scanner tried to learn.

    `readable` is the whole point of this type. A probe that failed on
    privileges is not a probe that found nothing, and a report that conflated
    them would tell a customer their Supabase project has no users.
    """

    readable: bool = True
    reason: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)

    def one(self, key: str, default: Any = None) -> Any:
        return self.rows[0][key] if self.rows else default


@dataclass
class SourceFacts:
    """Everything the scanner learned, before anything judges it."""

    database_bytes: int = 0
    server_version: str = ""
    schemas: Probe = field(default_factory=Probe)
    extensions: Probe = field(default_factory=Probe)
    relations: Probe = field(default_factory=Probe)
    policies: Probe = field(default_factory=Probe)
    functions: Probe = field(default_factory=Probe)
    triggers: Probe = field(default_factory=Probe)
    foreign_tables: Probe = field(default_factory=Probe)
    auth_users: Probe = field(default_factory=Probe)
    auth_identities: Probe = field(default_factory=Probe)
    storage_buckets: Probe = field(default_factory=Probe)
    storage_objects: Probe = field(default_factory=Probe)
    realtime_publication: Probe = field(default_factory=Probe)
    function_hooks: Probe = field(default_factory=Probe)
    vault_secrets: Probe = field(default_factory=Probe)


# Customer objects only: `pg_*`, `information_schema`, and Supabase's own
# schemas are excluded, because what a migration carries is the customer's.
_CUSTOMER_SCHEMA_FILTER = """
    n.nspname NOT LIKE 'pg\\_%%'
AND n.nspname <> ALL(%(system)s)
AND n.nspname <> ALL(%(supabase)s)
"""

_SCHEMAS = """
SELECT n.nspname AS name
  FROM pg_namespace n
 WHERE n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> ALL(%(system)s)
 ORDER BY 1
"""

_EXTENSIONS = """
SELECT e.extname AS name, n.nspname AS schema, e.extversion AS version
  FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
 ORDER BY 1
"""

_RELATIONS = f"""
SELECT n.nspname                     AS schema,
       c.relname                     AS name,
       c.relkind                     AS kind,
       c.relrowsecurity              AS rls_enabled,
       -- `security_invoker` decides whether a view applies the *caller's*
       -- privileges or its owner's. Off by default, which means a view over an
       -- RLS-protected table hands every row to whoever may read the view.
       array_to_string(c.reloptions, ',') AS options,
       c.reltuples::bigint           AS estimated_rows,
       pg_total_relation_size(c.oid) AS size_bytes
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = ANY('{{r,p,v,m,f}}') AND {_CUSTOMER_SCHEMA_FILTER}
 ORDER BY pg_total_relation_size(c.oid) DESC
"""  # noqa: S608 - the only interpolation is a constant in this file

_POLICIES = f"""
SELECT n.nspname AS schema, c.relname AS table_name, pol.polname AS name,
       pol.polcmd AS command, pol.polpermissive AS permissive,
       pg_get_expr(pol.polqual, pol.polrelid) AS using_expression,
       ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE pg_get_userbyid(r) END
               FROM unnest(pol.polroles) AS r) AS roles
  FROM pg_policy pol
  JOIN pg_class c ON c.oid = pol.polrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE {_CUSTOMER_SCHEMA_FILTER}
"""  # noqa: S608 - the only interpolation is a constant in this file

_FUNCTIONS = f"""
SELECT n.nspname AS schema, p.proname AS name, l.lanname AS language,
       p.prosecdef AS security_definer,
       -- The signature as it can be applied to the destination, quoted.
       -- Doubled, because this query carries bound parameters: psycopg does
       -- its own placeholder substitution first and would otherwise read
       -- format's markers as its own. The same escaping the schema filter
       -- above needs, and the reason the functions probe reported itself
       -- unreadable the moment this column was added -- twice, the second time
       -- because this comment itself contained a bare per-cent sign.
       format('%%I.%%I(%%s)', n.nspname, p.proname,
              pg_get_function_identity_arguments(p.oid)) AS signature,
       -- NULL means PostgreSQL's default, which is EXECUTE to PUBLIC. A
       -- non-null ACL is what a customer who locked a function down has, and
       -- `pg_dump --no-privileges` throws it away -- so the scanner has to
       -- carry it or the migration silently widens access (ADR-018's finding,
       -- reintroduced for customer code).
       p.proacl::text[] AS acl
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  JOIN pg_language l ON l.oid = p.prolang
 WHERE {_CUSTOMER_SCHEMA_FILTER}
   AND NOT EXISTS (SELECT 1 FROM pg_depend d
                    WHERE d.objid = p.oid AND d.classid = 'pg_proc'::regclass
                      AND d.deptype = 'e')
"""  # noqa: S608 - the only interpolation is a constant in this file

_TRIGGERS = f"""
SELECT n.nspname AS schema, c.relname AS table_name, t.tgname AS name
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE NOT t.tgisinternal AND {_CUSTOMER_SCHEMA_FILTER}
"""  # noqa: S608 - the only interpolation is a constant in this file

# Foreign tables and the servers behind them. A foreign table is a pointer into
# somebody else's database, so it does not migrate -- and ADR-045 refuses the
# extensions that would make one work on the destination.
_FOREIGN_TABLES = """
SELECT n.nspname AS schema, c.relname AS name, s.srvname AS server
  FROM pg_foreign_table ft
  JOIN pg_class c ON c.oid = ft.ftrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_foreign_server s ON s.oid = ft.ftserver
"""

_AUTH_USERS = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE encrypted_password IS NOT NULL) AS with_password,
       count(*) FILTER (WHERE confirmed_at IS NOT NULL) AS confirmed
  FROM auth.users
"""

_AUTH_IDENTITIES = """
SELECT provider, count(*) AS total FROM auth.identities GROUP BY provider ORDER BY 1
"""

_STORAGE_BUCKETS = "SELECT id, name, public FROM storage.buckets ORDER BY 1"

_STORAGE_OBJECTS = """
SELECT count(*) AS total,
       -- Cast: `sum(bigint)` is `numeric`, which psycopg maps to `Decimal` and
       -- `json.dumps` refuses. `--format json` is the documented runbook
       -- interface, so it crashed for exactly the projects that have storage.
       coalesce(sum((metadata->>'size')::bigint), 0)::bigint AS bytes
  FROM storage.objects
"""

# What Supabase's Realtime publishes. A table absent from it produces no
# Postgres Changes on the destination either, which is a fact worth carrying
# across rather than rediscovering.
_REALTIME_PUBLICATION = """
SELECT schemaname AS schema, tablename AS name
  FROM pg_publication_tables WHERE pubname = 'supabase_realtime'
"""

# Database webhooks: triggers Supabase installs that call an Edge Function or an
# HTTP endpoint. They are the one part of Edge Functions visible from the
# database, which is why they are worth looking for at all.
_FUNCTION_HOOKS = """
SELECT n.nspname AS schema, c.relname AS table_name, t.tgname AS name
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_proc p ON p.oid = t.tgfoid
  JOIN pg_namespace pn ON pn.oid = p.pronamespace
 WHERE NOT t.tgisinternal AND pn.nspname IN ('supabase_functions', 'net')
"""

_VAULT_SECRETS = "SELECT count(*) AS total FROM vault.secrets"

# **Relations whose emptiness cannot be taken at face value.**
#
# Row-level security is not an error. A session that is neither the owner nor a
# `BYPASSRLS` role reads an RLS-protected table as *zero rows, no message* --
# measured 2026-08-17. Supabase enables RLS on `storage.objects` and
# `storage.buckets` by design, since that is what storage policies are built
# from, and owns them as `supabase_storage_admin`.
#
# So a customer doing the responsible thing -- pointing this tool at a
# purpose-made read-only role instead of their project owner -- would have been
# told they have no stored objects, and would have cut over and lost their
# files. Each of these is checked before it is believed.
RLS_SENSITIVE = {
    "auth_users": "auth.users",
    "auth_identities": "auth.identities",
    "storage_buckets": "storage.buckets",
    "storage_objects": "storage.objects",
    "vault_secrets": "vault.secrets",
}

# Whether this session sees past row-level security at all.
_SESSION_BYPASSES_RLS = """
SELECT coalesce(bool_or(rolsuper OR rolbypassrls), false) AS bypasses
  FROM pg_roles WHERE pg_has_role(current_user, oid, 'USAGE')
"""

# `relforcerowsecurity` matters as much as `relrowsecurity`: FORCE applies the
# policies to the owner too, so owning the table stops being enough.
_RLS_APPLIES = """
SELECT c.relrowsecurity AS enabled,
       c.relforcerowsecurity AS forced,
       pg_has_role(current_user, c.relowner, 'USAGE') AS owns
  FROM pg_class c WHERE c.oid = to_regclass(%s)
"""


def read(dsn: str) -> SourceFacts:
    """Collect every fact the rules need, in one consistent snapshot.

    Raises `SourceError` only for a failure that stops the scan -- reaching the
    database at all. Everything narrower is a `Probe` that says it could not
    read, because a scan that dies on one inaccessible schema tells the customer
    nothing about the rest of their project.
    """
    try:
        conn = psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, row_factory=dict_row)
    except psycopg.Error as exc:
        # Deliberately not `str(exc)`: psycopg's connection errors can echo the
        # conninfo, and this one is the customer's production credential.
        log.debug("source connection failed: %s", exc.sqlstate)
        raise SourceError(
            "could not connect to the source project "
            f"(SQLSTATE {exc.sqlstate or 'unknown'}). Check the connection string and that "
            "your address is allowed by the source project's network restrictions."
        ) from exc

    try:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with conn.transaction():
            return _read(conn)
    except psycopg.Error as exc:
        raise SourceError(f"reading the source project failed: {exc.sqlstate}") from exc
    finally:
        conn.close()


def _read(conn: psycopg.Connection) -> SourceFacts:
    params = {"system": list(_SYSTEM_SCHEMAS), "supabase": list(SUPABASE_SCHEMAS)}
    facts = SourceFacts()

    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database()) AS bytes, version() AS v")
        row = cur.fetchone()
        facts.database_bytes = int(row["bytes"])
        facts.server_version = row["v"].split(" on ")[0]

    facts.schemas = _probe(conn, _SCHEMAS, params)
    facts.extensions = _probe(conn, _EXTENSIONS, params)
    facts.relations = _probe(conn, _RELATIONS, params)
    facts.policies = _probe(conn, _POLICIES, params)
    facts.functions = _probe(conn, _FUNCTIONS, params)
    facts.triggers = _probe(conn, _TRIGGERS, params)
    facts.foreign_tables = _probe(conn, _FOREIGN_TABLES, params)
    facts.auth_users = _probe(conn, _AUTH_USERS, params)
    facts.auth_identities = _probe(conn, _AUTH_IDENTITIES, params)
    facts.storage_buckets = _probe(conn, _STORAGE_BUCKETS, params)
    facts.storage_objects = _probe(conn, _STORAGE_OBJECTS, params)
    facts.realtime_publication = _probe(conn, _REALTIME_PUBLICATION, params)
    facts.function_hooks = _probe(conn, _FUNCTION_HOOKS, params)
    facts.vault_secrets = _probe(conn, _VAULT_SECRETS, params)

    _mark_rls_blind_probes(conn, facts)
    return facts


def _mark_rls_blind_probes(conn: psycopg.Connection, facts: SourceFacts) -> None:
    """Turn a silently filtered count into an admitted unknown.

    Runs after the probes rather than before, because the cheap case is the
    common one: a session that bypasses RLS needs none of this, and a relation
    the project does not have answers `to_regclass` with NULL.
    """
    bypasses = _scalar(conn, _SESSION_BYPASSES_RLS, key="bypasses", default=False)
    if bypasses:
        return

    for attribute, relation in RLS_SENSITIVE.items():
        probe: Probe = getattr(facts, attribute)
        if not probe.readable:
            continue  # already known to be unreadable, for a louder reason
        applies = _rls_applies(conn, relation)
        if applies:
            setattr(
                facts,
                attribute,
                Probe(
                    readable=False,
                    reason=(
                        f"row-level security on {relation} filters this session, so a "
                        "count from it would be a floor rather than a total"
                    ),
                ),
            )


def _rls_applies(conn: psycopg.Connection, relation: str) -> bool:
    """True when this session would silently see fewer rows than exist."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(_RLS_APPLIES, (relation,))
                row = cur.fetchone()
    except psycopg.Error:
        # Cannot tell. Say nothing rather than raise a finding on a guess: the
        # probe's own result stands, and a false blocker on every scan would
        # teach customers to ignore the real ones.
        return False
    if row is None or not row["enabled"]:
        return False
    return row["forced"] or not row["owns"]


def _scalar(conn: psycopg.Connection, query: str, *, key: str, default: Any) -> Any:
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()
        return default if row is None else row[key]
    except psycopg.Error:
        return default


def _probe(conn: psycopg.Connection, query: str, params: dict) -> Probe:
    """One catalogue read that is allowed to fail without ending the scan.

    Two failures are expected and mean different things, so they are told apart:
    a schema that does not exist (this project does not use that Supabase
    feature) and one the credential cannot read (it might). Anything else is
    reported with its `sqlstate` rather than swallowed.

    Each runs in its own savepoint. Without one, the first missing schema would
    abort the surrounding transaction and every later probe would fail with
    `25P02` -- a scan that reported the customer had nothing at all.
    """
    # Passed only to the queries that have placeholders. psycopg does no `%`
    # processing at all when parameters are absent, so a query written with
    # `%%` needs them and a query written without any needs them *not* to be
    # there -- handing parameters to a query with no placeholders is an error.
    bound = params if "%(" in query else None
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(query, bound)
                return Probe(rows=list(cur.fetchall()))
    except psycopg.errors.UndefinedTable:
        return Probe(readable=True, rows=[], reason="not present in this project")
    except psycopg.errors.InsufficientPrivilege:
        return Probe(readable=False, reason="the supplied credential may not read it")
    except psycopg.Error as exc:
        return Probe(readable=False, reason=f"read failed ({exc.sqlstate})")
