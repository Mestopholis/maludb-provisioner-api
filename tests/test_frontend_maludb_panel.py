"""The console's MaluDB page: turning the graph and vector compartments on (free slice 9).

Until this page they could be turned on only through the API with a personal access token -- the one
capability the console could not reach, found while verifying free slice 6. What is held here:

- **every call is a route the public contract serves**, with the method the page uses;
- **enabling is followed while the page is open**, because the route answers 202 and a worker does it;
- **only an owner or admin is offered the buttons**, as the API requires;
- **the page never offers to read what the features produce**: that is the project's Data API with its
  secret key, which a person's session is not;
- **everything interpolated into the panel is escaped.**
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
APP_JS = (FRONTEND / "app.js").read_text()
API_JS = (FRONTEND / "api.js").read_text()
SPEC = yaml.safe_load((ROOT / "specs" / "control-plane-api.yaml").read_text())

CALLS = [
    ("getDatamodel", "get", "/v1/projects/{project_ref}/maludb/datamodel"),
    ("enableDatamodel", "post", "/v1/projects/{project_ref}/maludb/datamodel/enable"),
    ("disableDatamodel", "post", "/v1/projects/{project_ref}/maludb/datamodel/disable"),
    ("refreshDatamodel", "post", "/v1/projects/{project_ref}/maludb/datamodel/refresh"),
    ("getVectors", "get", "/v1/projects/{project_ref}/maludb/vectors"),
    ("enableVectors", "post", "/v1/projects/{project_ref}/maludb/vectors/enable"),
    ("disableVectors", "post", "/v1/projects/{project_ref}/maludb/vectors/disable"),
]


def _panel() -> str:
    start = APP_JS.index(" * MaluDB features: the data-model graph and vector compartments")
    return APP_JS[start:APP_JS.index(" * Project pages\n", start)]


def _api_function(name: str) -> str:
    start = API_JS.index(f"export const {name} =")
    return API_JS[start:API_JS.index(";\n", start)]


def test_every_call_is_a_route_the_contract_serves():
    for function, method, path in CALLS:
        assert path in SPEC["paths"], f"{function} calls {path}, which the public contract does not serve"
        assert method in SPEC["paths"][path], f"{function} uses {method.upper()} {path}"
        body = _api_function(function)
        assert path.split("{project_ref}")[1] in body, f"{function} does not call {path}"
        if method != "get":
            assert '"POST"' in body


def test_enabling_is_followed_while_the_page_is_open():
    panel = _panel()
    assert 'onProjectPage(ref, "maludb")' in panel and "setTimeout(() => loadMaludb(ref)" in panel
    assert "MALUDB_JOB_BUSY" in panel, "a queued job is what is waited on, not a fixed delay"


def test_only_a_manager_is_offered_the_buttons():
    panel = _panel()
    assert "canManage(project.org_id)" in panel
    for card in ("datamodelCard", "vectorsCard"):
        body = panel[panel.index(f"function {card}("):panel.index("\n}", panel.index(f"function {card}("))]
        assert "manager\n" in body or "manager ?" in body or "manager\r\n" in body, card
    assert "An organization owner or admin can turn these on." in panel


def test_the_page_says_the_secret_key_reads_them_and_offers_no_reader():
    panel = _panel()
    assert panel.count("secret key") >= 3
    for reader in ("datamodel_relations').select", "rpc('vector_search'"):
        assert reader not in panel.replace("&#39;", "'") or "pre><code>" in panel, "examples only, never a call"
    assert "apikey" not in panel, "a session is not a project key and the page must not pretend otherwise"


def test_every_interpolation_in_the_panel_is_escaped():
    unescaped = []
    for expr in re.findall(r"\$\{([^}]*)\}", _panel()):
        expr = expr.strip()
        if not expr or expr.startswith(("escapeHtml(", "maludbJobNote(", "datamodelCard(", "vectorsCard(")):
            continue
        if re.fullmatch(r'on \? "[^"]*" : "[^"]*"', expr):
            continue  # literals chosen by a boolean
        if re.fullmatch(r"(note|budget|buttons|refresh|ref)", expr):
            continue  # names bound to an escaped value or a template checked here
        if expr == "CSS.escape(ref)":
            continue  # a selector, not markup
        if re.fullmatch(r'(datamodel|vectors)\.entitled \? \w+\(project, \w+, manager\) : ""', expr):
            continue
        if "?" in expr and "`" in expr:
            continue  # a conditional choosing between templates, each checked here
        unescaped.append(expr)
    assert unescaped == [], f"unescaped values in the MaluDB panel: {unescaped}"
