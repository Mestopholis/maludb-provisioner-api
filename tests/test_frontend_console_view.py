"""The signed-in console is its own view, and `hidden` actually hides.

`frontend/` has no JavaScript test runner, so what is checked here is checked against
the files:

- **Signing in replaces the sales page** rather than revealing the dashboard inside its
  signup section: the console is a `main` of its own, outside the marketing one, and
  `renderSession` swaps the two and the header's links for the account.
- **`[hidden]` wins over every class.** The browser's rule for the attribute has the
  lowest specificity there is, and `.usage-panel { display: grid }` overrode it -- every
  project card showed its Memory panel stuck on "Loading…" before it was opened.
"""

from __future__ import annotations

import pathlib
import re
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent / "frontend"
HTML = (ROOT / "index.html").read_text()
APP_JS = (ROOT / "app.js").read_text()
CSS = (ROOT / "styles.css").read_text()


class _Tree(HTMLParser):
    """Each element with an id, and the ids of the elements it sits inside."""

    VOID = {"meta", "link", "input", "br", "img", "hr"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, str | None]] = []
        self.parents: dict[str, list[str]] = {}
        self.tags: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        element_id = dict(attrs).get("id")
        if element_id:
            self.parents[element_id] = [i for _, i in self.stack if i]
            self.tags[element_id] = tag
        if tag not in self.VOID:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


TREE = _Tree()
TREE.feed(HTML)


def test_hidden_is_not_overridden_by_a_class():
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", CSS)
    assert rule, "styles.css needs a [hidden] rule; the browser's own loses to any class setting display"
    assert re.search(r"display:\s*none\s*!important", rule.group(1))


def test_the_console_is_a_view_of_its_own_not_part_of_the_sales_page():
    assert TREE.tags["console"] == "main" and TREE.tags["top"] == "main"
    assert "top" not in TREE.parents["console"], "the console must not sit inside the marketing page"
    for inside in ("dashboard", "project-grid", "create-project-form", "refresh"):
        assert "console" in TREE.parents[inside], f"#{inside} belongs to the console"
    assert "start" not in TREE.parents["dashboard"], "the dashboard is no longer revealed under the signup section"


def test_the_account_and_sign_out_live_in_the_header():
    for element in ("account-email", "account-name", "signout"):
        assert "nav-account" in TREE.parents[element], f"#{element} belongs in the header's account area"
    assert "console" not in TREE.parents["nav-account"] and "top" not in TREE.parents["nav-account"]
    assert re.search(r'<div class="nav-account" id="nav-account" hidden>', HTML), "hidden until signed in"


def test_render_session_swaps_the_views():
    body = APP_JS[APP_JS.index("function renderSession()"):APP_JS.index("function renderPlans()")]
    for swap in ('$("#top").hidden = signedIn', '$("#console").hidden = !signedIn',
                 '$("#nav-links").hidden = signedIn', '$("#nav-account").hidden = !signedIn'):
        assert swap in body, f"renderSession must do: {swap}"
    assert "scrollTo(0, 0)" in body, "arriving in the console starts at its top, not at the old #start offset"


def test_the_create_form_offers_only_a_plan_the_api_will_accept():
    """`/v1/plans` is the whole catalogue and `POST .../projects` accepts only the self-serve
    plan, answering anything else `404 unknown plan` so it cannot be used to map the catalogue.
    The form rendered every plan as an option, so two of the three were dead ends whose failure
    read as a broken platform -- found on the launch signup pass. The filter is the fix, and the
    flag comes from the control plane rather than the page knowing the string "free".
    """
    start = APP_JS.index('const planSelect = $("#project-plan");')
    block = APP_JS[start:APP_JS.index("clearTimeout(loadDashboard.timer)", start)]
    assert "state.plans.filter((p) => p.self_serve)" in block
    assert "selfServe" in block and "state.plans\n" not in block, "options come from the filtered list"
    assert '"free"' not in block, "which plan is self-serve is the control plane's to say"
    assert 'id="project-plan-note"' in HTML, "and the rest of the catalogue is accounted for in words"
