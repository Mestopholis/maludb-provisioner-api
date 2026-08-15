"""Project refs are untrusted input until validated (AGENTS.md, docs/TENANCY.md).

Phase 02 turns these into real SQL identifiers for databases and roles, so the
validation boundary matters before any of that exists.
"""

from __future__ import annotations

import pytest

from services.control_plane import models


@pytest.mark.parametrize(
    "value",
    [
        "ab12cd34",
        "00000000",
        "zzzzzzzz",
    ],
)
def test_accepts_well_formed_refs(value: str):
    assert models.is_valid_project_ref(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "toolongtoolong",
        "AB12CD34",  # uppercase is outside the alphabet
        "ab-12cd3",  # hyphen
        "ab12cd3;",  # statement terminator
        'ab"12cd3',  # quote, the identifier-escape risk
        "ab 12cd3",  # whitespace
        "ab12cd3\n",
        "drop tabl",
        None,
        12345678,
    ],
)
def test_rejects_malformed_refs(value):
    assert not models.is_valid_project_ref(value)


def test_generated_refs_are_always_valid():
    for _ in range(500):
        assert models.is_valid_project_ref(models.generate_project_ref())


def test_generated_refs_are_not_trivially_repeated():
    refs = {models.generate_project_ref() for _ in range(500)}
    assert len(refs) > 400


def test_database_name_derives_from_a_validated_ref():
    assert models.database_name_for("ab12cd34") == "mldb_ab12cd34"


@pytest.mark.parametrize("value", ['ab"12cd3', "ab12cd3;", "", "AB12CD34"])
def test_database_name_refuses_invalid_refs(value):
    """A generated identifier must never be built from unvalidated input."""
    with pytest.raises(ValueError, match="invalid project_ref"):
        models.database_name_for(value)
