"""The merge gate that makes a security review something other than a promise.

Phase 07's plan asked for a review before merge on every slice and four slices
merged without one. Phase 08's plan asked again, named the riskiest slice in
advance, and that slice merged without one. Both catch-up passes found real
findings in shipped code.

So the gate itself is a control, and a control this repository does not assert
is the shape of bug it keeps finding. These tests drive
`scripts/require-security-review.sh` against throwaway repositories, because
the interesting cases are all about what it *lets through*.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "require-security-review.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, a repository this test built
        ["git", *args],  # noqa: S607 - ruff anchors this one on the argv, not the call
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit on `main`, checked out on a branch.

    On a branch rather than on `main` because that is the shape the gate is
    given: a pull request is a range, and a test that committed onto the base
    would hand it an empty one -- which passes for a reason that has nothing to
    do with what is being asserted.
    """
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "gate@example.com")
    _git(path, "config", "user.name", "Gate Test")
    (path / "README.md").write_text("start\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    _git(path, "checkout", "-q", "-b", "work")
    return path


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, body in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def run(repo: Path, base: str = "main", head: str = "HEAD") -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, the script under test
        ["bash", str(GATE), base, head],  # noqa: S607
        cwd=repo, capture_output=True, text=True,
    )


def test_a_code_change_without_a_recorded_review_is_refused(repo):
    """The case both phases actually produced: not a dishonest declaration, an
    absent one that nobody could see the absence of."""
    commit(repo, {"services/control_plane/thing.py": "x = 1\n"}, "feat: a route")

    answered = run(repo)

    assert answered.returncode == 1
    assert "Security-Review:" in answered.stdout + answered.stderr


def test_a_recorded_review_of_none_is_an_answer_and_passes(repo):
    """`none` must be easy to say. A gate that made the honest common case
    expensive would be routed around, and then it would be measuring nothing."""
    commit(
        repo,
        {"services/control_plane/thing.py": "x = 1\n"},
        "feat: a route\n\nSecurity-Review: none\n",
    )

    answered = run(repo)

    assert answered.returncode == 0, answered.stdout + answered.stderr


def test_a_placeholder_is_refused_because_it_reads_as_a_review_later(repo):
    """Worse than an absent trailer: an audit two phases from now counts it."""
    commit(
        repo,
        {"services/control_plane/thing.py": "x = 1\n"},
        "feat: a route\n\nSecurity-Review: TODO\n",
    )

    answered = run(repo)

    assert answered.returncode == 1
    assert "placeholder" in (answered.stdout + answered.stderr)


def test_a_documentation_only_change_needs_no_review(repo):
    """Otherwise the gate is friction on the one kind of change that cannot
    carry a vulnerability, and friction is what teaches people to skip it."""
    commit(repo, {"docs/DECISIONS.md": "an ADR\n"}, "docs: record a decision")

    answered = run(repo)

    assert answered.returncode == 0, answered.stdout + answered.stderr


def test_a_tests_only_change_is_gated_rather_than_exempt(repo):
    """A test is not customer-reachable, and deleting a negative assertion is
    exactly how a control disappears -- ADR-041 found one that had been true by
    accident. The cheaper mistake is asking for a line that says `none`."""
    commit(repo, {"tests/test_thing.py": "def test_x(): pass\n"}, "test: a case")

    answered = run(repo)

    assert answered.returncode == 1


def test_the_review_may_be_recorded_on_any_commit_of_the_change(repo):
    """A branch is several commits, and the review belongs on the one that
    answered it rather than squashed onto whatever landed last."""
    commit(
        repo,
        {"services/control_plane/thing.py": "x = 1\n"},
        "feat: a route\n\nSecurity-Review: none\n",
    )
    commit(repo, {"services/control_plane/thing.py": "x = 2\n"}, "fix: a typo")

    answered = run(repo)

    assert answered.returncode == 0, answered.stdout + answered.stderr


def test_a_review_recorded_before_the_branch_does_not_count_for_it(repo):
    """The one that would make the gate ornamental. Every branch is cut from a
    main whose commits carry these trailers, so a range that reached behind the
    base would pass for every change forever."""
    commit(
        repo,
        {"services/control_plane/earlier.py": "x = 1\n"},
        "feat: something else\n\nSecurity-Review: none\n",
    )
    _git(repo, "branch", "-f", "main", "HEAD")
    commit(repo, {"services/control_plane/thing.py": "x = 1\n"}, "feat: a route")

    answered = run(repo)

    assert answered.returncode == 1


def test_the_trailer_is_matched_case_insensitively(repo):
    """Commit messages are typed by people, and the gate exists to catch a
    review that did not happen rather than one whose trailer was capitalised
    differently."""
    commit(
        repo,
        {"services/control_plane/thing.py": "x = 1\n"},
        "feat: a route\n\nsecurity-review: none\n",
    )

    answered = run(repo)

    assert answered.returncode == 0, answered.stdout + answered.stderr


def test_an_indented_example_in_the_message_does_not_count_as_a_declaration(repo):
    """Found by running the gate against its own first commit, whose message
    explained the convention and therefore quoted it. A trailer is unindented
    -- that is what `git interpret-trailers` means by one -- and a commit
    *describing* a security review is not a commit that had one."""
    commit(
        repo,
        {"services/control_plane/thing.py": "x = 1\n"},
        "docs: explain the convention\n\nWrite it like this:\n\n"
        "    Security-Review: none\n",
    )

    answered = run(repo)

    assert answered.returncode == 1
