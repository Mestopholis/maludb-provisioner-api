"""PostgreSQL access via psycopg3.

No ORM (ADR-024). Thin helpers over psycopg3 with dict rows, matching the
house style in maludb-python-api-server.

Every helper takes SQL and parameters separately. Never interpolate values
into SQL -- AGENTS.md calls out SQL injection through generated identifiers as
a primary review concern. Where an *identifier* must be generated (tenant
databases and roles in Phase 02), use psycopg.sql.Identifier rather than
string formatting.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init_pool(database_url: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(database_url, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row})
        _pool.wait(timeout=10)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    if _pool is None:
        raise RuntimeError("connection pool not initialised; call init_pool() first")
    with _pool.connection() as conn:
        yield conn


def query(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def one(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
