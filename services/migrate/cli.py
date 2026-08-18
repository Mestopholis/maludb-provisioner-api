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
from services.migrate import verify as verify_tools

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
    apply_cmd.add_argument(
        "--receipt", default=None, metavar="PATH",
        help=(
            "write what was copied to a JSON file, for `verify --receipt` to compare "
            "against afterwards. Without one, verify can still see a copy that fell "
            "short but not a source that kept taking writes."
        ),
    )
    apply_cmd.set_defaults(func=_cmd_apply)

    verify_cmd = sub.add_parser(
        "verify",
        help="after a migration: did the data arrive, and did the write freeze hold?",
        description=(
            "Compares the source and the migrated project table by table. Run it "
            "while the source is STILL FROZEN -- once writes resume there, the two "
            "databases diverge legitimately and every difference this reports is "
            "noise."
        ),
    )
    verify_cmd.add_argument(
        "--source-dsn", default=None,
        help=f"the Supabase project to compare against. Prefer {SOURCE_DSN_ENV}.",
    )
    verify_cmd.add_argument(
        "--project-ref", required=True, help="the migrated MaluDB project"
    )
    verify_cmd.add_argument(
        "--token", default=None,
        help=f"platform token. Prefer the {destination.TOKEN_ENV} environment variable.",
    )
    verify_cmd.add_argument(
        "--api-url", default=destination.DEFAULT_BASE_URL, help="the MaluDB API base URL"
    )
    verify_cmd.add_argument(
        "--receipt", default=None, metavar="PATH",
        help=(
            "the JSON `apply --receipt` wrote. With it, a source that gained rows "
            "after being copied is named -- which is the only way to see a write "
            "freeze that did not hold."
        ),
    )
    verify_cmd.add_argument(
        "--digest", action="store_true",
        help=(
            "compare content, not just row counts. Catches a table whose rows all "
            "arrived and were changed on the way -- a `handle_updated_at` trigger "
            "does exactly that. Measured at roughly a sixth of the speed of "
            "counting, and it runs inside your freeze window."
        ),
    )
    verify_cmd.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="text for a person watching a cutover, json for a runbook",
    )
    verify_cmd.set_defaults(func=_cmd_verify)
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

    return _copy_data(dsn, facts, target, receipt=args.receipt)


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


def _copy_data(dsn: str, facts, target, *, receipt: str | None = None) -> int:
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
        print(f"  {report.sanitise(table)}: {sent}/{total} rows", end="\r", flush=True)

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
    if copied.bytes_per_second:
        # The rate ADR-044 publishes, measured against the size the scanner
        # reports rather than against the SQL text sent -- the customer divides
        # this into the former, so measuring it against the latter would make
        # every estimate wrong by an inconstant ratio.
        # MiB, because `report.freeze_estimate` divides by 1024*1024 -- this
        # number exists to be pasted into `--throughput-mb-per-s`, so the two
        # must agree on what "MB" means or every estimate is 5% out.
        mib = 1024 * 1024
        print(
            f"  {copied.source_bytes / mib:.0f} MB of source data at "
            f"{copied.bytes_per_second / mib:.1f} MB/s"
        )
        print(
            "  That rate is this migration's, on this network and this source. "
            "Pass it to `scan --throughput-mb-per-s` to size a window for a "
            "database of another size on the same setup."
        )

    if receipt:
        # Written before the summary is judged, and before any non-zero exit:
        # a copy that ended badly is exactly the one whose numbers the
        # verification pass needs.
        try:
            _write_receipt(receipt, copied, target.project_ref)
            print(f"  receipt written to {receipt}")
        except OSError as exc:
            print(f"  could not write the receipt: {exc}", file=sys.stderr)

    unreadable = [t for t in copied.tables if t.skipped]
    for table in unreadable:
        print(
            f"  could not read {report.sanitise(table.name)}: {report.sanitise(table.skipped)}",
            file=sys.stderr,
        )

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


def _write_receipt(path: str, copied, project_ref: str) -> None:
    """What was copied, for the verification pass that comes after.

    Deliberately small and deliberately not a log: per-table counts, the sizes
    the rate was measured against, and nothing that identifies a connection.
    **No DSN and no token** -- this file is written to a path the customer
    chose, gets attached to change tickets, and is the sort of artefact that
    ends up in a repository.
    """
    import json

    payload = {
        "project_ref": project_ref,
        "seconds": round(copied.seconds, 3),
        "rows": copied.rows,
        "source_bytes": copied.source_bytes,
        "sent_bytes": copied.sent_bytes,
        "bytes_per_second": round(copied.bytes_per_second, 1),
        "tables": {
            table.name: {
                "source_rows": table.source_rows,
                "landed_rows": table.landed_rows,
                "source_bytes": table.source_bytes,
                "seconds": round(table.seconds, 3),
                "skipped": table.skipped,
            }
            for table in copied.tables
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_receipt(path: str) -> dict[str, int]:
    """The per-table counts `apply --receipt` recorded, or an error a person can act on."""
    import json

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return {
        name: entry["source_rows"]
        for name, entry in (payload.get("tables") or {}).items()
        if entry.get("source_rows") is not None
    }


def _cmd_verify(args: argparse.Namespace) -> int:
    """The post-migration comparison. Exit 1 means do not cut over."""
    import psycopg

    dsn = args.source_dsn or os.environ.get(SOURCE_DSN_ENV)
    if not dsn:
        print(
            f"no source connection string. Set {SOURCE_DSN_ENV} or pass --source-dsn.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    token = args.token or os.environ.get(destination.TOKEN_ENV)
    if not token:
        print(
            f"no platform token. Set {destination.TOKEN_ENV} or pass --token.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    copied: dict[str, int] | None = None
    if args.receipt:
        try:
            copied = _read_receipt(args.receipt)
        except (OSError, ValueError, KeyError) as exc:
            print(f"could not read the receipt at {args.receipt}: {exc}", file=sys.stderr)
            return EXIT_ERROR

    scanned = _scan_first(dsn)
    if scanned is None:
        return EXIT_ERROR
    facts, _scan = scanned
    tables = data.copyable_tables(facts.relations.rows)
    if not tables:
        print("No tables to verify.")
        return EXIT_OK

    target = destination.Destination(args.project_ref, token, base_url=args.api_url)

    def show(table: str) -> None:
        # Sanitised for the same reason the report is: this name came out of
        # the source catalogue, and a progress line is written to the same
        # terminal, on the same row, immediately before the verdict.
        if args.format == "text":
            print(f"  checking {report.sanitise(table)}...", end="\r", flush=True)

    try:
        with psycopg.connect(dsn, connect_timeout=10) as source_conn:
            source_conn.read_only = True
            result = verify_tools.verify(
                source_conn, target, tables,
                digest=args.digest, copied=copied, progress=show,
            )
    except psycopg.Error as exc:
        print(f"\nreading the source failed: {exc.sqlstate}", file=sys.stderr)
        return EXIT_ERROR

    if args.format == "json":
        print(_verify_json(result, copied is not None))
    else:
        print(_verify_text(result, copied is not None))
    return EXIT_OK if result.clean else EXIT_BLOCKED


def _verify_json(result, had_receipt: bool) -> str:
    import json
    from dataclasses import asdict

    return json.dumps(
        {
            "clean": result.clean,
            "digested": result.digested,
            "freeze_checked": had_receipt,
            "seconds": round(result.seconds, 3),
            "tables": [asdict(t) for t in result.tables],
            "sequences": [asdict(s) for s in result.sequences],
        },
        indent=2,
        sort_keys=True,
    )


def _verify_text(result, had_receipt: bool) -> str:
    """The report a person reads while their application is still switched off.

    Every name printed here came out of the source database's catalogue, so it
    goes through `report.sanitise` for the reason slice 5's review gave: this is
    the artefact somebody reads to decide a cutover is safe, which makes
    repainting it the whole attack.
    """
    lines: list[str] = []
    failures = result.failures
    checked = len(result.tables)

    if result.clean:
        lines.append(f"Verified {checked} table(s): the destination matches the source.")
    else:
        lines.append(
            f"NOT VERIFIED. {len(failures)} of {checked} table(s) do not match. "
            "Do not switch your application over."
        )

    lines.append("")
    lines.append(
        f"  content compared: {'yes, by digest' if result.digested else 'no -- row counts only'}"
    )
    # Said explicitly, because the weaker check reads exactly like the stronger
    # one in a clean report. Without the receipt this cannot see a source that
    # kept taking writes -- it can only see a copy that fell short.
    lines.append(
        "  write freeze:     "
        + (
            "checked against what was copied"
            if had_receipt
            else "NOT CHECKED -- no --receipt, so writes that continued on the "
                 "source after a table was copied are invisible here"
        )
    )
    lines.append(f"  took:             {result.seconds:.1f}s")

    for verdict in failures:
        name = report.sanitise(getattr(verdict, "name", None) or verdict.table)
        lines.append("")
        lines.append(f"  {name}  [{verdict.status}]")
        if verdict.detail:
            lines.append(f"    {report.sanitise(verdict.detail)}")
        for label, columns in (
            ("missing from the destination", getattr(verdict, "missing_columns", [])),
            ("only on the destination", getattr(verdict, "extra_columns", [])),
        ):
            if columns:
                shown = ", ".join(report.sanitise(c) for c in columns[:report.MAX_ITEMS_SHOWN])
                more = "" if len(columns) <= report.MAX_ITEMS_SHOWN else \
                    f" (+{len(columns) - report.MAX_ITEMS_SHOWN} more)"
                lines.append(f"    columns {label}: {shown}{more}")

    return "\n".join(lines)
