"""`specs/extension-allowlist.yaml` is data that decides a privilege (ADR-045).

The installer and the Phase 08 scanner both read it and neither has an opinion
of its own, so a typo here is a security change with no code review attached to
it. These tests are what a reviewer of that file gets for free.

The interesting ones are not "does it parse". They are the two ways a list like
this goes wrong: an entry appearing in both halves, so which one wins depends on
whichever consumer is asking; and an untrusted extension arriving with no
written review, which is how a curated allowlist becomes a list of whatever
somebody needed that week.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

SPEC = pathlib.Path(__file__).resolve().parent.parent / "specs" / "extension-allowlist.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC.read_text())


def test_the_default_is_deny(spec):
    """ADR-010 unchanged: anything absent is refused. A file that defaulted to
    allow would make every extension the node happens to package installable by
    a customer, which is the decision this one exists to *not* make."""
    assert spec["policy"]["default"] == "deny"
    assert spec["policy"]["node_availability"] == "required"


def test_nothing_is_both_allowed_and_denied(spec):
    """Which half wins would otherwise depend on which consumer is asking, and
    the two consumers are an installer and a scanner that must agree."""
    allowed = {entry["name"] for entry in spec["allowed"]}
    denied = {name for entry in spec["denied"] for name in entry["names"]}
    assert not allowed & denied, f"in both halves: {sorted(allowed & denied)}"


def test_every_allowed_extension_says_why_it_is_there(spec):
    """A list of names is a list nobody can review. The `why` is what a future
    reader argues with instead of guessing."""
    for entry in spec["allowed"]:
        assert entry.get("why"), f"{entry['name']} has no reason recorded"


def test_an_untrusted_extension_carries_a_written_review(spec):
    """Criterion 1. `trusted` is upstream's statement that an extension is safe
    for a non-superuser; admitting one without it needs a human to have said so
    in writing, and the file is where they say it."""
    for entry in spec["allowed"]:
        if not entry.get("trusted"):
            assert entry.get("review"), (
                f"{entry['name']} is not trusted and carries no review -- "
                "criterion 1 of ADR-045"
            )


def test_every_refusal_says_which_criterion_it_failed(spec):
    """A refusal a customer cannot understand becomes a support ticket, and one
    the next maintainer cannot understand becomes an entry they delete."""
    for entry in spec["denied"]:
        assert entry.get("reason"), f"{entry['names']} is refused with no reason"


def test_the_classes_that_must_never_be_allowlisted_are_named(spec):
    """Criteria 2 to 4, pinned by example rather than left to judgement.

    These are the refusals that matter on a *shared* node: code execution as the
    database's OS user, a path to another tenant's database, outbound network,
    cluster-wide statistics, and anything wanting a preload slot. If a future
    edit admits one of these, this test is the argument it has to win.
    """
    denied = {name for entry in spec["denied"] for name in entry["names"]}
    for name in (
        "plpython3u",       # code execution as the postgres user
        "plperlu",
        "dblink",           # reaches another database
        "postgres_fdw",     # ADR-014's whole subject
        "file_fdw",         # reaches the filesystem
        "http",             # outbound network from inside the database
        "pg_net",
        "pg_stat_statements",  # cluster-wide, so a window onto other tenants
        "pg_cron",          # background worker plus shared_preload_libraries
        "adminpack",        # raw file access
    ):
        assert name in denied, f"{name} is not refused anywhere in the allowlist"


def test_what_provisioning_already_installs_is_allowlisted(spec):
    """A migrated schema opens with `create extension if not exists pgcrypto`,
    and that must succeed rather than fail on something already present."""
    from services.control_plane import provisioning

    allowed = {entry["name"] for entry in spec["allowed"]}
    for name in provisioning.REQUIRED_EXTENSIONS:
        if name == "maludb_core":
            # The platform's own, installed per ADR-015 and never a customer's
            # to install or reinstall.
            continue
        assert name in allowed, f"provisioning installs {name} but a customer may not"
