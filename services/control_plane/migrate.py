"""Minimal SQL migration runner.

ADR-024: migrations are plain versioned .sql files applied by a minimal
runner. No ORM or migration DSL is introduced to restate SQL that already
executes.

Properties that matter:

- ordered by filename, applied exactly once, tracked in schema_migrations;
- each migration runs inside a transaction, so a failure leaves no partial
  version applied;
- re-running is a no-op, which is the "migrations are re-runnable" acceptance
  criterion in tasks/PHASE-01-FOUNDATION.md;
- a file whose checksum changed after being applied is an error, not a silent
  divergence between environments.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def discover() -> list[tuple[str, Path]]:
    return sorted((p.stem, p) for p in MIGRATIONS_DIR.glob("*.sql"))


def applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(_TRACKING_TABLE)
        conn.commit()
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def run(database_url: str, *, dry_run: bool = False) -> list[str]:
    """Apply pending migrations. Returns the versions applied."""
    newly_applied: list[str] = []
    with psycopg.connect(database_url) as conn:
        seen = applied(conn)
        for version, path in discover():
            body = path.read_text()
            digest = _checksum(body)

            if version in seen:
                if seen[version] != digest:
                    raise RuntimeError(
                        f"{version} was applied with a different checksum. "
                        "Migrations are immutable once applied -- add a new one instead."
                    )
                continue

            if dry_run:
                newly_applied.append(version)
                continue

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(body)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, digest),
                    )
            newly_applied.append(version)
    return newly_applied


def main() -> int:
    import os

    database_url = os.environ.get("MALUDB_CONTROL_PLANE_DATABASE_URL", "").strip()
    if not database_url:
        print("MALUDB_CONTROL_PLANE_DATABASE_URL is required", file=sys.stderr)
        return 2

    dry_run = "--dry-run" in sys.argv
    try:
        versions = run(database_url, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - surface the reason, exit non-zero
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    verb = "would apply" if dry_run else "applied"
    print(f"{verb} {len(versions)} migration(s)" + (": " + ", ".join(versions) if versions else " (up to date)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
