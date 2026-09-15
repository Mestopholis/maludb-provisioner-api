"""The Docs page and the console's help say only what is true.

Documentation drifts, and wrong documentation is worse than none: a customer follows it
and blames the product. So the page is held to the sources that establish what works:

- **what it calls working is `supported`** in `specs/compatibility-matrix.yaml` -- the
  status a feature earns only when the official client drives it through the gateway --
  and what it lists as not yet available is not;
- **every platform route it shows is served** by the public API, and every project route
  (`/memory/v1/...`) by the gateway;
- **every docs link from the console lands on a section that exists**;
- **it names no domain it cannot know**: project URLs are placeholders, and the platform
  API is filled in from the address the page is read on;
- **the console's key help never fills in a secret** -- only the publishable key, which
  is public by design.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = (ROOT / "frontend" / "docs.html").read_text()
INDEX = (ROOT / "frontend" / "index.html").read_text()
APP_JS = (ROOT / "frontend" / "app.js").read_text()
GATEWAY = (ROOT / "services" / "gateway" / "app.py").read_text()
SPEC = yaml.safe_load((ROOT / "specs" / "control-plane-api.yaml").read_text())
MATRIX = yaml.safe_load((ROOT / "specs" / "compatibility-matrix.yaml").read_text())["surfaces"]

# What the page presents as working, by the matrix entry that has to back it.
CLAIMED = {
    "data_api": ["select", "insert", "update", "delete", "upsert", "filters", "ordering", "range_limit", "count",
                 "rpc", "rls", "extension_functions_from_sql"],
    "auth": ["signup_password", "signin_password", "session_refresh", "get_user", "signout"],
    "storage": ["buckets", "upload", "download", "delete", "list", "signed_urls", "public_urls"],
    "realtime": ["postgres_changes"],
}
# What the page lists under "Not yet available".
NOT_YET = {
    "auth": ["oauth", "magic_link", "mfa", "enterprise_sso"],
    "storage": ["resumable_upload", "image_transformation", "signed_upload_urls", "s3_protocol"],
    "realtime": ["broadcast", "presence"],
    "database": ["direct_connection_paid"],
}


def _status(surface: str, feature: str) -> str:
    entry = MATRIX[surface]["features"][feature]
    return entry["status"] if isinstance(entry, dict) else entry


def test_what_the_page_calls_working_is_supported_in_the_matrix():
    for surface, features in CLAIMED.items():
        for feature in features:
            assert _status(surface, feature) == "supported", f"docs present {surface}.{feature} as working"


def test_what_the_page_lists_as_not_yet_is_not_supported():
    for surface, features in NOT_YET.items():
        for feature in features:
            assert _status(surface, feature) != "supported", f"{surface}.{feature} is supported; move it out of Not yet"
    not_yet = DOCS[DOCS.index('id="not-yet"'):]
    for words in ("OAuth", "magic links", "MFA", "resumable uploads", "Broadcast", "Presence", "direct connections"):
        assert words in not_yet


def test_every_platform_route_the_page_shows_is_served():
    shown = set(re.findall(r"/api</span>(/v1/[a-z/&;\-]+)", DOCS))
    assert shown, "the page shows platform routes after the /api placeholder"
    for path in shown:
        normalised = re.sub(r"&lt;project-ref&gt;", "{project_ref}", path)
        assert normalised in SPEC["paths"], f"docs show {path}, which the public API does not serve"


def test_every_project_route_the_page_shows_is_one_the_gateway_serves():
    assert 'MEMORY_PREFIX = "/memory/v1"' in GATEWAY
    for part in ('parts[2] == "ingest"', 'parts[2] == "search"', 'parts[0] == "ingests"'):
        assert part in GATEWAY
    for shown in re.findall(r"(/memory/v1/[a-z_/&;\-]+)", DOCS):
        assert re.fullmatch(r"/memory/v1/(spaces/[a-z_]+/(ingest|search)|ingests/&lt;id&gt;)", shown), shown


def test_every_docs_link_from_the_console_lands_on_a_section():
    links = set(re.findall(r'docs\.html#([a-z\-]+)', APP_JS + INDEX))
    assert links >= {"tables", "keys", "memory"}
    for anchor in links:
        assert f'id="{anchor}"' in DOCS, f"the console links to docs.html#{anchor}, which does not exist"
    assert 'href="./docs.html"' in INDEX, "the site links to the docs"


def test_the_page_names_no_domain_it_cannot_know():
    hard_coded = re.search(r"https://[a-z0-9\-]+\.maludb\.com", DOCS)
    assert not hard_coded, "project and platform URLs depend on the deployment"
    assert "window.location.origin" in DOCS and "data-platform-api" in DOCS


def test_the_key_help_fills_in_only_the_publishable_key():
    start = APP_JS.index("function keysHelp(")
    help_source = APP_JS[start:APP_JS.index("function keysPanel(", start)]
    assert 'k.key_type === "publishable" && k.key' in help_source
    assert "issuedKey" not in help_source and "issued" not in help_source
    assert "&lt;secret key&gt;" in help_source
