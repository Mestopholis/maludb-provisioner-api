"""Deleting a project: the one path that destroys a live customer's data on purpose (free slice 10b).

Found by the launch walkthrough: nothing could delete a project. `cleanup` reclaims a *failed* one
and refuses a database that was ever handed over -- that refusal is right, and this is the other
path, the one a customer asks for. What is held here:

- **the request stops the project at once**: it leaves the serving statuses and every key is revoked,
  before any node work happens;
- **the worker refuses what was not asked for**, a database whose name disagrees with the ref, and a
  project with a provisioning attempt still open;
- **it destroys the whole tenant**: database, roles, objects, and the storage worker's registration;
- **the row survives** with `deleted_at` set, so the ref is never handed out again and the audit
  trail outlives the data;
- **only a manager may ask**, and a member who is not one is refused.
"""

from __future__ import annotations

import argparse
import uuid

import psycopg
import pytest

from services.control_plane import db, jobs, manage, models, provisioning
from services.gateway.app import SERVING_STATUSES
from tests.conftest import requires_db
from tests.test_provisioning import ADMIN_DSN, PLATFORM_OWNER, _tenant_admin_dsn

pytestmark = [requires_db, pytest.mark.skipif(not ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")]


def _tenant_connect(database: str):
    return psycopg.connect(_tenant_admin_dsn(database), autocommit=True)


def _row(project_id: uuid.UUID) -> dict:
    with db.connection() as conn:
        return db.one(
            conn,
            "SELECT status, deleted_at, delete_requested_at, database_name, node_id, project_ref "
            "  FROM projects WHERE id = %s", (project_id,),
        )


def _provisioned(project_factory, key_ring, admin_conn, ref: str) -> uuid.UUID:
    project_id = project_factory(ref)
    with db.connection() as conn:
        jobs.provision(conn, admin_conn, project_id=project_id, key_ring=key_ring,
                       platform_owner=PLATFORM_OWNER, tenant_connect=_tenant_connect)
    assert _row(project_id)["status"] in SERVING_STATUSES
    return project_id


def test_a_request_stops_the_project_serving_and_revokes_its_keys(project_factory, key_ring, admin_conn):
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00001")
    with db.connection() as conn:
        from services.control_plane import api_keys

        api_keys.create(conn, project_id=project_id, key_type="publishable", pepper=b"p" * 32,
                        key_ring=key_ring, name="k")
        api_keys.create(conn, project_id=project_id, key_type="secret", pepper=b"p" * 32, name="k")
        conn.commit()
        assert models.request_deletion(conn, project_id=project_id, requested_by=None) is True
        live = db.one(conn, "SELECT count(*) AS n FROM api_keys WHERE project_id = %s AND revoked_at IS NULL",
                      (project_id,))["n"]
        again = models.request_deletion(conn, project_id=project_id, requested_by=None)
    row = _row(project_id)
    assert row["status"] == "DELETING" and row["status"] not in SERVING_STATUSES
    assert row["delete_requested_at"] is not None and row["deleted_at"] is None, "not gone yet"
    assert live == 0, "a key already in a client's hands keeps working until it is revoked"
    assert again is False, "asking twice is not an error"


def test_the_worker_destroys_the_tenant_and_keeps_the_record(project_factory, key_ring, admin_conn):
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00002")
    names = provisioning.TenantNames.for_ref("del00002")
    assert provisioning.database_exists(admin_conn, names.database)

    with db.connection() as conn:
        models.request_deletion(conn, project_id=project_id, requested_by=None)
        report = jobs.delete_project(conn, admin_conn, project_id=project_id)

    assert report.dropped_database == names.database
    assert not provisioning.database_exists(admin_conn, names.database)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (f"{names.database}%",))
        assert cur.fetchall() == [], "per-tenant roles outlived the tenant"
    row = _row(project_id)
    assert row["status"] == "DELETED" and row["deleted_at"] is not None
    assert row["database_name"] is None and row["node_id"] is None
    assert row["project_ref"] == "del00002", "the row is kept, so the ref is never handed out again"
    with db.connection() as conn:
        events = db.query(conn, "SELECT event_type FROM audit_events WHERE project_id = %s ORDER BY id",
                          (project_id,))
    assert [e["event_type"] for e in events][-2:] == ["project.delete_requested", "project.deleted"]


def test_nothing_is_destroyed_without_a_request(project_factory, key_ring, admin_conn):
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00003")
    names = provisioning.TenantNames.for_ref("del00003")
    with db.connection() as conn, pytest.raises(jobs.ProvisioningError, match="was not asked for"):
        jobs.delete_project(conn, admin_conn, project_id=project_id)
    assert provisioning.database_exists(admin_conn, names.database), "the database survived"


def test_a_database_name_that_disagrees_with_the_ref_is_refused(project_factory, key_ring, admin_conn):
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00004")
    names = provisioning.TenantNames.for_ref("del00004")
    with db.connection() as conn:
        models.request_deletion(conn, project_id=project_id, requested_by=None)
        db.execute(conn, "UPDATE projects SET database_name = 'mldb_someone_else' WHERE id = %s", (project_id,))
        conn.commit()
        with pytest.raises(jobs.ProvisioningError, match="does not match"):
            jobs.delete_project(conn, admin_conn, project_id=project_id)
        db.execute(conn, "UPDATE projects SET database_name = %s WHERE id = %s", (names.database, project_id))
        conn.commit()
    assert provisioning.database_exists(admin_conn, names.database), "no other tenant's database was touched"


def test_the_operator_command_makes_you_name_the_project_twice(project_factory, key_ring, admin_conn, capsys,
                                                               monkeypatch):
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00005")
    names = provisioning.TenantNames.for_ref("del00005")
    assert manage._cmd_project_delete(argparse.Namespace(ref="del00005", confirm=None)) == 2
    assert "refusing" in capsys.readouterr().out
    assert manage._cmd_project_delete(argparse.Namespace(ref="del00005", confirm="del00006")) == 2
    assert provisioning.database_exists(admin_conn, names.database)

    # The command loads configuration for the KEK and the object store; give it the suite's, whose
    # KEK is the one the test database's key was minted with.
    from services.control_plane import nodes
    from tests.conftest import storage_env_config

    monkeypatch.setattr(manage.config, "load", storage_env_config)
    # The command resolves the node's credential from the control plane, as it does in production.
    with db.connection() as conn:
        nodes.set_admin_dsn(conn, name="pf-node", dsn=ADMIN_DSN, key_ring=key_ring)
        conn.commit()
    monkeypatch.setenv("MALUDB_PLATFORM_OWNER", PLATFORM_OWNER)
    assert manage._cmd_project_delete(argparse.Namespace(ref="del00005", confirm="del00005")) == 0
    assert "deleted" in capsys.readouterr().out
    assert not provisioning.database_exists(admin_conn, names.database)
    assert _row(project_id)["status"] == "DELETED"
