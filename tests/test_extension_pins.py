"""Per-node extension pins and the refusals they drive (ADR-075, pinning slice 1).

The properties that matter are refusals: a node with no pin, one not checked since
it was pinned, one whose packages provide another version, or one with backends
still running a replaced library, takes no project, no move in and no restore --
and a pin cannot be set off the tested list or moved down under tenants. Each is
asserted as an outcome with its control beside it: the same node, agreeing with
its pins, accepting.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from services.control_plane import db, extension_pins, identity, nodes, restore, tenant_movement
from tests.conftest import NODE_ADMIN_DSN, TEST_CREDENTIAL, agree_with_pins, requires_db

TESTED = extension_pins.tested_versions()
NEWEST = {ext: max(TESTED[ext], key=extension_pins.version_key) for ext in extension_pins.PINNED}


# -- the list, and the rule, with no database ------------------------------


def test_the_tested_list_names_both_pinned_extensions_and_nothing_else():
    assert set(TESTED) == {"vector", "maludb_core"}
    assert all(TESTED[ext] for ext in TESTED)


def test_a_list_naming_a_contrib_extension_is_refused(tmp_path: Path):
    spec = tmp_path / "versions.yaml"
    spec.write_text(
        "extensions:\n  vector: [{version: '0.8.4'}]\n  maludb_core: [{version: '0.104.0'}]\n"
        "  pg_trgm: [{version: '1.6'}]\n"
    )
    with pytest.raises(extension_pins.PinError, match="does not pin"):
        extension_pins.tested_versions(spec)


def _pins(**versions) -> dict:
    set_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    return {ext: {"version": v, "set_by": "t", "set_at": set_at} for ext, v in versions.items()}


def _check(*, when: datetime | None = None, stale: int = 0, **provided) -> dict:
    return {"provided": provided, "stale_backends": stale,
            "checked_at": (when or datetime(2026, 9, 13, 12, 5, tzinfo=UTC)).isoformat()}


def test_the_rule_agrees_only_when_everything_does():
    pins = _pins(vector="0.8.4", maludb_core="0.104.0")
    assert extension_pins.rejection_reason(pins, _check(vector="0.8.4", maludb_core="0.104.0")) is None

    assert "no extension pin for maludb_core" in extension_pins.rejection_reason(
        _pins(vector="0.8.4"), _check(vector="0.8.4", maludb_core="0.104.0"))
    assert "never checked" in extension_pins.rejection_reason(pins, None)
    assert "not checked since the pin changed" in extension_pins.rejection_reason(
        pins, _check(when=datetime(2026, 9, 13, 11, 0, tzinfo=UTC), vector="0.8.4", maludb_core="0.104.0"))
    mismatch = extension_pins.rejection_reason(pins, _check(vector="0.8.6", maludb_core="0.104.0"))
    assert "vector is pinned at 0.8.4 but the node provides 0.8.6" in mismatch
    assert "provides nothing" in extension_pins.rejection_reason(pins, _check(maludb_core="0.104.0"))
    assert "replaced vector.so" in extension_pins.rejection_reason(
        pins, _check(stale=2, vector="0.8.4", maludb_core="0.104.0"))


def test_a_replaced_library_is_recognised_from_proc_maps():
    """The line pinning slice 0 read from a backend opened before `apt-get install`."""
    replaced = "7f1c2a000000-7f1c2a01e000 r--p 00000000 fd:00 123 /usr/lib/postgresql/17/lib/vector.so (deleted)\n"
    current = "7f1c2a000000-7f1c2a01e000 r--p 00000000 fd:00 456 /usr/lib/postgresql/17/lib/vector.so\n"
    assert extension_pins.maps_have_replaced_library(replaced)
    assert not extension_pins.maps_have_replaced_library(current)
    assert not extension_pins.maps_have_replaced_library(
        "... /usr/lib/postgresql/17/lib/pgvector_helper.so (deleted)\n")


# -- pins, against the control plane ----------------------------------------


def _node(name: str) -> int:
    with db.connection() as conn:
        node_id = db.one(
            conn,
            "INSERT INTO nodes (name, hostname, internal_host, node_pool, status, last_health_at) "
            "VALUES (%s,%s,%s,'shared','active',now()) "
            "ON CONFLICT (name) DO UPDATE SET status='active', last_health_at=now() RETURNING id",
            (name, f"{name}.example", f"{name}.internal"),
        )["id"]
        db.execute(conn, "DELETE FROM node_extension_pins WHERE node_id = %s", (node_id,))
        db.execute(conn, "UPDATE nodes SET capacity_json = capacity_json - 'extension_check' WHERE id = %s",
                   (node_id,))
        conn.commit()
    return node_id


def _tenant_on(node_id: int, ref: str) -> uuid.UUID:
    with db.connection() as conn:
        _, org = identity.create_user_with_personal_org(
            conn, email=f"{ref}-{uuid.uuid4().hex[:6]}@example.com", password=TEST_CREDENTIAL
        )
        plan = db.one(conn, "INSERT INTO plans (code,name) VALUES ('pin-plan','P') "
                            "ON CONFLICT (code) DO UPDATE SET name='P' RETURNING id")["id"]
        pid = uuid.uuid4()
        db.execute(
            conn,
            "INSERT INTO projects (id, org_id, project_ref, display_name, plan_id, status, node_id, "
            "database_name) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s)",
            (pid, org, ref, ref, plan, node_id, f"mldb_{ref}"),
        )
        conn.commit()
    return pid


@requires_db
def test_a_pin_off_the_tested_list_is_refused_and_writes_nothing(db_pool):  # noqa: ARG001
    node = _node("pin-offlist")
    with db.connection() as conn:
        with pytest.raises(extension_pins.PinError, match="not in extension-versions.yaml"):
            extension_pins.set_pin(conn, node_name="pin-offlist", extension="vector",
                                   version="0.8.99", actor="tester")
        conn.rollback()
        with pytest.raises(extension_pins.PinError, match="only vector, maludb_core"):
            extension_pins.set_pin(conn, node_name="pin-offlist", extension="pg_trgm",
                                   version="1.6", actor="tester")
        conn.rollback()
        assert extension_pins.pins(conn, node) == {}


@requires_db
def test_setting_a_pin_is_audited(db_pool):  # noqa: ARG001
    node = _node("pin-audit")
    with db.connection() as conn:
        result = extension_pins.set_pin(conn, node_name="pin-audit", extension="vector",
                                        version=NEWEST["vector"], actor="tester")
        assert result == {"previous": None, "version": NEWEST["vector"]}
        assert extension_pins.pins(conn, node)["vector"]["set_by"] == "tester"
        event = db.one(
            conn,
            "SELECT actor_type, actor_id, detail_json FROM audit_events "
            "WHERE event_type = %s ORDER BY id DESC LIMIT 1",
            (extension_pins.AUDIT_PIN_SET,),
        )
    assert event["actor_type"] == "staff" and event["actor_id"] == "tester"
    assert event["detail_json"] == {"node": "pin-audit", "extension": "vector",
                                    "from": None, "to": NEWEST["vector"]}


@requires_db
def test_a_pin_cannot_move_down_under_tenants(db_pool):  # noqa: ARG001
    """Pinning slice 0, finding 6: after a package downgrade the tenants' catalogues
    keep the newer version and `ALTER EXTENSION` has no path back."""
    if len(TESTED["vector"]) < 2:
        pytest.skip("needs two tested vector versions")
    low, high = sorted(TESTED["vector"], key=extension_pins.version_key)[:2]
    _node("pin-down")
    with db.connection() as conn:
        extension_pins.set_pin(conn, node_name="pin-down", extension="vector", version=high, actor="t")
        # The control: with no tenants a pin may move down.
        extension_pins.set_pin(conn, node_name="pin-down", extension="vector", version=low, actor="t")
        extension_pins.set_pin(conn, node_name="pin-down", extension="vector", version=high, actor="t")
    with db.connection() as conn:
        node_id = db.one(conn, "SELECT id FROM nodes WHERE name='pin-down'")["id"]
    _tenant_on(node_id, "pindown1")
    with db.connection() as conn:
        with pytest.raises(extension_pins.PinError, match="moving the pin down"):
            extension_pins.set_pin(conn, node_name="pin-down", extension="vector", version=low, actor="t")
        conn.rollback()
        node_id = db.one(conn, "SELECT id FROM nodes WHERE name='pin-down'")["id"]
        assert extension_pins.pins(conn, node_id)["vector"]["version"] == high


# -- the refusals ------------------------------------------------------------


@requires_db
def test_placement_refuses_an_unpinned_node_and_accepts_it_once_it_agrees(db_pool):  # noqa: ARG001
    node = _node("pin-place")
    with db.connection() as conn:
        assert all(c.node_id != node for c in nodes.eligible_nodes(conn))
        reason = nodes.capacity_of(conn, node).rejection_reason()
        assert "no extension pin" in reason

        agree_with_pins(conn, node)
        assert any(c.node_id == node for c in nodes.eligible_nodes(conn))

        # A package that moved under the pin, as the next check would record it.
        db.execute(
            conn,
            "UPDATE nodes SET capacity_json = jsonb_set(capacity_json, "
            "'{extension_check,provided,vector}', '\"0.0.1\"') WHERE id = %s",
            (node,),
        )
        conn.commit()
        assert all(c.node_id != node for c in nodes.eligible_nodes(conn))
        assert "provides 0.0.1" in nodes.capacity_of(conn, node).rejection_reason()


@requires_db
def test_a_pin_change_takes_the_node_out_until_it_is_checked_again(db_pool):  # noqa: ARG001
    """The rollout order ADR-075 names -- pin, then package -- fails safe."""
    node = _node("pin-order")
    with db.connection() as conn:
        agree_with_pins(conn, node)
        assert nodes.capacity_of(conn, node).rejection_reason() is None
        db.execute(conn, "UPDATE node_extension_pins SET set_at = now() + interval '1 second' "
                         "WHERE node_id = %s AND extension = 'vector'", (node,))
        conn.commit()
        assert "not checked since the pin changed" in nodes.capacity_of(conn, node).rejection_reason()


@requires_db
def test_a_move_between_nodes_with_different_pins_is_refused(db_pool):  # noqa: ARG001
    """Pinning slice 0, finding 6: a dump carries no extension version, so a moved
    tenant arrives at the target's. Both nodes agree with their own pins here --
    which is exactly the case the target's own refusal does not cover."""
    if len(TESTED["vector"]) < 2:
        pytest.skip("needs two tested vector versions")
    low, high = sorted(TESTED["vector"], key=extension_pins.version_key)[:2]
    source, target = _node("pin-src"), _node("pin-tgt")
    with db.connection() as conn:
        agree_with_pins(conn, source, versions={"vector": high})
        agree_with_pins(conn, target, versions={"vector": low})
    _tenant_on(source, "pinmove1")

    with db.connection() as conn:
        assert nodes.capacity_of(conn, target).rejection_reason() is None
        refusal = extension_pins.move_refusal(conn, source_node_id=source, target_node_id=target)
        assert f"pinned at {high} on the source and {low} on the target" in refusal
        with pytest.raises(tenant_movement.MovementError, match="pins must match"):
            tenant_movement._project_for_move(  # noqa: SLF001
                conn, project_ref="pinmove1", source_node="pin-src", target_node="pin-tgt"
            )
        conn.rollback()
        agree_with_pins(conn, target, versions={"vector": high})
        assert extension_pins.move_refusal(conn, source_node_id=source, target_node_id=target) is None


@requires_db
def test_a_restore_onto_a_node_that_disagrees_is_refused_before_anything_is_created(db_pool):  # noqa: ARG001
    node = _node("pin-restore")
    project_id = _tenant_on(node, "pinrest1")
    with db.connection() as conn:
        with pytest.raises(restore.RestoreError, match="refusing to restore onto this node: no extension pin"):
            restore.restore_tenant(
                conn, None, project_id=project_id, project_ref="pinrest1", node_id=node,
                stanza="maludb-bk", window=None,
            )
        conn.rollback()
        started = db.one(conn, "SELECT count(*) AS n FROM tenant_restores WHERE project_id = %s",
                         (project_id,))["n"]
    assert started == 0, "a restore row was written before the refusal"


@requires_db
def test_the_capacity_report_names_a_node_that_disagrees(db_pool):  # noqa: ARG001
    from services.control_plane import maintenance

    _node("pin-report")
    with db.connection() as conn:
        over = {row["name"]: row["reason"] for row in maintenance.unenforced_capacity(conn)}
    assert "no extension pin" in over.get("pin-report", "")


# -- the real check, against the node under test -----------------------------


@pytest.mark.skipif(not NODE_ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")
def test_the_check_reads_what_the_node_actually_provides():
    with psycopg.connect(NODE_ADMIN_DSN, autocommit=True) as admin:
        expected = dict(admin.execute(
            "SELECT name, default_version FROM pg_available_extensions "
            "WHERE name IN ('vector', 'maludb_core')").fetchall())
        server = admin.execute("SHOW server_version").fetchone()[0]
        check = extension_pins.inspect_node(admin)
    assert check.provided == {ext: expected.get(ext) for ext in extension_pins.PINNED}
    assert check.server_version == server
    assert "pg_trgm" in check.contrib, "contrib versions are recorded, never pinned"
    # Nothing on this node has had its vector package replaced under it.
    assert check.stale_backends == 0


@requires_db
@pytest.mark.skipif(not NODE_ADMIN_DSN, reason="MALUDB_NODE_ADMIN_DSN is unset")
def test_a_node_pinned_to_what_it_provides_agrees_after_a_real_check(db_pool):  # noqa: ARG001
    with psycopg.connect(NODE_ADMIN_DSN, autocommit=True) as admin:
        check = extension_pins.inspect_node(admin)
    for ext in extension_pins.PINNED:
        if check.provided.get(ext) not in TESTED[ext]:
            pytest.skip(f"this node provides {ext} {check.provided.get(ext)}, which is not listed")
    node = _node("pin-real")
    with db.connection() as conn:
        for ext in extension_pins.PINNED:
            extension_pins.set_pin(conn, node_name="pin-real", extension=ext,
                                   version=check.provided[ext], actor="tester")
        assert "never checked" in nodes.capacity_of(conn, node).rejection_reason()
        extension_pins.record_check(conn, node_name="pin-real", check=check)
        assert nodes.capacity_of(conn, node).rejection_reason() is None
        recorded = db.one(conn, "SELECT capacity_json FROM nodes WHERE id = %s", (node,))
    stamped = datetime.fromisoformat(recorded["capacity_json"]["extension_check"]["checked_at"])
    assert abs(stamped - datetime.now(UTC)) < timedelta(minutes=5)
