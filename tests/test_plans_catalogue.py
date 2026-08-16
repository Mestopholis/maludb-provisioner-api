"""Seeding the plan catalogue, which nothing did until now.

A freshly deployed control plane could not create a project at all: nothing
inserts into `plans`, `models.default_plan` looks for the code `free`, finds
nothing, and the route answers 503. Every environment that worked had had its
plans inserted by hand or by a test fixture, which is the definition of a step
nobody wrote down.

`cp-manage plans sync` is that step. What it writes is identity -- the code, the
display name, whether the plan is offered -- and not policy: the numbers live in
`entitlements.DEFAULTS` keyed by the same codes, so there is one source for them
rather than two that drift.
"""

from __future__ import annotations

import argparse

import psycopg
import pytest

from services.control_plane import db, entitlements, manage, models
from tests.conftest import requires_db

pytestmark = requires_db


def _sync(with_limits: bool = False) -> int:
    return manage._cmd_plans_sync(argparse.Namespace(with_limits=with_limits))


@pytest.fixture
def empty_catalogue(db_pool):  # noqa: ARG001 - db_pool truncates and prepares
    with db.connection() as conn:
        assert db.query(conn, "SELECT code FROM plans") == [], "the catalogue was not empty"


def test_a_fresh_deployment_has_no_default_plan_until_it_is_seeded(empty_catalogue):  # noqa: ARG001
    """The failure this command exists for, stated as a test.

    Without a default plan the create-project route answers 503 -- correctly,
    and unhelpfully, for an operator who has no idea a seeding step exists.
    """
    with db.connection() as conn:
        assert models.default_plan(conn) is None

    assert _sync() == 0

    with db.connection() as conn:
        default = models.default_plan(conn)
    assert default is not None
    assert default.code == "free"


def test_sync_seeds_every_plan_the_spec_lists(empty_catalogue):  # noqa: ARG001
    assert _sync() == 0
    with db.connection() as conn:
        codes = {row["code"] for row in db.query(conn, "SELECT code FROM plans WHERE is_active")}
    assert {"free", "starter", "production"} <= codes


def test_sync_is_idempotent(empty_catalogue):  # noqa: ARG001
    """An operator runs it on every deployment, not once."""
    _sync()
    with db.connection() as conn:
        first = db.query(conn, "SELECT code, name FROM plans ORDER BY code")
    _sync()
    with db.connection() as conn:
        second = db.query(conn, "SELECT code, name FROM plans ORDER BY code")
    assert first == second


def test_sync_leaves_the_numbers_to_entitlements(empty_catalogue):  # noqa: ARG001
    """Identity, not policy.

    Writing the spec's limits into `config_json` would put the numbers in two
    places -- here and in `entitlements.DEFAULTS` -- and every change to the
    defaults would then need a re-sync to take effect. `config_json` is for a
    deployment that wants to *override* them, which is a different intent.
    """
    _sync()
    with db.connection() as conn:
        row = db.one(conn, "SELECT config_json FROM plans WHERE code = 'free'")
        plan = models.plan_by_code(conn, "free")
    assert row["config_json"] == {}
    # And the entitlement still resolves to the free tier's real numbers.
    assert entitlements.resolve(plan.code, plan.config).max_projects == 2


def test_pinning_the_limits_is_available_but_must_be_asked_for(empty_catalogue):  # noqa: ARG001
    _sync(with_limits=True)
    with db.connection() as conn:
        row = db.one(conn, "SELECT config_json FROM plans WHERE code = 'free'")
    assert row["config_json"]["limits"]["max_projects"] == 2


def test_sync_does_not_overwrite_a_deployment_s_own_overrides(empty_catalogue):  # noqa: ARG001
    """An operator who raised a limit for one deployment must not lose it to a
    routine re-sync -- which is exactly what a nightly bring-up script does."""
    _sync()
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE code = 'free'",
            (psycopg.types.json.Jsonb({"limits": {"max_projects": 7}}),),
        )
        conn.commit()

    _sync()

    with db.connection() as conn:
        plan = models.plan_by_code(conn, "free")
    assert entitlements.resolve(plan.code, plan.config).max_projects == 7, (
        "a routine sync discarded a deployment's own override"
    )


def test_a_plan_that_leaves_the_spec_is_retired_rather_than_deleted(empty_catalogue):  # noqa: ARG001
    """Projects reference plans. Deleting a row would either fail a foreign key
    or orphan somebody's project, and neither is a thing a sync should risk."""
    with db.connection() as conn:
        db.execute(
            conn,
            "INSERT INTO plans (code, name, config_json, is_active) "
            "VALUES ('legacy','Legacy','{}', TRUE)",
        )
        conn.commit()

    _sync()

    with db.connection() as conn:
        row = db.one(conn, "SELECT is_active FROM plans WHERE code = 'legacy'")
    assert row is not None, "a plan was deleted rather than retired"
    assert row["is_active"] is False
    # And a retired plan is no longer offered.
    with db.connection() as conn:
        assert models.plan_by_code(conn, "legacy") is None


def test_listing_an_empty_catalogue_says_what_to_run(empty_catalogue, capsys):  # noqa: ARG001
    assert manage._cmd_plans_list(argparse.Namespace()) == 1
    assert "plans sync" in capsys.readouterr().out


def test_listing_shows_what_each_plan_actually_grants(empty_catalogue, capsys):  # noqa: ARG001
    """The numbers an operator wants are the resolved ones, not the stored ones:
    an empty `config_json` does not mean an unlimited plan."""
    _sync()
    assert manage._cmd_plans_list(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "free" in out
    assert "projects=2" in out
    assert "direct_db=False" in out
    assert "direct_db=True" in out, "the paid tiers should be visible too"
