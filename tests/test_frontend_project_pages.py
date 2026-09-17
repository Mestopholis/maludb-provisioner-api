"""A project's pages and the console frame around them, against the rules they keep.

The keys, usage and memory panels used to open inside a project's card; each is now a page
at `#/projects/<ref>/<page>`, beside the SQL editor and the table browser, with an overview
at `#/projects/<ref>`. Driven in headless Chromium against the dev control plane while being
built; what can be held without a browser is held here:

- **a value shown once does not survive leaving its page** -- the keys page's secret is
  dropped on any change of page, before the next page is drawn;
- **the overview shows a key's value only for a live publishable key**, the one kind the
  API lists with its value;
- **pages that need a serving project are offered only for one**, as the panels were, and
  an address naming one for a project that is not serving lands on its overview;
- **a project ref reaches an address only encoded**, and a project's name reaches the page
  title as text;
- **every value interpolated into the pages is escaped**.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text()
CSS = (ROOT / "frontend" / "styles.css").read_text()


def _section() -> str:
    start = APP_JS.index(" * Project pages\n")
    return APP_JS[start:APP_JS.index(" * SQL editor and table browser (Phase 08 slices 1-3)", start)]


def _function(name: str) -> str:
    section = _section()
    start = section.index(f"function {name}(")
    following = re.search(r"\n(?:async )?function |\n/\* --|\Z", section[start + 1:])
    return section[start:start + 1 + following.start()]


def test_leaving_a_page_drops_a_key_shown_on_it_before_the_next_page_is_drawn():
    route = _function("renderRoute")
    same = route.index("const same = ")
    dropped = route.index("if (!same) dropIssuedKeys();")
    assert same < dropped < route.index("renderProjectView(project, route.tab)")
    assert dropped < route.index("renderAccountRoute(account)"), "an account page is a change of page too"


def test_the_overview_shows_only_a_live_publishable_keys_value():
    key = _function("overviewKey")
    assert '!k.revoked_at && k.key_type === "publishable" && k.key' in key
    assert key.count("publishable.key)") == 1, "the one value rendered is the publishable key found above"
    assert "issuedKey" not in _section(), "a just-created secret is shown on the keys page only"


def test_pages_that_need_a_serving_project_are_offered_only_for_one():
    pages = APP_JS[APP_JS.index("const PROJECT_PAGES = ["):APP_JS.index("];", APP_JS.index("const PROJECT_PAGES = ["))]
    entries = dict(re.findall(r'tab: "([a-z]+)"[^}]*?(serving: true|\})', pages))
    assert entries.pop("overview") == "}", "the overview is for every project"
    assert set(entries) == {"sql", "tables", "keys", "usage", "memory", "maludb"}
    assert all(value == "serving: true" for value in entries.values())
    route = _function("renderRoute")
    assert "pageOf(route.tab).serving && !statusOf(project).serving" in route
    assert "serving || !page.serving" in _function("renderSidebar")
    assert "if (!status.serving)" in _function("projectOverview")


def test_a_ref_reaches_an_address_only_encoded_and_a_name_the_title_only_as_text():
    href = re.search(r"const projectHref = \(ref, tab = \"overview\"\) =>\s*(.+?);\n", _section(), re.S).group(1)
    assert "encodeURIComponent(ref)" in href and "${" not in href
    assert "$(\"#page-title\").textContent = title" in _function("renderPageHeader")
    assert "decodeURIComponent" in _function("parseRoute") and "catch" in _function("parseRoute")


def test_the_page_header_is_not_overridden_by_hidden():
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", CSS)
    assert rule and re.search(r"display:\s*none\s*!important", rule.group(1))


def _interpolations(source: str) -> list[str]:
    """Every `${...}` in `source`, at every depth.

    A regular expression reaches one level of nesting, so a value inside a template inside
    a `.map()` was never looked at: the outer expression was allowed and its contents with
    it. This walks each `${` to its matching brace instead, so nested ones are checked on
    their own.
    """
    found = []
    for start in (m.end() for m in re.finditer(r"\$\{", source)):
        depth = 1
        i = start
        while depth and i < len(source):
            depth += {"{": 1, "}": -1}.get(source[i], 0)
            i += 1
        found.append(source[start:i - 1].strip())
    return found


# Interpolations not escaped at the point of interpolation, each with why that is safe.
SAFE = {
    "id": "icon(id): an icon id, always a literal in this file",
    "ref": "bound to escapeHtml(project.project_ref) -- asserted below",
    "sep": "a literal separator",
    "head": "overviewPlan's header, whose own interpolations are checked here",
    "Number(pct)": "a number",
    'state.creating ? "true" : "false"': "a literal",
    "project.display_name": "document.title, which is text",
    "pageOf(route.tab).label": "document.title, a literal label",
    "CSS.escape(ref)": "a selector, escaped for CSS",
    "pct": "a number, inside a template passed to escapeHtml",
    "show(limit)": "a formatted number, inside a template passed to escapeHtml",
}
# Calls whose result is a template checked here or by that panel's own test. `icon(id)` is
# inside overviewStats, whose every call passes a literal id.
TEMPLATES = re.compile(
    r'icon\("i-[a-z]+"\)|icon\(page\.icon\)|icon\(id\)'
    r"|pageBody\(project, tab\)|sqlEditor\(project\)|tablesBrowser\(project\)"
    r"|keysPanel\(project\)|usagePanel\(project\)|memoryPanel\(project\)|maludbPanel\(project\)"
    r"|projectOverview\(project\)"
    r"|overview(Stats|Key|Plan)\(project\)|billingSummary\(usage\)|usageLimits\(usage\)"
)
# Expressions that only choose or join templates; every `${}` inside them is checked on its own.
CONTAINERS = ("PROJECT_PAGES.filter(", "page.tab === route.tab ?")


def test_every_interpolation_in_the_pages_is_escaped():
    found = _interpolations(_section())
    assert len(found) > 60, "the walk found almost nothing; the check would be vacuous"
    unescaped = []
    for expr in found:
        if expr.startswith("escapeHtml(") or expr in SAFE or TEMPLATES.fullmatch(expr):
            continue
        if expr.startswith(CONTAINERS):
            # Outside its own templates it may hold only literals.
            outside = re.sub(r"`(?:[^`\\]|\\.)*`", "", expr)
            assert "${" not in outside and not re.search(r"\bproject\.|\busage\.|\bkey\b", outside), expr
            continue
        unescaped.append(expr)
    assert unescaped == [], f"unescaped values in the project pages: {unescaped}"
    for function in ("renderProjectView", "pageBody", "projectOverview"):
        bound = re.search(r"const ref = escapeHtml\(project\.project_ref\);", _function(function))
        assert bound, f"{function}: ref must be escaped"
    assert 'const sep = \'<span class="sep" aria-hidden="true">/</span>\';' in _function("renderPageHeader")


def test_the_escaping_check_sees_inside_nested_templates():
    """Guards the check above: a raw value three templates deep must be found."""
    nested = 'x = `${list.map((page) => `<a>${cond ? `<b>${page.label}</b>` : ""}</a>`).join("")}`'
    assert "page.label" in _interpolations(nested)
