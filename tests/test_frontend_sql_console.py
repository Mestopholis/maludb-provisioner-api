"""The dashboard's SQL editor and table browser, against the contract and the rules they keep.

Driven in headless Chromium against a provisioned dev project while being built; what can
be held without a browser is held here:

- **both calls are public routes** with those methods, and **Run as offers exactly the
  roles the SQL route accepts** -- read from the contract, not restated;
- **a result is the customer's data**: every value interpolated into the page is escaped,
  and nothing typed or returned is written to browser storage or survives sign-out;
- **the table browser runs no statement of its own**: "Query this table" fills the editor
  and leaves running it to the person, because a free plan's budget is one statement per
  window;
- **a table name becomes a quoted identifier**, with any quote in it doubled;
- **the RLS warning is true**: it says the publishable key can read and change every row
  of a `public` table without row-level security, which holds only while bootstrap grants
  anon ALL on `public`'s tables -- so this test reads that grant;
- **a console address survives signing in**, so a reload lands where it was.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_JS = (ROOT / "frontend" / "api.js").read_text()
APP_JS = (ROOT / "frontend" / "app.js").read_text()
SPEC = yaml.safe_load((ROOT / "specs" / "control-plane-api.yaml").read_text())
GRANTS = (ROOT / "services" / "control_plane" / "bootstrap" / "004_api_grants.sql").read_text()


def _section() -> str:
    start = APP_JS.index(" * SQL editor and table browser (Phase 08 slices 1-3)")
    # Up to the next section: the account pages follow it, and they have their own test.
    end = APP_JS.index("/* ------------------------------------------------------------------ *", start + 1)
    return APP_JS[start:end]


def _function(name: str) -> str:
    section = _section()
    start = section.index(f"function {name}(")
    following = re.search(r"\n(?:async )?function |\n/\* --|\Z", section[start + 1:])
    return section[start:start + 1 + following.start()]


def _api_function(name: str) -> str:
    match = re.search(rf"export const {name} = .*?(?=\n\n|\nexport |\n/\*|\Z)", API_JS, re.S)
    assert match, f"api.js has no {name}"
    return match.group(0)


def test_both_calls_are_public_routes_with_those_methods():
    for function, method, path, suffix in (
        ("runSql", "post", "/v1/projects/{project_ref}/sql", "/sql"),
        ("getDatabaseSchema", "get", "/v1/projects/{project_ref}/database/schema", "/database/schema"),
    ):
        assert path in SPEC["paths"] and method in SPEC["paths"][path], f"{function}: {method} {path} is not served"
        body = _api_function(function)
        assert suffix in body
        if method == "post":
            assert '"POST"' in body


def test_run_as_offers_exactly_the_roles_the_route_accepts():
    body = SPEC["paths"]["/v1/projects/{project_ref}/sql"]["post"]["requestBody"]["content"]["application/json"]
    schema = SPEC["components"]["schemas"][body["schema"]["$ref"].split("/")[-1]]
    accepted = next(option["enum"] for option in schema["properties"]["role"]["anyOf"] if "enum" in option)
    start = APP_JS.index("const SQL_ROLES = [")
    offered = re.findall(r'\["([a-z_]*)", "', APP_JS[start:APP_JS.index("];", start)])
    assert offered[0] == "", "the first choice is the admin role, sent as no role at all"
    assert sorted(offered[1:]) == sorted(accepted)
    run_sql = _api_function("runSql")
    assert "role ? { role } : {}" in run_sql and "role && claims ? { claims } : {}" in run_sql


def test_nothing_typed_or_returned_reaches_storage_or_outlives_the_session():
    section = _section()
    assert "localStorage" not in section and "sessionStorage" not in section
    signout = APP_JS[APP_JS.index('$("#signout").addEventListener'):]
    before_toast = signout[:signout.index("toast(")]
    assert "state.sql = {}" in before_toast and "state.tables = {}" in before_toast
    assert '$("#project-view").innerHTML = ""' in before_toast


def test_the_table_browser_runs_no_statement_of_its_own():
    browser = _section()[_section().index("/* -- Table browser"):]
    assert "runSql(" not in browser, "the table browser must leave running a statement to the person"
    query = _function("projectViewAction")
    assert "openInEditor(ref, `select * from ${quoteIdent(t.schema_name)}.${quoteIdent(t.name)} limit 100;`)" in query


def test_a_table_name_becomes_a_quoted_identifier():
    assert "const quoteIdent = (name) => `\"${String(name).replace(/\"/g, '\"\"')}\"`;" in APP_JS


def test_the_rls_warning_rests_on_the_grant_that_makes_it_true():
    assert re.search(r"GRANT ALL ON ALL TABLES\s+IN SCHEMA public TO anon, authenticated, service_role;", GRANTS)
    assert re.search(r"ALTER DEFAULT PRIVILEGES IN SCHEMA public\s+GRANT ALL ON TABLES TO anon", GRANTS)
    note = _function("rlsNote")
    assert 'table.schema_name === "public"' in note
    assert "Anyone with the publishable key can read and\n          change every row of this table." in note


def test_a_console_address_survives_signing_in():
    session = APP_JS[APP_JS.index("function renderSession()"):APP_JS.index("function renderPlans()")]
    assert 'window.location.hash.startsWith("#/")' in session and "!(signedIn && consoleRoute)" in session


# Interpolations not escaped at the point of interpolation, each with why that is safe.
ALLOWED = (
    "encodeURIComponent(",          # a URL fragment built from a project ref
    "quoteIdent(",                  # inside a statement placed in the editor's value, which is escaped when rendered
    "roles",                        # options built from escaped values above
    "sqlEditor(project)",           # templates checked here
    "tablesBrowser(project)",
    "tab === \"sql\" ? sqlEditor(project) : tablesBrowser(project)",
    "href(\"sql\")", "href(\"tables\")",
    "tab === \"sql\" ?", "tab === \"tables\" ?",
    "sqlResults(editor)",
    "cell(row[c])",                 # escapes its own value
    "summary",                      # built from escaped parts
    "table",                        # the result table, built from escaped parts
    "notes.map(",                   # static sentences with escaped values inside
    "blocks.join(",
    "r.columns.map(", "rows.map(",
    "list(",                        # a helper rendering templates checked here
    "rlsNote(table)", "tableDetail(project, selected)",
    "Object.entries(bySchema)",
    "tables.map(",
    "functions.length ?",
    "selected ?", "visible.length",
    "schema.truncated.length ?",
    "table.managed ?", "table.comment ?", "enableRls ?",
    "c.is_identity ?", "c.is_generated ?", "c.comment ?", "c.is_nullable ?", "c.default_expression ?",
    "p.permissive ?", "p.using_expression ?", "p.check_expression ?",
    "i.is_valid ?",
    "f.returns ?", "f.security_definer ?",
    "t.kind === \"table\" ?", "[\"table\", \"partitioned_table\"].includes(t.kind)",
    "tableKey(t) === browser.selected ?",
    "Number(table.estimated_rows) < 0 ?",
    "table.rls_forced ?", "table.policies.length === 1 ?",
    "editor.role ?", "editor.statement ?", "value === editor.role ?",
    "out.requested_role && editor.ranAs", "out.storage_restricted",
    "rows.length === 1 ?",
    "error.retryAfter ?",
    "browser.showManaged ?",
    "project ?",
    "route.tab === \"sql\" ?",
    "project.display_name",         # document.title, not HTML
    "ref", "key", "name",           # names bound to escaped values in their functions
    'String(name).replace(',        # quoteIdent's own body: builds SQL, not HTML
    "t",                            # href(t): the tab, one of SQL_TABS
    'tab === "sql"', 'tab === "tables"',  # aria-selected="true|false"
    "t.schema_name", "t.name",      # tableKey(t), escaped wherever it reaches HTML
    "empty",                        # list()'s literal "None."
    "// -1: PostgreSQL",            # the row estimate: a literal, or an escaped number
)


def test_every_interpolation_in_the_page_is_escaped():
    section = _section()
    unescaped = []
    for expr in re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", section):
        expr = expr.strip()
        if not expr or expr.startswith(("escapeHtml(", "CSS.escape(")):
            continue
        if any(expr.startswith(allowed) for allowed in ALLOWED):
            continue
        unescaped.append(expr)
    assert unescaped == [], f"unescaped values in the SQL editor or table browser: {unescaped}"
    # tableKey is escaped at every use in HTML, and list() is only given literals.
    assert all(use.startswith("escapeHtml(tableKey(") or use.startswith("tableKey(t) ===")
               for use in re.findall(r"(?:escapeHtml\()?tableKey\([a-z]+\)(?: ===)?", section)
               if "${" in section), "tableKey reaches HTML only through escapeHtml"
    assert all(literal in ('"None."',) for literal in re.findall(r"list\([a-z.]+, (\"[^\"]*\")", section))
    # The names allowed above are bound to escaped values where they are used as HTML.
    for function, name in (("renderProjectView", "ref"), ("tableDetail", "ref"), ("tableDetail", "key")):
        assert re.search(rf"const {name} = escapeHtml\(", _function(function)), f"{function}: {name} must be escaped"


def test_the_table_browser_renders_without_a_schema_rather_than_throwing():
    """Tables -> SQL editor -> run -> Tables threw `schema.tables of undefined`.

    Running a statement drops the snapshot so the tab reads the database again, but the
    browser's state object stays. The tab then read `schema.tables` of nothing inside the
    hashchange handler, and the editor stayed on screen with the address on /tables.
    Found by hand; reproduced in headless Chromium.
    """
    browser = _function("tablesBrowser")
    guard = browser.index("if (!browser.schema) return")
    assert guard < browser.index("schema.tables.filter("), "no schema must return before anything reads it"
    assert "delete state.tables[ref]?.schema" in _function("runSqlForm"), "the premise: a statement drops the snapshot"
    assert "browser.error = null" in _function("loadTables")
