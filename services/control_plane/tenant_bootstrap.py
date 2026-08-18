"""Versioned bootstrap applied inside each tenant database.

`docs/PROVISIONING.md` requires tenant bootstrap SQL to be versioned. Two
places record the version, deliberately:

- `maludb_platform.bootstrap_migrations` inside the tenant database, because
  the tenant is the thing being migrated and must be able to answer "what
  version am I" without the control plane. A restore into temporary
  infrastructure has no control plane at all.
- `projects.bootstrap_version` in the control plane, so a fleet-wide question
  ("which tenants are behind?") does not require connecting to every database.

Bootstrap files are immutable once applied, same rule as the control-plane
migrations: a changed checksum is an error, not a silent divergence between
tenants provisioned at different times.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from services.control_plane import db

log = logging.getLogger(__name__)

BOOTSTRAP_DIR = Path(__file__).parent / "bootstrap"

_TRACKING_TABLE = """
CREATE SCHEMA IF NOT EXISTS maludb_platform;
CREATE TABLE IF NOT EXISTS maludb_platform.bootstrap_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# `pg_event_trigger.evtenabled` has four values, not two. `'D'` is disabled and
# `'O'`/`'A'` fire for ordinary DDL -- but **`'R'` (`ENABLE REPLICA`) fires only
# when `session_replication_role = 'replica'`**, which is to say never, for
# anything a customer does. A check written as `<> 'D'` therefore certifies a
# tenant whose hardening is silently inert. Measured during the slice 6a
# security review: with the allowlist trigger set to `ENABLE REPLICA`, the
# tenant admin installed a deliberately non-allowlisted extension and `verify`
# reported the tenant healthy.
#
# A customer cannot reach that state themselves -- `session_replication_role` is
# `PGC_SUSET` and no `GRANT SET ON PARAMETER` covers it -- but a fleet repair
# script or a mistyped `ALTER EVENT TRIGGER ... ENABLE REPLICA` can, which is
# the case these checks exist for.
_FIRING = ("O", "A")


class BootstrapError(RuntimeError):
    """Tenant bootstrap could not complete."""


def discover() -> list[tuple[str, Path]]:
    return sorted((path.stem, path) for path in BOOTSTRAP_DIR.glob("*.sql"))


def latest_version() -> int:
    """Highest bootstrap number available, for recording against a project."""
    versions = [int(name.split("_", 1)[0]) for name, _ in discover()]
    return max(versions, default=0)


def applied(tenant_conn: psycopg.Connection) -> dict[str, str]:
    with tenant_conn.cursor() as cur:
        cur.execute(_TRACKING_TABLE)
        tenant_conn.commit()
        cur.execute("SELECT version, checksum FROM maludb_platform.bootstrap_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def apply(tenant_conn: psycopg.Connection) -> list[str]:
    """Apply pending bootstrap files to one tenant database.

    Each runs in its own transaction, so a failure leaves no partial version
    recorded. Re-running is a no-op, which makes this safe on a retry path.
    """
    newly_applied: list[str] = []
    seen = applied(tenant_conn)

    for version, path in discover():
        body = path.read_text()
        digest = hashlib.sha256(body.encode()).hexdigest()

        if version in seen:
            if seen[version] != digest:
                raise BootstrapError(
                    f"{version} was applied with a different checksum. Bootstrap files are "
                    "immutable once applied -- add a new one instead."
                )
            continue

        with tenant_conn.transaction():
            with tenant_conn.cursor() as cur:
                cur.execute(body)
                cur.execute(
                    "INSERT INTO maludb_platform.bootstrap_migrations (version, checksum) VALUES (%s, %s)",
                    (version, digest),
                )
        newly_applied.append(version)

    return newly_applied


ALLOWLIST_SPEC = Path(__file__).resolve().parent.parent.parent / "specs" / "extension-allowlist.yaml"


def allowlisted_extensions(spec_path: Path | None = None) -> list[str]:
    """What `specs/extension-allowlist.yaml` currently permits (ADR-045).

    The spec is the authority and this reads it rather than caching a copy, so
    adding an extension stays "a review and a merge" as the ADR says.
    """
    import yaml

    spec = yaml.safe_load((spec_path or ALLOWLIST_SPEC).read_text()) or {}
    return sorted({entry["name"] for entry in spec.get("allowed", [])})


def sync_extension_allowlist(
    tenant_conn: psycopg.Connection, spec_path: Path | None = None
) -> tuple[int, int]:
    """Make one tenant's allowlist table equal the spec. Returns (added, removed).

    **Removal is the half that matters.** Taking an extension off the spec is
    how a security decision gets reversed, and a sync that only ever added
    would leave every tenant provisioned before the change still able to install
    it. Already-installed extensions are untouched -- this governs what may be
    installed next, and dropping a customer's extension out from under their
    schema is a different decision that nothing here is entitled to make.

    Idempotent, because it runs on every provision and on every fleet pass.
    """
    wanted = allowlisted_extensions(spec_path)
    with tenant_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO maludb_platform.allowed_extensions (name) "
            "SELECT unnest(%s::text[]) ON CONFLICT (name) DO NOTHING",
            (wanted,),
        )
        added = cur.rowcount
        cur.execute(
            "DELETE FROM maludb_platform.allowed_extensions WHERE name <> ALL(%s::text[])",
            (wanted,),
        )
        removed = cur.rowcount
    return added, removed


def verify(tenant_conn: psycopg.Connection) -> None:
    """Refuse to consider a tenant bootstrapped unless hardening actually took.

    Checks outcomes rather than trusting that the statements ran, the same way
    provisioning verifies isolation. The ADR-018 revoke is the one that matters:
    a tenant reaching Phase 03 without it exposes extension functions on the
    public Data API.

    Safe to call outside provisioning, and worth calling: this is the check a
    fleet-wide extension upgrade should gate on, and the one that catches a
    tenant whose hardening has drifted since it was provisioned.
    """
    with tenant_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT count(*) AS reachable FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
             WHERE n.nspname = 'public'
               AND (has_function_privilege('anon', p.oid, 'EXECUTE')
                 OR has_function_privilege('authenticated', p.oid, 'EXECUTE'))
            """
        )
        reachable = cur.fetchone()["reachable"]
        if reachable:
            raise BootstrapError(
                f"{reachable} extension functions in public are still executable by anon or "
                "authenticated; ADR-018 hardening did not take"
            )

        # The revoke above is point-in-time: it says nothing about the next
        # extension installed or the next maludb_core upgrade. ADR-015 makes
        # those routine, so the event trigger that re-applies it is part of the
        # hardening, not an optimisation. Only a superuser can drop or disable
        # it, but a superuser-run migration is exactly how it would go missing.
        cur.execute(
            "SELECT evtenabled FROM pg_event_trigger WHERE evtname = 'maludb_harden_extensions'"
        )
        trigger = cur.fetchone()
        if trigger is None:
            raise BootstrapError(
                "the maludb_harden_extensions event trigger is missing; a later CREATE or "
                "ALTER EXTENSION would re-expose extension functions to anon"
            )
        if trigger["evtenabled"] not in _FIRING:
            raise BootstrapError(
                "the maludb_harden_extensions event trigger does not fire for ordinary DDL "
                f"(evtenabled = {trigger['evtenabled']!r})"
            )

        # ADR-045's half of the same argument. Without this trigger the tenant
        # admin holds `CREATE ON DATABASE` -- granted by bootstrap 010 so a
        # migrated schema's own `create extension` line works -- and PostgreSQL
        # alone would then permit *any* extension its packager marked `trusted`,
        # which is a set nobody here reviewed. Checked as an outcome for the
        # same reason as the one above: a superuser-run migration is how it
        # would go missing.
        cur.execute(
            "SELECT evtenabled FROM pg_event_trigger WHERE evtname = 'maludb_allowlist_extensions'"
        )
        allowlist_trigger = cur.fetchone()
        if allowlist_trigger is None:
            raise BootstrapError(
                "the maludb_allowlist_extensions event trigger is missing; the tenant admin "
                "could install any extension the node marks trusted, past ADR-045's allowlist"
            )
        if allowlist_trigger["evtenabled"] not in _FIRING:
            raise BootstrapError(
                "the maludb_allowlist_extensions event trigger does not fire for ordinary DDL "
                f"(evtenabled = {allowlist_trigger['evtenabled']!r})"
            )

        # Not a security property like the one above, but a tenant whose schema
        # changes never reach its API is broken in a way that looks like a
        # platform fault: a table the customer just created returns PGRST205
        # (Phase 00 finding 3). Checked here so a project cannot be handed over
        # in that state.
        cur.execute(
            """
            SELECT count(*) AS present FROM pg_event_trigger
             WHERE evtname IN ('maludb_pgrst_reload_ddl', 'maludb_pgrst_reload_drop')
               AND evtenabled = ANY(%s)
            """,
            (list(_FIRING),),
        )
        if cur.fetchone()["present"] != 2:
            raise BootstrapError(
                "the PostgREST schema-reload event triggers are missing or disabled; tenant DDL "
                "would not reach the Data API"
            )

        for function in ("auth.uid()", "auth.jwt()", "auth.role()", "auth.email()"):
            cur.execute("SELECT to_regprocedure(%s) IS NOT NULL AS present", (function,))
            if not cur.fetchone()["present"]:
                raise BootstrapError(f"{function} is missing; migrated RLS policies depend on it")

    # The legacy claim key returns NULL against PostgREST 14, so a policy built
    # on it fails closed and the tenant looks broken rather than open.
    #
    # Probed inside an explicit transaction: set_config(..., true) is
    # transaction-local, and a caller in autocommit mode has no transaction for
    # it to be local to, so the setting would be gone by the next statement.
    # The check must not depend on how the caller opened the connection.
    with tenant_conn.transaction(), tenant_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            ('{"sub": "11111111-1111-1111-1111-111111111111", "role": "authenticated"}',),
        )
        cur.execute("SELECT auth.uid() IS NOT NULL AND auth.role() = 'authenticated' AS reads_claims")
        reads_claims = cur.fetchone()["reads_claims"]
    if not reads_claims:
        raise BootstrapError("auth helpers do not read request.jwt.claims; RLS would fail closed")


def bootstrap_project(
    conn: psycopg.Connection,
    tenant_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
) -> list[str]:
    """Apply and verify bootstrap, then record the version against the project."""
    try:
        versions = apply(tenant_conn)
        # After `apply`, because the table it fills is created by bootstrap 010,
        # and before `verify`, so a tenant is never recorded as bootstrapped
        # with an empty allowlist -- which would refuse every extension a
        # migrated schema opens with (ADR-045).
        sync_extension_allowlist(tenant_conn)
        tenant_conn.commit()
        verify(tenant_conn)
    except psycopg.Error:
        # Driver text can carry the failing statement; bootstrap SQL does not
        # embed credentials, but the habit is worth keeping consistent.
        log.error("tenant bootstrap failed for project %s", project_id)
        raise BootstrapError("tenant bootstrap failed") from None

    db.execute(
        conn,
        "UPDATE projects SET bootstrap_version = %s WHERE id = %s",
        (latest_version(), project_id),
    )
    conn.commit()
    return versions
