"""`pg_dump` carries every row `maludb_core` stores, and none it installs (ADR-078).

Registration slice 2's acceptance test, and the gate on `specs/extension-versions.yaml`:
a `maludb_core` version is listed only once this passes on it.

**Every table, not a sample.** The risk ADR-078 names is a wrong filter, which is
silent loss or silent duplication in one table out of a hundred and fifty. So a
customer row is written to *every* table the extension owns -- found from the
catalogue, not listed -- then the database goes through the platform's own
`pg_restore` invocation into a fresh one, and each table is compared: the rows
that should have travelled did, byte for byte, and the installed rows are not
there twice.

The rows are synthesised from the catalogue with `session_replication_role =
replica`, so neither foreign keys nor triggers stand in the way; CHECK
constraints still do, and `ROW_OVERRIDES` answers those table by table. A table a
later release adds is filled the same way or fails naming itself, so it cannot be
left out by not being thought of.

**The control is the same test on 0.104.0**, the last version that registers
nothing: the same rows, the same round trip, and every customer row lost. A test
that could not fail there would be proving nothing on 0.105.0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import psycopg
import pytest
from psycopg import sql

from services.control_plane import restore
from tests.conftest import REQUIRE_MALUDB_CORE
from tests.test_provisioning import ADMIN_DSN, _tenant_admin_dsn, requires_maludb_core

pytestmark = [requires_maludb_core]

REGISTERING = "0.105.0"
BEFORE_REGISTRATION = "0.104.0"
CUSTOMER_SCHEMA = "app"

# What a registering version deliberately leaves out of the dump
# (`specs/maludb-core-dump-registration.md` sections 5 and 6).
#
# Catalogues only a superuser can write: filled like any table, and the row must
# stay behind.
CATALOGUES = ("malu$object_type", "malu$relationship_type", "malu$source_type")
# One row each, generated at install; a CHECK allows no second. A dump never
# holds a key -- the owner's decision -- so the target must have its own.
SECRETS = ("malu$secret_master_key", "malu$auth_pepper")
NOT_REGISTERED = frozenset(CATALOGUES + SECRETS)

# Column values the catalogue cannot infer: CHECK constraints that relate two
# columns, or test a shape rather than list values. Each value is an SQL
# expression. Enumerations are not here -- `_allowed_values` reads those.
# 987654 is an id no fill writes, for "must differ from the other column".
ROW_OVERRIDES: dict[str, dict[str, str]] = {
    "malu$budget_policy": {  # scope decides which scope_* may be set
        "scope": "'global'", "scope_account_id": "NULL", "scope_template_id": "NULL",
    },
    "malu$chat_message": {"content_text": "'hello'"},
    "malu$embedding_adapter": {"target_space_id": "987654"},  # source <> target
    "malu$embedding_output": {"vector_dim": "1", "vector": "'\\x00000000'::bytea"},  # 4 bytes per dim
    "malu$index_migration": {"target_space_id": "987654"},
    "malu$mc2db_tool_external_exec": {"command_path": "'/bin/true'"},  # absolute path
    "malu$mc2db_tool_mcp_proxy": {"transport_type": "'http'", "endpoint_url": "'http://127.0.0.1/'"},
    "malu$memory_detail_object": {  # a plain memory detail belongs to no tree
        "mdo_kind": "'memory_detail'", "tree_id": "NULL", "chat_tree_id": "NULL", "node_kind": "NULL",
        "episode_id": "987654",
    },
    "malu$object_embedding": {"embedding_dim": "1", "embedding": "'\\x00000000'::bytea"},
    "malu$raw_ingest": {"content_text": "'hello'"},
    "malu$secret_version": {"value_encrypted": "'\\x01'::bytea", "external_ref": "NULL"},  # exactly one
    "malu$semantic_edge": {"target_id": "987654"},
    "malu$session_context": {"content_text": "'hello'"},
    "malu$skill_embedding": {"embedding_dim": "3", "embedding": "'[1,2,3]'::maludb_core.malu_vector"},
    "malu$source_object": {"content_hash": "sha256('x'::bytea)"},  # 32 bytes
    "malu$source_package": {"content_text": "'hello'"},
    "malu$svpor_subject_relationship_edge": {"to_subject_id": "987654"},
    "malu$vector_chunk": {"embedding_dim": "3", "embedding": "'[1,2,3]'::maludb_core.malu_vector"},
}


def _dsn(database: str) -> str:
    return _tenant_admin_dsn(database)


def _libpq_env() -> dict[str, str]:
    """Host, user and password for the command-line tools, from the admin DSN.

    The port and database come from the platform's own argv, which is the thing
    under test; only what that argv leaves to the environment is supplied here.
    """
    parsed = urlsplit(ADMIN_DSN)
    env = dict(os.environ)
    env["PGHOST"] = parsed.hostname or "127.0.0.1"
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return env


def _port() -> int:
    return urlsplit(ADMIN_DSN).port or 5432


@dataclass
class Column:
    name: str
    type: str
    category: str
    typtype: str
    not_null: bool
    default: str | None
    identity: str
    generated: str
    base: str
    typmod: int


def _extension_tables(conn: psycopg.Connection) -> list[str]:
    """Tables the extension owns, parents before the tables that reference them."""
    tables = [r[0] for r in conn.execute(
        "SELECT c.relname FROM pg_extension e "
        "JOIN pg_depend d ON d.refobjid = e.oid AND d.deptype = 'e' AND d.classid = 'pg_class'::regclass "
        "JOIN pg_class c ON c.oid = d.objid AND c.relkind = 'r' "
        "WHERE e.extname = 'maludb_core' ORDER BY 1"
    ).fetchall()]
    edges: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in conn.execute(
        "SELECT c.relname, p.relname FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
        "JOIN pg_class p ON p.oid = k.confrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE k.contype = 'f' AND n.nspname = 'maludb_core' AND c.relname <> p.relname"
    ).fetchall():
        if child in edges and parent in edges:
            edges[child].add(parent)
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(table: str, path: frozenset[str]) -> None:
        if table in seen or table in path:
            return
        for parent in sorted(edges[table]):
            visit(parent, path | {table})
        seen.add(table)
        ordered.append(table)

    for table in tables:
        visit(table, frozenset())
    return ordered


def _columns(conn: psycopg.Connection, table: str) -> list[Column]:
    return [Column(*r) for r in conn.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), bt.typcategory, bt.typtype, "
        "a.attnotnull, pg_get_expr(ad.adbin, ad.adrelid), a.attidentity::text, a.attgenerated::text, "
        "bt.typname, CASE WHEN t.typtype = 'd' THEN t.typtypmod ELSE a.atttypmod END "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "JOIN pg_type bt ON bt.oid = CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.oid END "
        "LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
        "WHERE n.nspname = 'maludb_core' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        (table,),
    ).fetchall()]


def _foreign_keys(conn: psycopg.Connection, table: str) -> dict[str, tuple[str, str]]:
    """column -> (referenced table, referenced column), for single-column keys."""
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT a.attname, p.relname, pa.attname FROM pg_constraint k "
        "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_class p ON p.oid = k.confrelid "
        "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1] "
        "JOIN pg_attribute pa ON pa.attrelid = k.confrelid AND pa.attnum = k.confkey[1] "
        "WHERE k.contype = 'f' AND cardinality(k.conkey) = 1 AND n.nspname = 'maludb_core' "
        "AND c.relname = %s",
        (table,),
    ).fetchall()}


_ANY_ARRAY = re.compile(r"\(?\b(\w+)\b\)?(?:::\w+)* = ANY \(\(?ARRAY\[\(?('(?:[^']|'')*')")
_EQUALS = re.compile(r"\(?\b(\w+)\b\)?(?:::\w+)* = ('(?:[^']|'')*')::")


def _allowed_values(conn: psycopg.Connection, table: str) -> dict[str, str]:
    """column -> the first literal a CHECK constraint lists for it.

    Most of the extension's constraints are enumerations written as
    `kind = ANY (ARRAY['a'::text, ...])`; taking the first value satisfies them
    without a hand-written row per table.
    """
    allowed: dict[str, str] = {}
    for (definition,) in conn.execute(
        "SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE k.contype = 'c' AND n.nspname = 'maludb_core' AND c.relname = %s",
        (table,),
    ).fetchall():
        for pattern in (_ANY_ARRAY, _EQUALS):
            for column, literal in pattern.findall(definition):
                allowed.setdefault(column, literal)
    return allowed


def _synthesised(conn: psycopg.Connection, column: Column, serial: int) -> sql.Composable:
    """A value for a NOT NULL column with no default, from its type alone."""
    typed = sql.SQL(column.type)
    if column.category == "S":
        return sql.SQL("{}::{}").format(sql.Literal(f"c{serial}"), typed)
    if column.category == "N":
        if column.base in ("float4", "float8", "numeric"):
            number = 1
        else:
            number = serial % 30_000 if column.base == "int2" else serial
        return sql.SQL("{}::{}").format(sql.Literal(number), typed)
    if column.category == "B":
        return sql.SQL("false")
    if column.category == "D":
        return sql.SQL("now()::{}").format(typed)
    if column.category == "T":
        return sql.SQL("'1 hour'::interval")
    if column.category == "A":
        return sql.SQL("'{{}}'::{}").format(typed)
    if column.category == "R":
        return sql.SQL("'empty'::{}").format(typed)
    if column.category == "I":
        return sql.SQL("'127.0.0.1'::{}").format(typed)
    if column.typtype == "c":
        # A composite: a row of nulls, which a NOT NULL column accepts -- the
        # constraint is on the value, not on its fields.
        fields = conn.execute(
            "SELECT count(*) FROM pg_attribute a JOIN pg_type t ON t.typrelid = a.attrelid "
            "WHERE t.typname = %s AND a.attnum > 0 AND NOT a.attisdropped", (column.base,)
        ).fetchone()[0]
        return sql.SQL("{}::{}").format(sql.Literal("(" + "," * (fields - 1) + ")"), typed)
    if column.typtype == "e":
        label = conn.execute(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = %s ORDER BY e.enumsortorder LIMIT 1", (column.base,)
        ).fetchone()[0]
        return sql.SQL("{}::{}").format(sql.Literal(label), typed)
    if column.base == "uuid":
        return sql.SQL("gen_random_uuid()")
    if column.base in ("json", "jsonb"):
        return sql.SQL("'{{}}'::{}").format(typed)
    if column.base == "bytea":
        return sql.SQL("'\\x01'::bytea")
    if column.base == "tsvector":
        return sql.SQL("''::tsvector")
    if column.base in ("vector", "malu_vector"):
        dims = column.typmod if column.typmod > 0 else 3
        return sql.SQL("{}::{}").format(sql.Literal("[" + ",".join(["1"] * dims) + "]"), typed)
    raise AssertionError(f"no synthesised value for a {column.type} column ({column.name}); add an override")


def _as_text(value) -> str:
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


def _value_for(
    conn: psycopg.Connection, column: Column, *, serial: int, override: str | None,
    parent: tuple[dict, str] | None, allowed: str | None,
) -> sql.Composable | None:
    """What to write in a column, or None to leave it to its default or NULL."""
    if override is not None:
        return sql.SQL(override)
    if allowed is not None and column.not_null and column.default is None:
        return sql.SQL("{}::{}").format(sql.SQL(allowed), sql.SQL(column.type))
    if column.name == "owner_schema":
        # The marker the filters read: a customer's rows carry their own schema.
        return sql.SQL("{}::{}").format(sql.Literal(CUSTOMER_SCHEMA), sql.SQL(column.type))
    if column.name == "system_defined":
        return sql.SQL("false")
    if parent is not None:
        # The referenced row this fill wrote, so a child filtered by its parent
        # (the mc2db tool tables) follows a customer parent.
        row, parent_column = parent
        if row.get(parent_column) is not None:
            return sql.SQL("{}::{}").format(sql.Literal(_as_text(row[parent_column])), sql.SQL(column.type))
    if column.default is not None or column.identity or not column.not_null:
        return None
    return _synthesised(conn, column, serial)


def fill(conn: psycopg.Connection, tables: list[str]) -> dict[str, str]:
    """One customer row in each table. Returns the tables that refused, with why."""
    conn.execute("SET session_replication_role = replica")
    written: dict[str, dict] = {}
    refused: dict[str, str] = {}
    for serial, table in enumerate(tables, start=900_001):
        keys = _foreign_keys(conn, table)
        overrides = ROW_OVERRIDES.get(table, {})
        allowed = _allowed_values(conn, table)
        names: list[sql.Composable] = []
        values: list[sql.Composable] = []
        for column in _columns(conn, table):
            if column.generated or column.identity == "a":
                continue
            parent = None
            if column.name in keys and keys[column.name][0] in written:
                parent = (written[keys[column.name][0]], keys[column.name][1])
            value = _value_for(conn, column, serial=serial, override=overrides.get(column.name), parent=parent,
                               allowed=allowed.get(column.name))
            if value is not None:
                names.append(sql.Identifier(column.name))
                values.append(value)
        target = sql.Identifier("maludb_core", table)
        if names:
            statement = sql.SQL("INSERT INTO {} AS t ({}) VALUES ({}) RETURNING to_jsonb(t.*)").format(
                target, sql.SQL(", ").join(names), sql.SQL(", ").join(values))
        else:
            statement = sql.SQL("INSERT INTO {} AS t DEFAULT VALUES RETURNING to_jsonb(t.*)").format(target)
        try:
            with conn.transaction():
                written[table] = conn.execute(statement).fetchone()[0]
        except psycopg.Error as exc:
            refused[table] = str(exc).splitlines()[0]
    conn.execute("SET session_replication_role = origin")
    return refused


# -- the round trip ------------------------------------------------------------


@dataclass
class RoundTrip:
    version: str
    source: str
    target: str
    restore: subprocess.CompletedProcess
    refused: dict[str, str]


def _available(version: str) -> bool:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        return bool(admin.execute(
            "SELECT 1 FROM pg_available_extension_versions WHERE name = 'maludb_core' AND version = %s",
            (version,),
        ).fetchone())


def _recreate(admin: psycopg.Connection, database: str) -> None:
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def _round_trip(version: str, *, argv=restore.pg_restore_argv) -> RoundTrip:
    """Fill a database on `version`, dump it, and load it the way the platform does."""
    if not _available(version):
        message = f"maludb_core {version} is not installable on this node"
        if REQUIRE_MALUDB_CORE:
            # CI builds the newest listed version and keeps the older install
            # scripts beside it; an absent one is a broken build, not a skip.
            pytest.fail(message)
        pytest.skip(message)
    tag = version.replace(".", "_")
    source, target = f"mldb_dumpreg_src_{tag}", f"mldb_dumpreg_dst_{tag}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        _recreate(admin, source)
        _recreate(admin, target)
    with psycopg.connect(_dsn(source), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE EXTENSION maludb_core VERSION {} CASCADE").format(sql.Literal(version)))
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(CUSTOMER_SCHEMA)))
        refused = fill(conn, [t for t in _extension_tables(conn) if t not in SECRETS])
    env = _libpq_env()
    with tempfile.TemporaryDirectory() as scratch:
        dump_path = f"{scratch}/source.dump"
        # The move's own dump invocation (`tenant_movement.dump_from_source`).
        subprocess.run(  # noqa: S603 - fixed argv
            ["pg_dump", "-p", str(_port()), "-Fc", "-f", dump_path, source],  # noqa: S607
            env=env, check=True, capture_output=True, text=True,
        )
        loaded = subprocess.run(  # noqa: S603 - the platform's argv
            argv(port=_port(), database=target, dump_path=dump_path),
            env=env, check=False, capture_output=True, text=True,
        )
    return RoundTrip(version=version, source=source, target=target, restore=loaded, refused=refused)


def _drop_round_trip(trip: RoundTrip) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        for database in (trip.source, trip.target):
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))


@pytest.fixture(scope="module")
def registering():
    trip = _round_trip(REGISTERING)
    try:
        yield trip
    finally:
        _drop_round_trip(trip)


@pytest.fixture(scope="module")
def before_registration():
    trip = _round_trip(BEFORE_REGISTRATION)
    try:
        yield trip
    finally:
        _drop_round_trip(trip)


def _registered(conn: psycopg.Connection) -> dict[str, str]:
    return dict(conn.execute(
        "SELECT c.relname, coalesce(e.extcondition[array_position(e.extconfig, c.oid)], '') "
        "FROM pg_extension e JOIN pg_class c ON c.oid = ANY (e.extconfig) "
        "WHERE e.extname = 'maludb_core' AND c.relkind = 'r'"
    ).fetchall())


def _rows(
    conn: psycopg.Connection, table: str, condition: str = "", *, without: tuple[str, ...] = ()
) -> tuple[int, str]:
    """How many rows, and a digest of exactly which, optionally through a dump filter."""
    row = sql.SQL("to_jsonb(t)")
    for column in without:
        row = sql.SQL("({} - {})").format(row, sql.Literal(column))
    statement = sql.SQL(
        "SELECT count(*), coalesce(md5(string_agg({row}::text, '|' ORDER BY {row}::text)), '') "
        "FROM {table} t {condition}"
    ).format(row=row, table=sql.Identifier("maludb_core", table), condition=sql.SQL(condition))
    count, digest = conn.execute(statement).fetchone()
    return int(count), digest


def _tables(conn: psycopg.Connection) -> list[str]:
    return _extension_tables(conn)


def _timestamps(conn: psycopg.Connection, table: str) -> tuple[str, ...]:
    return tuple(c.name for c in _columns(conn, table) if c.category == "D")


# -- on the registering version ---------------------------------------------------


def test_every_table_got_a_customer_row(registering):
    """Otherwise the comparisons below would pass on tables nothing was written to."""
    assert registering.refused == {}


def test_every_table_is_registered_or_named_as_left_out(registering):
    with psycopg.connect(_dsn(registering.source)) as conn:
        registered = _registered(conn)
        unregistered = set(_tables(conn)) - set(registered)
    assert unregistered == set(NOT_REGISTERED), (
        "a table is neither registered for dumping nor named in NOT_REGISTERED; a release that "
        f"adds a table must do one or the other: {sorted(unregistered ^ set(NOT_REGISTERED))}"
    )


def test_every_customer_row_passes_its_tables_dump_filter(registering):
    """A filter that excluded the customer's own row would lose it in every dump."""
    with psycopg.connect(_dsn(registering.source)) as conn:
        excluded = [table for table, condition in _registered(conn).items()
                    if _rows(conn, table, condition)[0] == 0]
    assert excluded == []


def test_the_platforms_restore_loads_without_an_error(registering):
    assert registering.restore.returncode == 0, registering.restore.stderr[-2000:]


def test_every_customer_row_arrives_and_no_installed_row_is_duplicated(registering):
    """The acceptance criterion, per table: what the filter passes arrives exactly,
    and the table as a whole holds exactly what the source held -- so neither a
    lost customer row nor a second copy of an installed one can hide in a total.

    The whole-table comparison leaves out timestamp columns and nothing else. A
    filtered table's installed rows are not in the dump; `CREATE EXTENSION`
    writes them again on the target, identical but for when. Measured: ids,
    names and bodies match, so a customer row pointing at a built-in still
    points at the same one.
    """
    with psycopg.connect(_dsn(registering.source)) as src, psycopg.connect(_dsn(registering.target)) as dst:
        registered = _registered(src)
        differing = {}
        for table, condition in sorted(registered.items()):
            filtered = (_rows(src, table, condition), _rows(dst, table, condition))
            stamps = _timestamps(src, table)
            whole = (_rows(src, table, without=stamps), _rows(dst, table, without=stamps))
            if filtered[0] != filtered[1] or whole[0] != whole[1]:
                differing[table] = {"filtered (count, digest)": filtered, "whole": whole}
    assert len(registered) > 100
    assert differing == {}


def test_a_superuser_catalogue_row_stays_behind(registering):
    with psycopg.connect(_dsn(registering.source)) as src, psycopg.connect(_dsn(registering.target)) as dst:
        travelled = {table: (_rows(src, table)[0], _rows(dst, table)[0]) for table in CATALOGUES
                     if _rows(dst, table)[0] != _rows(src, table)[0] - 1}
    assert travelled == {}


def test_a_restored_database_has_keys_of_its_own(registering):
    """A dump never holds a key (owner's decision): the target generated its own
    at install, and the source's did not overwrite it."""
    with psycopg.connect(_dsn(registering.source)) as src, psycopg.connect(_dsn(registering.target)) as dst:
        for table in SECRETS:
            source, target = _rows(src, table), _rows(dst, table)
            assert (source[0], target[0]) == (1, 1), table
            assert source[1] != target[1], f"{table} arrived from the source"


def test_sequences_arrive_where_the_source_left_them(registering):
    """A carried row with id 900001 and a sequence restarted at 1 is a collision
    waiting for the next insert."""
    query = (
        "SELECT s.relname, pg_sequence_last_value(s.oid) FROM pg_class s "
        "JOIN pg_namespace n ON n.oid = s.relnamespace JOIN pg_extension e ON e.extname = 'maludb_core' "
        "WHERE s.relkind = 'S' AND n.nspname = 'maludb_core' AND s.oid = ANY (e.extconfig)"
    )
    with psycopg.connect(_dsn(registering.source)) as src, psycopg.connect(_dsn(registering.target)) as dst:
        source, target = dict(src.execute(query).fetchall()), dict(dst.execute(query).fetchall())
    assert source
    assert target == source


# -- the control -------------------------------------------------------------------


def test_the_control_before_registration_loses_every_customer_row(before_registration):
    """Same rows, same round trip, 0.104.0: the test must be able to fail."""
    assert before_registration.refused == {}
    with psycopg.connect(_dsn(before_registration.source)) as src, \
            psycopg.connect(_dsn(before_registration.target)) as dst:
        assert _registered(src) == {}
        kept = [table for table in _tables(src)
                if table not in SECRETS and _rows(dst, table)[0] != _rows(src, table)[0] - 1]
    assert kept == []


def _plain_pg_restore(*, port: int, database: str, dump_path: str) -> list[str]:
    return ["pg_restore", "-p", str(int(port)), "-d", database, dump_path]


@pytest.fixture(scope="module")
def plain_restore():
    trip = _round_trip(REGISTERING, argv=_plain_pg_restore)
    try:
        yield trip
    finally:
        _drop_round_trip(trip)


def test_the_control_without_replica_mode_fires_the_extensions_triggers(plain_restore):
    """Why `pg_restore_argv` sets `session_replication_role`: the extension's tables
    exist with their triggers enabled before their rows load, so a plain restore
    re-runs them -- here writing embedding-queue rows nobody asked for -- and exits
    with errors."""
    assert plain_restore.restore.returncode != 0
    with psycopg.connect(_dsn(plain_restore.source)) as src, psycopg.connect(_dsn(plain_restore.target)) as dst:
        assert _rows(dst, "malu$embedding_dirty")[0] > _rows(src, "malu$embedding_dirty")[0]
