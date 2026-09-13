"""CI runs the extension versions the tested list says it does (ADR-075, pinning slice 4).

`specs/extension-versions.yaml` is what `cp-manage node pin set` accepts, and its
header says a version appears there once CI has run the suite against it. Until
this slice that was a claim held by prose: CI installed `postgresql-17-pgvector`
unpinned, so the version it tested was whatever apt.postgresql.org published that
day, and `MALUDB_CORE_REF` agreed with the list only because someone remembered.

Two layers, because they fail in different places:

- The workflow file against the list. No database, so it runs everywhere, and a
  pull request that bumps one without the other fails on the developer's machine.
- What the node under test actually provides against the list. Only CI can be
  held to that -- a developer's node is whatever it is -- so it asserts where
  `MALUDB_REQUIRE_TESTED_VERSIONS` is set and skips, saying what differs, elsewhere.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import psycopg
import pytest
import yaml

from services.control_plane import extension_pins
from tests.conftest import NODE_ADMIN_DSN

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
REQUIRE = os.environ.get("MALUDB_REQUIRE_TESTED_VERSIONS", "").strip() not in ("", "0", "false")


def _newest() -> dict[str, dict]:
    """The newest listed entry per pinned extension, with its package or commit."""
    spec = yaml.safe_load(extension_pins.VERSIONS_SPEC.read_text())
    return {
        extension: max(spec["extensions"][extension], key=lambda e: extension_pins.version_key(str(e["version"])))
        for extension in extension_pins.PINNED
    }


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


# -- the workflow file against the list --------------------------------------


def test_ci_builds_the_newest_listed_maludb_core():
    newest = _newest()["maludb_core"]
    ref = _workflow()["jobs"]["check"]["env"]["MALUDB_CORE_REF"]
    assert ref == newest["commit"], (
        f"MALUDB_CORE_REF is {ref} but the newest maludb_core in {extension_pins.VERSIONS_SPEC.name} "
        f"({newest['version']}) is commit {newest['commit']}; change both in the same pull request"
    )


def test_ci_installs_the_newest_listed_pgvector_exactly():
    newest = _newest()["vector"]
    env = _workflow()["jobs"]["check"]["env"]
    assert env.get("PGVECTOR_PACKAGE_VERSION") == newest["package_version"], (
        f"PGVECTOR_PACKAGE_VERSION is {env.get('PGVECTOR_PACKAGE_VERSION')} but the newest vector "
        f"in {extension_pins.VERSIONS_SPEC.name} ({newest['version']}) is package "
        f"{newest['package_version']}; change both in the same pull request"
    )


def test_the_install_step_uses_the_pinned_package_and_nothing_unpinned():
    """The variable is only worth something if the install uses it. An unpinned
    `postgresql-17-pgvector` anywhere in the workflow installs apt's candidate."""
    text = WORKFLOW.read_text()
    assert 'postgresql-17-pgvector="$PGVECTOR_PACKAGE_VERSION"' in text
    unpinned = [
        line.strip() for line in text.splitlines()
        if not line.strip().startswith("#") and re.search(r"postgresql-17-pgvector(?![=\w-])", line)
    ]
    assert unpinned == [], f"an unpinned pgvector install remains: {unpinned}"


def test_the_workflow_insists_on_the_node_check():
    """Without the flag the node check below skips in CI too, and the list's
    claim decays back into prose without anything failing."""
    assert _workflow()["jobs"]["check"]["env"].get("MALUDB_REQUIRE_TESTED_VERSIONS") == "1"


# -- the node under test against the list ------------------------------------


def _node_differences() -> list[str]:
    newest = _newest()
    with psycopg.connect(NODE_ADMIN_DSN) as conn:
        provided = dict(conn.execute(
            "SELECT name, default_version FROM pg_available_extensions WHERE name = ANY(%s)",
            (list(extension_pins.PINNED),),
        ).fetchall())
    differences = [
        f"{extension}: node provides {provided.get(extension) or 'nothing'}, newest listed is {entry['version']}"
        for extension, entry in newest.items()
        if provided.get(extension) != str(entry["version"])
    ]
    package = newest["vector"]["package"]
    result = subprocess.run(  # noqa: S603 - fixed arguments
        ["dpkg-query", "-W", "-f=${Version}", package],  # noqa: S607
        capture_output=True, text=True, check=False,
    )
    installed = result.stdout.strip() if result.returncode == 0 else None
    if installed != newest["vector"]["package_version"]:
        differences.append(
            f"{package}: {installed or 'not installed'} installed, newest listed is "
            f"{newest['vector']['package_version']}"
        )
    return differences


@pytest.mark.skipif(not NODE_ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")
def test_the_node_under_test_provides_exactly_the_newest_listed_versions():
    differences = _node_differences()
    if differences and not REQUIRE:
        pytest.skip(
            "this node is not at the newest tested versions, which only CI is held to "
            "(MALUDB_REQUIRE_TESTED_VERSIONS): " + "; ".join(differences)
        )
    assert differences == [], (
        "CI ran against versions specs/extension-versions.yaml does not name as newest: "
        + "; ".join(differences)
    )
