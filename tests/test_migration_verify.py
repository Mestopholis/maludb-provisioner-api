"""Verifying a migration after the fact (Phase 08 slice 8).

`apply --with-data` already compares what it read against what the destination
holds, and that comparison is honest -- it asks the destination rather than
trusting a client-side counter. What it structurally cannot see is a write that
landed on the source *after* a table was copied, because both of its numbers are
taken while the copy runs. ADR-044 is explicit that MaluDB cannot enforce a
freeze on somebody else's platform, so this is the arithmetic that checks
whether the freeze held.

The tests here are mostly about **false clean verdicts**, which is the only
failure direction that matters for a tool whose output is "you may cut over
now". Three are pinned because they were measured rather than reasoned about:

- two databases whose rows are identical except for one rewritten timestamp
  compare **equal on `count(*)`** and differ on the digest;
- the same table digests to three different values under three `DateStyle`
  settings, so a digest is meaningless unless both sides are pinned;
- a role that cannot see every row of an RLS-protected table reads it as **zero
  rows with no error**, and zero equals zero.
"""

from __future__ import annotations

import pytest

from services.migrate import data, destination, schema, source, verify
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_provisioning import ADMIN_DSN

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")


def _client_transport(client):
    def transport(method, path, payload, headers):
        if method == "GET":
            return client.get(path, headers=headers)
        return client.post(path, json=payload, headers=headers)
    return transport


@pytest.fixture
def source_db(request):
    """A small source database with one table worth comparing."""
    import psycopg

    name = f"mldb_vsrc_{abs(hash(request.node.name)) % 10**6}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""
            CREATE SCHEMA app;
            CREATE TABLE app.notes (
                id serial PRIMARY KEY,
                body text NOT NULL,
                amount numeric(12,2),
                updated_at timestamptz DEFAULT now()
            );
            INSERT INTO app.notes (body, amount, updated_at)
            SELECT 'note ' || g, (g * 1.5)::numeric(12,2),
                   '2026-01-01T00:00:00Z'::timestamptz + (g || ' hours')::interval
              FROM generate_series(1, 40) g;
        """)
    yield dsn
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


def _migrate(client, tenant, ref, source_dsn):
    """Schema then rows, through the real route, exactly as the CLI does it."""
    import psycopg

    from services.control_plane import db

    tenant(ref)
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = "
            "  (SELECT plan_id FROM projects WHERE project_ref = %s)",
            (psycopg.types.json.Jsonb({"limits": {"sql_console_concurrent": 60}}), ref),
        )
        conn.commit()

    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]
    target = destination.Destination(ref, token, transport=_client_transport(client))

    facts = source.read(source_dsn)
    dumped = schema.dump(source_dsn, ["app"])
    target.apply(schema.batches(schema.statements_for(dumped, facts.functions.rows)))

    with psycopg.connect(source_dsn) as conn:
        conn.read_only = True
        data.prepare_source(conn)
        copied = data.copy(conn, ["app.notes"], target.execute,
                           count_destination=target.count_rows)
        sequences = data.sequence_statements(conn, ["app.notes"])
    if sequences:
        target.execute("\n".join(sequences))
    return target, copied


def _verify(source_dsn, target, **kwargs):
    import psycopg

    with psycopg.connect(source_dsn) as conn:
        conn.read_only = True
        return verify.verify(conn, target, ["app.notes"], **kwargs)


# -- the measurements the design rests on ----------------------------------


@requires_node
def test_a_clean_migration_verifies_clean(client, tenant, source_db):
    target, copied = _migrate(client, tenant, "vfy00001", source_db)
    assert copied.rows == 40

    result = _verify(source_db, target, digest=True,
                     copied={t.name: t.source_rows for t in copied.tables})
    assert result.clean, [f"{t.name}: {t.detail}" for t in result.failures]
    table = result.tables[0]
    assert table.source_rows == table.destination_rows == 40
    assert table.source_digest == table.destination_digest
    assert table.source_digest is not None


@requires_node
def test_a_row_changed_on_the_way_is_invisible_to_counts_and_caught_by_the_digest(
    client, tenant, source_db
):
    """The finding this slice is built on.

    Slice 6c found a `handle_updated_at` trigger rewriting every migrated
    timestamp to the migration's own clock. It changes no count at all, so the
    check that shipped with slice 6c reports a clean migration -- which is why
    the digest exists, and why it is worth what it costs.
    """
    import psycopg

    from tests.test_provisioning import _tenant_admin_dsn

    ref = "vfy00002"
    target, copied = _migrate(client, tenant, ref, source_db)
    copied_counts = {t.name: t.source_rows for t in copied.tables}

    # Exactly the corruption a trigger causes: same rows, one value rewritten.
    with psycopg.connect(_tenant_admin_dsn(f"mldb_{ref}"), autocommit=True) as conn:
        conn.execute("UPDATE app.notes SET updated_at = now() WHERE id = 7")

    counted = _verify(source_db, target, digest=False, copied=copied_counts)
    assert counted.clean, "counting alone reported this migration clean"
    assert counted.tables[0].source_rows == counted.tables[0].destination_rows == 40

    digested = _verify(source_db, target, digest=True, copied=copied_counts)
    assert not digested.clean
    assert digested.failures[0].status == verify.CONTENT


@requires_node
def test_a_source_that_kept_taking_writes_is_named(client, tenant, source_db):
    """The freeze, measured after the fact. This is the whole point of ADR-044's
    arithmetic: MaluDB cannot stop writes on somebody else's platform."""
    import psycopg

    target, copied = _migrate(client, tenant, "vfy00003", source_db)
    copied_counts = {t.name: t.source_rows for t in copied.tables}

    with psycopg.connect(source_db, autocommit=True) as conn:
        conn.execute("INSERT INTO app.notes (body) VALUES ('written during the freeze')")

    result = _verify(source_db, target, copied=copied_counts)
    assert not result.clean
    failure = result.failures[0]
    assert failure.status == verify.GREW
    assert "1 row(s) after it was copied" in failure.detail


@requires_node
def test_without_a_receipt_a_broken_freeze_is_invisible_and_the_report_says_so(
    client, tenant, source_db
):
    """The weaker check must not read like the stronger one.

    Without the copy-time counts there is nothing to compare *against*: the
    source has 41 rows and the destination has 40, which is indistinguishable
    from a copy that fell one row short. It is still caught -- but as the wrong
    diagnosis, and the remedies are opposite. So the report states which of the
    two questions it actually answered.
    """
    import psycopg

    from services.migrate import cli

    target, _copied = _migrate(client, tenant, "vfy00004", source_db)
    with psycopg.connect(source_db, autocommit=True) as conn:
        conn.execute("INSERT INTO app.notes (body) VALUES ('written during the freeze')")

    result = _verify(source_db, target, copied=None)
    assert not result.clean
    assert result.failures[0].status == verify.SHORT  # not GREW: it cannot tell

    rendered = cli._verify_text(result, had_receipt=False)
    assert "NOT CHECKED" in rendered
    assert "--receipt" in rendered
    assert "NOT CHECKED" not in cli._verify_text(result, had_receipt=True)


@requires_node
def test_a_sequence_left_behind_is_caught_though_every_row_arrived(
    client, tenant, source_db
):
    """Counts match, digest matches, and the customer's first insert collides.

    `apply --with-data` advances sequences for exactly this reason. That this
    check is separate from that code is the point -- it asserts the advance
    happened rather than re-implementing it.
    """
    import psycopg

    from tests.test_provisioning import _tenant_admin_dsn

    ref = "vfy00005"
    target, copied = _migrate(client, tenant, ref, source_db)
    counts = {t.name: t.source_rows for t in copied.tables}

    assert _verify(source_db, target, digest=True, copied=counts).clean

    # Wind it back to where a copy that never ran setval would have left it.
    with psycopg.connect(_tenant_admin_dsn(f"mldb_{ref}"), autocommit=True) as conn:
        conn.execute("SELECT setval('app.notes_id_seq', 1, false)")

    result = _verify(source_db, target, digest=True, copied=counts)
    assert not result.clean
    behind = result.failures[0]
    assert behind.status == verify.SHORT
    assert behind.table == "app.notes"
    assert "next insert will collide" in behind.detail
    assert behind.last_value is None  # never advanced, which is not "no sequence"
    # Every row still arrived: this is invisible to both other checks.
    assert all(t.status == verify.OK for t in result.tables)


@requires_node
def test_a_source_the_reader_cannot_fully_see_is_unreadable_rather_than_clean(
    client, tenant, source_db
):
    """Zero equals zero, which is how a filtered read reports a clean cutover.

    Measured: a role that is neither the owner nor `BYPASSRLS` reads an
    RLS-protected table as zero rows and no error. `row_security = off` turns
    that into a `42501`, and this asserts the refusal is reported as *unreadable*
    rather than crashing the pass or, worse, counting it.
    """
    import psycopg

    target, copied = _migrate(client, tenant, "vfy00006", source_db)
    counts = {t.name: t.source_rows for t in copied.tables}

    with psycopg.connect(source_db, autocommit=True) as conn:
        conn.execute("DROP ROLE IF EXISTS mldb_vfy_reader")
        conn.execute("CREATE ROLE mldb_vfy_reader LOGIN PASSWORD 'x'")
        conn.execute("GRANT USAGE ON SCHEMA app TO mldb_vfy_reader")
        conn.execute("GRANT SELECT ON app.notes TO mldb_vfy_reader")
        conn.execute("ALTER TABLE app.notes ENABLE ROW LEVEL SECURITY")
        conn.execute("CREATE POLICY nothing ON app.notes FOR SELECT USING (false)")

    reader_dsn = (
        source_db.split("//")[0] + "//mldb_vfy_reader:x@" + source_db.split("@", 1)[1]
    )

    # First: the failure this guards against, with RLS left to filter silently.
    with psycopg.connect(reader_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.notes")
        assert cur.fetchone()[0] == 0, "the premise: a filtered read looks empty"

    result = _verify(reader_dsn, target, copied=counts)
    assert not result.clean
    assert result.tables[0].status == verify.UNREADABLE
    assert "row-level security" in result.tables[0].detail

    with psycopg.connect(source_db, autocommit=True) as conn:
        conn.execute("ALTER TABLE app.notes DISABLE ROW LEVEL SECURITY")
        conn.execute("DROP POLICY IF EXISTS nothing ON app.notes")
        conn.execute("REVOKE ALL ON app.notes FROM mldb_vfy_reader")
        conn.execute("REVOKE ALL ON SCHEMA app FROM mldb_vfy_reader")
        conn.execute("DROP ROLE IF EXISTS mldb_vfy_reader")


# -- the digest is only meaningful if both sides render values the same way --


@requires_node
def test_the_digest_depends_on_session_settings_that_must_be_pinned(source_db):
    """Why `VERIFY_SESSION` exists, rather than being a tidy-looking constant.

    The same table, unchanged, digests to a different value under a different
    `DateStyle` or `extra_float_digits`. Two databases holding identical data
    would report corrupt if the two sides were left on their own defaults --
    and a customer with an `ALTER DATABASE ... SET DateStyle` has exactly that.
    """
    import psycopg

    digests = set()
    with psycopg.connect(source_db) as conn, conn.cursor() as cur:
        for style in ("ISO, MDY", "SQL, DMY", "German, DMY"):
            cur.execute(f"SET DateStyle = '{style}'")
            cur.execute(f"SELECT {verify._DIGEST} FROM ONLY app.notes v")  # noqa: S608
            digests.add(cur.fetchone()[0])
    assert len(digests) == 3, "the digest does not depend on DateStyle after all"

    # And pinned, every session agrees regardless of where it started.
    pinned = set()
    for style in ("SQL, DMY", "German, DMY"):
        with psycopg.connect(source_db) as conn, conn.cursor() as cur:
            cur.execute(f"SET DateStyle = '{style}'")
            for statement in data.SOURCE_SESSION:
                cur.execute(statement)
            cur.execute(f"SELECT {verify._DIGEST} FROM ONLY app.notes v")  # noqa: S608
            pinned.add(cur.fetchone()[0])
    assert len(pinned) == 1


def test_the_source_and_destination_digest_with_one_expression():
    """If these ever drift apart every table on every migration reports corrupt.

    Cheap to assert and easy to break: the two sides are built in different
    functions, one against a connection and one against a string.
    """
    source_sql = verify._DIGEST
    assert "md5" in source_sql and "bit(32)" in source_sql
    # `hashtext` is twice as fast and is not promised stable across major
    # versions. The source is somebody else's Supabase, on a version this tool
    # does not choose.
    assert "hashtext" not in source_sql


def test_the_verify_session_turns_row_security_off():
    """The single setting that separates a refusal from a silent zero."""
    assert "row_security = off" in verify.VERIFY_SESSION
    # And it pins everything the digest renders through, not just the two
    # `DESTINATION_SESSION` carries -- which is why this does not reuse it.
    for setting in ("DateStyle", "extra_float_digits", "bytea_output", "IntervalStyle"):
        assert setting in verify.VERIFY_SESSION


# -- the receipt, and the rate it carries ----------------------------------


def test_a_receipt_carries_no_dsn_and_no_token(tmp_path):
    """It is written to a path the customer chose and ends up in change tickets."""
    from services.migrate import cli

    report_obj = data.CopyReport(
        tables=[data.TableCopy(name="app.notes", source_rows=40, landed_rows=40,
                               source_bytes=8192, sent_bytes=4096, seconds=0.5)],
        seconds=0.5,
    )
    path = tmp_path / "receipt.json"
    cli._write_receipt(str(path), report_obj, "vfy00009")
    body = path.read_text()
    assert "postgres://" not in body and "postgresql://" not in body
    assert "password" not in body.lower() and "token" not in body.lower()

    assert cli._read_receipt(str(path)) == {"app.notes": 40}


def test_the_published_rate_is_measured_against_the_size_the_scanner_reports():
    """ADR-044's arithmetic is `scanner size / rate`, so the rate must be in
    scanner units. Measured in SQL-text bytes it would be wrong by a ratio that
    is not even constant -- a narrow row inflates as SQL, a TOASTed one shrinks.
    """
    report_obj = data.CopyReport(
        tables=[data.TableCopy(name="t", source_bytes=100_000_000, sent_bytes=7)],
        seconds=10.0,
    )
    assert report_obj.bytes_per_second == pytest.approx(10_000_000)


def test_an_unmeasured_copy_reports_no_rate_rather_than_a_plausible_one():
    """`report.freeze_estimate` prints "not measured yet" for a falsy rate, and
    that is the behaviour worth protecting: a number nobody measured is worse
    than an admission that nobody has."""
    assert data.CopyReport(tables=[], seconds=0.0).bytes_per_second == 0.0
    assert data.CopyReport(
        tables=[data.TableCopy(name="t", source_bytes=0)], seconds=5.0
    ).bytes_per_second == 0.0


def test_a_crafted_table_name_cannot_repaint_the_verification_report():
    """Slice 5's finding, on the artefact that decides a cutover.

    Every name here comes from the source database's catalogue, which is the
    customer's to write -- and this report is what somebody reads to decide
    their data is safe.
    """
    from services.migrate import cli

    result = verify.VerifyReport(tables=[
        verify.TableVerdict(
            name="app.\x1b[2J\x1b[Hnotes\r\nVerified 40 table(s): all clean.",
            status=verify.SHORT, detail="the destination holds 0 row(s)",
        )
    ])
    rendered = cli._verify_text(result, had_receipt=True)
    assert "\x1b" not in rendered and "\r" not in rendered
    assert rendered.splitlines()[0].startswith("NOT VERIFIED")


@requires_node
def test_one_unreadable_table_does_not_make_every_later_table_unreadable(
    client, tenant, source_db
):
    """The savepoint, pinned.

    Catching a `psycopg.Error` does not un-abort the transaction. Without a
    savepoint around each source read, the first table `row_security = off`
    refuses leaves every table after it failing `25P02` and the sequence pass
    raising outright -- so one real finding is reported as "none of your data
    could be read". Found by running this, not by reading it.
    """
    import psycopg

    ref = "vfy00007"
    target, copied = _migrate(client, tenant, ref, source_db)

    # A second table, on both sides, that nothing will stop this from reading.
    with psycopg.connect(source_db, autocommit=True) as conn:
        conn.execute("CREATE TABLE app.tags (id serial PRIMARY KEY, label text)")
        conn.execute("INSERT INTO app.tags (label) SELECT 't'||g FROM generate_series(1,5) g")
        conn.execute("DROP ROLE IF EXISTS mldb_vfy_reader2")
        conn.execute("CREATE ROLE mldb_vfy_reader2 LOGIN PASSWORD 'x'")
        conn.execute("GRANT USAGE ON SCHEMA app TO mldb_vfy_reader2")
        conn.execute("GRANT SELECT ON app.notes, app.tags TO mldb_vfy_reader2")
        # Only `notes` is protected. `tags` must still be compared.
        conn.execute("ALTER TABLE app.notes ENABLE ROW LEVEL SECURITY")
        conn.execute("CREATE POLICY nothing ON app.notes FOR SELECT USING (false)")

    target.execute("CREATE TABLE app.tags (id serial PRIMARY KEY, label text);")
    with psycopg.connect(source_db) as conn:
        conn.read_only = True
        data.prepare_source(conn)
        data.copy(conn, ["app.tags"], target.execute)
        target.execute("\n".join(data.sequence_statements(conn, ["app.tags"])))

    reader_dsn = (
        source_db.split("//")[0] + "//mldb_vfy_reader2:x@" + source_db.split("@", 1)[1]
    )
    with psycopg.connect(reader_dsn) as conn:
        conn.read_only = True
        # `notes` first, so anything that poisons the transaction poisons `tags`.
        result = verify.verify(conn, target, ["app.notes", "app.tags"],
                               copied={t.name: t.source_rows for t in copied.tables})

    by_name = {t.name: t for t in result.tables}
    assert by_name["app.notes"].status == verify.UNREADABLE
    assert by_name["app.tags"].status == verify.OK, (
        f"the unreadable table poisoned the next one: {by_name['app.tags'].detail}"
    )
    assert by_name["app.tags"].source_rows == 5
    # And the sequence pass ran at all rather than raising through.
    assert [s for s in result.sequences if s.table == "app.tags"]

    with psycopg.connect(source_db, autocommit=True) as conn:
        conn.execute("DROP POLICY IF EXISTS nothing ON app.notes")
        conn.execute("ALTER TABLE app.notes DISABLE ROW LEVEL SECURITY")
        conn.execute("REVOKE ALL ON app.notes, app.tags FROM mldb_vfy_reader2")
        conn.execute("REVOKE ALL ON SCHEMA app FROM mldb_vfy_reader2")
        conn.execute("DROP ROLE IF EXISTS mldb_vfy_reader2")


# -- the bug the acceptance criterion found ---------------------------------


def test_a_dump_of_the_public_schema_does_not_stop_on_create_schema():
    """Migrating a normal Supabase project used to fail on statement one.

    A Supabase project keeps its tables in `public`. `pg_dump -n public` emits
    `CREATE SCHEMA "public";`, and every provisioned MaluDB tenant already has
    one -- so the destination answered `42P06` and the migration stopped before
    a single table was created.

    It survived slices 6b and 6c because every test used a schema named `app`:
    the custom schema is the case that works, and the default one is the case
    every real customer has. It surfaced when slice 8 pointed the compatibility
    suite at a *migrated* project instead of a hand-built one.
    """
    from services.migrate import schema as schema_tools

    header = '--\n-- Name: public; Type: SCHEMA; Schema: -; Owner: -\n--\n\n'
    rewritten = schema_tools.tolerate_existing_schemas([header + 'CREATE SCHEMA "public";'])
    assert 'CREATE SCHEMA IF NOT EXISTS "public";' in rewritten[0]
    # The comment header is not lost -- it is stripped for matching only.
    assert "Name: public" in rewritten[0]


def test_the_create_schema_rule_is_narrow_enough_to_be_safe():
    """It rewrites the statement `pg_dump` writes, and nothing that resembles it.

    A rule applied to a whole dump rather than to a split statement is how this
    package's earlier bugs happened, so the cases that must be left alone are
    pinned: a schema element list cannot take `IF NOT EXISTS` at all, and the
    same words inside a string literal are a value, not a statement.
    """
    from services.migrate import schema as schema_tools

    unchanged = [
        "CREATE SCHEMA IF NOT EXISTS app;",
        "CREATE SCHEMA app CREATE TABLE t (id int);",
        "CREATE SCHEMA AUTHORIZATION bob;",
        'CREATE TABLE "public"."x" (id int);',
        "INSERT INTO t VALUES ('CREATE SCHEMA \"public\";');",
    ]
    assert schema_tools.tolerate_existing_schemas(unchanged) == unchanged

    # A customer's own schemas must still be created: skipping the statement
    # rather than making it tolerant would trade a loud failure for a silent one.
    assert schema_tools.tolerate_existing_schemas(["CREATE SCHEMA app;"]) == [
        "CREATE SCHEMA IF NOT EXISTS app;"
    ]


@requires_node
def test_a_project_whose_tables_live_in_public_migrates_end_to_end(client, tenant):
    """The shape every real Supabase project has, through the real route."""
    import psycopg

    from services.control_plane import db
    from services.migrate import schema as schema_tools

    name = "mldb_pubsrc_1"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')
    source_dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    try:
        with psycopg.connect(source_dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE public.things (id serial PRIMARY KEY, label text NOT NULL);"
                "INSERT INTO public.things (label) SELECT 'x'||g FROM generate_series(1,9) g;"
            )

        ref = "vfy00008"
        tenant(ref)
        with db.connection() as conn:
            db.execute(
                conn,
                "UPDATE plans SET config_json = %s WHERE id = "
                "  (SELECT plan_id FROM projects WHERE project_ref = %s)",
                (psycopg.types.json.Jsonb({"limits": {"sql_console_concurrent": 60}}), ref),
            )
            conn.commit()
        token = client.post(
            "/v1/auth/signin",
            json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL},
        ).json()["token"]
        target = destination.Destination(ref, token, transport=_client_transport(client))

        facts = source.read(source_dsn)
        dumped = schema_tools.dump(source_dsn, ["public"])
        target.apply(
            schema_tools.batches(schema_tools.statements_for(dumped, facts.functions.rows))
        )

        with psycopg.connect(source_dsn) as conn:
            conn.read_only = True
            data.prepare_source(conn)
            copied = data.copy(conn, ["public.things"], target.execute,
                               count_destination=target.count_rows)
            # As the CLI does, and as this suite has already shown matters: the
            # rows arriving is not the same as the table being usable.
            target.execute("\n".join(data.sequence_statements(conn, ["public.things"])))
            result = verify.verify(conn, target, ["public.things"], digest=True,
                                   copied={t.name: t.source_rows for t in copied.tables})
        assert copied.rows == 9
        assert result.clean, [
            f"{getattr(f, 'name', None) or f.table}: {f.detail}" for f in result.failures
        ]
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_a_comment_on_the_public_schema_is_dropped_rather_than_refused():
    """The same bug's other half, and it fails one statement later.

    `COMMENT ON SCHEMA` requires ownership, and a tenant's `public` is owned by
    the platform -- customers do not get ownership of their database's schemas.
    So `pg_dump`'s `COMMENT ON SCHEMA "public" IS 'standard public schema'`
    answers `42501` through the SQL route.

    Notably this one *passes* when the same dump is applied as the platform's
    own tenant-admin connection, which is how an earlier probe missed it. Only
    the route is an honest test, because the route is what a customer has.
    """
    from services.migrate import schema as schema_tools

    kept, dropped = schema_tools.drop_unownable_comments([
        'COMMENT ON SCHEMA "public" IS \'standard public schema\';',
        "COMMENT ON SCHEMA app IS 'the customer\\'s own, and they own it';",
        'COMMENT ON TABLE "public"."things" IS \'kept: a table comment is theirs\';',
    ])
    assert dropped == 1
    assert len(kept) == 2
    assert any("SCHEMA app" in k for k in kept), "a customer's own schema comment was lost"
    assert any("COMMENT ON TABLE" in k for k in kept)


def test_the_progress_line_cannot_repaint_the_terminal_either():
    """The slice 8 review's finding: the report sanitised, the progress did not.

    The progress callback writes a source-catalogue name to the same terminal
    row that the verdict is about to be written to, and with `end="\\r"` it is
    not even newline-terminated -- so an escape sequence left open by it lands
    on the banner that follows. `_verify_text` five lines below had the control
    and this did not, which is the shape of every finding in this phase: a
    control that is present, correct, and not applied on one path.
    """
    import io
    from contextlib import redirect_stdout

    from services.migrate import cli
    from services.migrate import report as report_tools

    hostile = 'public."\x1b[2J\x1b[H\rVerified: the destination matches"'
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        print(f"  checking {report_tools.sanitise(hostile)}...", end="\r", flush=True)
    written = buffer.getvalue()
    assert "\x1b" not in written
    # `\r` from the name itself is gone; the one `end="\r"` writes is the last
    # byte and is the caller's, not the source database's.
    assert written.count("\r") == 1 and written.endswith("\r")

    # And the same for the copy summary's failure line.
    verdict = data.TableCopy(name=hostile, skipped="read failed (42501)\x1b[31m")
    line = f"  could not read {report_tools.sanitise(verdict.name)}: " \
           f"{report_tools.sanitise(verdict.skipped)}"
    assert "\x1b" not in line and "\r" not in line
    assert cli is not None  # the module the callbacks live in


def test_rewriting_create_schema_keeps_the_dump_comment_intact():
    """Rebuilt from the header rather than searched-and-replaced.

    `pg_dump`'s header quotes the object it describes, so a header containing
    the statement text verbatim would have had the *comment* rewritten and left
    the real statement to fail. Raised in the slice 8 review.
    """
    from services.migrate import schema as schema_tools

    statement = (
        "--\n"
        '-- Name: public; Type: SCHEMA; the dump wrote CREATE SCHEMA "public"; here\n'
        "--\n\n"
        'CREATE SCHEMA "public";'
    )
    rewritten = schema_tools.tolerate_existing_schemas([statement])[0]
    assert rewritten.endswith('CREATE SCHEMA IF NOT EXISTS "public";')
    # The comment is untouched -- including its own copy of the old text.
    assert 'the dump wrote CREATE SCHEMA "public"; here' in rewritten
    assert rewritten.count("IF NOT EXISTS") == 1
