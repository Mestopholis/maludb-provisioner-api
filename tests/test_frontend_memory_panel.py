"""The dashboard's memory panel, checked against the contract and the rules it must keep.

`frontend/` has no JavaScript test runner, so what can be checked without a
browser is checked here, against the files:

- **every memory call the panel makes is a public route** with that method, in
  `specs/control-plane-api.yaml` -- a panel calling an internal or renamed route
  fails here rather than as a 404 in front of a customer;
- **a provider key is only ever sent**: it is typed into a password field, the
  field is cleared before the request returns, and no rendering path reads a key
  back -- the panel shows the hint the API returns and nothing else;
- **deleting a space asks for its name typed back**, because it cannot be undone;
- **every value interpolated into the panel's HTML is escaped**.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_JS = (ROOT / "frontend" / "api.js").read_text()
APP_JS = (ROOT / "frontend" / "app.js").read_text()
SPEC = yaml.safe_load((ROOT / "specs" / "control-plane-api.yaml").read_text())

MEMORY_CALLS = [
    ("listMemorySpaces", "get", "/v1/projects/{project_ref}/maludb/memory/spaces"),
    ("createMemorySpace", "post", "/v1/projects/{project_ref}/maludb/memory/spaces"),
    ("deleteMemorySpace", "delete", "/v1/projects/{project_ref}/maludb/memory/spaces/{name}"),
    ("setMemoryModels", "put", "/v1/projects/{project_ref}/maludb/memory/spaces/{name}/models"),
    ("listProviderKeys", "get", "/v1/projects/{project_ref}/maludb/memory/provider-keys"),
    ("setProviderKey", "put", "/v1/projects/{project_ref}/maludb/memory/provider-keys/{provider}"),
    ("removeProviderKey", "delete", "/v1/projects/{project_ref}/maludb/memory/provider-keys/{provider}"),
]


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"export const {name} = .*?(?=\n\n|\nexport |\Z)", source, re.S)
    assert match, f"api.js has no {name}"
    return match.group(0)


def _panel_source() -> str:
    start = APP_JS.index("Memory spaces (ADR-079)")
    return APP_JS[start:APP_JS.index("async function loadDashboard", start)]


def test_every_memory_call_is_a_public_route_with_that_method():
    for function, method, path in MEMORY_CALLS:
        assert path in SPEC["paths"], f"{function} calls {path}, which the public contract does not serve"
        assert method in SPEC["paths"][path], f"{function} uses {method.upper()} {path}, which is not served"
        body = _function_source(API_JS, function)
        suffix = path.split("/maludb/memory", 1)[1].split("/{", 1)[0]
        assert suffix in body, f"{function} does not call {suffix}"
        expected = {"get": None, "post": '"POST"', "put": '"PUT"', "delete": '"DELETE"'}[method]
        if expected:
            assert expected in body, f"{function} does not send {method.upper()}"


def test_a_provider_key_is_typed_into_a_password_field_and_cleared_before_it_is_sent():
    panel = _panel_source()
    assert re.search(r'name="api_key" type="password" autocomplete="new-password"', panel)
    cleared = panel.index('form.elements.api_key.value = ""')
    sent = panel.index("await setProviderKey(")
    assert cleared < sent, "the key field must be cleared before the request, so a failure does not leave it"


def test_nothing_renders_a_key_only_its_hint():
    panel = _panel_source()
    assert "key.hint" in panel
    # The listing carries no key field; a template reading one would be rendering a secret.
    assert not re.search(r"\bkey\.(api_key|key|secret|ciphertext)\b", panel)


def test_deleting_a_space_asks_for_its_name_typed_back():
    panel = _panel_source()
    delete = panel[panel.index("if (button.dataset.memoryDelete)"):panel.index("await deleteMemorySpace(")]
    assert "window.prompt(" in delete and "typed.trim() !== name" in delete


# Interpolations that are not escaped at the point of interpolation, each with why that is safe.
NOT_HTML_OR_ALREADY_ESCAPED = {
    # Nested templates whose own interpolations are checked by this same test.
    "keys.providers": "a .map() over templates checked here",
    'spaces.spaces.map((space) => spaceCard(project, space, manager, spaces.models)).join("")': "cards checked here",
    # The model picker: templates whose own interpolations are checked here, a constant,
    # and `m.dimensions`, which only builds `notes` -- escaped where it is interpolated.
    "modelOptions(": "options checked here",
    "modelField(": "fields checked here",
    "OTHER_MODEL": "a constant, `__other__`",
    "m.dimensions": "a number, inside notes that are escaped at interpolation",
    'other ? escapeHtml(current) : ""': "escaped inside the conditional",
    # Selector and plain-text contexts: `toast` sets textContent, `confirm` and `prompt` show text.
    "CSS.escape(ref)": "a CSS selector, escaped for CSS",
    "form.dataset.space": "toast text",
    "PROVIDER_NAMES[provider] || provider": "toast and confirm text",
}


def test_every_interpolation_in_the_panel_html_is_escaped():
    panel = _panel_source()
    unescaped = []
    for expr in re.findall(r"\$\{([^}]*)\}", panel):
        expr = expr.strip()
        if not expr or expr.startswith(("escapeHtml(", "options(", "spaceModels(", "spaceCard(", "providerKeys(")):
            continue
        if any(expr.startswith(allowed) for allowed in NOT_HTML_OR_ALREADY_ESCAPED):
            continue
        if re.fullmatch(r'busy \? "…" : ""|[a-z]+|spaces\.max_spaces === 1 \? " is" : "s are"'
                        r'|v === selected \? " selected" : ""'
                        r'|m\.model === chosen \? " selected" : ""|chosen === OTHER_MODEL \? " selected" : ""'
                        r'|other \|\| !offer \? "" : "hidden"'
                        r'|space\.state === "active" \? spaceModels\(space\) : ""', expr):
            continue  # literals, or a name bound to an escaped value above
        if "?" in expr and "`" in expr:
            continue  # a conditional choosing between templates, each checked here
        unescaped.append(expr)
    assert unescaped == [], f"unescaped values in the memory panel: {unescaped}"


def test_the_model_picker_offers_the_apis_catalog_with_an_other_escape():
    panel = _panel_source()
    assert "spaces.models" in panel, "the picker lists the API's catalog, not a list of its own"
    options = panel[panel.index("function modelOptions("):panel.index("function modelField(")]
    assert "Other model…" in options and "offer.default" in options
    form = panel[panel.index('} else if (kind === "models")'):panel.index('} else if (kind === "key")')]
    assert 'choice === OTHER_MODEL ? String(data.get(`${which}_model_other`)' in form


def test_a_space_holding_memories_shows_its_embedding_model_and_sends_it_back_unchanged():
    """The API refuses the change (409): the picker must not offer it, nor send anything else."""
    panel = _panel_source()
    card = panel[panel.index("function spaceCard("):panel.index("function providerKeys(")]
    assert re.search(r"embeddingLocked = Number\(space\.item_count \|\| 0\) > 0", card)
    assert "search compares" in card and "only vectors from one model" in card
    form = panel[panel.index('} else if (kind === "models")'):panel.index('} else if (kind === "key")')]
    assert "locked ? form.dataset.embeddingProvider" in form and "locked ? form.dataset.embeddingModel" in form
