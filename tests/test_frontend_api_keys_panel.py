"""The dashboard's API keys panel, checked against the contract and the rules it must keep.

`frontend/` has no JavaScript test runner, so what can be checked without a browser is
checked here, against the files:

- **every key call the panel makes is a public route** with that method;
- **a secret key's value is held in one place, briefly**: `state.issuedKey`, filled only
  from the answer to creating it, dropped on dismiss, on closing the panel and on
  sign-out, and never written to storage;
- **a listed key shows its value only when it is publishable** -- the API never lists a
  secret, and the panel must not invent a place to show one;
- **revoking asks first**;
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

KEY_CALLS = [
    ("listApiKeys", "get", "/v1/projects/{project_ref}/api-keys"),
    ("createApiKey", "post", "/v1/projects/{project_ref}/api-keys"),
    ("revokeApiKey", "delete", "/v1/projects/{project_ref}/api-keys/{key_id}"),
]


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"export const {name} = .*?(?=\n\n|\nexport |\Z)", source, re.S)
    assert match, f"api.js has no {name}"
    return match.group(0)


def _panel_source() -> str:
    start = APP_JS.index(" * API keys (Phase 07 slice 2)")
    return APP_JS[start:APP_JS.index(" * Memory spaces (ADR-079)", start)]


def _function(name: str) -> str:
    panel = _panel_source()
    start = panel.index(f"function {name}(")
    following = re.search(r"\n(?:async )?function |\Z", panel[start + 1:])
    return panel[start:start + 1 + following.start()]


def test_every_key_call_is_a_public_route_with_that_method():
    for function, method, path in KEY_CALLS:
        served = path in SPEC["paths"] and method in SPEC["paths"][path]
        assert served, f"{function}: {method.upper()} {path} not served"
        body = _function_source(API_JS, function)
        assert "/api-keys" in body, f"{function} does not call /api-keys"
        if method != "get":
            assert f'"{method.upper()}"' in body, f"{function} does not send {method.upper()}"


def test_the_create_call_sends_what_the_route_accepts():
    body = _function_source(API_JS, "createApiKey")
    assert "key_type: keyType" in body and "name" in body


def test_a_secret_is_held_only_in_issued_key_and_dropped_on_dismiss_close_and_sign_out():
    panel = _panel_source()
    assert "localStorage" not in panel and "sessionStorage" not in panel
    assert "state.issuedKey[ref] = { key_type: issued.key_type, key: issued.key }" in _function("keysForm")
    assert "delete state.issuedKey[" in _function("toggleKeys"), "closing a panel must drop a key on screen"
    assert "delete state.issuedKey[ref]" in _function("keysAction"), "dismissing must drop it"
    signout = APP_JS[APP_JS.index('$("#signout").addEventListener'):]
    assert "state.issuedKey = {}" in signout[:signout.index("toast(")], "sign-out must drop it"


def test_a_listed_key_shows_a_value_only_when_it_is_publishable():
    row = _function("keyRow")
    assert re.search(r'key\.key_type === "publishable" && key\.key', row)
    assert row.count("key.key)") == 1, "the one place a listed value is rendered is the publishable branch"


def test_the_one_time_notice_says_it_cannot_be_retrieved():
    notice = _function("issuedKeyNotice")
    assert "shown once and cannot be retrieved again" in notice


def test_revoking_asks_first():
    action = _function("keysAction")
    revoke = action[action.index("if (button.dataset.keyRevoke)"):]
    assert revoke.index("window.confirm(") < revoke.index("await revokeApiKey(")


# Interpolations not escaped at the point of interpolation, each with why that is safe.
NOT_HTML_OR_ALREADY_ESCAPED = (
    "live.map(",          # rows, whose own interpolations are checked here
    "issued ? issuedKeyNotice(",  # the notice, checked here
    "secret ?",           # a choice between literal strings
    "key.last_used_at ?",  # a conditional choosing templates checked here
    "manager",            # a conditional choosing templates checked here
    "revoked ?",          # a conditional choosing templates checked here
    "value",              # the key line, built from escaped parts above it
    "revoked === 1 ?",    # a literal plural
    "button.dataset.keyLabel",  # confirm() text, not HTML
    "keysHelp(",          # the help, whose own interpolations are checked here
    'live.length ? "" : " open"',  # a literal attribute choice
)


def test_every_interpolation_in_the_panel_html_is_escaped():
    unescaped = []
    for expr in re.findall(r"\$\{([^}]*)\}", _panel_source()):
        expr = expr.strip()
        if not expr or expr.startswith(("escapeHtml(", "CSS.escape(")):
            continue
        if any(expr.startswith(allowed) for allowed in NOT_HTML_OR_ALREADY_ESCAPED):
            continue
        if re.fullmatch(r"projectPath\(ref\)|encodeURIComponent\(keyId\)", expr):
            continue
        if re.fullmatch(r"[a-z]+", expr) and re.search(rf"const {expr} = escapeHtml\(", _panel_source()):
            continue  # a name bound to an escaped value
        unescaped.append(expr)
    assert unescaped == [], f"unescaped values in the API keys panel: {unescaped}"
