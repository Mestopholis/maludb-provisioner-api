"""`maludb-migrate` -- the tool a customer runs to leave Supabase (ADR-042).

It runs on their machine, not ours. That is the decision, and everything else
here follows from it:

- **The source credential is theirs and stays theirs.** It is read from
  `MALUDB_SOURCE_DSN` by preference, because a connection string passed as an
  argument is visible in `ps` output and lands in shell history. `--source-dsn`
  exists for scripting and says so. Nothing writes it to the report, an error,
  or a log line.
- **No platform credential is involved at all in `scan`.** The scan reads the
  source and consults two files in this repository. It talks to no MaluDB
  project, which is why it can be run before a customer has one.
- **The exit code is the answer**, so this can gate a deployment script: 0 when
  the project is migratable, 1 when a blocker was found, 2 when the tool itself
  could not run.

Slice 5 ships `scan`. `apply` lands with the schema-migration slice and will
take a destination project ref and a platform token; it is deliberately absent
rather than stubbed, so `maludb-migrate --help` does not advertise a subcommand
that would fail.
"""

from __future__ import annotations

import argparse
import os
import sys

from services.migrate import report, rules, source

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2

SOURCE_DSN_ENV = "MALUDB_SOURCE_DSN"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maludb-migrate",
        description="Analyse and migrate a Supabase project to MaluDB.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help="report what would block a migration, without changing anything",
        description=(
            "Reads a Supabase project read-only and reports what MaluDB can and cannot "
            "carry. Nothing is written to the source, and no MaluDB project is needed."
        ),
    )
    scan.add_argument(
        "--source-dsn",
        default=None,
        help=(
            f"PostgreSQL connection string for the Supabase project. Prefer the "
            f"{SOURCE_DSN_ENV} environment variable: an argument is visible in `ps` and "
            "in your shell history."
        ),
    )
    scan.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="text for a person, json for a runbook or a pipeline",
    )
    scan.add_argument(
        "--throughput-mb-per-s", type=float, default=None,
        help=(
            "Measured copy rate, for the write-freeze estimate. Omitted by default "
            "because nothing has measured it yet and a made-up figure is worse than none "
            "(ADR-044)."
        ),
    )
    scan.set_defaults(func=_cmd_scan)
    return parser


def _cmd_scan(args: argparse.Namespace) -> int:
    dsn = args.source_dsn or os.environ.get(SOURCE_DSN_ENV)
    if not dsn:
        print(
            f"no source connection string. Set {SOURCE_DSN_ENV} or pass --source-dsn.\n"
            "On Supabase: Project Settings > Database > Connection string > URI.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        facts = source.read(dsn)
    except source.SourceError as exc:
        # `SourceError` is built never to carry the DSN; printing it is safe and
        # printing the underlying driver error would not be.
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    try:
        matrix, allowlist = rules.load_specs()
    except OSError as exc:
        print(f"could not read the compatibility specs: {exc}", file=sys.stderr)
        return EXIT_ERROR

    scan = rules.evaluate(facts, matrix=matrix, allowlist=allowlist)
    freeze = report.freeze_estimate(facts.database_bytes, args.throughput_mb_per_s)

    if args.format == "json":
        print(report.as_json(scan, freeze))
    else:
        print(report.as_text(scan, freeze))

    return EXIT_OK if scan.migratable else EXIT_BLOCKED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - the console script calls main()
    raise SystemExit(main())
