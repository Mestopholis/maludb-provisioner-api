"""Deleting a project: the one path that destroys a live customer's data on purpose (free slice 10b).

Found by the launch walkthrough: nothing could delete a project. `cleanup` reclaims a *failed* one
and refuses a database that was ever handed over -- that refusal is right, and this is the other
path, the one a customer asks for. What is held here:

- **the request stops the project at once**: it leaves the serving statuses and every key is revoked,
  before any node work happens;
- **the worker refuses what was not asked for**, a database whose name disagrees with the ref, and a
  project with a provisioning attempt still open;
- **it destroys the whole tenant**: database, *every* per-tenant role -- including the ones a project
  only grows later -- its objects, its stored credentials, and the storage worker's registration;
- **the row survives** with `deleted_at` set, so the ref is never handed out again and the audit
  trail outlives the data;
- **only a manager may ask**, and a member who is not one is refused.
"""

from __future__ import annotations

import argparse
import uuid

import psycopg
import pytest

from services.control_plane import db, jobs, manage, models, provider_keys, provisioning
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


def test_the_roles_a_project_grew_later_are_destroyed_too(project_factory, key_ring, admin_conn):
    """The roles above are the ones every project has. Four more are conditional -- Realtime's
    `replicator`, ADR-077's `vectors`, ADR-079's `memwriter` and `memreader` -- and `_drop_roles`
    carried a written-out list that none of them had joined. The first deletion on the rehearsal
    deployment left `memreader` and `memwriter` behind, `memwriter` being a LOGIN role, while the
    audit recorded six roles dropped and the project DELETED. A project with every role it can have
    is the case that catches that, and the one the old test could not: a freshly provisioned project
    has none of the four, so `LIKE 'mldb_<ref>%'` was empty either way.
    """
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00009")
    names = provisioning.TenantNames.for_ref("del00009")
    provisioning.create_replicator_role(admin_conn, names, password=provisioning.generate_password())
    provisioning.create_vectors_role(admin_conn, names)
    provisioning.create_memreader_role(admin_conn, names)
    provisioning.create_memwriter_role(admin_conn, names, password=provisioning.generate_password())
    for role in names.roles:
        assert provisioning.role_exists(admin_conn, role), role

    with db.connection() as conn:
        models.request_deletion(conn, project_id=project_id, requested_by=None)
        report = jobs.delete_project(conn, admin_conn, project_id=project_id)

    assert set(report.dropped_roles) == set(names.roles), "every per-tenant role, not a subset"
    with admin_conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (f"{names.database}%",))
        assert cur.fetchall() == []
    for shared in ("anon", "authenticated", "service_role"):
        assert provisioning.role_exists(admin_conn, shared), "a cluster-wide role is not this tenant's"


def test_the_stored_credentials_do_not_outlive_the_project(project_factory, key_ring, admin_conn):
    """Provisioning stores one encrypted password per role. Once the roles are dropped they
    authenticate nothing, and what is left is recoverable plaintext for a project the customer
    asked to be rid of -- carried in every control-plane dump, and unwrapped one by one by
    `control-plane verify`. The rehearsal's first deleted project kept all seven.
    """
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00010")
    with db.connection() as conn:
        from services.control_plane import api_keys

        api_keys.create(conn, project_id=project_id, key_type="secret", pepper=b"p" * 32, name="k")
        conn.commit()
        before = db.one(conn, "SELECT count(*) AS n FROM project_credentials WHERE project_id = %s",
                        (project_id,))["n"]
    assert before > 0

    with db.connection() as conn:
        models.request_deletion(conn, project_id=project_id, requested_by=None)
        report = jobs.delete_project(conn, admin_conn, project_id=project_id)

    assert report.credentials_removed == before
    with db.connection() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM project_credentials WHERE project_id = %s",
                      (project_id,))["n"] == 0
        detail = db.one(
            conn,
            "SELECT detail_json FROM audit_events WHERE project_id = %s AND event_type = 'project.deleted'",
            (project_id,),
        )["detail_json"]
        keys = db.query(conn, "SELECT revoked_at FROM api_keys WHERE project_id = %s", (project_id,))
    assert detail["credentials_removed"] == before, "the audit says what was destroyed"
    assert keys and all(k["revoked_at"] is not None for k in keys), (
        "api_keys are stored hashed, so a revoked row is a record rather than a secret: kept, not deleted"
    )


def test_the_customers_provider_keys_do_not_outlive_the_project(project_factory, key_ring, admin_conn):
    """The stronger case of the one above. A `project_credentials` row authenticates a role this job
    has just dropped, so what survived was useless as well as wrong. A model provider key is the
    customer's credential at Anthropic, OpenAI or Voyage: it goes on working, and spending their
    money, after they have deleted the project they gave it to. Nothing removed these -- both
    `set_key` and `remove_key` mark `revoked_at` and keep the ciphertext, which is right for
    rotation and wrong for a project that no longer exists -- so a deleted project left a live
    third-party credential in the control plane and in every dump of it.
    """
    project_id = _provisioned(project_factory, key_ring, admin_conn, "del00011")
    with db.connection() as conn:
        provider_keys.set_key(conn, project_id=project_id, provider="anthropic",
                              api_key="sk-ant-" + "x" * 30, key_ring=key_ring, actor_user_id=None)
        provider_keys.set_key(conn, project_id=project_id, provider="voyage",
                              api_key="pa-" + "y" * 30, key_ring=key_ring, actor_user_id=None)
        conn.commit()
        before = db.one(conn, "SELECT count(*) AS n FROM project_provider_keys WHERE project_id = %s",
                        (project_id,))["n"]
    assert before == 2

    with db.connection() as conn:
        models.request_deletion(conn, project_id=project_id, requested_by=None)
        report = jobs.delete_project(conn, admin_conn, project_id=project_id)

    assert report.provider_keys_removed == 2
    with db.connection() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM project_provider_keys WHERE project_id = %s",
                      (project_id,))["n"] == 0, "a revoked row is not good enough: it is still the key"
        detail = db.one(
            conn,
            "SELECT detail_json FROM audit_events WHERE project_id = %s AND event_type = 'project.deleted'",
            (project_id,),
        )["detail_json"]
    assert detail["provider_keys_removed"] == 2


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
