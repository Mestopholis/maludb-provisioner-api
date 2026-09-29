"""The legal pages, and the promises they must not make (free slice 9, H-4).

A draft written from what the platform does, not from a template, and these tests hold the parts that
would otherwise drift into a promise the beta cannot keep:

- **no placeholder is left**, and a new one cannot be introduced quietly: the operator's facts were
  supplied on 2026-09-29, so the test that used to insist every blank looked unfinished now insists
  there are none -- and the facts that were filled are held by name, so a later edit cannot blank
  one and stay green;
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
def test_no_placeholder_is_left(page):
    """The blanks were [SQUARE BRACKETS] so none could reach launch unnoticed; they are filled now.

    Reversed rather than deleted. The shape a blank takes is still the thing being checked, so a
    page that grows a new operator fact -- a second processor, another retention period -- fails
    here until somebody supplies it, exactly as the original test intended.
    """
    text = _read(page)
    placeholders = set(re.findall(r"\[([A-Z][A-Z ,./]*[A-Z])(?:,[^\]]*)?\]", text))
    assert not placeholders, f"{page} still asks the operator for {', '.join(sorted(placeholders))}"
    assert "TODO" not in text and "Lorem" not in text
    assert "NOT YET REVIEWED BY A LAWYER" in text, (
        "the pages carry the operator's facts but not a lawyer's read (free step H-4); the source "
        "says so until one happens"
    )


@pytest.mark.parametrize("page", PAGES)
def test_the_operators_facts_are_on_the_pages_that_need_them(page):
    """Named, so a later edit cannot quietly blank one and stay green.

    The entity and its address identify who is contracting and who controls the data; an
    unreachable contact address is the failure mode these pages have -- every one of them asks the
    reader to write somewhere.
    """
    text = _read(page)
    expected = {
        "terms.html": ("Kinetic Seas Inc.", "1501 E. Woodfield Rd, Schaumburg, IL 60173",
                       "the State of Illinois", "Cook County, Illinois",
                       "mailto:support@maludb.org"),
        "privacy.html": ("Kinetic Seas Inc.", "1501 E. Woodfield Rd, Schaumburg, IL 60173",
                         "New York, USA", "InterServer", "mailto:support@maludb.org"),
        "acceptable-use.html": ("the State of Illinois", "mailto:abuse@maludb.org",
                                "mailto:security@maludb.org"),
    }[page]
    missing = [fact for fact in expected if fact not in text]
    assert not missing, f"{page} lost {missing}"


def test_the_retention_the_pages_promise_is_the_one_the_node_is_configured_for():
    """30 days is not a round number: it is `repo1-retention-full=30` on the node, time-based.

    Held here because the sentence "backups already taken are removed in the ordinary rotation
    within 30 days" is a claim about a config file on a machine, and the two can drift apart
    silently. `docs/DEPLOYMENT.md` carries the same number for the same reason; if an operator
    lengthens the node's retention, these pages become untrue and this test is where that shows.
    """
    for page in ("terms.html", "privacy.html"):
        assert "within 30 days" in _read(page), f"{page} no longer states the rotation window"
    deployment = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md").read_text()
    assert "repo1-retention-full=30" in deployment, (
        "DEPLOYMENT no longer pins the retention the legal pages promise"
    )


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
    for processor in ("MaluMail", "Cloudflare", "InterServer"):
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
