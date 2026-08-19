"""Reconciling a project with its plan (Phase 09 slice 0).

The phase turns on a split that was measured before it was planned: an
entitlement resolved per request changes the moment the plan row changes, and
an entitlement written into the node during provisioning never changes at all.
`cp-manage project direct-sql` exists because of the second half and says so in
its own help text.

Two things are asserted here that were not true before this slice:

- **A plan's GUCs reach the roles a customer logs in as.** They were applied to
  `authenticator` and `auth` -- the roles *the platform* logs in as, for
  PostgREST and GoTrue -- and not to `admin` or `executor`. Measured on a
  provisioned paid tenant: a direct connection reported `temp_file_limit = -1`
  against a plan that says 256 MB. ADR-017 found `temp_file_limit` to be one of
  only two of these settings that bind against a client that does not want
  them, so the one control a tenant could not switch off was applied to nobody
  who could.
- **A plan change can be made true afterwards**, idempotently, and a report
  says which way each difference points.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from services.control_plane import (
    db,
    entitlements,
    maintenance,
    nodes,
    plan_apply,
    provisioning,
)
from tests.conftest import requires_db
from tests.test_direct_sql import _as_client, paid_project  # noqa: F401 - fixture
from tests.test_provisioning import ADMIN_DSN, _tenant_dsn, requires_maludb_core

pytestmark = [requires_db]
requires_node = pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")


def _entitlements_for(project_id) -> entitlements.Entitlements:
    with db.connection() as conn:
        return entitlements.for_project(conn, project_id)


def _set_plan_config(project_id, config: dict) -> None:
    with db.connection() as conn:
        db.execute(
            conn,
            "UPDATE plans SET config_json = %s WHERE id = "
            "(SELECT plan_id FROM projects WHERE id = %s)",
            (psycopg.types.json.Jsonb(config), project_id),
        )
        conn.commit()


# -- the pure parts, which need no node ------------------------------------


def test_a_setting_whose_value_contains_a_separator_survives_being_read():
    """`search_path = auth, public` is written by bootstrap 007. A parser that
    split on every `=` would corrupt it, and a reconciler that then wrote it
    back would corrupt the tenant."""
    parsed = plan_apply._parsed(["statement_timeout=8s", "search_path=auth, public"])
    assert parsed == {"statement_timeout": "8s", "search_path": "auth, public"}


def test_a_setting_the_plan_does_not_name_is_left_alone():
    """Otherwise reconciliation removes whatever it does not recognise, which
    includes the auth role's search_path."""
    allowed = entitlements.resolve("free", None)
    names = provisioning.TenantNames.for_ref("recon001")
    observed = {
        role: plan_apply.RoleState(
            name=role, exists=True, can_login=True, connection_limit=10,
            settings={**{k: str(v) for k, v in allowed.postgres_settings().items()},
                      "search_path": "auth, public"},
        )
        for role in provisioning.settings_roles(names)
    }
    observed[names.admin].can_login = False
    observed[names.admin].connection_limit = 0

    found, missing = plan_apply.divergences(allowed, names, observed)

    assert missing == []
    assert [d for d in found if d.setting == "search_path"] == []


def test_an_unset_setting_is_excess_rather_than_withheld():
    """The direction matters for what an operator does about it. An absent
    `temp_file_limit` is not a project missing out -- the cluster default is no
    limit, so the project has more than its plan grants."""
    allowed = entitlements.resolve("free", None)
    names = provisioning.TenantNames.for_ref("recon002")
    observed = {
        role: plan_apply.RoleState(name=role, exists=True, can_login=True, connection_limit=10)
        for role in provisioning.settings_roles(names)
    }
    observed[names.admin].can_login = False
    observed[names.admin].connection_limit = 0

    found, _ = plan_apply.divergences(allowed, names, observed)

    temp_file = [d for d in found if d.setting == "temp_file_limit"]
    assert temp_file, "an unset temp_file_limit must be reported"
    assert all(d.direction == plan_apply.EXCESS for d in temp_file)


def test_a_node_granting_login_the_plan_denies_is_excess():
    """The case a reconciler must not correct on a timer: an operator revoking
    a paid project's access during an incident looks the same as an upgrade
    that has not been applied, and only the direction tells them apart."""
    allowed = entitlements.resolve("free", None)  # direct_database_access: False
    names = provisioning.TenantNames.for_ref("recon003")
    observed = {
        role: plan_apply.RoleState(
            name=role, exists=True, can_login=True, connection_limit=10,
            settings={k: str(v) for k, v in allowed.postgres_settings().items()},
        )
        for role in provisioning.settings_roles(names)
    }

    found, _ = plan_apply.divergences(allowed, names, observed)

    login = next(d for d in found if d.kind == plan_apply.LOGIN)
    assert login.direction == plan_apply.EXCESS


def test_a_missing_role_is_reported_rather_than_created():
    """A project provisioned before `mldb_<ref>_executor` existed is a
    `backfill-executor` problem. Creating a role here would be this module
    quietly taking over provisioning."""
    allowed = entitlements.resolve("free", None)
    names = provisioning.TenantNames.for_ref("recon004")
    observed = {role: plan_apply.RoleState(name=role) for role in provisioning.settings_roles(names)}
    observed[names.authenticator].exists = True

    _, missing = plan_apply.divergences(allowed, names, observed)

    assert names.executor in missing


# -- against a real tenant -------------------------------------------------


@requires_node
@requires_maludb_core
def test_the_plans_settings_reach_the_roles_a_customer_logs_in_as(paid_project, admin_conn):  # noqa: F811
    """The finding this slice starts from, as an assertion.

    Before it, `apply_plan_settings` wrote to `authenticator` and `auth` only,
    so a paid project's direct session ran on the cluster's defaults.
    """
    project_id, names, _ = paid_project("recon101")
    allowed = _entitlements_for(project_id)
    with db.connection() as conn:
        provisioning.apply_plan_settings(admin_conn, names, settings=allowed.postgres_settings())
        admin_conn.commit()
        assert conn is not None

    observed = plan_apply.read_roles(admin_conn, names)

    for role in (names.admin, names.executor, names.authenticator, names.auth):
        assert observed[role].settings.get("temp_file_limit") == \
            allowed.postgres_settings()["temp_file_limit"], role


@requires_node
@requires_maludb_core
def test_a_direct_session_actually_gets_the_limit_rather_than_the_clusters(paid_project, admin_conn):  # noqa: F811
    """Asserted through a real connection rather than off the catalogue.

    A role setting that is present and not applied is the failure mode this
    phase exists to remove, and `pg_db_role_setting` cannot tell the two apart.
    """
    project_id, names, passwords = paid_project("recon102")
    allowed = _entitlements_for(project_id)
    provisioning.apply_plan_settings(admin_conn, names, settings=allowed.postgres_settings())
    admin_conn.commit()

    with _as_client(names, passwords) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SHOW temp_file_limit")
        assert cur.fetchone()["temp_file_limit"] != "-1"
        cur.execute("SHOW max_parallel_workers_per_gather")
        assert cur.fetchone()["max_parallel_workers_per_gather"] == str(
            allowed.max_parallel_workers_per_gather
        )


@requires_node
@requires_maludb_core
def test_a_plan_change_is_invisible_until_it_is_applied_and_then_it_is_not(paid_project, admin_conn):  # noqa: F811
    """The whole slice, end to end: change the plan, see the drift, apply it,
    see it gone."""
    project_id, names, _ = paid_project("recon103")
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))

    _set_plan_config(project_id, {"direct_database_access": True,
                                  "limits": {"work_mem_mb": 64, "database_connections": 42}})
    changed = _entitlements_for(project_id)

    before = plan_apply.inspect(admin_conn, names, changed)
    assert not before.clean
    assert any(d.setting == "work_mem" for d in before.divergences)
    assert any(d.kind == plan_apply.CONNECTION_LIMIT for d in before.divergences)

    applied = plan_apply.apply(admin_conn, names, changed)
    assert applied.corrected

    assert plan_apply.inspect(admin_conn, names, changed).clean


@requires_node
@requires_maludb_core
def test_applying_an_unchanged_plan_is_a_no_op_that_reports_nothing(paid_project, admin_conn):  # noqa: F811
    """Idempotency is not a nicety here: the drift report is meant to be run
    against every project, and an apply that always claimed to have changed
    something would make the report useless."""
    project_id, names, _ = paid_project("recon104")
    allowed = _entitlements_for(project_id)

    plan_apply.apply(admin_conn, names, allowed)
    second = plan_apply.apply(admin_conn, names, allowed)

    assert second.clean
    assert second.corrected == []


@requires_node
@requires_maludb_core
def test_applying_does_not_mint_a_new_credential(paid_project, admin_conn):  # noqa: F811
    """A customer's connection string must survive reconciliation. The password
    was stored at provisioning and enabling access flips an attribute."""
    project_id, names, passwords = paid_project("recon105")

    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))

    with psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"])) as conn:
        conn.execute("SELECT 1")


@requires_node
@requires_maludb_core
def test_a_downgrade_closes_the_door_and_an_upgrade_reopens_it_with_the_same_key(
    paid_project, admin_conn,  # noqa: F811
):
    """Criterion 2 in both directions, and the reason revocation flips an
    attribute rather than rotating a secret: a customer who downgrades and
    upgrades again does not have to reconfigure their application."""
    project_id, names, passwords = paid_project("recon106")
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))

    _set_plan_config(project_id, {"direct_database_access": False})
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"]))

    _set_plan_config(project_id, {"direct_database_access": True})
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))
    with psycopg.connect(_tenant_dsn(names.database, names.client, passwords["client"])) as conn:
        conn.execute("SELECT 1")


@requires_node
@requires_maludb_core
def test_the_auth_roles_search_path_survives_reconciliation(paid_project, admin_conn):  # noqa: F811
    """Bootstrap 007 writes it, the plan knows nothing about it, and a tenant
    whose auth role lost it would answer every GoTrue query against the wrong
    schema."""
    project_id, names, _ = paid_project("recon107")

    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))

    observed = plan_apply.read_roles(admin_conn, names)
    assert "auth" in observed[names.auth].settings.get("search_path", "")


# -- the operator surface --------------------------------------------------


@requires_node
@requires_maludb_core
def test_the_drift_pass_names_a_project_whose_plan_moved_without_it(
    paid_project, admin_conn, key_ring,  # noqa: F811
):
    """The fleet view, and the reason it reports rather than corrects: an
    operator's `direct-sql --disable` during an incident looks identical to an
    upgrade that never landed, and only the direction distinguishes them."""
    project_id, names, _ = paid_project("recon108")
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))
    _set_plan_config(project_id, {"direct_database_access": True,
                                  "limits": {"work_mem_mb": 99}})

    with db.connection() as conn:
        # The pass reaches tenants through the node's stored credential, which
        # the project fixture has no reason to set. Sealed with the suite's own
        # key ring, not the deployment's.
        nodes.set_admin_dsn(conn, name="ds-node", dsn=ADMIN_DSN, key_ring=key_ring)
        conn.commit()
        result = maintenance.report_plan_drift(conn, key_ring=key_ring)

    assert result.handled >= 1
    assert any("recon108" in note for note in result.detail), result.detail
    # And it changed nothing: the node still disagrees after a reporting pass.
    assert not plan_apply.inspect(
        admin_conn, names, _entitlements_for(project_id)
    ).clean


@requires_node
@requires_maludb_core
def test_a_dry_run_reports_and_writes_nothing(paid_project, admin_conn):  # noqa: F811
    """`--dry-run` is the half of the command an operator runs first, and a
    dry run that wrote would be worse than no dry run at all."""
    project_id, names, _ = paid_project("recon109")
    plan_apply.apply(admin_conn, names, _entitlements_for(project_id))
    _set_plan_config(project_id, {"direct_database_access": True,
                                  "limits": {"work_mem_mb": 77}})
    changed = _entitlements_for(project_id)

    before = plan_apply.inspect(admin_conn, names, changed)
    assert not before.clean
    # inspect is what --dry-run calls, so asserting it twice is asserting the
    # command: the second reading must find exactly what the first did.
    after = plan_apply.inspect(admin_conn, names, changed)
    assert [str(d) for d in after.divergences] == [str(d) for d in before.divergences]
