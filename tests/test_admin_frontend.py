"""The operator console's pages (ADR-082 slice 4), against the files and the listener.

- **every value interpolated into HTML is escaped**, walked at every template depth;
- **nothing is written to browser storage** but the theme, and the session is a cookie the
  script never touches;
- **every state change sends X-MaluDB-Staff**, and every path the script calls is served;
- **no inline script or style**, which is what lets the CSP forbid both;
- **the listener serves exactly the named files** with the CSP, and the public and internal
  applications serve none of them.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from services.control_plane import config as config_module
from services.control_plane.admin_main import CONTENT_SECURITY_POLICY, create_admin_app
from services.control_plane.main import create_app, create_public_app
from tests.test_admin_frontend_helpers import interpolations
from tests.test_control_plane_surfaces import _paths

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "admin" / "admin.js").read_text()
HTML = (ROOT / "admin" / "index.html").read_text()
STAFF_KEY = b"test-staff-key-material-not-the-kek" * 2


# Interpolations not escaped where they are interpolated, each with why that is safe.
SAFE = {
    "count(": "a number, formatted with toLocaleString",
    "Number(": "a number",
    "statCard(": "a template whose own interpolations are checked here",
    "tableCard(": "ditto",
    "badge(": "ditto",
    "miniMeter(": "ditto",
    "meter(": "ditto",
    "customerLink(": "ditto; encodeURIComponent for the address, escapeHtml for the name",
    "usageRows(": "ditto",
    "encodeURIComponent(": "a URL component",
    "API": "a constant",
    "path": "api()'s argument: a literal path built here",
    "rows.join(": "rows built from escaped parts",
    "head.map(": "column names, escaped inside",
    "states.map(": "literal options, escaped inside",
    "outcomes.map(": "ditto",
    "nodes.map(": "cards, escaped inside",
    "p.failed.map(": "rows, escaped inside",
    "p.stuck.map(": "ditto",
    "value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)": "a formatted number",
    "response.statusText": "HTTP reason phrase in an error, shown with textContent or escaped by the caller",
    "units[unit]": "a literal unit",
    "response.status": "error text, set as textContent by the caller",
    "query": "URLSearchParams output",
    "form.dataset.filter": "a literal page name from this file, in an address",
    "title": "document.title, text",
}


def _is_safe(expr: str) -> bool:
    if expr.startswith("escapeHtml("):
        return True
    if any(expr == key or (key.endswith("(") and expr.startswith(key)) for key in SAFE):
        return True
    # tableCard's body: chooses between two templates, each walked on its own.
    table_card = r"rows\.length\s*\?\s*`.*`\s*:\s*`<p class=\"usage-note\">\$\{escapeHtml\(empty\)\}</p>`"
    if re.fullmatch(table_card, expr, re.S):
        return True
    # A conditional choosing literal strings or checked templates.
    if re.fullmatch(r"[\w.!?\s=<>&|()\"',-]+\?\s*(\"[^\"]*\"|'[^']*'|`[^`]*`)\s*:\s*(\"[^\"]*\"|'[^']*'|`[^`]*`)",
                    expr, re.S):
        return True
    return False


def test_every_interpolation_in_the_console_is_escaped():
    found = interpolations(JS)
    assert len(found) > 150, "the walk found almost nothing"
    unescaped = sorted({expr for expr in found if not _is_safe(expr)})
    assert unescaped == [], f"unescaped interpolations in admin/admin.js: {unescaped}"


def test_the_escaping_check_sees_a_raw_value_deep_in_a_template():
    nested = "x = `${rows.map((r) => `<td>${cond ? `<b>${r.email}</b>` : \"\"}</td>`).join(\"\")}`"
    assert not _is_safe("r.email") and "r.email" in interpolations(nested)


def test_nothing_but_the_theme_reaches_browser_storage():
    uses = re.findall(r"(localStorage|sessionStorage|indexedDB)\.(\w+)\(([^)]*)\)", JS)
    assert uses == [("localStorage", "setItem", '"maludb.theme", root.dataset.theme')], uses
    assert "document.cookie" not in JS, "the session cookie is HttpOnly; the script has no business with cookies"


def test_every_state_change_sends_the_staff_header_and_every_path_is_served(migrated_database):
    api = JS[JS.index("async function api("):JS.index("/* -- formatting")]
    assert 'if (method !== "GET") headers["X-MaluDB-Staff"] = "1";' in api
    assert 'credentials: "same-origin"' in api
    served = _paths(create_admin_app(config_module.AdminConfig(
        environment="test", database_url=migrated_database, staff_key=STAFF_KEY)))
    called = set(re.findall(r'api\(`?"?(/[a-z-]+)', JS))
    assert called >= {"/overview", "/sales", "/billing-events", "/customers", "/usage", "/abuse", "/nodes",
                      "/provisioning", "/session"}, called
    for path in called:
        assert f"/admin/v1{path}" in served, path
    assert "/admin/v1/customers/{org_id}" in served


def test_no_inline_script_or_style():
    for tag in re.findall(r"<script\b[^>]*>", HTML):
        assert "src=" in tag, f"inline script: {tag}"
    assert not re.search(r"<script\b[^>]*>\s*[^<\s]", HTML), "a script element with a body"
    assert "style=" not in HTML and "<style" not in HTML
    assert 'style="' not in JS, "a style attribute in a template is blocked by the CSP; set widths through CSSOM"
    assert re.search(r"\bon[a-z]+=", HTML) is None, "no inline event handlers"


def _admin_client(database_url):
    cfg = config_module.AdminConfig(environment="test", database_url=database_url, staff_key=STAFF_KEY)
    return TestClient(create_admin_app(cfg), base_url="https://testserver")


@pytest.mark.parametrize(("path", "kind"), [
    ("/admin/", "text/html"), ("/admin/admin.js", "text/javascript"), ("/admin/theme.js", "text/javascript"),
    ("/admin/assets/styles.css", "text/css"), ("/admin/assets/admin.css", "text/css"),
])
def test_the_listener_serves_the_named_files_with_the_policy(migrated_database, db_pool, path, kind):
    with _admin_client(migrated_database) as client:
        response = client.get(path)
    assert response.status_code == 200 and response.headers["content-type"].startswith(kind)
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY and "'unsafe-inline'" not in CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("path", ["/admin/index.html", "/admin/../frontend/app.js", "/admin/%2e%2e/pyproject.toml",
                                  "/admin/assets/app.js", "/admin/README.md", "/frontend/styles.css"])
def test_nothing_else_is_served(migrated_database, db_pool, path):
    with _admin_client(migrated_database) as client:
        assert client.get(path).status_code == 404


def test_the_other_applications_serve_no_console_page(migrated_database):
    cfg = config_module.Config(environment="test", database_url=migrated_database, gateway_domain="x",
                               database_domain="db.x", docs_enabled=False, kek=b"k" * 32, token_pepper=b"p" * 32)
    for app in (create_app(cfg), create_public_app(cfg)):
        assert not {p for p in _paths(app) if p.startswith("/admin")}
    assert dataclasses.is_dataclass(cfg)
