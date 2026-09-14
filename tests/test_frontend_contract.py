"""The dashboard calls only routes the public control plane serves.

`frontend/api.js` is hand-written against the API with no generated client, so a
renamed route or a wrong prefix fails only in a browser -- and for billing, only
after a customer pressed the button. The usage routes, for one, are mounted
under `/v1` while the upgrade-request route's own path looks unprefixed in
`api/usage.py`; this reads the paths the page actually requests and checks each
against the public application (ADR-037), the one a browser can reach.
"""

from __future__ import annotations

import pathlib
import re

from services.control_plane.main import create_public_app
from tests.test_control_plane_surfaces import _paths

API_JS = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "api.js"

_PARAM = re.compile(r"\{[^}]+\}")


def _normalise(path: str) -> str:
    return _PARAM.sub("{}", path.split("?", 1)[0].rstrip("/"))


def _requested_paths() -> set[str]:
    source = API_JS.read_text()
    helper = re.search(r"const projectPath = \([^)]*\) => `([^`]+)`", source)
    if helper:
        source = source.replace("${projectPath(ref)}", helper.group(1))
    found = set()
    for literal in re.findall(r"[`\"'](/v1/[^`\"']*)[`\"']", source):
        found.add(_normalise(re.sub(r"\$\{[^}]+\}", "{x}", literal)))
    return found


def _served_paths(app_config) -> set[str]:
    # Walked, not read off `app.routes` -- see `_paths`: included routers nest.
    return {_normalise(path) for path in _paths(create_public_app(app_config))}


def test_the_parse_finds_the_routes_the_dashboard_uses():
    """Guards the next test from passing on an empty parse."""
    requested = _requested_paths()
    for expected in ("/v1/auth/signin", "/v1/projects/{}/usage", "/v1/projects/{}/billing/checkout"):
        assert expected in requested, (expected, sorted(requested))


def test_every_path_the_dashboard_requests_is_served_publicly(app_config):
    served = _served_paths(app_config)
    assert len(served) > 20, "the route walk found almost nothing; the comparison would be vacuous"
    missing = sorted(_requested_paths() - served)
    assert missing == [], f"frontend/api.js requests routes the public app does not serve: {missing}"
