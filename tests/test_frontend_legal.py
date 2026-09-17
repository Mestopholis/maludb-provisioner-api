"""The legal pages, and the promises they must not make (free slice 9, H-4).

A draft written from what the platform does, not from a template, and these tests hold the parts that
would otherwise drift into a promise the beta cannot keep:

- **every placeholder is obvious**, so none reaches launch unnoticed;
- **the beta's real limits are stated** on the terms page: one machine, backups beside the data
  (ADR-087), a single copy of uploaded files (ADR-085), restore to the last backup for free projects;
- **the privacy page lists the third parties the code actually calls**, and says a customer's model
  provider keys are theirs;
- **every page is reachable** from the site and from the signup form.
"""

from __future__ import annotations

import pathlib
import re

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
PAGES = ("terms.html", "privacy.html", "acceptable-use.html")


def _read(name: str) -> str:
    return (FRONTEND / name).read_text()


@pytest.mark.parametrize("page", PAGES)
def test_a_placeholder_is_never_quiet(page):
    """[SQUARE BRACKETS] and nothing subtler: a blank a lawyer must fill has to look unfinished."""
    text = _read(page)
    placeholders = set(re.findall(r"\[([A-Z][A-Z ,./]*[A-Z])(?:,[^\]]*)?\]", text))
    assert placeholders, f"{page} claims to need nothing from the operator"
    assert "TODO" not in text and "Lorem" not in text
    assert "DRAFT, and not legal advice" in text, "the draft says so in its own source"


def test_the_terms_state_what_the_beta_does_not_promise():
    terms = _read("terms.html")
    flat = " ".join(terms.split())
    for claim in ("single machine", "backups are kept on that same machine", "single copy",
                  "restored to its most recent backup, not to an arbitrary point in time"):
        assert claim in flat, claim
    assert "as is" in flat and "no uptime commitment" in flat
    for overclaim in ("99.9", "guaranteed uptime", "geo-redundant", "highly available"):
        assert overclaim not in flat, f"the beta does not do {overclaim}"


def test_the_privacy_page_lists_the_processors_the_code_actually_calls():
    privacy = _read("privacy.html")
    for processor in ("MaluMail", "Cloudflare", "[HOSTING PROVIDER]"):
        assert processor in privacy, processor
    assert "with your own API key" in privacy, "memory providers are the customer's, not ours"
    assert "do not sell" in privacy and "not use project data to train models" in privacy
    assert "hash of your password" in privacy and "never the password itself" in privacy


def test_acceptable_use_matches_how_abuse_is_actually_handled():
    aup = _read("acceptable-use.html")
    assert "weekly" in aup, "the cadence the owner committed to (H-5)"
    assert "only reports" in aup and "deliberate action a person takes" in aup


@pytest.mark.parametrize("page", PAGES)
def test_every_page_is_reachable(page):
    for source in ("index.html", "docs.html"):
        assert f'href="./{page}"' in _read(source), f"{page} is not linked from {source}"
    assert 'href="./terms.html"' in _read("index.html")


def test_the_signup_form_says_what_creating_an_account_agrees_to():
    index = _read("index.html")
    form = index[index.index('id="signup-form"'):index.index("</form>", index.index('id="signup-form"'))]
    for page in PAGES:
        assert f'href="./{page}"' in form, f"the signup form does not link {page}"
