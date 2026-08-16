"""Plan entitlements.

The interesting cases are all about input the platform does not control.
`plans.config_json` is operator-supplied, so a malformed value must fall back to
a documented default rather than raise mid-request or -- much worse -- read as
no limit at all. Every assertion here is about which direction a failure goes.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
import yaml

from services.control_plane import db, entitlements, identity
from tests.conftest import TEST_CREDENTIAL, requires_db

# -- the merge -------------------------------------------------------------


def test_an_absent_value_resolves_to_the_tier_default_not_unlimited():
    """The whole point. `plans-and-limits.yaml` shipped with every value null
    meaning 'not yet approved'; read as no-limit, the free tier is unbounded."""
    allowed = entitlements.resolve("free", None)
    assert allowed.emails_per_day == entitlements.DEFAULTS["free"]["emails_per_day"]
    assert allowed.database_storage_bytes > 0
    assert allowed.concurrent_api_requests > 0


@pytest.mark.parametrize(
    "bad",
    ["lots", None, -5, True, [], {}, float("nan")],
)
def test_an_unusable_value_falls_back_rather_than_raising(bad):
    """Operator-supplied JSON reaches this on a request path. Raising would turn
    a bookkeeping mistake into an outage; reading it as unlimited would turn one
    into a free-for-all."""
    allowed = entitlements.resolve("free", {"limits": {"emails_per_day": bad}})
    assert allowed.emails_per_day == entitlements.DEFAULTS["free"]["emails_per_day"]


def test_a_usable_override_is_honoured():
    """Otherwise the defaults are not defaults, they are constants."""
    assert entitlements.resolve("free", {"limits": {"emails_per_day": 7}}).emails_per_day == 7


def test_zero_is_a_real_value_not_a_missing_one():
    """`max_parallel_workers_per_gather: 0` is how the free tier disables
    parallel query. Treating 0 as absent would silently re-enable it."""
    allowed = entitlements.resolve("production", {"limits": {"max_parallel_workers_per_gather": 0}})
    assert allowed.max_parallel_workers_per_gather == 0


def test_an_unknown_plan_resolves_to_the_tightest_tier():
    """A project whose plan cannot be identified must not be handed production
    allowances."""
    assert entitlements.resolve("enterprise-platinum", None).plan_code == "free"
    assert entitlements.resolve(None, None).plan_code == "free"


def test_a_malformed_limits_block_is_ignored_wholesale():
    for config in ({"limits": "everything"}, {"limits": None}, {"limits": []}, {}):
        assert entitlements.resolve("free", config).emails_per_day > 0


# -- what the values are used for ------------------------------------------


def test_postgres_settings_render_as_gucs():
    settings = entitlements.resolve("free", None).postgres_settings()
    assert settings["statement_timeout"].endswith("ms")
    assert settings["work_mem"].endswith("MB")
    assert settings["max_parallel_workers_per_gather"] == "0"


def test_an_unlimited_timeout_is_omitted_not_written_as_zero():
    """PostgreSQL reads 0 as no limit, so writing it on a role would override a
    stricter cluster default rather than inheriting it."""
    settings = entitlements.resolve(
        "free", {"limits": {"statement_timeout_ms": entitlements.UNLIMITED}}
    ).postgres_settings()
    assert "statement_timeout" not in settings


def test_production_has_no_statement_timeout_by_default():
    """A long analytical query is a legitimate workload at that tier; ADR-009
    puts the protection at the gateway and in scheduling instead."""
    assert "statement_timeout" not in entitlements.resolve("production", None).postgres_settings()


def test_the_auth_role_gets_a_small_fixed_connection_allowance():
    """GoTrue opens a handful of connections and does not scale with customer
    traffic. Giving it the project's whole allowance would let Auth exhaust what
    the Data API needs."""
    limits = entitlements.resolve("production", None).connection_limits()
    assert limits["auth"] == entitlements.AUTH_ROLE_CONNECTIONS
    assert limits["authenticator"] > limits["auth"]


def test_the_free_tier_is_tighter_than_the_paid_ones():
    """Not a style preference: the free tier shares a node with everyone else,
    and ADR-022 found connections rather than memory to be the binding
    constraint on how many tenants fit."""
    free = entitlements.resolve("free", None)
    starter = entitlements.resolve("starter", None)
    production = entitlements.resolve("production", None)
    for field in ("database_connections", "postgrest_pool_size", "concurrent_api_requests",
                  "api_requests_per_window", "database_storage_bytes", "emails_per_day"):
        assert getattr(free, field) < getattr(starter, field) <= getattr(production, field), field
    assert free.direct_database_access is False, "ADR-005: free is API-only"


# -- the spec and the code must agree --------------------------------------


def test_the_published_spec_matches_the_resolved_defaults():
    """`specs/plans-and-limits.yaml` exists so the numbers can be read without
    reading code. If it drifts it is worse than absent, because it will be
    believed."""
    spec = yaml.safe_load(open("specs/plans-and-limits.yaml"))
    for code, defaults in entitlements.DEFAULTS.items():
        published = spec["plans"][code]["limits"]
        for key, value in defaults.items():
            if key == "direct_database_access":
                assert spec["plans"][code]["direct_database_access"] == value, code
                continue
            assert published[key] == value, f"{code}.{key}: spec says {published.get(key)}, code says {value}"


# -- resolution from a real project ----------------------------------------


@requires_db
def test_a_project_resolves_through_its_plan(db_pool):
    project_id = uuid.uuid4()
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email="ent0001@example.com", password=TEST_CREDENTIAL
        )
        plan = db.one(
            conn,
            "INSERT INTO plans (code,name,config_json) VALUES ('starter','Starter',%s) "
            "ON CONFLICT (code) DO UPDATE SET config_json = EXCLUDED.config_json RETURNING id",
            (psycopg.types.json.Jsonb({"limits": {"emails_per_day": 42}}),),
        )["id"]
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status) "
            "VALUES (%s,%s,'ent00001','Ent',%s,'ACTIVE')",
            (project_id, org, plan),
        )
        conn.commit()

        allowed = entitlements.for_project(conn, project_id)
    assert allowed.plan_code == "starter"
    assert allowed.emails_per_day == 42, "the stored override did not win"
    assert allowed.postgrest_pool_size == entitlements.DEFAULTS["starter"]["postgrest_pool_size"]


@requires_db
def test_the_schema_forbids_a_project_without_a_plan(db_pool):
    """`for_project` uses a LEFT JOIN and tolerates a missing plan, which reads
    like defence against a reachable state. It is not: `projects.plan_id` is NOT
    NULL with a foreign key, so this asserts the constraint rather than the
    fallback -- and the fallback stays as belt and braces for the case that *is*
    reachable, an id that matches no project at all."""
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email="ent0002@example.com", password=TEST_CREDENTIAL
        )
        with pytest.raises(psycopg.errors.NotNullViolation):
            db.execute(
                conn,
                "INSERT INTO projects (id, org_id, project_ref, display_name, status) "
                "VALUES (%s,%s,'ent00002','Ent','ACTIVE')",
                (uuid.uuid4(), org),
            )
        conn.rollback()


@requires_db
def test_an_unknown_project_resolves_rather_than_raising(db_pool):
    with db.connection() as conn:
        assert entitlements.for_project(conn, uuid.uuid4()).plan_code == "free"
