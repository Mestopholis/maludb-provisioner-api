"""After the copy: did the data arrive, and did the freeze hold?

Slice 8. `apply --with-data` already compares what it read against what the
destination holds (`data.CopyReport.mismatches`), and that check is real -- it
asks the destination rather than trusting a client-side counter. But **both of
its numbers are taken while the copy is running**, so there is one thing it
structurally cannot see: a write that lands on the source *after* that table was
copied. The customer freezes writes on somebody else's platform, and ADR-044 is
explicit that MaluDB cannot enforce that freeze. So the check for "the freeze
held" is arithmetic after the fact, and this is the arithmetic.

**Counts are not enough, and that is measured rather than assumed.** Two
databases of five thousand rows, one of which had a single row's `ts` rewritten,
compare equal on `count(*)` and differ on every digest tried. That is not a
hypothetical corruption: slice 6c found a `handle_updated_at` trigger rewriting
every migrated timestamp to the migration's own clock, and a `BEFORE INSERT`
trigger silently dropping rows. The first of those changes no count at all.

**The digest is opt-in, because it costs freeze.** Verify is only meaningful
while the source is still frozen -- once writes resume the source legitimately
diverges and every mismatch is noise -- so its running time is inside the window
the customer scheduled. Measured on 1M rows / 161 MB: counting runs at ~735
MB/s, the digest at ~121 MB/s. Six times the window for a stronger answer is a
trade the customer makes, not one this tool makes for them.

**Why this digest.** It is `md5` of each row's text rendering, folded to 32 bits
and summed:

- *Order-independent and streaming.* `md5(string_agg(row::text ORDER BY ...))`
  was the obvious first try and it materialises and sorts the entire table --
  fine at five thousand rows, not at a hundred million.
- *`md5`, not `hashtext`.* `hashtext` is twice as fast and PostgreSQL does not
  promise its result is stable across major versions. The source here is
  somebody else's Supabase project, on a major version this tool does not
  choose, so a hash that may change with the version is a false mismatch
  waiting to happen. `md5` of a text rendering is defined.
- *One 32-bit fold, not two.* A corrupted row changes the sum with probability
  1 - 2^-32. The second fold doubles the cost of the slowest thing here to
  improve that in a digit nobody will reach.

**A digest is only faithful if both sides render values the same way**, and that
is not the default: the same table digests to three different values under three
`DateStyle` settings, and to different values under `extra_float_digits` 0 and 3.
`data.SOURCE_SESSION` already pins the six that matter, for the copy's sake, and
this pins the identical set on *both* sides. `data.DESTINATION_SESSION` pins two
of them, which is enough for the values the copier sends as literals and is not
enough to digest what arrived -- so this does not reuse it.

Nothing here carries a DSN or a token, on the same terms as the rest of the
package.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from services.migrate import data

# The session both sides are read under. `data.SOURCE_SESSION` is the same list;
# it is re-stated as a rendered string here because the destination is reached
# through the SQL route, which takes text rather than a connection.
#
# `row_security = off` earns its place twice over. Measured: a role that is not
# the table's owner reads an RLS-protected table as *zero rows* with no error,
# and so does its owner once `FORCE ROW LEVEL SECURITY` is set -- which a
# careful customer does set. With it off PostgreSQL raises `42501` instead. A
# verification tool that can be handed a silent zero is worse than no tool,
# because zero equals zero and the report comes back clean.
VERIFY_SESSION = "; ".join(data.SOURCE_SESSION) + ";"

# What the digest is. Kept as one string so the source and the destination
# cannot drift apart in a later edit -- if these two expressions ever differ,
# every table on every migration reports corrupt.
_DIGEST = (
    "sum(('x' || substr(md5(v.*::text), 1, 8))::bit(32)::bigint)::text"
)

OK = "ok"
GREW = "source_grew"
SHORT = "short"
CONTENT = "content_differs"
COLUMNS = "columns_differ"
UNREADABLE = "unreadable"

# The statuses that mean "do not cut over". `GREW` is here deliberately: it is
# the freeze failing, which is the one thing this pass exists to catch.
FAILURES = (GREW, SHORT, CONTENT, COLUMNS, UNREADABLE)


@dataclass
class TableVerdict:
    """One table, compared across the two databases after the copy."""

    name: str
    source_rows: int | None = None
    destination_rows: int | None = None
    # What the copy recorded at the time it ran, when it is available. The
    # difference between this and `source_rows` is the write that landed after
    # the table was copied -- which is the freeze, measured.
    copied_rows: int | None = None
    source_digest: str | None = None
    destination_digest: str | None = None
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)
    status: str = OK
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in FAILURES


@dataclass
class VerifyReport:
    tables: list[TableVerdict] = field(default_factory=list)
    sequences: list = field(default_factory=list)
    digested: bool = False
    seconds: float = 0.0

    @property
    def failures(self) -> list:
        """Tables and sequences alike.

        Kept as one list because `clean` is what the exit code is built on, and
        a report that returned zero from `failures` while a sequence was behind
        would be the false-clean verdict this whole slice is about.
        """
        return [t for t in self.tables if t.failed] + [
            s for s in self.sequences if s.failed
        ]

    @property
    def clean(self) -> bool:
        return not self.failures

    @property
    def rows(self) -> int:
        return sum(t.source_rows or 0 for t in self.tables)


def _quoted(table: str) -> str:
    """`schema.table` as an identifier, quoted by psycopg rather than by hand.

    Every name here came out of the source database's catalogue, which is the
    customer's to write. The rest of this package composes with `psycopg.sql`
    for that reason and this is no different -- it renders to text only because
    the destination is reached over HTTP.
    """
    schema, _, name = table.partition(".")
    return sql.Identifier(schema, name).as_string()


def _source_facts(
    conn: psycopg.Connection, table: str, *, digest: bool
) -> tuple[int, list[str], str | None]:
    """Count, columns and optional digest, read from the source in one pass."""
    quoted = _quoted(table)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 "
            "AND NOT a.attisdropped ORDER BY a.attnum",
            tuple(table.split(".", 1)),
        )
        columns = [row[0] for row in cur.fetchall()]

        # `ONLY`, matching how the copier reads: a partitioned parent must not
        # be counted once as itself and again through its partitions.
        if digest:
            cur.execute(
                f"SELECT count(*)::text, {_DIGEST} FROM ONLY {quoted} v"  # noqa: S608
            )
            count, digest_value = cur.fetchone()
            return int(count), columns, digest_value
        cur.execute(f"SELECT count(*) FROM ONLY {quoted}")  # noqa: S608
        return int(cur.fetchone()[0]), columns, None


def _destination_facts(
    destination, table: str, *, digest: bool
) -> tuple[int, list[str], str | None]:
    """The same three, through the SQL route.

    Read in one request rather than three: the console's rate limit is per
    project and deliberately tight, and a verification pass that spent a
    statement per column list would be waiting on `Retry-After` for most of a
    freeze window.
    """
    quoted = _quoted(table)
    schema, _, name = table.partition(".")
    digest_select = f", {_DIGEST} AS digest" if digest else ", NULL::text AS digest"
    body = destination.execute(
        VERIFY_SESSION
        + "\nSELECT string_agg(a.attname, ',' ORDER BY a.attnum) AS columns"
        "  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        "  JOIN pg_namespace n ON n.oid = c.relnamespace"
        f" WHERE n.nspname = {sql.Literal(schema).as_string()}"
        f"   AND c.relname = {sql.Literal(name).as_string()}"
        "   AND a.attnum > 0 AND NOT a.attisdropped;"
        f"\nSELECT count(*)::text AS n{digest_select} FROM ONLY {quoted} v;"  # noqa: S608
    )
    columns: list[str] = []
    count: int | None = None
    digest_value: str | None = None
    for result in body.get("results", []):
        for row in result.get("rows", []):
            if "columns" in row and row["columns"]:
                columns = str(row["columns"]).split(",")
            if "n" in row:
                count = int(row["n"])
                digest_value = row.get("digest")
    if count is None:
        raise data.DataError(f"the destination did not answer a row count for {table}")
    return count, columns, digest_value


def verify_table(
    source_conn: psycopg.Connection,
    destination,
    table: str,
    *,
    digest: bool = False,
    copied_rows: int | None = None,
) -> TableVerdict:
    """Compare one table across the two databases.

    The order of the checks is the order a person needs the answers in. Columns
    first: a digest mismatch with no explanation is the kind of red that gets a
    tool switched off, and "the destination is missing `deleted_at`" is a
    sentence somebody can act on. Then the counts, which separate *the freeze
    did not hold* from *the copy did not finish* -- opposite problems with
    opposite remedies, and a single "mismatch" would hide which one it is.
    Then the digest, which only means anything once the shapes agree.
    """
    verdict = TableVerdict(name=table, copied_rows=copied_rows)
    try:
        # **Inside a savepoint.** Catching the exception does not un-abort the
        # transaction, and the read below is the one most likely to raise --
        # `row_security = off` answers an RLS-protected table with `42501` by
        # design. Without this, the first unreadable table left every later one
        # failing `25P02` and the sequence pass crashing: a report that turns
        # one real finding into "none of your data could be read", which is
        # both wrong and the kind of wrong that gets a tool switched off.
        # Caught in test, not in review.
        with source_conn.transaction():
            source_rows, source_columns, source_digest = _source_facts(
                source_conn, table, digest=digest
            )
    except psycopg.Error as exc:
        # `42501` here is the RLS refusal `row_security = off` produces, which
        # is the whole reason that setting is on: the alternative was a silent
        # zero that compares equal to a silent zero.
        verdict.status = UNREADABLE
        verdict.detail = (
            "the source refused to read this table without applying row-level "
            f"security to the read ({exc.sqlstate})"
            if exc.sqlstate == "42501"
            else f"the source could not be read ({exc.sqlstate})"
        )
        return verdict

    verdict.source_rows = source_rows
    verdict.source_digest = source_digest

    try:
        dest_rows, dest_columns, dest_digest = _destination_facts(
            destination, table, digest=digest
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised through
        verdict.status = UNREADABLE
        verdict.detail = f"the destination could not be read: {exc}"
        return verdict

    verdict.destination_rows = dest_rows
    verdict.destination_digest = dest_digest

    if not dest_columns:
        verdict.status = UNREADABLE
        verdict.detail = "the destination has no such table"
        return verdict

    verdict.missing_columns = [c for c in source_columns if c not in dest_columns]
    verdict.extra_columns = [c for c in dest_columns if c not in source_columns]
    if verdict.missing_columns or verdict.extra_columns:
        verdict.status = COLUMNS
        verdict.detail = "the two tables do not have the same columns"
        return verdict

    # The freeze, measured. `copied_rows` is what the copy read at the time it
    # ran; a source that holds more now is a source that was still taking
    # writes. Reported ahead of a short copy because it is the more
    # consequential finding: a short copy is repaired by copying again, and a
    # broken freeze means rows were accepted by a database the customer is
    # about to stop using.
    if copied_rows is not None and source_rows > copied_rows:
        verdict.status = GREW
        verdict.detail = (
            f"the source gained {source_rows - copied_rows} row(s) after it was "
            "copied -- writes were still reaching it"
        )
        return verdict

    if dest_rows != source_rows:
        verdict.status = SHORT
        verdict.detail = (
            f"the destination holds {dest_rows} row(s) and the source holds {source_rows}"
        )
        return verdict

    if digest and source_digest != dest_digest:
        verdict.status = CONTENT
        verdict.detail = (
            "the two tables hold the same number of rows and different content"
        )
        return verdict

    return verdict


# A table whose rows all arrived and whose sequence did not move is a migration
# that verifies clean and fails on the customer's first `INSERT` with a
# duplicate key. `apply --with-data` runs `setval` for exactly this reason
# (`data.sequence_statements`), so this is not a second implementation of that
# -- it is the check that it ran, which is a different thing to assert.
#
# Measured: `pg_get_serial_sequence` finds the sequence behind a
# `GENERATED ... AS IDENTITY` column as well as a `serial` one, so both shapes
# are covered by the one query.
_SEQUENCE_CHECK = """
SELECT n.nspname || '.' || c.relname AS table_name, a.attname AS column_name,
       pg_get_serial_sequence(c.oid::regclass::text, a.attname) AS sequence_name
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p')
   AND n.nspname = ANY(%s)
   AND pg_get_serial_sequence(c.oid::regclass::text, a.attname) IS NOT NULL
"""


# Composed rather than formatted, on the same terms as everything else that
# touches a customer's identifiers. `last_value` and `max` are read in one
# statement because the console's rate limit is per project, and a verification
# pass that spent two requests per sequence would spend a cutover window on
# `Retry-After`.
# `last_value` is **NULL for a sequence that has never been advanced**, not for
# a sequence that does not exist -- measured, and the two need opposite
# answers. A never-advanced sequence behind a table full of rows is precisely
# the failure this looks for (the copy's `setval` did not run), and reporting it
# as "there is no such sequence" was a real finding given the wrong name. So the
# resolved name is selected too: absent means missing, present with a NULL
# `last_value` means never advanced.
_SEQUENCE_QUERY = sql.SQL(  # noqa: S608 - composed by psycopg.sql, never by string formatting
    "SELECT {sequence}::text AS sequence_name, "
    "       (SELECT last_value FROM pg_sequences s "
    "          WHERE s.schemaname || '.' || s.sequencename = {sequence})::text AS last_value, "
    "       (SELECT max({column}) FROM ONLY {table})::text AS max_value"
)


@dataclass
class SequenceVerdict:
    """One sequence, and whether it is ahead of the rows that arrived."""

    table: str
    column: str
    last_value: int | None = None
    max_value: int | None = None
    status: str = OK
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in FAILURES


def sequence_verdicts(
    source_conn: psycopg.Connection, destination, tables: list[str]
) -> list[SequenceVerdict]:
    """Is every copied table's sequence past the rows in it?

    Composed from the *source*'s knowledge of which columns own a sequence --
    the destination's catalogue would do, but this way the check covers the
    sequences the customer actually had rather than the ones that happen to
    exist after the restore, which is the direction a missing one hides in.
    """
    wanted = set(tables)
    schemas = sorted({t.partition(".")[0] for t in tables})
    verdicts: list[SequenceVerdict] = []
    with source_conn.cursor() as cur:
        cur.execute(_SEQUENCE_CHECK, (schemas,))
        owned = [row for row in cur.fetchall() if row[0] in wanted]

    for table, column, _sequence in owned:
        quoted = _quoted(table)
        del _sequence  # the destination's own name for it is what is checked
        # `pg_get_serial_sequence` is evaluated on the destination rather than
        # carried across: the sequence's name there is whatever the restored
        # schema gave it, and assuming it matched the source's would be
        # assuming the thing being checked.
        # Composed by `psycopg.sql`, never by string formatting: the table and
        # column names are the customer's to write.
        statement = VERIFY_SESSION + "\n" + _SEQUENCE_QUERY.format(
            sequence=sql.SQL("pg_get_serial_sequence({}, {})").format(
                sql.Literal(table), sql.Literal(column)
            ),
            column=sql.Identifier(column),
            table=sql.SQL(quoted),
        ).as_string() + ";"
        try:
            body = destination.execute(statement)
        except Exception as exc:  # noqa: BLE001 - reported, never raised through
            verdicts.append(
                SequenceVerdict(table=table, column=column, status=UNREADABLE,
                                detail=f"the destination could not be read: {exc}")
            )
            continue

        last_value = max_value = None
        sequence_name = None
        for result in body.get("results", []):
            for row in result.get("rows", []):
                if "last_value" in row:
                    sequence_name = row.get("sequence_name")
                    last_value = None if row["last_value"] is None else int(row["last_value"])
                    max_value = None if row.get("max_value") is None else int(row["max_value"])

        verdict = SequenceVerdict(
            table=table, column=column, last_value=last_value, max_value=max_value
        )
        if max_value is None:
            # No rows arrived: nothing for a sequence to be behind.
            verdicts.append(verdict)
            continue
        if sequence_name is None:
            verdict.status = UNREADABLE
            verdict.detail = "the destination has no sequence for this column"
        elif last_value is None:
            # Never advanced. The rows are there and the counter is not, which
            # is what a migration that skipped `setval` leaves behind.
            verdict.status = SHORT
            verdict.detail = (
                "the sequence has never been advanced and the table already holds "
                f"{max_value} row(s) -- the next insert will collide"
            )
        elif last_value < max_value:
            verdict.status = SHORT
            verdict.detail = (
                f"the sequence is at {last_value} and the table already holds "
                f"{max_value} -- the next insert will collide"
            )
        verdicts.append(verdict)
    return verdicts


def verify(
    source_conn: psycopg.Connection,
    destination,
    tables: list[str],
    *,
    digest: bool = False,
    copied: dict[str, int] | None = None,
    progress=None,
) -> VerifyReport:
    """Compare every table, then every sequence behind them.

    `copied` is the per-table count `apply --with-data` recorded, when the
    customer still has that report. Without it the freeze check degrades to
    "does the destination match the source now", which catches a copy that fell
    short but not a source that grew -- so the CLI says which of the two answers
    it is giving rather than letting a weaker check read as the stronger one.
    """
    import time

    report = VerifyReport(digested=digest)
    started = time.monotonic()
    data.prepare_source(source_conn)

    for table in tables:
        if progress:
            progress(table)
        report.tables.append(
            verify_table(
                source_conn, destination, table,
                digest=digest,
                copied_rows=(copied or {}).get(table),
            )
        )

    report.sequences = sequence_verdicts(source_conn, destination, tables)
    report.seconds = time.monotonic() - started
    return report
