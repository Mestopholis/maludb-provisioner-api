"""The compatibility scanner (Phase 08 slice 5).

Two halves, and the split is deliberate. The rules are pure functions over
facts, so the cases that matter -- a project with Vault secrets, an OAuth-only
user base, a function in `plpython3u` -- are testable without anybody owning a
Supabase project shaped like that. The reading is exercised against a real
database built to look like one.

The assertions that matter most are not "does it find things". They are:

- **an unreadable probe is a blocker.** A scan that silently skipped
  `auth.users` because the credential could not read it would report a clean
  project and produce a migration with no users in it.
- **the source is not modified**, which is an acceptance criterion in the task
  file and is proved by making the scan try to write rather than by reading the
  code that says it does not.
- **the report never contains the connection string**, which on this path is a
  customer's production credential for somebody else's platform.
"""

from __future__ import annotations

import pytest

from services.migrate import report, rules, source
from services.migrate.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, build_parser, main
from services.migrate.source import Probe, SourceFacts
from tests.conftest import requires_db
from tests.test_provisioning import ADMIN_DSN

requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")

SPECS = rules.load_specs()


def _evaluate(facts: SourceFacts) -> rules.Scan:
    matrix, allowlist = SPECS
    return rules.evaluate(facts, matrix=matrix, allowlist=allowlist)


def _codes(scan: rules.Scan, severity: str | None = None) -> set[str]:
    return {
        f.code for f in scan.findings if severity is None or f.severity == severity
    }


# -- the specs the rules read ----------------------------------------------


def test_the_rules_read_the_specs_rather_than_a_copy_of_them():
    """ADR-043 makes the matrix the authority and ADR-045 makes the allowlist
    data, so adding an extension is a merge rather than a release. A scanner
    with its own hard-coded list would quietly become the third opinion."""
    matrix, allowlist = SPECS
    assert "uuid-ossp" in rules.allowed_extensions(allowlist)
    assert "postgres_fdw" in rules.denial_reasons(allowlist)
    # Read out of the file rather than compared to a constant. This asserted
    # `storage.upload == deferred` until slice 5 made it supported, which is a
    # canary doing its job -- but it tied the check to a status that was always
    # going to move. `storage_policy_authoring` is deferred by decision
    # (ADR-061) rather than by not having got to it yet, so it is the more
    # honest thing to hold a scanner against.
    features = matrix["surfaces"]["storage"]["features"]
    assert features["storage_policy_authoring"]["status"] == "deferred"
    assert features["upload"]["phase"] == 10


# -- what stops a migration ------------------------------------------------


def test_a_probe_that_could_not_be_read_blocks_the_migration():
    """The finding that keeps 'no blockers' meaning what it says."""
    facts = SourceFacts()
    facts.auth_users = Probe(readable=False, reason="the supplied credential may not read it")

    scan = _evaluate(facts)
    assert "source.unreadable" in _codes(scan, rules.BLOCKER)
    assert not scan.migratable
    finding = next(f for f in scan.findings if f.code == "source.unreadable")
    assert any("auth_users" in item for item in finding.items)


def test_an_extension_off_the_allowlist_blocks_and_says_why():
    """ADR-045. A refusal a customer cannot understand becomes the support
    ticket self-service was chosen to avoid."""
    facts = SourceFacts()
    facts.extensions = Probe(rows=[
        {"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"},
        {"name": "uuid-ossp", "schema": "extensions", "version": "1.1"},
        {"name": "postgres_fdw", "schema": "public", "version": "1.1"},
    ])

    scan = _evaluate(facts)
    finding = next(f for f in scan.findings if f.code == "extension.not_allowlisted")
    assert finding.severity == rules.BLOCKER
    assert any("postgres_fdw" in item for item in finding.items)
    # The allowlisted one is not reported, and neither is plpgsql -- which is in
    # every PostgreSQL database and would otherwise block every project.
    assert not any("uuid-ossp" in item for item in finding.items)
    assert not any("plpgsql" in item for item in finding.items)
    # The reason comes from the spec rather than from this module.
    assert "ADR-014" in finding.items[0] or "another database" in finding.items[0]


def test_stored_objects_block_and_name_the_phase_that_will_carry_them():
    """ADR-043: a blocker is a not-yet, so the answer is a date rather than a
    refusal."""
    facts = SourceFacts()
    facts.storage_buckets = Probe(rows=[{"id": "avatars", "name": "avatars", "public": True}])
    facts.storage_objects = Probe(rows=[{"total": 412, "bytes": 900_000}])

    finding = next(f for f in _evaluate(facts).findings if f.code == "storage.objects")
    assert finding.severity == rules.BLOCKER
    assert finding.phase == 10


def test_empty_buckets_are_a_warning_rather_than_a_blocker():
    """Nothing is lost, so nothing should stop a cutover -- but they are
    configuration and they do not come across."""
    facts = SourceFacts()
    facts.storage_buckets = Probe(rows=[{"id": "unused", "name": "unused", "public": False}])
    facts.storage_objects = Probe(rows=[{"total": 0, "bytes": 0}])

    scan = _evaluate(facts)
    assert "storage.empty_buckets" in _codes(scan, rules.WARNING)
    assert "storage.objects" not in _codes(scan)


def test_external_identities_block_and_email_ones_do_not():
    """A user row whose only authenticator is a provider configuration migrates
    into an account nobody can sign in to, which is worse than not migrating."""
    facts = SourceFacts()
    facts.auth_identities = Probe(rows=[
        {"provider": "email", "total": 900},
        {"provider": "google", "total": 61},
        {"provider": "github", "total": 12},
    ])

    finding = next(f for f in _evaluate(facts).findings if f.code == "auth.external_identities")
    assert finding.severity == rules.BLOCKER
    assert "73" in finding.title
    # Listed per provider, so the decision can be made one provider at a time.
    assert sorted(finding.items) == ["github: 12", "google: 61"]

    facts.auth_identities = Probe(rows=[{"provider": "email", "total": 900}])
    assert "auth.external_identities" not in _codes(_evaluate(facts))


def test_users_without_a_password_are_reported_before_the_cutover():
    """ADR-043 requires the reset be reported in advance rather than discovered
    by the customer's users on the morning after."""
    facts = SourceFacts()
    facts.auth_users = Probe(rows=[{"total": 100, "with_password": 60, "confirmed": 100}])

    finding = next(f for f in _evaluate(facts).findings if f.code == "auth.no_password")
    assert finding.severity == rules.WARNING
    assert "40 of 100" in finding.title


def test_a_function_in_an_untrusted_language_blocks():
    facts = SourceFacts()
    facts.functions = Probe(rows=[
        {"schema": "public", "name": "safe", "language": "plpgsql"},
        {"schema": "public", "name": "dangerous", "language": "plpython3u"},
    ])

    finding = next(f for f in _evaluate(facts).findings if f.code == "schema.untrusted_language")
    assert finding.severity == rules.BLOCKER
    assert finding.items == ["public.dangerous (plpython3u)"]


def test_foreign_tables_and_webhooks_and_vault_all_block():
    """Three separate surfaces with one thing in common: each reaches outside
    the database, which is what a shared node cannot allow."""
    facts = SourceFacts()
    facts.foreign_tables = Probe(rows=[{"schema": "public", "name": "remote", "server": "other"}])
    facts.function_hooks = Probe(rows=[
        {"schema": "public", "name": "on_insert", "table_name": "orders"}
    ])
    facts.vault_secrets = Probe(rows=[{"total": 3}])

    blockers = _codes(_evaluate(facts), rules.BLOCKER)
    assert {"schema.foreign_tables", "schema.database_webhooks", "vault.secrets"} <= blockers


def test_a_table_without_rls_is_a_warning_about_todays_project_too():
    """ADR-018: MaluDB grants `anon` what Supabase does, so RLS is what decides
    whether a public read returns rows. A migration is when somebody looks."""
    facts = SourceFacts()
    facts.relations = Probe(rows=[
        {"schema": "public", "name": "open", "kind": "r", "rls_enabled": False,
         "estimated_rows": 10, "size_bytes": 8192},
        {"schema": "public", "name": "closed", "kind": "r", "rls_enabled": True,
         "estimated_rows": 10, "size_bytes": 8192},
    ])

    finding = next(f for f in _evaluate(facts).findings if f.code == "schema.rls_disabled")
    assert finding.severity == rules.WARNING
    assert finding.items == ["public.open"]


def test_what_cannot_be_seen_from_the_database_is_still_reported():
    """Absence of evidence is not evidence of absence, and a clean report must
    not be readable as 'everything I have migrates'. Edge Functions live in
    Supabase's platform; broadcast and presence are client-side."""
    scan = _evaluate(SourceFacts())
    assert {"edge_functions.undetectable", "realtime.undetectable"} <= _codes(scan, rules.WARNING)
    # And an empty project is still migratable -- these are warnings, not
    # blockers, or every migration would be blocked forever.
    assert scan.migratable


def test_a_table_the_planner_has_never_counted_is_not_reported_as_empty():
    """`reltuples` is -1 until something analyses a table, which is every table
    in a freshly restored database. Flooring that to zero would tell a customer
    sizing a maintenance window that there is nothing to copy -- the same
    mistake as reporting unmetered usage as a zero, found by running the scanner
    against a real database rather than by reading the query."""
    facts = SourceFacts()
    facts.relations = Probe(rows=[
        {"schema": "public", "name": "counted", "kind": "r", "rls_enabled": True,
         "estimated_rows": 500, "size_bytes": 8192},
        {"schema": "public", "name": "never_analysed", "kind": "r", "rls_enabled": True,
         "estimated_rows": -1, "size_bytes": 8192},
    ])

    summary = _evaluate(facts).summary
    assert summary["estimated_rows"] == 500
    assert summary["tables_never_analysed"] == 1

    text = report.as_text(_evaluate(facts), report.freeze_estimate(0, None))
    assert "1 table(s) never analysed, not counted" in text


def test_a_definer_rights_view_is_reported_as_an_exposure():
    """The gap the security review found: the first version of this rule
    checked `relrowsecurity` on ordinary tables and nothing else.

    Measured on PostgreSQL 17: a role that reads 0 rows from an RLS-protected
    table reads every row through a view over it, because a view without
    `security_invoker=true` runs as its owner. A materialized view is worse --
    RLS cannot be enabled on one at all.
    """
    facts = SourceFacts()
    facts.relations = Probe(rows=[
        {"schema": "public", "name": "leaky", "kind": "v", "rls_enabled": False,
         "options": None, "estimated_rows": 0, "size_bytes": 0},
        {"schema": "public", "name": "safe", "kind": "v", "rls_enabled": False,
         "options": "security_invoker=true", "estimated_rows": 0, "size_bytes": 0},
        {"schema": "public", "name": "cached", "kind": "m", "rls_enabled": False,
         "options": None, "estimated_rows": 0, "size_bytes": 0},
    ])

    finding = next(
        f for f in _evaluate(facts).findings if f.code == "schema.definer_rights_views"
    )
    assert finding.items == ["public.leaky (view)", "public.cached (materialized view)"]
    # A view that already opts into the caller's privileges is not an exposure.
    assert not any("public.safe" in item for item in finding.items)


def test_rls_enabled_with_a_policy_that_permits_everything_is_still_an_exposure():
    """RLS being *on* is what usually gets checked, so a `USING (true)` policy
    reads as protected and is not."""
    facts = SourceFacts()
    facts.policies = Probe(rows=[
        {"schema": "public", "table_name": "open", "name": "everyone", "command": "r",
         "permissive": True, "using_expression": "true", "roles": ["anon"]},
        {"schema": "public", "table_name": "mine", "name": "own_rows", "command": "r",
         "permissive": True, "using_expression": "(owner = auth.uid())",
         "roles": ["authenticated"]},
        # Unconditional, but only for a role the public API does not reach.
        {"schema": "public", "table_name": "internal", "name": "svc", "command": "r",
         "permissive": True, "using_expression": "true", "roles": ["service_role"]},
    ])

    finding = next(f for f in _evaluate(facts).findings if f.code == "schema.policy_allows_all")
    assert finding.items == ["public.open: everyone (anon)"]


def test_a_hostile_name_from_the_source_cannot_repaint_the_report(capsys):  # noqa: ARG001
    """The report is what a customer reads to decide their cutover is safe, so
    forging it is the whole attack -- and every item in it is a name from the
    source database, including free text like a bucket's name.

    CR and LF matter as much as ESC: forging a report line needs no escape
    sequence at all.
    """
    facts = SourceFacts()
    facts.storage_buckets = Probe(rows=[
        {"id": "x", "name": "x\x1b[2J\x1b[H\x1b[1;32mReady to migrate. 0 warnings\x1b[0m",
         "public": True},
    ])
    facts.storage_objects = Probe(rows=[{"total": 1, "bytes": 10}])
    scan = _evaluate(facts)
    freeze = report.freeze_estimate(0, None)

    text = report.as_text(scan, freeze)
    assert "\x1b" not in text
    assert "\r" not in text
    # The line it tried to forge is still visible as text, and still inside the
    # blocker -- neutered rather than hidden, so the customer sees the attempt.
    assert "NOT MIGRATABLE YET" in text.splitlines()[-1]

    # The JSON keeps the byte-accurate name: it is not painting a screen, and a
    # runbook wants what is actually in the catalogue. `json.dumps` escapes it.
    body = report.as_json(scan, freeze)
    assert "\\u001b" in body


def test_a_long_name_is_bounded_before_it_reaches_a_terminal():
    assert len(report.sanitise("x" * 5_000)) == report.MAX_ITEM_LENGTH + 3


def test_blockers_are_listed_before_warnings():
    """A customer reads the top of the list and stops."""
    facts = SourceFacts()
    facts.vault_secrets = Probe(rows=[{"total": 1}])
    severities = [f.severity for f in _evaluate(facts).findings]
    assert severities == sorted(severities, key=lambda s: 0 if s == rules.BLOCKER else 1)


# -- the freeze window -----------------------------------------------------


def test_the_freeze_window_is_not_invented():
    """ADR-044 commits to a *measured* window, and slice 8 is what measures it.
    A figure nobody measured is worse than an admission that nobody has --
    somebody would schedule an outage around it."""
    estimate = report.freeze_estimate(5 * 1024 * 1024 * 1024, None)
    assert estimate["seconds"] is None
    assert estimate["measured"] is False
    assert "not measured" in estimate["note"].lower()


def test_a_measured_rate_produces_arithmetic_and_says_so():
    estimate = report.freeze_estimate(1024 * 1024 * 600, 10)
    assert estimate["seconds"] == 60
    assert estimate["measured"] is True
    assert "10 MB/s" in estimate["note"]


# -- the report ------------------------------------------------------------


def test_neither_rendering_can_contain_the_connection_string():
    """The scan never handles the DSN after connecting, and this is the test
    that keeps it that way: the report is built from facts, and no fact carries
    it."""
    facts = SourceFacts()
    facts.server_version = "PostgreSQL 15.8"
    facts.database_bytes = 1024
    scan = _evaluate(facts)
    freeze = report.freeze_estimate(facts.database_bytes, None)

    secret = "postgresql://postgres:hunter2@db.abcdefgh.supabase.co:5432/postgres"  # noqa: S105 - a fixture DSN, and the point is that it never appears
    assert secret not in report.as_text(scan, freeze)
    assert secret not in report.as_json(scan, freeze)
    assert "hunter2" not in report.as_json(scan, freeze)


def test_the_text_report_says_the_freeze_is_the_customers_to_apply():
    """ADR-044's uncomfortable half. The platform cannot stop writes to somebody
    else's platform, and a migration that ran while writes continued produces a
    copy quietly missing rows."""
    scan = _evaluate(SourceFacts())
    text = report.as_text(scan, report.freeze_estimate(0, None))
    assert "cannot stop writes" in text
    assert "row counts" in text


def test_a_long_item_list_is_truncated_in_text_but_not_in_json():
    """A terminal that printed forty thousand table names would bury the
    sentence above them; a runbook consuming the JSON wants all of them."""
    facts = SourceFacts()
    facts.relations = Probe(rows=[
        {"schema": "public", "name": f"t{i}", "kind": "r", "rls_enabled": False,
         "estimated_rows": 0, "size_bytes": 0}
        for i in range(40)
    ])
    scan = _evaluate(facts)
    freeze = report.freeze_estimate(0, None)

    text = report.as_text(scan, freeze)
    assert "and 30 more" in text
    assert text.count("public.t") == report.MAX_ITEMS_SHOWN
    assert '"public.t39"' in report.as_json(scan, freeze)


# -- the command line ------------------------------------------------------


def test_a_missing_connection_string_is_a_tool_error_not_a_clean_scan(capsys):
    """Exit 2 rather than 0: a scan that did not run must not look like a scan
    that found nothing."""
    assert main(["scan"]) == EXIT_ERROR
    assert "MALUDB_SOURCE_DSN" in capsys.readouterr().err


def test_the_environment_variable_is_the_documented_way_in():
    """An argument is visible in `ps` and in shell history, and this one is a
    production credential for somebody else's platform."""
    parser = build_parser()
    help_text = parser.parse_args(["scan", "--help"]) if False else None  # noqa: F841
    action = next(a for a in parser._subparsers._group_actions[0].choices["scan"]._actions
                  if a.dest == "source_dsn")
    assert "MALUDB_SOURCE_DSN" in action.help
    assert "shell history" in action.help


def test_an_unreachable_source_reports_sqlstate_and_never_the_dsn(capsys, monkeypatch):
    """psycopg's connection errors can echo the conninfo."""
    monkeypatch.setenv("MALUDB_SOURCE_DSN", "postgresql://u:s3cret@127.0.0.1:1/nope")
    assert main(["scan"]) == EXIT_ERROR
    assert "s3cret" not in capsys.readouterr().err


# -- against a database shaped like a Supabase project ---------------------


@pytest.fixture
def supabase_like(request):
    """A throwaway database with the shape a Supabase project has.

    Not a MaluDB tenant: the point is to read something built the way the
    *source* is, including the schemas Supabase creates and this platform does
    not.
    """
    import psycopg

    name = f"mldb_scan_{request.node.name[-8:].replace('-', '_')}"[:60]
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')

    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""
            CREATE SCHEMA auth;
            CREATE SCHEMA storage;
            CREATE TABLE auth.users (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                email text, encrypted_password text, confirmed_at timestamptz);
            CREATE TABLE auth.identities (
                id text PRIMARY KEY, user_id uuid, provider text NOT NULL);
            CREATE TABLE storage.buckets (id text PRIMARY KEY, name text, public boolean);
            CREATE TABLE storage.objects (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                                          bucket_id text, metadata jsonb);
            INSERT INTO auth.users (email, encrypted_password, confirmed_at)
                 VALUES ('a@example.com', 'hash', now()), ('b@example.com', NULL, now());
            INSERT INTO auth.identities VALUES ('1', gen_random_uuid(), 'email'),
                                               ('2', gen_random_uuid(), 'google');

            CREATE TABLE public.notes (id bigint PRIMARY KEY, owner uuid, body text);
            CREATE TABLE public.secrets (id bigint PRIMARY KEY, value text);
            ALTER TABLE public.secrets ENABLE ROW LEVEL SECURITY;
            CREATE POLICY own ON public.secrets FOR SELECT USING (true);
            CREATE FUNCTION public.touch() RETURNS trigger LANGUAGE plpgsql
                AS $$ BEGIN RETURN NEW; END $$;
            CREATE TRIGGER touch_notes BEFORE UPDATE ON public.notes
                FOR EACH ROW EXECUTE FUNCTION public.touch();
            CREATE PUBLICATION supabase_realtime FOR TABLE public.notes;
        """)
    yield dsn
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@requires_db
@requires_node
def test_a_real_scan_reads_the_project_and_judges_it(supabase_like):
    facts = source.read(supabase_like)
    scan = _evaluate(facts)

    assert facts.database_bytes > 0
    assert facts.server_version.startswith("PostgreSQL")
    assert facts.auth_users.one("total") == 2
    assert facts.auth_users.one("with_password") == 1

    # Customer objects only: Supabase's own schemas are the destination's to
    # build, and copying them would overwrite what provisioning made.
    names = {f"{row['schema']}.{row['name']}" for row in facts.relations.rows}
    assert names == {"public.notes", "public.secrets"}
    assert facts.policies.count == 1
    assert facts.triggers.count == 1
    assert {row["name"] for row in facts.functions.rows} == {"touch"}
    assert facts.realtime_publication.count == 1

    codes = _codes(scan)
    # A Google identity and a passwordless user, both found.
    assert "auth.external_identities" in codes
    assert "auth.no_password" in codes
    # `public.notes` has no RLS; `public.secrets` does.
    rls = next(f for f in scan.findings if f.code == "schema.rls_disabled")
    assert rls.items == ["public.notes"]
    # Nothing in storage, so no blocker from it.
    assert "storage.objects" not in codes


@requires_db
@requires_node
def test_the_scan_cannot_write_to_the_source(supabase_like, monkeypatch):
    """The acceptance criterion in tasks/PHASE-08-SUPABASE-MIGRATION.md, proved
    by making it try. Every statement in `source` is a constant, so nothing can
    issue the `SET` that ADR-040 showed escapes a read-only session -- which
    makes READ ONLY a real backstop here rather than a claim."""
    import psycopg

    def write_instead(conn):
        conn.execute("CREATE TABLE public.should_not_exist (id int)")
        return SourceFacts()

    monkeypatch.setattr(source, "_read", write_instead)
    with pytest.raises(source.SourceError, match="25006|read"):
        source.read(supabase_like)

    monkeypatch.undo()
    with psycopg.connect(supabase_like) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.should_not_exist') IS NULL AS absent")
        assert cur.fetchone()[0] is True


@requires_db
@requires_node
def test_a_missing_supabase_schema_does_not_end_the_scan(supabase_like):
    """A project with no Vault is not a project the scanner cannot read. Each
    probe runs in its own savepoint, or the first missing schema would abort the
    transaction and every later probe would report nothing found."""
    facts = source.read(supabase_like)
    # `vault` was never created in the fixture.
    assert facts.vault_secrets.readable
    assert facts.vault_secrets.count == 0
    # And the probes after it still worked.
    assert facts.realtime_publication.count == 1
    assert _evaluate(facts).migratable is False  # the Google identity blocks it


@requires_db
@requires_node
def test_a_read_only_role_is_told_rls_is_hiding_things_rather_than_shown_a_zero(supabase_like):
    """The finding that would have cost a customer their files.

    Row-level security is not an error: a session that is neither the owner nor
    `BYPASSRLS` reads an RLS-protected table as **zero rows with no message**
    (measured 2026-08-17). Supabase enables RLS on `storage.objects` by design
    -- that is what storage policies are built from -- and owns it as
    `supabase_storage_admin`.

    So the customer who does the responsible thing, pointing this tool at a
    purpose-made read-only role rather than their project owner, was told they
    had no stored objects. They would have cut over during a write freeze and
    discovered the files missing afterwards. Now the probe says it could not
    see, which routes into the `source.unreadable` blocker.
    """
    import psycopg

    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute("DROP ROLE IF EXISTS mldb_scan_reader")
    admin.execute("CREATE ROLE mldb_scan_reader LOGIN PASSWORD 'readonly'")
    try:
        with psycopg.connect(supabase_like, autocommit=True) as owner:
            owner.execute("INSERT INTO storage.buckets VALUES ('files','files',false)")
            owner.execute(
                "INSERT INTO storage.objects (bucket_id, metadata) "
                "VALUES ('files', '{\"size\": 4096}'::jsonb)"
            )
            owner.execute("ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY")
            owner.execute("ALTER TABLE storage.objects FORCE ROW LEVEL SECURITY")
            owner.execute("GRANT USAGE ON SCHEMA public, auth, storage TO mldb_scan_reader")
            owner.execute(
                "GRANT SELECT ON ALL TABLES IN SCHEMA public, auth, storage "
                "TO mldb_scan_reader"
            )

        reader_dsn = supabase_like.replace(
            ADMIN_DSN.split("@")[0].split("//")[1], "mldb_scan_reader:readonly"
        )
        facts = source.read(reader_dsn)

        # The object is there and this session cannot see it. Saying "0" would
        # have been the wrong answer to the only question that mattered.
        assert facts.storage_objects.readable is False
        assert "row-level security" in facts.storage_objects.reason

        scan = _evaluate(facts)
        assert not scan.migratable
        blocker = next(f for f in scan.findings if f.code == "source.unreadable")
        assert any("storage_objects" in item for item in blocker.items)
    finally:
        # The grants above are objects that depend on the role, so they have to
        # go first -- and `DROP OWNED BY` is per-database, hence the connection.
        with psycopg.connect(supabase_like, autocommit=True) as owner:
            owner.execute("DROP OWNED BY mldb_scan_reader")
        admin.execute("DROP ROLE IF EXISTS mldb_scan_reader")
        admin.close()


@requires_db
@requires_node
def test_the_json_report_survives_a_project_that_has_storage(supabase_like, monkeypatch, capsys):
    """`sum(bigint)` is `numeric`, which psycopg maps to `Decimal` and
    `json.dumps` refuses -- so `--format json`, the documented runbook
    interface, crashed for exactly the projects that have files in them. The
    unit tests missed it because a hand-written fake row holds a plain int."""
    import json

    import psycopg

    with psycopg.connect(supabase_like, autocommit=True) as conn:
        conn.execute("INSERT INTO storage.buckets VALUES ('files','files',false)")
        conn.execute(
            "INSERT INTO storage.objects (bucket_id, metadata) "
            "VALUES ('files', '{\"size\": 4096}'::jsonb)"
        )

    monkeypatch.setenv("MALUDB_SOURCE_DSN", supabase_like)
    assert main(["scan", "--format", "json"]) == EXIT_BLOCKED
    body = json.loads(capsys.readouterr().out)
    assert body["summary"]["storage_bytes"] == 4096
    assert any(f["code"] == "storage.objects" for f in body["findings"])


@requires_db
@requires_node
def test_the_cli_exits_non_zero_when_something_blocks(supabase_like, monkeypatch, capsys):
    """The exit code is the answer, so a deployment script can gate on it."""
    monkeypatch.setenv("MALUDB_SOURCE_DSN", supabase_like)
    assert main(["scan", "--format", "json"]) == EXIT_BLOCKED

    import json

    body = json.loads(capsys.readouterr().out)
    assert body["migratable"] is False
    assert body["freeze"]["measured"] is False
    assert any(f["code"] == "auth.external_identities" for f in body["findings"])


@requires_db
@requires_node
def test_a_project_with_nothing_blocking_scans_clean(supabase_like, monkeypatch):
    """The other half of the exit code, and the one a customer wants."""
    import psycopg

    with psycopg.connect(supabase_like, autocommit=True) as conn:
        conn.execute("DELETE FROM auth.identities WHERE provider <> 'email'")
        conn.execute("ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY")

    monkeypatch.setenv("MALUDB_SOURCE_DSN", supabase_like)
    assert main(["scan"]) == EXIT_OK
