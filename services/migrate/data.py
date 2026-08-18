"""Carrying the rows (Phase 08 slice 6c).

Schema migration was structure; this is the part where being 99% right is
worthless. Three decisions, each measured before it was written.

**Values travel as their own text representation, not as JSON.** The obvious
design -- `json_agg` on the source, `json_populate_recordset` on the
destination -- was tried first and silently corrupts data: a `jsonb` column
holding the JSON value `null` becomes SQL `NULL`, because `row_to_json` renders
both as `null` and nothing downstream can tell them apart. Measured 2026-08-18.
Text out and text in is what `COPY` itself does, and the same probe round-tripped
every column identically: `bytea`, `text[]` with embedded commas and quotes,
`infinity` timestamps, `NaN` numerics, `±Infinity` floats, unicode, the empty
string, and `jsonb 'null'` distinct from `NULL`.

**Nothing is cast explicitly.** A quoted literal arrives as `unknown` and
PostgreSQL coerces it through the target column's own input function -- again
exactly what `COPY` does. Writing casts here would mean re-deriving every type
name correctly and getting `numeric(20,8)` and domains and arrays right, for no
gain.

**Tables are copied in dependency order.** A foreign key does not care that the
rows are arriving; inserting a child before its parent fails. The alternative --
disabling triggers -- needs privileges a tenant does not have and should not
get, so the order is computed instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

# Rows are accumulated until the composed statement reaches this, then sent.
# Under the console's 1,000,000-character request cap with room for the JSON
# envelope, the same budget the schema batches use.
MAX_BATCH_BYTES = 700_000

# How many rows to pull from the source at a time. A server-side cursor keeps a
# large table off the client's heap; this only bounds the round trips.
FETCH_SIZE = 5_000


# What the source session must be pinned to before a single value is read.
#
# The whole design rests on "out through the type's output function, back in
# through its input function" -- and several output functions are **GUC
# dependent**. `pg_dump` pins exactly these for the same reason; the first
# version of this module pinned nothing, and the slice 6c security review
# measured what that costs:
#
# - `DateStyle` at `SQL, DMY` renders 2024-03-04 as `04/03/2024`, which a
#   destination at the default `MDY` reads back as **3 April**. Silent, and
#   worse than uniform corruption because it only moves dates whose day is 12
#   or less -- so spot-checking a few rows finds nothing.
# - `extra_float_digits` at 0 renders 0.30000000000000004 as `0.3`.
#
# `row_security = off` is here for a different and equally quiet failure: with
# it on, a source role that cannot see every row copies a *subset* and the row
# counts agree, because the count is filtered too. With it off PostgreSQL
# raises instead, which is the outcome a migration wants.
SOURCE_SESSION = (
    "SET DateStyle = 'ISO, MDY'",
    "SET IntervalStyle = 'postgres'",
    "SET extra_float_digits = 3",
    "SET bytea_output = 'hex'",
    "SET standard_conforming_strings = on",
    "SET client_encoding = 'UTF8'",
    "SET row_security = off",
    # Empty, so `regclass` always renders a schema-qualified name and nothing in
    # a read resolves through a schema the customer controls. pg_dump pins this
    # for the same two reasons.
    "SET search_path = ''",
)

# The same two on the destination, prepended to each batch, so a tenant with an
# `ALTER DATABASE ... SET` of its own cannot reinterpret what arrives.
DESTINATION_SESSION = "SET DateStyle = 'ISO, MDY'; SET IntervalStyle = 'postgres';"


class DataError(RuntimeError):
    """The data could not be read or written. Never carries a DSN or a token."""


def prepare_source(conn: psycopg.Connection) -> None:
    """Pin the session the values are read out of. Raises if RLS would filter.

    `row_security = off` makes PostgreSQL refuse a read it would otherwise have
    quietly filtered, which is the difference between a migration that stops and
    one that copies a subset and reports success.
    """
    with conn.cursor() as cur:
        for statement in SOURCE_SESSION:
            cur.execute(statement)


@dataclass
class TableCopy:
    """One table's outcome, for the report and the row-count check ADR-044 wants."""

    name: str
    source_rows: int = 0
    sent_rows: int = 0
    # What the **destination** holds afterwards. Counted there rather than
    # inferred from what was sent: the first version compared `sent_rows` to
    # `source_rows`, which are both client-side counters, so it agreed with
    # itself no matter what the destination did with the rows. Measured during
    # the slice 6c review: a `BEFORE INSERT ... RETURN NULL` trigger dropped two
    # of three rows and the check reported a clean migration.
    landed_rows: int | None = None
    batches: int = 0
    seconds: float = 0.0
    skipped: str | None = None
    # Two sizes, because they answer different questions and the wrong one
    # published as "throughput" makes every freeze estimate wrong.
    #
    # `sent_bytes` is the SQL text this actually pushed through the route --
    # what the network and the destination did work on. `source_bytes` is
    # `pg_total_relation_size`, which is what the *scanner* measures and
    # therefore the only number a customer has before their cutover. A rate
    # published in sent-bytes and then divided into a scanner-measured size is
    # off by whatever the ratio between them happens to be, and that ratio is
    # not a constant -- a narrow row inflates in SQL text, a TOASTed one
    # shrinks. ADR-044's published rate is the source-bytes one.
    sent_bytes: int = 0
    source_bytes: int = 0


@dataclass
class CopyReport:
    tables: list[TableCopy] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def rows(self) -> int:
        return sum(table.sent_rows for table in self.tables)

    @property
    def sent_bytes(self) -> int:
        return sum(table.sent_bytes for table in self.tables)

    @property
    def source_bytes(self) -> int:
        return sum(table.source_bytes for table in self.tables)

    @property
    def bytes_per_second(self) -> float:
        """The rate ADR-044 publishes, in the units the scanner reports.

        Measured against `pg_total_relation_size`, not against the SQL text
        sent, because the number a customer divides by this is the size the
        scanner showed them before they scheduled anything.

        Zero while nothing has been copied or no time has passed, so a caller
        cannot turn an unmeasured run into a plausible-looking figure --
        `report.freeze_estimate` prints "not measured yet" for a falsy rate,
        which is the behaviour that module exists to protect.
        """
        if self.seconds <= 0 or not self.source_bytes:
            return 0.0
        return self.source_bytes / self.seconds

    def mismatches(self) -> list[TableCopy]:
        """Tables the destination does not hold as many rows of as the source.

        ADR-044: the platform cannot enforce a write freeze on somebody else's
        platform, so the check for "writes continued" is arithmetic after the
        fact. This is the arithmetic -- and it asks the *destination*, because
        a client-side sent-count agrees with itself whatever the destination did.
        """
        return [
            t for t in self.tables
            if t.skipped is None
            and (t.sent_rows != t.source_rows or (t.landed_rows is not None
                                                  and t.landed_rows != t.source_rows))
        ]


# Columns that cannot be written to, and the one that needs permission to be.
# `attgenerated = 's'` is a stored generated column: PostgreSQL computes it and
# refuses an explicit value. `attidentity = 'a'` is GENERATED ALWAYS AS
# IDENTITY, which accepts one only with OVERRIDING SYSTEM VALUE -- and a
# migration must keep the source's ids, or every foreign key pointing at them
# breaks.
_COLUMNS = """
SELECT a.attname AS name,
       a.attgenerated <> '' AS generated,
       a.attidentity = 'a'  AS identity_always
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relname = %s
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""

# Foreign keys between tables, for the ordering. Self-references are excluded:
# a table that points at itself cannot be ordered around, and its rows go in one
# batch that PostgreSQL resolves within the statement.
_FOREIGN_KEYS = """
SELECT child_ns.nspname  || '.' || child.relname  AS child,
       parent_ns.nspname || '.' || parent.relname AS parent
  FROM pg_constraint con
  JOIN pg_class child ON child.oid = con.conrelid
  JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
  JOIN pg_class parent ON parent.oid = con.confrelid
  JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
 WHERE con.contype = 'f' AND con.conrelid <> con.confrelid
"""

# Sequences a copied column owns. After rows arrive with their original ids the
# sequence still sits at its start value, so the customer's next insert collides
# with a migrated row -- the classic migration bug, and one that only shows up
# in production traffic.
# `c.oid::regclass::text`, not `nspname || '.' || relname`.
# `pg_get_serial_sequence` parses its first argument as SQL text, so an
# unquoted concatenation downcases the name and splits on dots -- and a table
# called `"Users"`, which is what every Prisma, TypeORM and Drizzle project
# produces, raises `42P01`. Measured during the slice 6c security review, along
# with a worse variant: with no `relkind` filter the function was evaluated for
# every relation in the database, so a mixed-case *index* in an unrelated schema
# killed the query too. `regclass` renders a name already correctly quoted.
_SEQUENCES = """
SELECT n.nspname AS schema, c.relname AS table_name, a.attname AS column_name,
       pg_get_serial_sequence(c.oid::regclass::text, a.attname) AS sequence_name
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE a.attnum > 0 AND NOT a.attisdropped
   AND c.relkind IN ('r', 'p')
   AND n.nspname = ANY(%s)
   AND pg_get_serial_sequence(c.oid::regclass::text, a.attname) IS NOT NULL
"""


def trigger_statements(tables: list[str], *, enable: bool) -> list[str]:
    """Turn the customer's own triggers off for the copy, and on again after.

    **Migrated rows are not application writes.** Slice 6b applies the whole
    schema first -- triggers included -- and then this inserts as
    `mldb_<ref>_admin`, so every `BEFORE INSERT` trigger fires on rows that
    already happened. Measured during the slice 6c review, on two entirely
    ordinary Supabase patterns: a filter trigger returning NULL dropped two of
    three rows, and a `handle_updated_at` trigger rewrote every migrated
    timestamp to the migration's own clock. `pg_restore` has `--disable-triggers`
    for exactly this.

    `DISABLE TRIGGER USER` rather than `ALL`: it leaves foreign-key triggers
    running, so referential integrity is still checked as rows arrive, and it is
    available to the table's owner -- which the tenant admin is -- so it needs
    no privilege a customer does not already have.
    """
    verb = "ENABLE" if enable else "DISABLE"
    return [
        sql.SQL("ALTER TABLE {table} " + verb + " TRIGGER USER")
        .format(table=_quote(table))
        .as_string()
        + ";"
        for table in tables
    ]


def order_tables(tables: list[str], foreign_keys: list[tuple[str, str]]) -> list[str]:
    """Parents before children, so a foreign key is satisfied when it is checked.

    Kahn's algorithm, with one deliberate concession: a cycle -- two tables
    referencing each other, which PostgreSQL permits when at least one side is
    deferrable or nullable -- is not an error here. The remaining tables are
    appended in their original order and the copy will fail loudly on the
    constraint if it genuinely cannot be satisfied. Refusing to migrate a schema
    this tool merely cannot *order* would be the wrong trade.
    """
    known = set(tables)
    parents: dict[str, set[str]] = {table: set() for table in tables}
    for child, parent in foreign_keys:
        if child in known and parent in known and child != parent:
            parents[child].add(parent)

    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(tables)
    while remaining:
        ready = [t for t in remaining if parents[t] <= placed]
        if not ready:
            # A cycle. Everything left goes in source order.
            ordered.extend(remaining)
            break
        for table in ready:
            ordered.append(table)
            placed.add(table)
        remaining = [t for t in remaining if t not in placed]
    return ordered


def _unquote(name: str) -> str:
    """`app."Users"` -> `app.Users`, for comparing a regclass against a plain name."""
    return ".".join(part.strip('"').replace('""', '"') for part in _split_qualified(name))


def _split_qualified(name: str) -> list[str]:
    """Split `schema.table` without cutting a dot that is inside quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in name:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "." and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _quote(name: str) -> sql.Identifier:
    schema, _, table = name.partition(".")
    return sql.Identifier(schema, table)


def _columns(conn: psycopg.Connection, table: str) -> tuple[list[str], bool]:
    schema, _, name = table.partition(".")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_COLUMNS, (schema, name))
        rows = cur.fetchall()
    writable = [row["name"] for row in rows if not row["generated"]]
    overriding = any(row["identity_always"] for row in rows if not row["generated"])
    return writable, overriding


def read_foreign_keys(conn: psycopg.Connection) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(_FOREIGN_KEYS)
        return [(row[0], row[1]) for row in cur.fetchall()]


def sequence_statements(
    conn: psycopg.Connection, tables: list[str], schemas: list[str] | None = None
) -> list[str]:
    """`setval` for every sequence a copied column owns.

    Composed against the *source*'s knowledge of which columns own sequences,
    but computed on the destination from the rows that actually arrived --
    `max()` there rather than a number carried across, so a partial copy leaves
    the sequence consistent with what is really in the table.
    """
    wanted = set(tables)
    if schemas is None:
        schemas = sorted({table.partition(".")[0] for table in tables})
    statements: list[str] = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SEQUENCES, (schemas,))
        for row in cur.fetchall():
            # Matched on the catalogue's own `nspname`/`relname` rather than on
            # the `regclass` rendering, which omits the schema whenever it is on
            # the session's `search_path` -- so `public.seq` came back as `seq`
            # and matched nothing. `regclass` is still what the function
            # argument uses, because that is the part that needs quoting.
            qualified = f"{row['schema']}.{row['table_name']}"
            if qualified not in wanted:
                continue
            statements.append(
                sql.SQL(  # noqa: S608 - composed by psycopg.sql, never by string formatting
                    "SELECT setval({sequence}, coalesce((SELECT max({column}) FROM {table}), 1), "
                    "(SELECT count(*) FROM {table}) > 0)"
                )
                .format(
                    sequence=sql.Literal(row["sequence_name"]),
                    column=sql.Identifier(row["column_name"]),
                    table=sql.Identifier(row["schema"], row["table_name"]),
                )
                .as_string(conn)
                + ";"
            )
    return statements


def insert_batches(
    conn: psycopg.Connection, table: str, *, max_bytes: int = MAX_BATCH_BYTES
):
    """Yield `(statement, row_count)` for one table, reading it as text.

    `col::text` on the source and a bare quoted literal on the destination: the
    value goes out through the type's output function and back in through its
    input function, which is what `COPY` does and the only way measured to
    preserve `jsonb 'null'`, `bytea`, arrays and the float specials. `None` is
    SQL `NULL` and is emitted as the keyword, so it stays distinct from the
    *string* "null" that a `jsonb` column would otherwise swallow it into.
    """
    columns, overriding = _columns(conn, table)
    if not columns:
        return

    selected = sql.SQL(", ").join(
        sql.SQL("{}::text").format(sql.Identifier(column)) for column in columns
    )
    # `ONLY`, which is the whole of the partitioning fix. `SELECT ... FROM
    # parent` returns every partition's rows, and `INSERT INTO parent` routes
    # them straight back into the leaf that is *also* copied on its own -- so
    # every row in a partitioned or inherited table arrived twice, and the
    # per-table counts agreed because each table individually was consistent.
    # Measured: three rows in, six rows out, `Row counts match on every table.`
    query = sql.SQL("SELECT {columns} FROM ONLY {table}").format(
        columns=selected, table=_quote(table)
    )

    prefix = (
        sql.SQL("INSERT INTO {table} ({columns}) {overriding}VALUES ")
        .format(
            table=_quote(table),
            columns=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            overriding=sql.SQL("OVERRIDING SYSTEM VALUE " if overriding else ""),
        )
        .as_string(conn)
    )

    # A server-side cursor: a table larger than memory must not become a
    # migration that dies on the customer's laptop.
    with conn.cursor(name=f"maludb_copy_{abs(hash(table))}") as cur:
        cur.itersize = FETCH_SIZE
        cur.execute(query)
        tuples: list[str] = []
        size = len(prefix)
        for row in cur:
            rendered = "(" + ", ".join(_literal(value) for value in row) + ")"
            if tuples and size + len(rendered) + 2 > max_bytes:
                yield prefix + ", ".join(tuples) + ";", len(tuples)
                tuples, size = [], len(prefix)
            tuples.append(rendered)
            size += len(rendered) + 2
        if tuples:
            yield prefix + ", ".join(tuples) + ";", len(tuples)


def _literal(value: str | None) -> str:
    """A value as SQL text, or the NULL keyword.

    `psycopg.sql.Literal` does the quoting rather than this module: doubling
    quotes by hand is how a migration tool ends up with an injection through the
    customer's own data.
    """
    if value is None:
        return "NULL"
    return sql.Literal(value).as_string()


def count_rows(conn: psycopg.Connection, table: str) -> int:
    """`ONLY`, to match what `insert_batches` reads. A count that included
    children would disagree with a copy that did not."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM ONLY {}").format(_quote(table)))
        return int(cur.fetchone()[0])


def relation_bytes(conn: psycopg.Connection, table: str) -> int:
    """`pg_total_relation_size`, the unit the scanner reports and ADR-044 uses.

    Indexes and TOAST included, because that is what the scanner sums into the
    size the customer was shown -- a rate measured against the heap alone would
    be divided into a bigger number and would under-estimate every window.

    Zero rather than an exception if the relation cannot be sized: a size is for
    reporting a rate, and failing a migration over one would be the tail wagging
    the dog.

    **Inside its own savepoint**, which is not decoration. Swallowing the
    exception does not un-abort the transaction, so one relation this cannot
    size -- dropped between the scan and the copy, or in a schema this role
    cannot reach -- would leave every subsequent read failing `25P02` and the
    whole copy reporting tables it never tried. The scanner learned this in
    slice 5, where one missing Supabase schema aborted the transaction and the
    report came back finding nothing at all.
    """
    try:
        with conn.transaction(force_rollback=False):
            with conn.cursor() as cur:
                cur.execute("SELECT pg_total_relation_size(%s::regclass)", (table,))
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
    except psycopg.Error:
        return 0


def copyable_tables(relations: list[dict]) -> list[str]:
    """The relations that actually hold rows.

    A partitioned parent (`relkind = 'p'`) stores nothing itself -- its rows
    live in the leaves, which are ordinary tables and are copied on their own
    account. Including the parent copied every partitioned row twice.
    """
    return [
        f"{row['schema']}.{row['name']}"
        for row in relations
        if row.get("kind") == "r"
    ]


def copy(
    source_conn: psycopg.Connection,
    tables: list[str],
    apply_batch,
    *,
    max_bytes: int = MAX_BATCH_BYTES,
    progress=None,
    count_destination=None,
) -> CopyReport:
    """Copy every table, in dependency order, reporting per table.

    `apply_batch` is a callable taking one SQL string -- the destination's
    `execute` -- so this module never learns how to reach the destination and
    the tests can drive a real project through the real route.
    """
    report = CopyReport()
    started = time.monotonic()

    ordered = order_tables(tables, read_foreign_keys(source_conn))
    for table in ordered:
        entry = TableCopy(name=table)
        table_started = time.monotonic()
        try:
            entry.source_rows = count_rows(source_conn, table)
            entry.source_bytes = relation_bytes(source_conn, table)
            for statement, rows in insert_batches(source_conn, table, max_bytes=max_bytes):
                apply_batch(DESTINATION_SESSION + '\n' + statement)
                entry.batches += 1
                entry.sent_rows += rows
                entry.sent_bytes += len(statement)
                if progress:
                    progress(table, entry.sent_rows, entry.source_rows)
            if count_destination is not None:
                entry.landed_rows = count_destination(table)
        except psycopg.Error as exc:
            entry.skipped = f"read failed ({exc.sqlstate})"
        entry.seconds = time.monotonic() - table_started
        report.tables.append(entry)

    report.seconds = time.monotonic() - started
    return report
