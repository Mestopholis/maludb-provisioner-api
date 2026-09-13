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

# ADR-076. Bootstrap 013 creates the Data API check; a worker names it in
# `db-pre-request` once this version is recorded against its project.
RPC_CHECK_VERSION = 13
RPC_CHECK_FUNCTION = "maludb_guard.refuse_extension_rpc"

# Files that make extension functions executable by customer roles, which is
# only safe once the tenant's PostgREST refuses them as RPC. `apply` stops before
# the first of these unless told the check is live: a new tenant has no worker
# yet, and a serving one waits for the fleet run that reloads its worker and
# confirms the refusal (grants slice 2). Stops rather than skips, so nothing
# after one of these is applied out of order.
REQUIRES_LIVE_RPC_CHECK = frozenset({"014_extension_function_grants"})

# The six roles ADR-076 decision 4 grants to, as suffixes of the tenant database
# for the three per-tenant ones.
_SHARED_CUSTOMER_ROLES = ("anon", "authenticated", "service_role")
_TENANT_CUSTOMER_SUFFIXES = ("_admin", "_client", "_executor")


class BootstrapError(RuntimeError):
    """Tenant bootstrap could not complete."""


def discover() -> list[tuple[str, Path]]:
    return sorted((path.stem, path) for path in BOOTSTRAP_DIR.glob("*.sql"))


def latest_version() -> int:
    """Highest bootstrap number available, for recording against a project."""
    versions = [int(name.split("_", 1)[0]) for name, _ in discover()]
    return max(versions, default=0)


def applied_version(tenant_conn: psycopg.Connection) -> int:
    """The highest bootstrap number recorded in this tenant."""
    return max((int(name.split("_", 1)[0]) for name in applied(tenant_conn)), default=0)


def applied(tenant_conn: psycopg.Connection) -> dict[str, str]:
    with tenant_conn.cursor() as cur:
        cur.execute(_TRACKING_TABLE)
        tenant_conn.commit()
        cur.execute("SELECT version, checksum FROM maludb_platform.bootstrap_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def apply(tenant_conn: psycopg.Connection, *, rpc_check_live: bool = False) -> list[str]:
    """Apply pending bootstrap files to one tenant database.

    Each runs in its own transaction, so a failure leaves no partial version
    recorded. Re-running is a no-op, which makes this safe on a retry path.

    `rpc_check_live` says the tenant's PostgREST already refuses extension
    functions as RPC, or that the tenant has no worker at all. Without it, `apply`
    stops before the files in `REQUIRES_LIVE_RPC_CHECK`: applying one to a
    serving tenant whose worker lacks the check reopens ADR-018's finding.
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

        if version in REQUIRES_LIVE_RPC_CHECK and not rpc_check_live:
            log.info("bootstrap %s held until the tenant's Data API check is live", version)
            break

        with tenant_conn.transaction():
            _run_file(tenant_conn, version, body, digest)
        newly_applied.append(version)

    return newly_applied


def _run_file(tenant_conn: psycopg.Connection, version: str, body: str, digest: str) -> None:
    with tenant_conn.cursor() as cur:
        cur.execute(body)
        cur.execute(
            "INSERT INTO maludb_platform.bootstrap_migrations (version, checksum) VALUES (%s, %s)",
            (version, digest),
        )


def apply_held(tenant_conn: psycopg.Connection) -> list[str]:
    """Apply the files `apply` holds back, and verify, in one transaction.

    For the grants fleet run (ADR-076 grants slice 2), once it has shown the
    tenant's Data API check live. One transaction because a verification that
    fails after the grants committed could only report exposure, not undo it.

    Refuses unless every file before the held ones is already applied -- the
    caller runs `apply` first -- so nothing else rides along unverified. Files
    after the held ones are left for `apply`.
    """
    seen = applied(tenant_conn)
    held: list[tuple[str, str, str]] = []
    for version, path in discover():
        if version in seen:
            continue
        if version not in REQUIRES_LIVE_RPC_CHECK:
            if held:
                break
            raise BootstrapError(
                f"{version} is pending ahead of the held grants; run apply() first so it is "
                "applied and verified on its own"
            )
        body = path.read_text()
        held.append((version, body, hashlib.sha256(body.encode()).hexdigest()))
    if not held:
        return []

    try:
        with tenant_conn.transaction():
            for version, body, digest in held:
                _run_file(tenant_conn, version, body, digest)
            verify(tenant_conn)
    except psycopg.errors.RaiseException as exc:
        raise BootstrapError(exc.diag.message_primary or "bootstrap refused") from None
    return [version for version, _, _ in held]


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
        _verify_extension_function_posture(cur)

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

        # Phase 10 slice 1. Checked as outcomes for the same reason as
        # everything above it: bootstrap 012 derives the storage role's name
        # from `current_database()`, and a tenant whose name did not derive the
        # way provisioning expected would apply cleanly and be wrong.
        cur.execute(
            """
            SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname = 'storage'
            """
        )
        schema = cur.fetchone()
        if schema is None:
            raise BootstrapError(
                "the storage schema is missing; upstream storage-api would create it itself on "
                "first connection, owned by whoever connected and outside the platform's control"
            )
        cur.execute("SELECT current_database() || '_storage' AS expected")
        expected_owner = cur.fetchone()["expected"]
        if schema["owner"] != expected_owner:
            raise BootstrapError(
                f"the storage schema is owned by {schema['owner']!r} rather than "
                f"{expected_owner!r}; upstream's migrations would run as the wrong role and the "
                "tables they create would be owned by it"
            )

        # The grant upstream's migrations do not make when DB_INSTALL_ROLES is
        # false. Its absence is not subtle in production -- every Storage
        # request answers 403 -- but it is invisible until a project first uses
        # Storage, which may be months after the tenant was provisioned.
        cur.execute(
            """
            SELECT count(*) AS granted FROM unnest(ARRAY['anon','authenticated','service_role']) r
             WHERE has_schema_privilege(r, 'storage', 'USAGE')
            """
        )
        if cur.fetchone()["granted"] != 3:
            raise BootstrapError(
                "anon, authenticated and service_role do not all hold USAGE on the storage "
                "schema; every Storage request would answer 403 AccessDenied"
            )

        # The tenant admin is the customer's ceiling inside their own database
        # and `storage` is service-owned bookkeeping whose consistency with the
        # object store is the platform's responsibility. Asserted rather than
        # assumed because bootstrap 010 grants that role CREATE ON DATABASE,
        # which is a broader privilege than it looks.
        cur.execute(
            "SELECT has_schema_privilege(current_database() || '_admin', 'storage', 'USAGE') AS reachable"
        )
        if cur.fetchone()["reachable"]:
            raise BootstrapError(
                "the tenant admin role holds USAGE on the storage schema; object metadata is "
                "reachable from customer SQL past every bucket policy"
            )

        # Bootstrap 012's hardening runs when upstream's migrations create the
        # tables, which is long after bootstrap. Without the trigger the schema
        # is hardened once, while empty, and never again -- bootstrap 003's
        # mistake, which is why 005 exists.
        cur.execute(
            "SELECT evtenabled FROM pg_event_trigger WHERE evtname = 'maludb_harden_storage'"
        )
        storage_trigger = cur.fetchone()
        if storage_trigger is None:
            raise BootstrapError(
                "the maludb_harden_storage event trigger is missing; storage-api's migrations "
                "would create tables in the storage schema with no hardening applied"
            )
        if storage_trigger["evtenabled"] not in _FIRING:
            raise BootstrapError(
                "the maludb_harden_storage event trigger does not fire for ordinary DDL "
                f"(evtenabled = {storage_trigger['evtenabled']!r})"
            )

        # Vacuously true on a tenant the storage worker has never served, and
        # the point is that it stays true once it has. Upstream enables RLS on
        # every table it creates; this refuses to certify a tenant where a
        # future migration did not, because bootstrap 004's grant posture
        # (ADR-018) means a table in `storage` without RLS is readable by
        # anyone holding the project's publishable key.
        cur.execute(
            """
            SELECT count(*) AS unguarded FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'storage' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity
            """
        )
        unguarded = cur.fetchone()["unguarded"]
        if unguarded:
            raise BootstrapError(
                f"{unguarded} table(s) in the storage schema have row-level security disabled; "
                "storage policies are RLS policies, so those rows are ungoverned"
            )

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


# One query shape for "extension-owned functions, and which extension", used by
# every check below. `classid` constrained because an objid is only unique within
# its own catalogue.
_EXTENSION_FUNCTIONS = """
    SELECT p.oid, p.oid::regprocedure::text AS signature, p.proowner, p.proacl, e.extname
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
      JOIN pg_extension e ON e.oid = d.refobjid
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
"""


def _recorded_versions(cur) -> set[str]:
    """Bootstrap versions recorded in the tenant, read without side effects.

    Not `applied()`, which creates the ledger and commits: `verify` runs inside
    the extension upgrade's per-tenant transaction, and a commit here would make
    that upgrade permanent before it had been verified.
    """
    cur.execute("SELECT to_regclass('maludb_platform.bootstrap_migrations') IS NOT NULL AS present")
    if not cur.fetchone()["present"]:
        return set()
    cur.execute("SELECT version FROM maludb_platform.bootstrap_migrations")
    return {row["version"] for row in cur.fetchall()}


def _verify_extension_function_posture(cur) -> None:
    """ADR-018 before bootstrap 014, ADR-076 after it -- and both halves always.

    A tenant is legitimately in either state while grants slice 2's fleet run is
    in progress, so which property is asserted follows what the tenant records.
    """
    versions = _recorded_versions(cur)
    customer_roles_sql = """
        SELECT r.rolname FROM pg_roles r
         WHERE r.rolname IN ('anon', 'authenticated', 'service_role',
                             current_database() || '_admin',
                             current_database() || '_client',
                             current_database() || '_executor')
    """

    # Always: maludb_core's functions are MaluDB's, reached through its own roles
    # and ADR-074's platform copy. 94 are SECURITY DEFINER, owned by the node
    # superuser; some in `mc2db`, which PUBLIC can reach, write the MCP registry.
    cur.execute(
        f"""
        SELECT f.signature, r.rolname FROM ({_EXTENSION_FUNCTIONS}) f
          CROSS JOIN ({customer_roles_sql}) r
         WHERE f.extname = 'maludb_core' AND has_function_privilege(r.rolname, f.oid, 'EXECUTE')
         LIMIT 3
        """  # noqa: S608 - module constants, no input
    )
    leaked = cur.fetchall()
    if leaked:
        sample = ", ".join(f"{row['signature']} to {row['rolname']}" for row in leaked)
        raise BootstrapError(
            f"maludb_core functions are executable by customer roles ({sample}); they are "
            "MaluDB's own surface and must stay reachable only through its roles (ADR-076)"
        )

    # Always: nothing extension-owned carries PUBLIC's grant. Under ADR-018 that
    # was the revoke itself; under ADR-076 the grant is explicit, and a PUBLIC
    # grant is every role in the cluster rather than the six the platform names.
    cur.execute(
        f"""
        SELECT f.signature FROM ({_EXTENSION_FUNCTIONS}) f
         WHERE EXISTS (SELECT 1 FROM aclexplode(coalesce(f.proacl, acldefault('f', f.proowner))) a
                        WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')
         LIMIT 3
        """  # noqa: S608 - module constants, no input
    )
    public = [row["signature"] for row in cur.fetchall()]
    if public:
        raise BootstrapError(
            f"extension functions are still executable by PUBLIC ({', '.join(public)}); "
            "the grant must name the customer roles, not every role on the node"
        )

    if "014_extension_function_grants" not in versions:
        # ADR-018's posture, still correct for a tenant the fleet run has not
        # reached: its worker may not refuse extension functions as RPC.
        cur.execute(
            f"""
            SELECT count(*) AS reachable FROM ({_EXTENSION_FUNCTIONS}) f
             WHERE has_function_privilege('anon', f.oid, 'EXECUTE')
                OR has_function_privilege('authenticated', f.oid, 'EXECUTE')
            """  # noqa: S608 - module constants, no input
        )
        reachable = cur.fetchone()["reachable"]
        if reachable:
            raise BootstrapError(
                f"{reachable} extension functions are still executable by anon or authenticated "
                "before bootstrap 014; without the Data API check that is ADR-018's finding"
            )
    else:
        # ADR-076: exactly the six roles, on every extension function that is not
        # maludb_core's. Exactly, because a grant beyond them is as much a change
        # of posture as a missing one.
        cur.execute(
            f"""
            WITH customer AS ({customer_roles_sql}),
                 fn AS (SELECT * FROM ({_EXTENSION_FUNCTIONS}) f WHERE f.extname <> 'maludb_core'),
                 granted AS (
                     SELECT fn.signature, pg_get_userbyid(a.grantee) AS rolname
                       FROM fn, aclexplode(coalesce(fn.proacl, acldefault('f', fn.proowner))) a
                      WHERE a.privilege_type = 'EXECUTE' AND a.grantee <> fn.proowner AND a.grantee <> 0)
            SELECT 'missing' AS problem, fn.signature, c.rolname FROM fn CROSS JOIN customer c
             WHERE NOT EXISTS (SELECT 1 FROM granted g WHERE g.signature = fn.signature
                                                         AND g.rolname = c.rolname)
            UNION ALL
            SELECT 'extra', g.signature, g.rolname FROM granted g
             WHERE g.rolname NOT IN (SELECT rolname FROM customer)
            LIMIT 3
            """  # noqa: S608 - module constants, no input
        )
        wrong = cur.fetchall()
        if wrong:
            sample = ", ".join(f"{row['problem']} {row['rolname']} on {row['signature']}" for row in wrong)
            raise BootstrapError(
                "extension function grants are not exactly the customer roles ADR-076 names: "
                f"{sample}"
            )

    if "013_extension_rpc_check" in versions:
        _verify_rpc_check(cur)

    # Always. PostgREST reads in-database configuration on its authenticator
    # over its file, and grants slice 0 measured an empty `pgrst.db_pre_request`
    # set there switching the check off. A customer cannot set it. The grants
    # fleet run sets it deliberately -- that is how a serving worker, whose file
    # the control plane cannot reach, gets the check -- so the one accepted form
    # is that run's: on this tenant's authenticator, in this database, naming
    # exactly the check. Anything else carrying the key is how it would be lost.
    cur.execute(
        """
        SELECT s.setrole <> 0 AS on_role, s.setdatabase <> 0 AS in_database, c.setting
          FROM pg_db_role_setting s
         CROSS JOIN LATERAL unnest(s.setconfig) AS c(setting)
         WHERE s.setrole IN (0, coalesce((SELECT oid FROM pg_roles
                                           WHERE rolname = current_database() || '_authenticator'), 0))
           AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))
           AND c.setting LIKE 'pgrst.db\\_pre\\_request=%'
        """
    )
    expected = f"pgrst.db_pre_request={RPC_CHECK_FUNCTION}"
    for row in cur.fetchall():
        if not (row["on_role"] and row["in_database"] and row["setting"] == expected):
            raise BootstrapError(
                f"an in-database {row['setting']!r} is set for this tenant's authenticator or "
                "database; PostgREST prefers it to the worker's file, so anything but the "
                f"platform's check ({RPC_CHECK_FUNCTION}), on the authenticator in this "
                "database, switches off the Data API check on extension functions"
            )


def _verify_rpc_check(cur) -> None:
    """Bootstrap 013's check exists, belongs to the platform, and is all there is."""
    cur.execute(
        """
        SELECT n.nspowner = d.datdba OR r.rolsuper AS platform_owned
          FROM pg_namespace n
          JOIN pg_database d ON d.datname = current_database()
          JOIN pg_roles r ON r.oid = n.nspowner
         WHERE n.nspname = 'maludb_guard'
        """
    )
    schema = cur.fetchone()
    if schema is None or not schema["platform_owned"]:
        raise BootstrapError(
            "the maludb_guard schema is missing or not owned by the platform; the owner of a "
            "schema can replace the Data API check inside it"
        )
    cur.execute(
        """
        SELECT p.proname, p.prosecdef, p.proconfig,
               has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_runs
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'maludb_guard'
        """
    )
    functions = cur.fetchall()
    names = sorted(row["proname"] for row in functions)
    if names != ["refuse_extension_rpc"]:
        raise BootstrapError(
            f"maludb_guard should hold only refuse_extension_rpc, found {names}; anon, "
            "authenticated and service_role hold USAGE on that schema"
        )
    check = functions[0]
    if check["prosecdef"] or not any(
        (setting or "").startswith("search_path=") for setting in (check["proconfig"] or [])
    ):
        raise BootstrapError(
            "the Data API check must be SECURITY INVOKER with a pinned search_path"
        )
    if not check["anon_runs"]:
        raise BootstrapError(
            "anon cannot execute the Data API check; PostgREST runs it as the request role, so "
            "every anonymous request would fail"
        )
    cur.execute(
        "SELECT count(*) AS other FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'maludb_guard'"
    )
    if cur.fetchone()["other"]:
        raise BootstrapError("maludb_guard holds relations; it should hold only the Data API check")


def bootstrap_project(
    conn: psycopg.Connection,
    tenant_conn: psycopg.Connection,
    *,
    project_id: uuid.UUID,
    rpc_check_live: bool = False,
) -> list[str]:
    """Apply and verify bootstrap, then record the version against the project.

    Records the version the tenant actually reached, not the newest file: with
    `rpc_check_live` false a serving tenant stops short of the grants, and
    recording it as current would tell the provisioning pipeline and the fleet
    run there was nothing left to do.
    """
    try:
        versions = apply(tenant_conn, rpc_check_live=rpc_check_live)
        # After `apply`, because the table it fills is created by bootstrap 010,
        # and before `verify`, so a tenant is never recorded as bootstrapped
        # with an empty allowlist -- which would refuse every extension a
        # migrated schema opens with (ADR-045).
        sync_extension_allowlist(tenant_conn)
        tenant_conn.commit()
        verify(tenant_conn)
    except psycopg.errors.RaiseException as exc:
        # Our own RAISE, from a bootstrap file -- the reserved-schema refusal in
        # 013, say. Its message is written by this repository and says what to
        # do; hiding it behind the generic text would leave an operator with
        # nothing to act on.
        log.error("tenant bootstrap refused for project %s", project_id)
        raise BootstrapError(exc.diag.message_primary or "tenant bootstrap refused") from None
    except psycopg.Error:
        # Driver text can carry the failing statement; bootstrap SQL does not
        # embed credentials, but the habit is worth keeping consistent.
        log.error("tenant bootstrap failed for project %s", project_id)
        raise BootstrapError("tenant bootstrap failed") from None

    db.execute(
        conn,
        "UPDATE projects SET bootstrap_version = %s WHERE id = %s",
        (applied_version(tenant_conn), project_id),
    )
    conn.commit()
    return versions
