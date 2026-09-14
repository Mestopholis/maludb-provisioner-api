"""Carry `maludb_core`'s vector data across a move or a restore (ADR-077 decision 8).

**`pg_dump` does not carry it.** `maludb_core` registers none of its tables with
`pg_extension_config_dump`, and `pg_dump` leaves out the rows of every table an
extension owns unless it does. A tenant move (ADR-066) and a per-tenant restore
(ADR-059) are both a `pg_dump` and a `pg_restore`, so without this a tenant with
vector compartments arrives with an empty store and no error anywhere
(`specs/vector-compartments-model.md`, finding 7).

So after the load, the rows of the tables below are copied from the source into
the target in one transaction, the sequences behind them are moved past the
highest id carried, and the counts are compared before it commits. Any
disagreement rolls the whole carry back and fails the operation that asked for it,
which for a move is before customer traffic is repointed.

**By column list, never by layout.** A move is between nodes on the same pins
(ADR-075), but a point-in-time restore can read a tenant from before an extension
upgrade into a target where `pg_restore` created the extension at the node's
current version. A column the target has and the source lacks takes its default.
A column the source has and the target lacks is refused: carrying the rest would
drop data silently, which is the failure this module exists to prevent.

**What it refuses before copying anything:**

- a target that already holds rows in any of these tables -- carrying twice would
  duplicate ids, and a fresh target from `pg_restore` never has any;
- a row linked to something outside the carried tables -- a chunk's
  `statement_id`, a subject's or verb's knowledge-graph id -- whose foreign key
  would fail halfway through. Found from the catalogue, not listed by hand, so a
  link an upstream release adds is refused too.

**It steps aside, per table, once the source registers it** (ADR-078). From
`maludb_core` 0.105.0 the extension registers these tables with
`pg_extension_config_dump`, so `pg_dump` carries their rows itself and the target
already holds them when this runs. Carrying them again would be refused as a
second carry -- every move and restore after the pin would fail -- so a table in
the *source's* `extconfig` is skipped and reported as carried by the dump. The
source decides, not the target: whether the rows are in the dump depends only on
what the database that was dumped had registered. A point-in-time restore of a
tenant from before its upgrade still has an unregistered source, and is still
carried.

A table registered **with a filter** is refused rather than skipped: the dump
holds only the rows the filter passes, and nothing here can say which the rest
were. None of the vector tables has one.

Retired once every node is pinned to a registering version and no backup inside
any plan's recovery window predates it.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from psycopg import sql

log = logging.getLogger("maludb.extension_data")

SCHEMA = "maludb_core"
EXTENSION = "maludb_core"

# In foreign-key order: every table here references only tables before it.
CARRIED_TABLES = (
    "malu$vector_subject",
    "malu$vector_verb",
    "malu$vector_compartment",
    "malu$vector_chunk",
    "malu$vector_tombstone",
    "malu$ann_index",
    "malu$ann_delta",
)

# Tables matching the carried prefixes that are deliberately left behind, with the
# reason. A test fails on any other match, so an upstream release adding a vector
# table cannot be silently left out.
NOT_CARRIED = {
    # Upstream's demonstration table; not written by anything the platform exposes.
    "malu$vector_demo": "upstream demo data",
    # Derived bookkeeping, rebuilt by the next index build; row-level security on it
    # would also refuse the carry's own reads.
    "malu$vector_index_status": "derived; rebuilt by the next ann_build",
}
CARRIED_PREFIXES = ("malu$vector_", "malu$ann_")

COPY_TIMEOUT_S = 3600


class CarryError(RuntimeError):
    """The vector data could not be carried exactly, so nothing was."""


@dataclass
class CarryReport:
    rows: dict[str, int] = field(default_factory=dict)
    # Tables the source registered for dumping, so `pg_restore` already loaded them.
    by_dump: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.rows.values())


class Source(Protocol):
    """Where rows are read from: a live connection, or a scratch cluster by subprocess."""

    def columns(self, table: str) -> list[str]: ...
    def count(self, table: str, where: str | None = None) -> int: ...
    def copy_out(self, table: str, columns: list[str]) -> Iterator[bytes]: ...
    def registered(self) -> dict[str, str]: ...


def _qualified(table: str) -> sql.Composed:
    return sql.Identifier(SCHEMA, table)


def _columns_sql() -> str:
    return (
        "SELECT a.attname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum"
    )


def _registered_sql() -> sql.Composed:
    """Each table `maludb_core` registered for dumping, with its filter ('' for none)."""
    return sql.SQL(
        "SELECT c.relname, coalesce(e.extcondition[array_position(e.extconfig, c.oid)], '') "
        "FROM pg_extension e JOIN pg_class c ON c.oid = ANY (e.extconfig) "
        "WHERE e.extname = {} AND c.relkind = 'r'"
    ).format(sql.Literal(EXTENSION))


class ConnectionSource:
    """A source tenant database reached over a psycopg connection -- a move's."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self.conn.execute(_columns_sql(), (SCHEMA, table)).fetchall()]

    def count(self, table: str, where: str | None = None) -> int:
        statement = sql.SQL("SELECT count(*) FROM {}").format(_qualified(table))
        if where:
            statement = statement + sql.SQL(" WHERE ") + sql.SQL(where)
        return int(self.conn.execute(statement).fetchone()[0])

    def copy_out(self, table: str, columns: list[str]) -> Iterator[bytes]:
        statement = sql.SQL("COPY {} ({}) TO STDOUT").format(
            _qualified(table), sql.SQL(", ").join(map(sql.Identifier, columns))
        )
        with self.conn.cursor().copy(statement) as copy:
            yield from copy

    def registered(self) -> dict[str, str]:
        return dict(self.conn.execute(_registered_sql()).fetchall())


class ScratchSource:
    """A source database in a restore's scratch cluster, reached as its owner.

    The scratch cluster is only reachable as the cluster owner over its local
    socket (`restore.py`), so this runs `psql` under `sudo -u`, with an argv and
    never a shell. Statements are composed with psycopg's quoting against the
    target connection and passed as one `-c` argument.
    """

    def __init__(self, *, port: int, database: str, run_as: str, quoting: psycopg.Connection) -> None:
        self.port = int(port)
        self.database = database
        self.run_as = run_as
        self.quoting = quoting

    def _argv(self, statement: str, *, tuples: bool) -> list[str]:
        argv = ["sudo", "-n", "-u", self.run_as, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                "-p", str(self.port), "-d", self.database]
        if tuples:
            argv += ["-t", "-A"]
        return [*argv, "-c", statement]

    def _scalar_rows(self, statement: str) -> list[str]:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            self._argv(statement, tuples=True), capture_output=True, text=True,
            check=False, timeout=300,
        )
        if proc.returncode != 0:
            raise CarryError(f"reading the restored copy failed: {(proc.stderr or '').strip()[-300:]}")
        return [line for line in proc.stdout.splitlines() if line]

    def columns(self, table: str) -> list[str]:
        statement = sql.SQL(
            "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = {} AND c.relname = {} "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
        ).format(sql.Literal(SCHEMA), sql.Literal(table)).as_string(self.quoting)
        return self._scalar_rows(statement)

    def count(self, table: str, where: str | None = None) -> int:
        statement = sql.SQL("SELECT count(*) FROM {}").format(_qualified(table))
        if where:
            statement = statement + sql.SQL(" WHERE ") + sql.SQL(where)
        return int(self._scalar_rows(statement.as_string(self.quoting))[0])

    def registered(self) -> dict[str, str]:
        # One row per table as JSON, because a filter is free text and may hold
        # the separator any plain `-A` output would split on.
        statement = sql.SQL(
            "SELECT json_build_array(r.relname, r.condition) FROM ({}) r(relname, condition)"
        ).format(_registered_sql())
        pairs = [json.loads(line) for line in self._scalar_rows(statement.as_string(self.quoting))]
        return {name: condition for name, condition in pairs}

    def copy_out(self, table: str, columns: list[str]) -> Iterator[bytes]:
        statement = sql.SQL("COPY {} ({}) TO STDOUT").format(
            _qualified(table), sql.SQL(", ").join(map(sql.Identifier, columns))
        ).as_string(self.quoting)
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            self._argv(statement, tuples=False), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None  # noqa: S101 - Popen with PIPE always sets it
        try:
            while block := proc.stdout.read(1 << 20):
                yield block
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            if proc.wait(timeout=COPY_TIMEOUT_S) != 0:
                raise CarryError(f"copying {table} out of the restored copy failed: {stderr.strip()[-300:]}")
        finally:
            if proc.poll() is None:
                proc.kill()


def _links_outside(target: psycopg.Connection, tables: list[str]) -> list[tuple[str, str, str]]:
    """(table, column, referenced table) for each foreign key leaving the carried set."""
    return [
        (row[0], row[1], row[2])
        for row in target.execute(
            "SELECT c.relname, a.attname, r.relname FROM pg_constraint k "
            "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_class r ON r.oid = k.confrelid "
            "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY(k.conkey) "
            "WHERE k.contype = 'f' AND n.nspname = %s AND c.relname = ANY(%s) "
            "AND NOT r.relname = ANY(%s) ORDER BY 1, 2",
            (SCHEMA, tables, tables),
        ).fetchall()
    ]


def _target_columns(target: psycopg.Connection, table: str) -> list[str]:
    return [r[0] for r in target.execute(_columns_sql(), (SCHEMA, table)).fetchall()]


def carry(source: Source, target: psycopg.Connection) -> CarryReport:
    """Copy the vector tables from `source` into `target`, exactly, or not at all.

    `target` is a connection to the target tenant database as a superuser, not in
    autocommit; the carry commits on success and rolls back on any failure.
    """
    import time

    if target.autocommit:
        raise CarryError("the carry needs a transactional target connection")
    started = time.monotonic()
    report = CarryReport()
    plan: list[tuple[str, list[str]]] = []

    try:
        registered = source.registered()
        for table in CARRIED_TABLES:
            if table in registered:
                if registered[table]:
                    raise CarryError(
                        f"{SCHEMA}.{table} is registered for dumping with a filter "
                        f"({registered[table]}), so the dump holds only some of its rows and "
                        "nothing here can tell which the rest were"
                    )
                report.by_dump.append(table)
                continue
            source_columns = source.columns(table)
            target_columns = _target_columns(target, table)
            if not source_columns:
                # The source's extension predates this table: nothing to carry.
                continue
            if not target_columns:
                raise CarryError(
                    f"{SCHEMA}.{table} exists in the source but not in the target; the target's "
                    "maludb_core is older than the source's"
                )
            lost = [c for c in source_columns if c not in target_columns]
            if lost:
                raise CarryError(
                    f"{SCHEMA}.{table} in the source has column(s) the target lacks "
                    f"({', '.join(lost)}); carrying the rest would drop that data"
                )
            existing = target.execute(sql.SQL("SELECT count(*) FROM {}").format(_qualified(table))).fetchone()[0]
            if existing:
                raise CarryError(
                    f"the target already holds {existing} row(s) in {SCHEMA}.{table}; refusing to "
                    "carry into it"
                )
            plan.append((table, source_columns))

        for table, column, referenced in _links_outside(target, [t for t, _ in plan]):
            linked = source.count(table, f"{sql.Identifier(column).as_string(target)} IS NOT NULL")
            if linked:
                raise CarryError(
                    f"{linked} row(s) of {SCHEMA}.{table} are linked through {column} to {referenced}, "
                    "which is not carried; refusing rather than breaking that foreign key"
                )

        for table, columns in plan:
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                _qualified(table), sql.SQL(", ").join(map(sql.Identifier, columns))
            )
            with target.cursor().copy(statement) as into:
                for block in source.copy_out(table, columns):
                    into.write(block)
            carried = target.execute(sql.SQL("SELECT count(*) FROM {}").format(_qualified(table))).fetchone()[0]
            expected = source.count(table)
            if carried != expected:
                raise CarryError(f"{SCHEMA}.{table}: carried {carried} row(s), the source has {expected}")
            report.rows[table] = int(carried)

        # Sequences owned by the carried tables, moved past the highest id so the
        # next insert on the target does not collide with a carried row.
        for seq, table, column in target.execute(
            "SELECT s.oid::regclass::text, t.relname, a.attname FROM pg_class s "
            "JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') "
            "JOIN pg_class t ON t.oid = d.refobjid "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE s.relkind = 'S' AND n.nspname = %s AND t.relname = ANY(%s)",
            (SCHEMA, [t for t, _ in plan]),
        ).fetchall():
            target.execute(
                sql.SQL("SELECT setval({}::regclass, GREATEST((SELECT max({}) FROM {}), 1), "
                        "(SELECT max({}) FROM {}) IS NOT NULL)").format(
                    sql.Literal(seq), sql.Identifier(column), _qualified(table),
                    sql.Identifier(column), _qualified(table),
                )
            )
        target.commit()
    except Exception:
        target.rollback()
        raise

    report.seconds = time.monotonic() - started
    log.info("carried %s vector row(s) in %.1fs; %s table(s) carried by the dump",
             report.total, report.seconds, len(report.by_dump))
    return report


__all__ = [
    "CARRIED_PREFIXES",
    "CARRIED_TABLES",
    "NOT_CARRIED",
    "CarryError",
    "CarryReport",
    "ConnectionSource",
    "ScratchSource",
    "carry",
]
