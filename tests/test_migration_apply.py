"""Migrating a schema into a MaluDB project (Phase 08 slice 6b).

The end-to-end test is the one that matters: a Supabase-shaped source, a real
provisioned tenant, `pg_dump` writing the DDL, and the real
`POST /v1/projects/{ref}/sql` route applying it -- then the destination's own
introspection route asked what arrived. Everything else here exists because
something in that chain is easy to get quietly wrong.

Two findings are pinned as tests rather than as comments:

- **a modern `pg_dump` emits psql meta-commands.** `\restrict` on line one is
  `42601` to a SQL API, measured. Nothing that applies a dump outside psql works
  without stripping them.
- **splitting SQL on `;` is wrong**, and wrong in the direction that does not
  fail: it applies half a function body. The splitter is tested against the
  constructs a dump actually contains.
"""

from __future__ import annotations

import pytest

from services.migrate import destination, rules, schema, source
from tests.conftest import TEST_CREDENTIAL, requires_db
from tests.test_provisioning import ADMIN_DSN

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")


class _Response:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self) -> dict:
        return self._body


# -- preparing a dump, which needs no database -----------------------------


def test_psql_meta_commands_are_stripped():
    r"""`pg_dump` 17 brackets its output with `\restrict` / `\unrestrict`.

    Invisible when the dump is piped into psql, and `42601 syntax error at or
    near "\"` when it is sent to a SQL API -- measured against a real server on
    the first line of a real dump. A migration that applies a dump through
    anything but psql has to remove them.
    """
    dumped = (
        "\\restrict abc123\n"
        "SET statement_timeout = 0;\n"
        "CREATE TABLE t (id int);\n"
        "\\unrestrict abc123\n"
    )
    cleaned = schema.strip_meta_commands(dumped)

    assert "\\restrict" not in cleaned
    assert "\\unrestrict" not in cleaned
    assert "CREATE TABLE t (id int);" in cleaned
    # A backslash inside a statement is untouched: only whole meta-command
    # lines go.
    assert "E'\\\\'" in schema.strip_meta_commands("SELECT E'\\\\';")


def test_a_function_body_is_not_split_on_its_own_semicolons():
    """The mis-split that does not fail. It applies half a function."""
    sql = (
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $body$\n"
        "BEGIN\n"
        "  RAISE NOTICE 'semi ; colon';\n"
        "  RETURN 1;\n"
        "END $body$;\n"
        "CREATE TABLE after_it (id int);\n"
    )
    statements = schema.split_statements(sql)

    assert len(statements) == 2
    assert statements[0].count(";") == 4  # three inside the body, one closing
    assert statements[1] == "CREATE TABLE after_it (id int);"


def test_the_splitter_survives_the_rest_of_what_a_dump_contains():
    """Quoted identifiers, nested block comments and E-strings can all hold a
    `;`. PostgreSQL nests block comments, which a naive scanner ends early."""
    cases = {
        'CREATE TABLE "weird;name" (id int);': 1,
        "/* nested /* ; */ still ; in it */ SELECT 1;": 1,
        "SELECT E'back\\\\slash ; quoted';": 1,
        "SELECT 'plain ; string'; SELECT 2;": 2,
        "SELECT 1; -- trailing ; comment\nSELECT 2;": 2,
        "SELECT $$dollar ; body$$; SELECT 3;": 2,
    }
    for sql, expected in cases.items():
        assert len(schema.split_statements(sql)) == expected, sql


def test_statements_are_batched_into_as_few_requests_as_fit():
    """Measured: a forty-table schema with eighty policies is 254 statements and
    37 KB -- one request. One statement per request would be 254 requests
    against a plan that allows one per eight seconds."""
    statements = [f"SELECT {i};" for i in range(1000)]
    assert len(schema.batches(statements)) == 1

    small = schema.batches(statements, max_bytes=100)
    assert len(small) > 1
    assert sum(batch.count("SELECT") for batch in small) == 1000


def test_a_statement_too_large_to_batch_is_passed_through_whole():
    """It cannot be divided without changing its meaning, and the console
    refuses it with an error naming the size -- a better failure than a silent
    truncation here."""
    giant = "SELECT '" + ("x" * 500) + "';"
    grouped = schema.batches([giant], max_bytes=100)
    assert grouped == [giant]


def test_a_backslash_line_inside_a_function_body_survives():
    r"""The corruption the security review demonstrated against a real dump.

    The first version filtered every line starting with `\`, with no idea of
    lexical context -- and `pg_dump` writes function bodies verbatim. The
    statement still parsed, still applied, and the destination got a different
    function body. `SET check_function_bodies = false` is in every dump, so
    plpgsql is not even parsed on the way in.
    """
    dumped = (
        "\\restrict abc\n"
        "CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        "RETURN 'a\n"
        "\\this line starts with a backslash\n"
        "b';\n"
        "END;\n"
        "$$;\n"
        "\\unrestrict abc\n"
    )
    cleaned = schema.strip_meta_commands(dumped)

    assert "restrict abc" not in cleaned
    assert "\\this line starts with a backslash" in cleaned


def test_the_dump_is_not_rewritten_by_splitlines():
    r"""`str.splitlines()` splits on eight characters that are not `\n` -- `\v`,
    `\f`, 0x1C-0x1E, U+0085, U+2028 and U+2029 -- and rejoining with `\n`
    rewrites every one. U+2028 and U+2029 arrive routinely in web application
    content, so a `DEFAULT` carrying one migrated to a different value."""
    exotic = (
        "SELECT 'ls" + chr(0x2028) + "x ps" + chr(0x2029) + "y nel" + chr(0x85)
        + "z ff" + chr(0x0C) + "w vt" + chr(0x0B) + "q';"
    )
    assert schema.strip_meta_commands(exotic) == exotic


def test_the_constructs_that_hold_an_unquoted_semicolon():
    """`BEGIN ATOMIC` (PostgreSQL 14+, and Supabase runs 15 and 17) is the one
    function body pg_dump writes **unquoted**, and a multi-action `CREATE RULE`
    puts statement terminators inside parentheses. Split, they fail loudly --
    but a batch boundary landing between the halves abandons a migration
    part-applied, mid-freeze, citing a syntax error in SQL pg_dump wrote
    correctly."""
    atomic = (
        "CREATE FUNCTION g(a integer) RETURNS integer LANGUAGE sql\n"
        "    BEGIN ATOMIC\n SELECT (a + 1);\nEND;\n"
        "CREATE TABLE after_it (id int);\n"
    )
    assert len(schema.split_statements(atomic)) == 2

    rule = (
        "CREATE RULE multi AS ON INSERT TO src DO INSTEAD ( "
        "INSERT INTO log VALUES (new.id, 'a'); INSERT INTO log VALUES (new.id, 'b'); );\n"
        "SELECT 1;\n"
    )
    assert len(schema.split_statements(rule)) == 2


def test_a_schema_name_that_pg_dump_would_read_as_a_pattern_is_refused():
    """`-n` takes a *pattern*, not a name. Measured: a source schema literally
    named `*` dumped `pg_catalog`, `auth`, `storage` and `vault` -- defeating
    both the customer-schema boundary and the scan, which only judged the
    schemas it enumerated."""
    for hostile in ("*", "a?b", "pub.lic", 'quo"te', ""):
        with pytest.raises(schema.SchemaError, match="pattern|empty"):
            schema.dump("postgresql://unused", [hostile])


def test_the_source_dsn_is_not_passed_as_a_command_line_argument():
    r"""`/proc/<pid>/cmdline` is world-readable on every mainstream Linux, so a
    DSN in `argv` hands the customer's Supabase production password to every
    local account for the length of the dump -- verified on this project's own
    development host. `/proc/<pid>/environ` is readable only by the process
    owner and root, which is the boundary that matters: the threat is another
    unprivileged user on a shared or CI machine."""
    import inspect

    source_text = inspect.getsource(schema.dump)
    assert "_pg_dump(), dsn" not in source_text
    assert "env=_libpq_env(dsn)" in source_text

    env = schema._libpq_env("postgresql://someone:hunter2@db.example:6543/postgres")
    assert env["PGPASSWORD"] == "hunter2"  # noqa: S105 - a fixture value
    assert env["PGHOST"] == "db.example"
    assert env["PGPORT"] == "6543"


def test_a_locked_down_function_does_not_arrive_open():
    """The privilege regression the review demonstrated end to end.

    `--no-privileges` suppresses the ACL restore, and an ACL restore is REVOKE
    *then* GRANT -- so it drops restrictions too. `SECURITY DEFINER` is a
    property rather than a privilege and survives, and a freshly created
    function is EXECUTE to PUBLIC. A privileged RPC unreachable by anonymous
    callers on Supabase became reachable by anyone holding the destination's
    publishable key.
    """
    locked = [{
        "schema": "sd", "name": "promote", "security_definer": True,
        "signature": '"sd"."promote"("uid" text)',
        "acl": ["postgres=X/postgres", "anon=X/postgres"],
    }]
    carried = schema.privilege_statements(locked)
    assert carried[0].startswith("REVOKE ALL ON FUNCTION")
    # Faithful rather than merely strict: the source granted anon, so anon keeps
    # it. A migration that quietly locked down a working RPC would break the
    # customer's application in the other direction.
    assert any("GRANT EXECUTE" in statement and "TO anon" in statement for statement in carried)

    # A source at PostgreSQL's default is left at the destination's default.
    assert schema.privilege_statements([dict(locked[0], acl=None)]) == []
    # As is one that explicitly grants PUBLIC.
    assert schema.privilege_statements(
        [dict(locked[0], acl=["postgres=X/postgres", "=X/postgres"])]
    ) == []


# -- talking to the destination --------------------------------------------


def test_the_rate_limit_is_waited_out_rather_than_failed_on():
    """The console allows one statement per eight seconds on free, so a
    migration of several batches *will* meet 429. Honouring `Retry-After` is the
    difference between a slow migration and a failed one."""
    slept: list[float] = []
    attempts = []

    def transport(method, path, payload, headers):
        attempts.append(path)
        if len(attempts) < 3:
            return _Response(429, {"detail": "too many attempts", "retry_after": 4})
        return _Response(200, {"results": [], "requested_role": "x"})

    target = destination.Destination(
        "abcd0001", "tok", transport=transport, sleep=slept.append  # noqa: S106
    )
    applied = target.apply(["SELECT 1;"])

    assert applied.batches == 1
    assert slept == [4.0, 4.0]
    assert applied.rate_limited_seconds == 8.0


def test_an_absurd_retry_after_is_capped_rather_than_obeyed():
    """A header asking for an hour is a reason to stop and tell the customer,
    not to sleep through their maintenance window."""
    assert destination.Destination._retry_after({"retry_after": 86_400}) == (
        destination.MAX_RETRY_AFTER_SECONDS
    )
    # And a missing or unparseable one still backs off rather than spinning.
    assert destination.Destination._retry_after({}) >= 1.0


def test_each_refusal_says_what_the_customer_should_do():
    """A migration that stops mid-cutover must say why in a sentence somebody
    can act on."""
    explain = destination.Destination._explain
    assert "MALUDB_TOKEN" in explain(401, {})
    assert "does not belong to you" in explain(404, {})
    assert "backfill-executor" in explain(503, {})
    assert "not ready" in explain(409, {})
    assert "no room" in explain(400, {"detail": "no room"})


def test_the_token_never_appears_in_an_error():
    """It is the customer's platform credential and lives in a header."""
    def transport(method, path, payload, headers):
        assert headers["Authorization"] == "Bearer s3cret-token"
        return _Response(403, {"detail": "this project's SQL console is disabled"})

    target = destination.Destination(
        "abcd0001", "s3cret-token", transport=transport  # noqa: S106
    )
    with pytest.raises(destination.DestinationError) as raised:
        target.execute("SELECT 1;")
    assert "s3cret-token" not in str(raised.value)


# -- end to end, into a real project ---------------------------------------


@pytest.fixture
def supabase_source(request):
    """A throwaway database shaped like a Supabase project, with a real schema."""
    import psycopg

    name = f"mldb_src_{abs(hash(request.node.name)) % 10**6}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')

    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""
            CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
            CREATE SCHEMA auth;
            CREATE TABLE auth.users (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                                     email text, encrypted_password text,
                                     confirmed_at timestamptz);
            CREATE TABLE auth.identities (id text PRIMARY KEY, user_id uuid, provider text);
            INSERT INTO auth.identities VALUES ('1', gen_random_uuid(), 'email');

            CREATE SCHEMA app;
            CREATE TABLE app.customers (
                id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
                owner uuid NOT NULL,
                email text NOT NULL,
                created_at timestamptz DEFAULT now(),
                CONSTRAINT email_not_blank CHECK (email <> '')
            );
            CREATE INDEX customers_owner_idx ON app.customers (owner);
            ALTER TABLE app.customers ENABLE ROW LEVEL SECURITY;
            CREATE POLICY own_rows ON app.customers FOR SELECT TO authenticated
                USING (owner::text = 'x');
            CREATE FUNCTION app.touch() RETURNS trigger LANGUAGE plpgsql AS $body$
            BEGIN
              -- a body with a semicolon in it, which is the splitter's job
              NEW.created_at := now();
              RETURN NEW;
            END $body$;
            CREATE TRIGGER touch_customers BEFORE UPDATE ON app.customers
                FOR EACH ROW EXECUTE FUNCTION app.touch();
            CREATE VIEW app.recent AS SELECT id, email FROM app.customers;
            -- The careful developer's pattern: a privileged helper, locked down.
            CREATE FUNCTION app.locked(uid text) RETURNS text
                LANGUAGE sql SECURITY DEFINER AS $$ SELECT uid $$;
            REVOKE ALL ON FUNCTION app.locked(text) FROM PUBLIC;
        """)
    yield dsn
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


def _client_transport(client):
    """Drive the real control-plane application the way the CLI drives HTTP."""
    def transport(method, path, payload, headers):
        if method == "GET":
            return client.get(path, headers=headers)
        return client.post(path, json=payload, headers=headers)
    return transport


@requires_node
def test_a_schema_migrates_and_the_destination_reports_it(client, tenant, supabase_source):
    """The whole chain, with nothing stubbed: pg_dump writes the DDL, the real
    SQL route applies it as `mldb_<ref>_admin`, and slice 2's introspection
    route is asked what arrived."""
    from services.control_plane import db

    ref = "mig00001"
    tenant(ref)

    # Free allows one statement per eight-second window; a migration is a few
    # requests. Raising it through `plans.config_json` is how a deployment does
    # it for real, and keeps the test from measuring the rate limiter.
    import psycopg

    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = "
            "  (SELECT plan_id FROM projects WHERE project_ref = %s)",
            (psycopg.types.json.Jsonb({"limits": {"sql_console_concurrent": 20}}), ref),
        )
        conn.commit()

    token = client.post(
        "/v1/auth/signin", json={"email": f"{ref}@example.com", "password": TEST_CREDENTIAL}
    ).json()["token"]

    facts = source.read(supabase_source)
    matrix, allowlist = rules.load_specs()
    scan = rules.evaluate(facts, matrix=matrix, allowlist=allowlist)
    assert scan.migratable, [f.title for f in scan.blockers]

    dumped = schema.dump(supabase_source, ["app"])
    # `statements_for`, not a list built here: the schema plus the permission
    # statements pg_dump drops. Building it separately is how the first version
    # of this test asserted the locked-down-function property while never
    # applying the thing that makes it hold.
    to_apply = schema.statements_for(dumped, facts.functions.rows)
    target = destination.Destination(ref, token, transport=_client_transport(client))
    target.install_extensions(["uuid-ossp"])
    applied = target.apply(schema.batches(to_apply), statement_count=len(to_apply))
    assert applied.batches >= 1

    snapshot = target.schema_snapshot()
    tables = {f"{t['schema_name']}.{t['name']}": t for t in snapshot["tables"]}

    assert "app.customers" in tables
    assert tables["app.customers"]["rls_enabled"] is True
    # The customer's own object, so the destination's admin role owns it.
    assert tables["app.customers"]["managed"] is False
    assert "app.recent" in tables and tables["app.recent"]["kind"] == "view"

    columns = {c["name"] for c in tables["app.customers"]["columns"]}
    assert {"id", "owner", "email", "created_at"} <= columns

    kinds = {c["kind"] for c in tables["app.customers"]["constraints"]}
    assert {"primary_key", "check"} <= kinds
    assert any(i["name"] == "customers_owner_idx" for i in tables["app.customers"]["indexes"])

    # The policy came across naming the shared role, which is ADR-016's point:
    # a migrated policy works unmodified because the role names are the same.
    policy = tables["app.customers"]["policies"][0]
    assert policy["name"] == "own_rows"
    assert policy["roles"] == ["authenticated"]

    # And the function body survived the splitter.
    functions = {f["name"] for f in snapshot["functions"]}
    assert "touch" in functions
    assert "locked" in functions

    # The locked-down SECURITY DEFINER function is not callable by anon on the
    # destination, which is what `--no-privileges` would otherwise have given
    # away. Asked of the database rather than inferred from the statements sent.
    import psycopg

    from tests.test_provisioning import _tenant_admin_dsn

    with psycopg.connect(_tenant_admin_dsn(f"mldb_{ref}")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT has_function_privilege('anon', p.oid, 'EXECUTE') "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'app' AND p.proname = 'locked'"
        )
        assert cur.fetchone()[0] is False, "a locked-down function arrived callable by anon"


@requires_node
def test_apply_refuses_a_project_the_scan_blocks(client, tenant, supabase_source):
    """The scanner exists so this decision is made before a write freeze."""
    import psycopg

    ref = "mig00002"
    tenant(ref)

    with psycopg.connect(supabase_source, autocommit=True) as conn:
        # An identity no MaluDB Auth surface can carry yet (ADR-043).
        conn.execute("INSERT INTO auth.identities VALUES ('2', gen_random_uuid(), 'google')")

    from services.migrate import cli

    code = cli.main([
        "apply", "--source-dsn", supabase_source, "--project-ref", ref, "--token", "unused",
    ])
    assert code == cli.EXIT_BLOCKED
