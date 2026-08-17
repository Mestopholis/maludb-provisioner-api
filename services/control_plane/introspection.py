"""Reading a tenant's catalogue on the customer's behalf (Phase 08 slice 2).

The half of a dashboard a frontend cannot compose out of raw SQL. Slice 1 gave
every tier a way to *run* a statement; this gives it a way to see what it is
running against, without reimplementing `pg_catalog` knowledge in TypeScript.

Three properties hold here that do not hold in `sql_console`, and each is a
consequence of the same difference: **no customer text reaches the database on
this path.** Every statement below is a constant in this file, and the only
caller-supplied values are schema names, passed as parameters rather than
composed into an identifier.

- **The transaction is genuinely read-only.** ADR-040 records that a read-only
  transaction is *not* a control against submitted SQL, because
  `SET default_transaction_read_only = off` is accepted inside one. That finding
  applies to a session running text the customer wrote. It does not apply here,
  where nothing can issue a `SET`, so `READ ONLY` is what it appears to be: a
  backstop that makes a future edit adding a write fail loudly rather than
  quietly succeed.
- **The snapshot is consistent.** `REPEATABLE READ` for the same reason a
  dashboard should not show a table whose columns it failed to fetch because a
  migration landed between two queries.
- **The output is filtered, not dumped.** Two catalogues are cluster-scoped
  rather than database-scoped, and one of them is a cross-tenant disclosure if
  it is passed through:

  `pg_roles` lists every role on the node, which on a shared node means every
  other tenant's `mldb_<ref>_*` roles -- and a ref is the customer's API
  subdomain (ADR-008). So roles are answered from an **allowlist of names built
  from this project**, never from what the catalogue happens to contain. The
  ADR-014 `CONNECT` lockdown does not help here: role rows are visible from
  inside any database on the cluster.

  `pg_available_extensions` is node-wide and is deliberately *not* reported.
  Listing what could be installed advertises a capability no customer currently
  has -- `mldb_<ref>_admin` cannot `CREATE EXTENSION` at all (negative test H) --
  and what to do about that is the open question slice 4 answers.

`maludb_platform` is hidden along with the system schemas. Bootstrap 004 created
`maludb_platform.tables_without_rls` "so the dashboard can surface it", and the
schema is `REVOKE ALL ... FROM PUBLIC`, so no role this path can reach may read
it. Nothing is lost: `rls_enabled` is reported per table below, from the same
catalogue column the view reads, without granting anything on a platform schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.control_plane import provisioning, sql_console

log = logging.getLogger(__name__)

# Schemas a customer is not shown. `pg_*` is matched by pattern so `pg_toast`,
# `pg_temp_N` and any future one are covered without a list to maintain.
#
# `auth` is deliberately absent: a customer writing an RLS policy needs to see
# `auth.users`, Supabase's dashboard shows it, and hiding it would make a
# migrated policy unexplainable. What protects it is grants, not concealment.
HIDDEN_SCHEMAS = ("information_schema", "maludb_platform")

# Per catalogue, not per response. A project with more relations than this is
# pathological rather than large -- and the honest answer to one is a named
# truncation, not a request that reads the whole catalogue into the control
# plane's memory. `?schema=` is how a real project narrows this.
CATALOG_ROW_CAP = 5_000

# Columns outnumber tables by an order of magnitude and a wide schema is normal
# rather than pathological, so this one is separate and larger.
COLUMN_ROW_CAP = 50_000

_SCHEMA_FILTER = """
    n.nspname NOT LIKE 'pg\\_%%'
AND n.nspname <> ALL(%(hidden)s)
AND (%(only)s::text[] IS NULL OR n.nspname = ANY(%(only)s))
"""

# Relations a customer can act on. Indexes, sequences and TOAST tables are
# omitted: an index is reported against the table it belongs to, and a sequence
# is an implementation detail of the column that owns it.
_RELKINDS = ("r", "p", "v", "m", "f")

_SCHEMAS = f"""
SELECT n.nspname                       AS name,
       pg_get_userbyid(n.nspowner)     AS owner,
       obj_description(n.oid, 'pg_namespace') AS comment
  FROM pg_namespace n
 WHERE {_SCHEMA_FILTER}
 ORDER BY n.nspname
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_TABLES = f"""
SELECT c.oid                           AS id,
       n.nspname                       AS schema,
       c.relname                       AS name,
       c.relkind                       AS kind,
       c.relrowsecurity                AS rls_enabled,
       c.relforcerowsecurity           AS rls_forced,
       pg_get_userbyid(c.relowner)     AS owner,
       obj_description(c.oid, 'pg_class') AS comment,
       -- The planner's estimate, and named as one. An exact count means
       -- reading every table on a shared node to render a page.
       c.reltuples::bigint             AS estimated_rows,
       pg_total_relation_size(c.oid)   AS size_bytes
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = ANY(%(relkinds)s) AND {_SCHEMA_FILTER}
 ORDER BY n.nspname, c.relname
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_COLUMNS = f"""
SELECT a.attrelid                      AS table_id,
       a.attnum                        AS position,
       a.attname                       AS name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull                AS is_nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default_expression,
       a.attidentity <> ''             AS is_identity,
       a.attgenerated <> ''            AS is_generated,
       col_description(a.attrelid, a.attnum) AS comment
  FROM pg_attribute a
  JOIN pg_class c     ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE a.attnum > 0 AND NOT a.attisdropped
   AND c.relkind = ANY(%(relkinds)s) AND {_SCHEMA_FILTER}
 ORDER BY a.attrelid, a.attnum
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_INDEXES = f"""
SELECT i.indrelid                      AS table_id,
       ic.relname                      AS name,
       i.indisunique                   AS is_unique,
       i.indisprimary                  AS is_primary,
       i.indisvalid                    AS is_valid,
       pg_get_indexdef(i.indexrelid)   AS definition
  FROM pg_index i
  JOIN pg_class ic    ON ic.oid = i.indexrelid
  JOIN pg_class c     ON c.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = ANY(%(relkinds)s) AND {_SCHEMA_FILTER}
 ORDER BY i.indrelid, ic.relname
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_CONSTRAINTS = f"""
SELECT con.conrelid                    AS table_id,
       con.conname                     AS name,
       con.contype                     AS kind,
       pg_get_constraintdef(con.oid)   AS definition
  FROM pg_constraint con
  JOIN pg_class c     ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = ANY(%(relkinds)s) AND {_SCHEMA_FILTER}
 ORDER BY con.conrelid, con.conname
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_POLICIES = f"""
SELECT pol.polrelid                    AS table_id,
       pol.polname                     AS name,
       pol.polcmd                      AS command,
       pol.polpermissive               AS permissive,
       -- An empty polroles means PUBLIC, which PostgreSQL stores as the zero
       -- OID rather than as an empty array. Resolving it through
       -- pg_get_userbyid would answer "unknown (OID=0)" and a policy editor
       -- would render that as a role name.
       ARRAY(SELECT CASE WHEN r = 0 THEN 'public' ELSE pg_get_userbyid(r) END
               FROM unnest(pol.polroles) AS r) AS roles,
       pg_get_expr(pol.polqual, pol.polrelid)      AS using_expression,
       pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expression
  FROM pg_policy pol
  JOIN pg_class c     ON c.oid = pol.polrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE {_SCHEMA_FILTER}
 ORDER BY pol.polrelid, pol.polname
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_FUNCTIONS = f"""
SELECT n.nspname                       AS schema,
       p.proname                       AS name,
       p.prokind                       AS kind,
       pg_get_function_identity_arguments(p.oid) AS arguments,
       pg_get_function_result(p.oid)   AS returns,
       l.lanname                       AS language,
       p.prosecdef                     AS security_definer,
       CASE p.provolatile WHEN 'i' THEN 'immutable'
                          WHEN 's' THEN 'stable'
                          ELSE 'volatile' END AS volatility,
       pg_get_userbyid(p.proowner)     AS owner,
       obj_description(p.oid, 'pg_proc') AS comment,
       -- Withheld unless the caller's own admin role owns it; see
       -- `_visible_source`. A C-language function has no readable source
       -- anyway, and `prosrc` for one is the symbol name.
       CASE WHEN l.lanname IN ('internal', 'c') THEN NULL
            ELSE p.prosrc END          AS source
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  JOIN pg_language l  ON l.oid = p.prolang
 WHERE {_SCHEMA_FILTER}
   -- Extension-owned functions are not the customer's code and there are
   -- hundreds of them: pgcrypto and vector alone would bury a function list.
   AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
         WHERE d.objid = p.oid AND d.classid = 'pg_proc'::regclass
           AND d.deptype = 'e')
 ORDER BY n.nspname, p.proname
 LIMIT %(cap)s
"""  # noqa: S608 - the only interpolation is _SCHEMA_FILTER, a constant in this file

_EXTENSIONS = """
SELECT e.extname                       AS name,
       n.nspname                       AS schema,
       e.extversion                    AS installed_version
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
 ORDER BY e.extname
 LIMIT %(cap)s
"""

# By name, from an allowlist this process builds. Never `SELECT ... FROM
# pg_roles` unfiltered: that catalogue is cluster-scoped and would answer with
# every other tenant's project ref.
_ROLES = """
SELECT r.rolname                       AS name,
       r.rolcanlogin                   AS can_login,
       r.rolconnlimit                  AS connection_limit
  FROM pg_roles r
 WHERE r.rolname = ANY(%(names)s)
 ORDER BY r.rolname
"""


# PostgreSQL stores these as single characters. Translating them here rather
# than in the response model is the point of the endpoint: a frontend that had
# to know `relkind = 'm'` means a materialized view is reimplementing
# `information_schema` in TypeScript, which is the thing slice 2 exists to
# prevent. Every lookup falls back to the raw character, so a future PostgreSQL
# kind surfaces as itself rather than crashing a dashboard.
RELKINDS = {
    "r": "table", "p": "partitioned_table", "v": "view",
    "m": "materialized_view", "f": "foreign_table",
}
CONSTRAINT_KINDS = {
    "p": "primary_key", "f": "foreign_key", "u": "unique",
    "c": "check", "x": "exclusion", "t": "trigger",
}
POLICY_COMMANDS = {
    "r": "select", "a": "insert", "w": "update", "d": "delete", "*": "all",
}
FUNCTION_KINDS = {"f": "function", "p": "procedure", "a": "aggregate", "w": "window"}


@dataclass
class Snapshot:
    """One consistent read of a tenant's catalogue.

    `truncated` names the catalogues that hit their cap, rather than carrying a
    single boolean. A dashboard that must say "there are more tables" should not
    have to guess which list it is talking about.
    """

    schemas: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)
    extensions: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


def role_allowlist(project_ref: str) -> list[str]:
    """Every role name this project may legitimately be told about.

    Its own five, plus the three shared Supabase names an RLS policy can target.
    `TenantNames.for_ref` validates the ref against a strict alphabet before
    deriving anything, so a crafted ref cannot widen this list.
    """
    names = provisioning.TenantNames.for_ref(project_ref)
    return [
        *provisioning.SHARED_ROLES,
        names.admin,
        names.authenticator,
        names.auth,
        names.executor,
        names.replicator,
    ]


def snapshot(
    dsn: str,
    *,
    run_as: str,
    project_ref: str,
    timeout_ms: int,
    schemas: list[str] | None = None,
) -> Snapshot:
    """Read the catalogue as `run_as`, in one read-only repeatable-read snapshot.

    `run_as` matters for more than tidiness: several of these queries answer
    what the *current role* may see, so reading as anything other than the
    tenant's admin role would show a customer a database that is not the one
    their own statements run against.
    """
    if timeout_ms <= 0:  # pragma: no cover - entitlements refuses a zero
        raise ValueError("timeout_ms must be positive; a zero ceiling is no ceiling")

    try:
        conn = psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=sql_console.CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        )
    except psycopg.Error as exc:
        # Deliberately not `str(exc)`: a connection error can echo the DSN.
        log.warning("introspection could not reach the tenant database: %s", exc.sqlstate)
        raise sql_console.ConsoleError("could not reach the project's database") from exc

    timer = None
    try:
        with conn.cursor() as setup:
            setup.execute("SELECT set_config('statement_timeout', %s, false)", (str(timeout_ms),))
            setup.execute(
                psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(run_as))
            )

        timer = sql_console.cancel_after(conn, timeout_ms / 1000)
        # Composed by the driver into the `BEGIN` below rather than issued as a
        # `SET` afterwards, so there is no window in which the transaction has
        # started and is not yet read-only. Opened after `SET ROLE` so the role
        # change is not rolled back with the transaction.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            return _read(conn, project_ref=project_ref, schemas=schemas)
    except psycopg.errors.QueryCanceled as exc:
        raise sql_console.ConsoleError(f"introspection cancelled after {timeout_ms} ms") from exc
    except psycopg.Error as exc:
        raise sql_console.ConsoleError(f"{exc.sqlstate}: {sql_console.first_line(exc)}") from exc
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def _read(conn: psycopg.Connection, *, project_ref: str, schemas: list[str] | None) -> Snapshot:
    params: dict[str, Any] = {
        "hidden": list(HIDDEN_SCHEMAS),
        # None, not an empty list: an empty `?schema=` filter must not be the
        # difference between "every schema" and "no schema at all".
        "only": schemas or None,
        "relkinds": list(_RELKINDS),
        "cap": CATALOG_ROW_CAP + 1,
    }
    out = Snapshot()

    out.schemas = _fetch(conn, _SCHEMAS, params, "schemas", out)
    tables = _fetch(conn, _TABLES, params, "tables", out)
    columns = _fetch(
        conn, _COLUMNS, {**params, "cap": COLUMN_ROW_CAP + 1}, "columns", out,
        cap=COLUMN_ROW_CAP,
    )
    indexes = _fetch(conn, _INDEXES, params, "indexes", out)
    constraints = _fetch(conn, _CONSTRAINTS, params, "constraints", out)
    policies = _fetch(conn, _POLICIES, params, "policies", out)
    functions = _fetch(conn, _FUNCTIONS, params, "functions", out)
    out.extensions = _fetch(conn, _EXTENSIONS, params, "extensions", out)

    with conn.cursor() as cur:
        cur.execute(_ROLES, {"names": role_allowlist(project_ref)})
        out.roles = [
            {**row, "is_shared": row["name"] in provisioning.SHARED_ROLES} for row in cur.fetchall()
        ]

    # The admin role is what a customer's own statements run as, so anything it
    # owns is theirs and anything it does not is the platform's. That is the
    # line `sql_console.first_line` already draws for error text, applied to
    # function bodies: a customer may read their own, and the platform's
    # `SECURITY DEFINER` bodies are not theirs to read.
    admin_role = provisioning.TenantNames.for_ref(project_ref).admin
    out.functions = [
        _visible_source({**row, "kind": FUNCTION_KINDS.get(row["kind"], row["kind"])}, admin_role)
        for row in functions
    ]

    by_table = {row["id"]: row for row in tables}
    for row in tables:
        row.update(columns=[], indexes=[], constraints=[], policies=[])
        row["kind"] = RELKINDS.get(row["kind"], row["kind"])
        row["managed"] = row["owner"] != admin_role
    for constraint in constraints:
        constraint["kind"] = CONSTRAINT_KINDS.get(constraint["kind"], constraint["kind"])
    for policy in policies:
        policy["command"] = POLICY_COMMANDS.get(policy["command"], policy["command"])
    for key, rows in (
        ("columns", columns), ("indexes", indexes),
        ("constraints", constraints), ("policies", policies),
    ):
        for row in rows:
            parent = by_table.get(row.pop("table_id"))
            # A parent missing means its catalogue was truncated while this one
            # was not. Dropping the orphan is right: the alternative is a column
            # attached to nothing.
            if parent is not None:
                parent[key].append(row)

    for row in tables:
        row.pop("id")
    out.tables = tables
    return out


def _fetch(
    conn: psycopg.Connection,
    query: str,
    params: dict[str, Any],
    name: str,
    out: Snapshot,
    *,
    cap: int | None = None,
) -> list[dict[str, Any]]:
    """One catalogue, capped, with the overflow recorded rather than hidden.

    `cap` resolves at call time rather than as a default argument, so the module
    constant is one thing to change rather than two.
    """
    cap = CATALOG_ROW_CAP if cap is None else cap
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if len(rows) > cap:
        out.truncated.append(name)
        return rows[:cap]
    return rows


def _visible_source(row: dict[str, Any], admin_role: str) -> dict[str, Any]:
    managed = row["owner"] != admin_role
    return {**row, "managed": managed, "source": None if managed else row["source"]}
