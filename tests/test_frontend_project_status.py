"""Project status on the dashboard says what it means, and stops being watched once settled.

- **Every status the control plane can record has a label.** The list is read from the
  newest `projects_status_check` in the migrations, so a status added there without a
  label here fails.
- **A ready project is ready whether `PROVISIONED` or `ACTIVE`.** `PROVISIONED` means the
  database is built and its API starts on first use; showing it verbatim, with no panels,
  made working projects look unfinished. Both statuses are the gateway's
  `SERVING_STATUSES`, so both get the panels.
- **Polling follows only what is changing.** It used to run until every project was
  ACTIVE -- every four seconds, indefinitely, for a PROVISIONED project nobody had called.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text()
MIGRATIONS = sorted((ROOT / "services" / "control_plane" / "migrations").glob("*.sql"))
GATEWAY = (ROOT / "services" / "gateway" / "app.py").read_text()


def _statuses_the_database_allows() -> set[str]:
    newest = None
    for path in MIGRATIONS:
        text = path.read_text()
        for match in re.finditer(r"ADD CONSTRAINT projects_status_check\s+CHECK \(status IN \((.*?)\)\)", text, re.S):
            newest = match.group(1)
    assert newest, "no projects_status_check found in the migrations"
    return set(re.findall(r"'([A-Z_]+)'", newest))


def _section(start_marker: str) -> str:
    start = APP_JS.index(start_marker)
    return APP_JS[start:APP_JS.index("/* ----", start)]


def _status_table() -> str:
    start = APP_JS.index("const STATUS = (() => {")
    return APP_JS[start:APP_JS.index("})();", start)]


def _labelled() -> dict[str, str]:
    table = _status_table()
    setup = set(re.findall(r'"([A-Z_]+)"', table[:table.index("const table")]))
    labelled = {s: "setup" for s in setup}
    for status, body in re.findall(r"\b([A-Z_]+): \{([^}]*)\}", table):
        labelled[status] = body
    return labelled


def test_every_status_the_database_allows_has_a_label():
    missing = _statuses_the_database_allows() - set(_labelled())
    assert not missing, f"statuses with no dashboard label: {sorted(missing)}"


def test_provisioned_and_active_are_both_ready_and_both_get_the_panels():
    labelled = _labelled()
    serving = {s for s, body in labelled.items() if body != "setup" and "serving: true" in body}
    gateway = set(re.findall(r'"([A-Z_]+)"', re.search(r"SERVING_STATUSES = \(([^)]*)\)", GATEWAY).group(1)))
    assert serving == gateway == {"PROVISIONED", "ACTIVE"}
    for status in serving:
        assert 'label: "Ready"' in labelled[status]
    cards = _section("function renderProjects()")
    assert "statusOf(p).serving" in cards and 'p.status === "ACTIVE"' not in cards


def test_the_badge_shows_the_label_and_keeps_the_raw_status_as_its_title():
    cards = APP_JS[APP_JS.index("function renderProjects()"):]
    assert 'title="${escapeHtml(p.status)}">${escapeHtml(statusOf(p).label)}' in cards


def test_polling_runs_only_while_something_is_changing():
    load = _section("async function loadDashboard()")
    assert "statusOf(p).moving" in load and not re.search(r"\bPENDING\b", APP_JS)
    assert load.index("clearTimeout(loadDashboard.timer)") < load.index("statusOf(p).moving"), \
        "a settled dashboard must also cancel a poll already scheduled"
    labelled = _labelled()
    for settled in ("PROVISIONED", "ACTIVE", "PAUSED", "SUSPENDED", "DELETED", "FAILED"):
        assert "moving: true" not in labelled[settled], f"{settled} is settled; polling must stop"
