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

Slice 6b adds `apply`, which migrates the **schema**: extensions from the
allowlist, then the customer's own schemas as `pg_dump` writes them. Data is not
carried yet and `apply` says so rather than implying a finished migration -- a
tool that silently moved a schema and left the rows behind would be the worst
kind of success.

`apply` refuses to run while the scan reports a blocker. That is the point of
having a scanner: a blocker means the migration will not complete correctly, and
the moment to find out is before the write freeze, not during it.
"""

from __future__ import annotations

import argparse
import os
import sys

from services.migrate import auth as auth_export
from services.migrate import data, destination, report, rules, source
from services.migrate import schema as schema_tools

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

    apply_cmd = sub.add_parser(
        "apply",
        help="migrate a scanned project's schema into a MaluDB project",
        description=(
            "Applies the source project's extensions and schema to a MaluDB project, "
            "through the same public API any client uses. Scans first and refuses if "
            "anything would block the migration. Does not carry data yet."
        ),
    )
    apply_cmd.add_argument(
        "--source-dsn", default=None,
        help=f"the Supabase project to read. Prefer the {SOURCE_DSN_ENV} environment variable.",
    )
    apply_cmd.add_argument(
        "--project-ref", required=True, help="the destination MaluDB project"
    )
    apply_cmd.add_argument(
        "--token", default=None,
        help=(
            f"platform token for the destination. Prefer the {destination.TOKEN_ENV} "
            "environment variable: an argument is visible in `ps` and in shell history."
        ),
    )
    apply_cmd.add_argument(
        "--api-url", default=destination.DEFAULT_BASE_URL, help="the MaluDB API base URL"
    )
    apply_cmd.add_argument(
        "--with-data", action="store_true",
        help=(
            "copy the rows as well as the schema. Freeze writes on the source first: "
            "MaluDB cannot stop them for you, and rows written during the copy are the "
            "ones that quietly go missing (ADR-044)."
        ),
    )
    apply_cmd.add_argument(
        "--with-auth", action="store_true",
        help=(
            "import the source's email/password Auth users, password hashes included, "
            "so they sign in with the password they already had (ADR-043)."
        ),
    )
    apply_cmd.add_argument(
        "--dry-run", action="store_true",
        help="scan, dump and report what would be applied, without sending anything",
    )
    apply_cmd.set_defaults(func=_cmd_apply)
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


def _scan_first(dsn: str) -> tuple[source.SourceFacts, rules.Scan] | None:
    """Read and judge the source, or return None having explained why not."""
    try:
        facts = source.read(dsn)
    except source.SourceError as exc:
        print(str(exc), file=sys.stderr)
        return None
    try:
        matrix, allowlist = rules.load_specs()
    except OSError as exc:
        print(f"could not read the compatibility specs: {exc}", file=sys.stderr)
        return None
    return facts, rules.evaluate(facts, matrix=matrix, allowlist=allowlist)


def _cmd_apply(args: argparse.Namespace) -> int:
    dsn = args.source_dsn or os.environ.get(SOURCE_DSN_ENV)
    if not dsn:
        print(
            f"no source connection string. Set {SOURCE_DSN_ENV} or pass --source-dsn.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    token = args.token or os.environ.get(destination.TOKEN_ENV)
    if not token and not args.dry_run:
        print(
            f"no platform token. Set {destination.TOKEN_ENV} or pass --token. "
            "A personal access token from the dashboard is what this needs.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    scanned = _scan_first(dsn)
    if scanned is None:
        return EXIT_ERROR
    facts, scan = scanned

    if not scan.migratable:
        # The scanner exists so this decision is made before a write freeze.
        print("Not migrating: the scan found blockers.\n", file=sys.stderr)
        print(report.as_text(scan, report.freeze_estimate(facts.database_bytes, None)),
              file=sys.stderr)
        return EXIT_BLOCKED

    # Only the customer's own schemas. Supabase's are the destination's to
    # build, and provisioning already built them.
    customer_schemas = [
        row["name"] for row in facts.schemas.rows
        if row["name"] not in source.SUPABASE_SCHEMAS
    ]
    installed = {row["name"] for row in facts.extensions.rows} - {"plpgsql"}
    _, allowlist = rules.load_specs()
    extensions = sorted(installed & rules.allowed_extensions(allowlist))

    try:
        schema_tools.check_version(facts.server_version)
        dumped = schema_tools.dump(dsn, customer_schemas)
    except schema_tools.SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    # The restrictions `pg_dump --no-privileges` dropped, carried explicitly.
    # Applied after the schema, because they name functions the dump creates.
    restrictions = schema_tools.privilege_statements(facts.functions.rows)
    batches = schema_tools.batches(schema_tools.statements_for(dumped, facts.functions.rows))
    print(
        f"Source: {len(customer_schemas)} schema(s), {len(dumped.statements)} statements, "
        f"{dumped.size / 1024:.0f} KB, {len(extensions)} extension(s).\n"
        f"Applying as {len(batches)} request(s) to {args.project_ref}."
    )
    if restrictions:
        print(
            f"Carrying {len(restrictions)} function permission statement(s): pg_dump drops "
            "REVOKEs along with GRANTs, and a new function is EXECUTE to PUBLIC."
        )

    if args.dry_run:
        print("\nDry run: nothing was sent.")
        return EXIT_OK

    target = destination.Destination(args.project_ref, token, base_url=args.api_url)
    try:
        target.install_extensions(extensions)
        applied = target.apply(batches, statement_count=len(dumped.statements) + len(restrictions))
    except destination.DestinationError as exc:
        print(f"\nThe migration stopped: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(
        f"\nApplied {applied.statements} statements in {applied.batches} request(s), "
        f"{applied.seconds:.1f}s"
        + (f" ({applied.rate_limited_seconds:.0f}s waiting for the rate limit)"
           if applied.rate_limited_seconds else "")
    )

    if args.with_auth:
        code = _import_auth(dsn, target)
        if code != EXIT_OK:
            return code

    if not args.with_data:
        print(
            "\nThe schema is migrated. **The data is not** -- run again with --with-data "
            "once you have frozen writes on the source. Do not switch your application "
            "over yet."
        )
        return EXIT_OK

    return _copy_data(dsn, facts, target)


def _import_auth(dsn: str, target) -> int:
    """Users and their password hashes, through the platform-mediated route."""
    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.read_only = True
            exported = auth_export.read(conn)
    except auth_export.AuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_ERROR
    except psycopg.Error as exc:
        print(f"\nreading the source's auth schema failed: {exc.sqlstate}", file=sys.stderr)
        return EXIT_ERROR

    if not exported.users:
        print("\nNo Auth users to import.")
        return EXIT_OK

    print(f"\nImporting {len(exported.users)} user(s) and "
          f"{len(exported.identities)} identity/identities.")

    inserted_users = inserted_identities = 0
    dropped: set[str] = set()
    try:
        for payload in exported.batches():
            answer = target.import_auth(payload)
            inserted_users += answer.get("users_inserted", 0)
            inserted_identities += answer.get("identities_inserted", 0)
            dropped |= set(answer.get("dropped_columns", []))
    except destination.DestinationError as exc:
        print(f"\nthe Auth import stopped: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"Imported {inserted_users} user(s) and {inserted_identities} identity/identities. "
          "Passwords carried across; your users sign in with what they already had.")
    if dropped:
        # Named rather than dropped in silence: the two sides pin different
        # GoTrue versions, and a customer should hear what did not come across.
        print(f"  not carried (this platform's Auth has no such column): {', '.join(sorted(dropped))}")
    if exported.external_identities:
        print(
            f"  {exported.external_identities} identity/identities use an external provider "
            "and were not imported (ADR-043). Those users need a password set, or to wait "
            "for the provider surface."
        )
    return EXIT_OK


def _copy_data(dsn: str, facts, target) -> int:
    """The rows, after the schema, with the count comparison ADR-044 requires."""
    import psycopg

    # Leaf relations only: a partitioned parent holds no rows of its own, and
    # copying it as well copied every partitioned row twice.
    tables = data.copyable_tables(facts.relations.rows)
    if not tables:
        print("\nNo tables to copy.")
        return EXIT_OK

    print(f"\nCopying {len(tables)} table(s). Do not let anything write to the source now.")

    def show(table: str, sent: int, total: int) -> None:
        print(f"  {table}: {sent}/{total} rows", end="\r", flush=True)

    try:
        with psycopg.connect(dsn, connect_timeout=10) as source_conn:
            source_conn.read_only = True
            # Pins DateStyle and the rest, and turns row_security off so a
            # source role that cannot see every row fails loudly instead of
            # copying a subset whose counts agree with themselves.
            data.prepare_source(source_conn)

            # Computed *before* a single row moves. The first version read them
            # afterwards, so a failure here -- and one relation named `"Users"`
            # anywhere was enough -- left every row written and no sequence
            # advanced, inside the customer's write freeze.
            sequences = data.sequence_statements(source_conn, tables)

            # The customer's own triggers are not for rows that already
            # happened: a filter trigger drops them, an `updated_at` trigger
            # rewrites them to migration time.
            target.execute("\n".join(data.trigger_statements(tables, enable=False)))
            try:
                copied = data.copy(
                    source_conn, tables, target.execute,
                    progress=show, count_destination=target.count_rows,
                )
            finally:
                # Always, or a failed migration leaves the customer's triggers
                # off in a database they are about to use.
                target.execute("\n".join(data.trigger_statements(tables, enable=True)))
    except psycopg.Error as exc:
        print(f"\nreading the source failed: {exc.sqlstate}", file=sys.stderr)
        if exc.sqlstate == "42501":
            print(
                "  row-level security is filtering this connection. Use a role that can "
                "read every row -- on Supabase, the `postgres` role.",
                file=sys.stderr,
            )
        return EXIT_ERROR
    except destination.DestinationError as exc:
        print(f"\nthe copy stopped: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if sequences:
        # Without this the customer's next insert collides with a migrated row.
        try:
            target.execute("\n".join(sequences))
        except destination.DestinationError as exc:
            print(f"\nthe rows arrived but a sequence could not be advanced: {exc}",
                  file=sys.stderr)
            return EXIT_ERROR

    print(f"\nCopied {copied.rows} row(s) across {len(copied.tables)} table(s) in "
          f"{copied.seconds:.1f}s.")

    unreadable = [t for t in copied.tables if t.skipped]
    for table in unreadable:
        print(f"  could not read {table.name}: {table.skipped}", file=sys.stderr)

    mismatches = copied.mismatches()
    if mismatches or unreadable:
        # ADR-044: the platform cannot enforce a freeze on somebody else's
        # platform, so this arithmetic is the check that one happened.
        print("\nROW COUNTS DO NOT MATCH:", file=sys.stderr)
        for table in mismatches:
            landed = table.landed_rows if table.landed_rows is not None else "unknown"
            print(
                f"  {table.name}: source {table.source_rows}, sent {table.sent_rows}, "
                f"in the destination {landed}",
                file=sys.stderr,
            )
        print(
            "\nEither writes continued on the source during the copy, or something was "
            "unreadable. Do not cut over on this migration.",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    print("Row counts match on every table.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - the console script calls main()
    raise SystemExit(main())
