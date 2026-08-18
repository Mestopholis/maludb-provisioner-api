"""Carrying the rows (Phase 08 slice 6c).

Structure migration can be 99% right and still useful. Data cannot: a value that
arrives subtly different is worse than one that fails to arrive, because nothing
reports it.

The test that mattered most was written before the code. The obvious design --
`json_agg` out, `json_populate_recordset` in -- silently turns a `jsonb` column
holding the JSON value `null` into SQL `NULL`, because `row_to_json` renders
both as `null`. Measured against a real server, which is why values travel as
their own text representation instead.
"""

from __future__ import annotations

import psycopg
import pytest

from services.migrate import data
from tests.conftest import requires_db
from tests.test_provisioning import ADMIN_DSN

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

# Every type whose text round trip is worth doubting, and the two values in each
# that a naive copier gets wrong.
_AWKWARD_SCHEMA = """
CREATE TABLE public.awkward (
    id bigint PRIMARY KEY,
    b bytea, j jsonb, arr text[], ts timestamptz,
    n numeric(20,8), f double precision, s text, nothing text,
    uid uuid, flag boolean
);
INSERT INTO public.awkward VALUES
 (1, '\\x00ff10'::bytea, '{"k":[1,2,{"n":null}]}'::jsonb, ARRAY['a','b,c','d"e'],
  '2026-01-02 03:04:05.123456+00', 12345678901.23456789, 'Infinity'::float8,
  E'tab\\there and a quote''s', NULL,
  '00000000-0000-0000-0000-00000000000a', true),
 (2, NULL, 'null'::jsonb, '{}', 'infinity'::timestamptz, 'NaN'::numeric,
  '-Infinity'::float8, '', NULL, NULL, false);
"""


@pytest.fixture
def awkward_source(request):
    """A source database holding the values that break a careless copier."""
    name = f"mldb_data_{abs(hash(request.node.name)) % 10**6}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_AWKWARD_SCHEMA)
    yield dsn
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


# -- ordering, which needs no database ------------------------------------


def test_parents_are_copied_before_their_children():
    """A foreign key does not care that the rows are still arriving. The
    alternative -- disabling triggers -- needs privileges a tenant does not have
    and should not get."""
    order = data.order_tables(
        ["public.orders", "public.customers", "public.items"],
        [("public.orders", "public.customers"), ("public.items", "public.orders")],
    )
    assert order.index("public.customers") < order.index("public.orders")
    assert order.index("public.orders") < order.index("public.items")


def test_a_cycle_is_copied_rather_than_refused():
    """Two tables referencing each other is legal when a side is deferrable or
    nullable. Refusing to migrate a schema this tool merely cannot *order* would
    be the wrong trade -- the copy fails loudly on the constraint if it really
    cannot be satisfied."""
    order = data.order_tables(
        ["public.a", "public.b"], [("public.a", "public.b"), ("public.b", "public.a")]
    )
    assert sorted(order) == ["public.a", "public.b"]


def test_a_self_reference_does_not_stall_the_ordering():
    """`employees.manager_id -> employees.id`. Ordering cannot help, and the
    rows go in one statement PostgreSQL resolves internally."""
    assert data.order_tables(["public.employees"], [("public.employees", "public.employees")]) == [
        "public.employees"
    ]


# -- the round trip, against a real server --------------------------------


@requires_db
@requires_node
def test_every_awkward_value_arrives_unchanged(awkward_source):
    """The whole reason values travel as text.

    `jsonb 'null'` is the one that made the decision: it is a JSON value, not a
    SQL NULL, and any transport that renders both as `null` loses the
    difference for good.
    """
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.copied (LIKE public.awkward INCLUDING ALL)")

    with psycopg.connect(awkward_source) as source, \
         psycopg.connect(awkward_source, autocommit=True) as dest:
        batches = list(data.insert_batches(source, "public.awkward"))
        assert sum(rows for _, rows in batches) == 2
        for statement, _ in batches:
            dest.execute(statement.replace('"public"."awkward"', '"public"."copied"'))

    with psycopg.connect(awkward_source) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM (SELECT * FROM public.awkward "
                    "EXCEPT SELECT * FROM public.copied) x")
        assert cur.fetchone()[0] == 0, "a value in the source did not arrive"
        cur.execute("SELECT count(*) FROM (SELECT * FROM public.copied "
                    "EXCEPT SELECT * FROM public.awkward) x")
        assert cur.fetchone()[0] == 0, "a value arrived that the source did not have"

        # Named explicitly, because this is the one the first design lost.
        cur.execute("SELECT j::text FROM public.copied ORDER BY id")
        assert [row[0] for row in cur.fetchall()] == ['{"k": [1, 2, {"n": null}]}', "null"]


@requires_db
@requires_node
def test_a_json_null_is_not_the_same_as_a_missing_value(awkward_source):
    """Stated as its own test because it is the finding, not a detail.

    A `jsonb` column can hold the JSON value `null`. `row_to_json` renders that
    identically to a SQL `NULL`, so a JSON-shaped transport cannot tell them
    apart and `json_populate_recordset` writes SQL NULL for both.
    """
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.via_json (LIKE public.awkward INCLUDING ALL)")
        conn.execute(
            "INSERT INTO public.via_json SELECT * FROM json_populate_recordset("
            "  NULL::public.awkward, (SELECT json_agg(row_to_json(a)) FROM public.awkward a))"
        )
        with conn.cursor() as cur:
            cur.execute("SELECT j IS NULL FROM public.via_json WHERE id = 2")
            assert cur.fetchone()[0] is True, (
                "the JSON transport has stopped losing jsonb null -- if this now passes, "
                "the reason data.py avoids json_populate_recordset has changed"
            )


@requires_db
@requires_node
def test_generated_columns_are_not_written_and_identity_ones_are(awkward_source):
    """A stored generated column refuses an explicit value; a
    `GENERATED ALWAYS AS IDENTITY` column accepts one only with
    `OVERRIDING SYSTEM VALUE` -- and a migration must keep the source's ids or
    every foreign key pointing at them breaks."""
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE public.gen (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                price numeric, tax numeric GENERATED ALWAYS AS (price * 0.2) STORED
            );
            INSERT INTO public.gen (price) VALUES (100), (250);
            CREATE TABLE public.gen_copy (LIKE public.gen INCLUDING ALL);
        """)

    with psycopg.connect(awkward_source) as source, \
         psycopg.connect(awkward_source, autocommit=True) as dest:
        batches = list(data.insert_batches(source, "public.gen"))
        statement = batches[0][0]
        assert "OVERRIDING SYSTEM VALUE" in statement
        assert '"tax"' not in statement, "a generated column cannot be written to"
        dest.execute(statement.replace('"public"."gen"', '"public"."gen_copy"'))

    with psycopg.connect(awkward_source) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, price, tax FROM public.gen_copy ORDER BY id")
        assert cur.fetchall() == [(1, 100, 20.0), (2, 250, 50.0)]


@requires_db
@requires_node
def test_the_sequence_is_advanced_past_the_migrated_rows(awkward_source):
    """The classic migration bug, and one that only shows up in production
    traffic: rows arrive with their original ids while the sequence still sits
    at its start value, so the customer's next insert collides."""
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE public.seq (id bigserial PRIMARY KEY, name text);
            INSERT INTO public.seq (id, name) VALUES (10, 'a'), (11, 'b');
        """)

    with psycopg.connect(awkward_source) as source:
        statements = data.sequence_statements(source, ["public.seq"])
    assert statements, "no setval was produced for a serial column"

    with psycopg.connect(awkward_source, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)
        # The next insert must not collide with a migrated row.
        conn.execute("INSERT INTO public.seq (name) VALUES ('next')")
        with conn.cursor() as cur:
            cur.execute("SELECT max(id) FROM public.seq")
            assert cur.fetchone()[0] == 12


@requires_db
@requires_node
def test_a_batch_is_bounded_by_bytes_rather_than_rows(awkward_source):
    """The console caps a request, and one wide row can be larger than a
    thousand narrow ones."""
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE public.many (id int PRIMARY KEY, body text);"
            "INSERT INTO public.many SELECT g, repeat('x', 200) FROM generate_series(1, 200) g;"
        )

    with psycopg.connect(awkward_source) as source:
        batches = list(data.insert_batches(source, "public.many", max_bytes=4_000))
    assert len(batches) > 1
    assert sum(rows for _, rows in batches) == 200
    assert all(len(statement) <= 8_000 for statement, _ in batches)


@requires_db
@requires_node
def test_a_row_count_mismatch_is_reported_rather_than_averaged(awkward_source):
    """ADR-044: the platform cannot enforce a write freeze on somebody else's
    platform, so the check that one happened is arithmetic after the fact."""
    applied: list[str] = []

    def drop_everything(statement: str) -> None:
        # A destination that accepts the statement and stores nothing, which is
        # what a copy racing a live source looks like from here.
        applied.append(statement)

    with psycopg.connect(awkward_source) as source:
        report = data.copy(source, ["public.awkward"], drop_everything)

    assert report.rows == 2
    assert not report.mismatches(), "sent rows should match what was read"

    # And when fewer rows are sent than the source holds, it is named.
    report.tables[0].sent_rows = 1
    assert [t.name for t in report.mismatches()] == ["public.awkward"]


# -- what the slice 6c security review found -------------------------------


@requires_db
@requires_node
def test_a_partitioned_table_is_not_copied_twice(awkward_source):
    """The duplication the per-table count could never see.

    `SELECT ... FROM parent` returns every partition's rows, and `INSERT INTO
    parent` routes them back into the leaf that is *also* copied on its own
    account. Measured: three rows in, six rows out, and `mismatches()` empty
    because each table individually agreed with itself.
    """
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE public.events (id int, at date) PARTITION BY RANGE (at);
            CREATE TABLE public.events_2024 PARTITION OF public.events
                FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
            CREATE TABLE public.events_2025 PARTITION OF public.events
                FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
            INSERT INTO public.events VALUES (1,'2024-05-05'), (2,'2024-06-06'),
                                             (3,'2025-02-02');
            CREATE TABLE public.base (id int, note text);
            CREATE TABLE public.child (extra text) INHERITS (public.base);
            INSERT INTO public.base VALUES (1, 'own');
            INSERT INTO public.child VALUES (2, 'inherited', 'x');
        """)

    relations = [
        {"schema": "public", "name": "events", "kind": "p"},
        {"schema": "public", "name": "events_2024", "kind": "r"},
        {"schema": "public", "name": "events_2025", "kind": "r"},
        {"schema": "public", "name": "base", "kind": "r"},
        {"schema": "public", "name": "child", "kind": "r"},
    ]
    # The partitioned parent holds no rows of its own and is not copied.
    assert "public.events" not in data.copyable_tables(relations)

    with psycopg.connect(awkward_source) as source:
        data.prepare_source(source)
        # `ONLY`: the inheritance parent yields its own row, not the child's.
        assert data.count_rows(source, "public.base") == 1
        rows = sum(n for _, n in data.insert_batches(source, "public.base"))
        assert rows == 1, "an inheritance parent returned its children's rows"


@requires_db
@requires_node
def test_row_level_security_on_the_source_stops_the_copy_rather_than_shrinking_it(
    awkward_source,
):
    """The quietest of the five.

    With `row_security` on, a role that cannot see every row copies a *subset*
    and the counts agree -- because the count is filtered too. `pg_dump` sets
    `row_security = off` so PostgreSQL raises instead, which is what
    `prepare_source` now does.
    """
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute("DROP ROLE IF EXISTS zz_rls_reader")
    admin.execute("CREATE ROLE zz_rls_reader LOGIN PASSWORD 'x'")
    try:
        with psycopg.connect(awkward_source, autocommit=True) as conn:
            conn.execute("""
                CREATE TABLE public.secrets (id int, owner text);
                INSERT INTO public.secrets SELECT g, 'someone' FROM generate_series(1, 9) g;
                ALTER TABLE public.secrets ENABLE ROW LEVEL SECURITY;
                CREATE POLICY mine ON public.secrets USING (owner = current_user);
                GRANT USAGE ON SCHEMA public TO zz_rls_reader;
                GRANT SELECT ON public.secrets TO zz_rls_reader;
            """)

        # A superuser bypasses RLS entirely, so the role matters: this is the
        # security-conscious customer who points the tool at a purpose-made
        # read-only role rather than their project owner.
        reader_dsn = awkward_source.replace(
            ADMIN_DSN.split("@")[0].split("//")[1], "zz_rls_reader:x"
        )
        with psycopg.connect(reader_dsn) as conn:
            # Without the pin, this silently returns 0 of 9 rows.
            conn.execute("SET row_security = on")
            assert data.count_rows(conn, "public.secrets") == 0

            data.prepare_source(conn)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                data.count_rows(conn, "public.secrets")
    finally:
        with psycopg.connect(awkward_source, autocommit=True) as conn:
            conn.execute("DROP OWNED BY zz_rls_reader")
        admin.execute("DROP ROLE IF EXISTS zz_rls_reader")
        admin.close()


@requires_db
@requires_node
def test_the_source_session_is_pinned_so_dates_do_not_transpose(awkward_source):
    """`DateStyle` at `SQL, DMY` renders 2024-03-04 as `04/03/2024`, which a
    destination at the default reads back as 3 April -- silently, and only for
    days of 12 or less, which defeats spot-checking."""
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE public.dated (id int, d date, f double precision);"
            "INSERT INTO public.dated VALUES (1, '2024-03-04', 0.30000000000000004);"
        )

    with psycopg.connect(awkward_source) as conn:
        # A session that arrived misconfigured -- PGDATESTYLE in the customer's
        # environment, or an ALTER ROLE on the source.
        conn.execute("SET DateStyle = 'SQL, DMY'")
        conn.execute("SET extra_float_digits = 0")
        data.prepare_source(conn)

        statements = list(data.insert_batches(conn, "public.dated"))
    body = statements[0][0]
    assert "2024-03-04" in body, "the date was not rendered unambiguously"
    assert "0.30000000000000004" in body, "the float lost precision on the way out"


@requires_db
@requires_node
def test_a_mixed_case_table_does_not_break_the_sequence_lookup(awkward_source):
    """`pg_get_serial_sequence` parses its argument as SQL text, so an unquoted
    `schema || '.' || name` downcases it -- and `"Users"` is what every Prisma,
    TypeORM and Drizzle project produces. It also used to run over every
    relation in the database, so a mixed-case *index* in an unrelated schema
    broke it too."""
    with psycopg.connect(awkward_source, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE public."Users" (id serial PRIMARY KEY, email text);
            INSERT INTO public."Users" (id, email) VALUES (7, 'a@b.c');
            CREATE SCHEMA other;
            CREATE TABLE other.plain (id int);
            CREATE INDEX "MixedIdx" ON other.plain (id);
        """)

    with psycopg.connect(awkward_source) as conn:
        data.prepare_source(conn)
        statements = data.sequence_statements(conn, ['public.Users'], ["public"])
    assert statements, "no setval for a mixed-case table"
    assert '"public"."Users"' in statements[0]


def test_triggers_are_turned_off_for_the_copy_and_back_on_after():
    """Migrated rows are not application writes. A `BEFORE INSERT` filter
    trigger dropped two of three rows and an `updated_at` trigger rewrote every
    migrated timestamp to migration time -- both measured, both reported as a
    clean migration by the old check.

    `USER` rather than `ALL`, so foreign keys are still enforced as rows arrive
    and no privilege beyond table ownership is needed."""
    off = data.trigger_statements(["app.orders"], enable=False)
    on = data.trigger_statements(["app.orders"], enable=True)
    assert off == ['ALTER TABLE "app"."orders" DISABLE TRIGGER USER;']
    assert on == ['ALTER TABLE "app"."orders" ENABLE TRIGGER USER;']


def test_the_count_check_asks_the_destination():
    """Both sides of the old comparison were client-side counters, so it agreed
    with itself whatever the destination did with the rows."""
    report = data.CopyReport(tables=[
        data.TableCopy(name="app.t", source_rows=3, sent_rows=3, landed_rows=3),
    ])
    assert report.mismatches() == []

    # Sent everything, and the destination kept one -- a trigger ate the rest.
    report.tables[0].landed_rows = 1
    assert [t.name for t in report.mismatches()] == ["app.t"]
