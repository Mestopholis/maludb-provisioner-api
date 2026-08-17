"""What the facts mean (ADR-043, ADR-044, ADR-045).

The scanner's judgement, kept apart from its reading so it can be tested without
a Supabase project — which matters, because the interesting cases are the ones
nobody has a spare production database to reproduce.

**Two severities, and the distinction is load-bearing.** A `blocker` means the
migration will not complete correctly and must not be attempted. A `warning` is
something the customer should know before they commit to a maintenance window
but can proceed past. Anything softer than that belongs in the summary counts,
not in a list a customer has to read at 2 a.m.

**A blocker names the phase that will carry the surface**, so the answer to
"when can I migrate this?" is a date rather than a refusal. That is ADR-043's
shape: the compatibility matrix is the authority on what is supported, and a
blocker is what the matrix says read back to a customer about their own project.

**Nothing here fabricates a number.** The freeze window ADR-044 commits to
publishing is measured by slice 8, not estimated here, so an unmeasured rate
produces "not measured yet" and not a guess — the same rule `api/usage.py`
follows when it reports `metered: false` rather than a zero.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from services.migrate.source import SourceFacts

BLOCKER = "blocker"
WARNING = "warning"

# Repository-relative, so the CLI works from a checkout. A packaged build reads
# the same files from the installed package directory.
SPEC_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "specs"


@dataclass
class Finding:
    """One thing the customer needs to know, and what to do about it."""

    severity: str
    code: str
    title: str
    detail: str
    # The phase that will carry this surface, for a blocker that is a
    # not-yet rather than a never. None where the answer is "change your
    # project", not "wait for ours".
    phase: int | None = None
    # What the customer can do now. Empty where there is nothing to do but wait.
    remedy: str = ""
    # The objects this is about. Bounded when rendered, never when recorded.
    items: list[str] = field(default_factory=list)


@dataclass
class Scan:
    """The whole answer: what was found, and how big the job is."""

    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCKER]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def migratable(self) -> bool:
        return not self.blockers


def load_specs(spec_dir: pathlib.Path | None = None) -> tuple[dict, dict]:
    """The compatibility matrix and the extension allowlist, as data.

    Read at runtime rather than compiled in, because ADR-045 makes adding an
    extension a review and a merge rather than a release, and ADR-043 makes the
    matrix the authority the scanner reports against.
    """
    directory = spec_dir or SPEC_DIR
    matrix = yaml.safe_load((directory / "compatibility-matrix.yaml").read_text())
    allowlist = yaml.safe_load((directory / "extension-allowlist.yaml").read_text())
    return matrix, allowlist


def allowed_extensions(allowlist: dict) -> set[str]:
    return {entry["name"] for entry in allowlist.get("allowed", [])}


def denial_reasons(allowlist: dict) -> dict[str, str]:
    """Extension name -> why it is refused, so a blocker can say which criterion.

    A refusal a customer cannot understand becomes a support ticket, which is
    the thing ADR-045 chose self-service to avoid.
    """
    return {
        name: entry["reason"]
        for entry in allowlist.get("denied", [])
        for name in entry["names"]
    }


def evaluate(facts: SourceFacts, *, matrix: dict, allowlist: dict) -> Scan:
    """Turn what the source contains into what a customer must decide about."""
    scan = Scan()
    scan.summary = _summarise(facts)

    for rule in (
        _unreadable_probes,
        _extensions,
        _storage,
        _auth_identities,
        _auth_passwords,
        _foreign_tables,
        _untrusted_languages,
        _database_webhooks,
        _vault,
        _realtime,
        _edge_functions,
        _rls_gaps,
    ):
        scan.findings.extend(rule(facts, matrix, allowlist))

    # Blockers first, then warnings, each in the order the rules produced them.
    # A customer reads the top of this list and stops; what stops a cutover
    # belongs there.
    scan.findings.sort(key=lambda f: 0 if f.severity == BLOCKER else 1)
    return scan


# -- the rules -------------------------------------------------------------


def _unreadable_probes(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """What the scan could not see, said out loud.

    A scan that silently skipped `auth.users` because the credential could not
    read it would report a clean project and produce a migration with no users
    in it. This is the finding that keeps "no blockers" meaning what it says.
    """
    blind = [
        (name, probe.reason)
        for name, probe in vars(facts).items()
        if hasattr(probe, "readable") and not probe.readable
    ]
    if not blind:
        return []
    return [
        Finding(
            severity=BLOCKER,
            code="source.unreadable",
            title="Part of the source project could not be read",
            detail=(
                "The scan is incomplete, so its result cannot be trusted: what it could not "
                "read may contain the thing that would have blocked the migration."
            ),
            remedy=(
                "Re-run with a connection string for a role that can read the whole project "
                "-- on Supabase that is the `postgres` role from Project Settings > Database."
            ),
            items=[f"{name}: {reason}" for name, reason in blind],
        )
    ]


def _extensions(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """ADR-045. The one that decides whether a schema applies at all."""
    allowed = allowed_extensions(allowlist)
    reasons = denial_reasons(allowlist)
    # `plpgsql` is in every PostgreSQL database and is not an extension anybody
    # installs or migrates; treating it as unlisted would block every project.
    installed = {row["name"] for row in facts.extensions.rows} - {"plpgsql"}

    unlisted = sorted(installed - allowed)
    if not unlisted:
        return []

    named = [f"{name} -- {reasons.get(name, 'not on the allowlist')}" for name in unlisted]
    return [
        Finding(
            severity=BLOCKER,
            code="extension.not_allowlisted",
            title=f"{len(unlisted)} extension(s) cannot be installed on MaluDB",
            detail=(
                "A migrated schema that begins `create extension ...` fails on its first "
                "statement if the extension is not on the platform's allowlist "
                "(specs/extension-allowlist.yaml, ADR-045)."
            ),
            remedy=(
                "Remove the dependency, or ask for the extension to be reviewed for the "
                "allowlist. Refusals for a security criterion will not be reviewed."
            ),
            items=named,
        )
    ]


def _storage(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """Phase 10. A bucket with nothing in it is a warning; objects are a blocker."""
    findings: list[Finding] = []
    objects = facts.storage_objects.one("total", 0) or 0
    buckets = facts.storage_buckets.count
    phase = _phase_for(matrix, "storage", "upload", default=10)

    if objects:
        findings.append(
            Finding(
                severity=BLOCKER,
                code="storage.objects",
                title=f"{objects} stored object(s) cannot be migrated yet",
                detail=(
                    "MaluDB has no Storage surface until Phase "
                    f"{phase}, so the objects and their policies have nowhere to land."
                ),
                phase=phase,
                remedy=(
                    "Migrate the database now and keep serving files from Supabase Storage, "
                    "or wait for the Storage phase."
                ),
                items=[f"{row['name']} (public)" if row.get("public") else str(row.get("name"))
                       for row in facts.storage_buckets.rows][:20],
            )
        )
    elif buckets:
        findings.append(
            Finding(
                severity=WARNING,
                code="storage.empty_buckets",
                title=f"{buckets} empty Storage bucket(s) will not be recreated",
                detail="The buckets hold no objects, so nothing is lost -- but they are "
                       "configuration, and they are not carried across.",
                phase=phase,
            )
        )
    return findings


def _auth_identities(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """ADR-043. A user row whose only authenticator is a provider configuration
    migrates into an account nobody can sign in to, which is worse than not
    migrating it."""
    providers = {row["provider"]: row["total"] for row in facts.auth_identities.rows}
    external = {p: n for p, n in providers.items() if p != "email"}
    if not external:
        return []

    supported = _supported_auth_features(matrix)
    detail = (
        "Only email and password identities are migrated at launch. These identities "
        "authenticate through a provider configuration that MaluDB Auth does not carry "
        f"yet (supported today: {', '.join(sorted(supported)) or 'email/password only'})."
    )
    return [
        Finding(
            severity=BLOCKER,
            code="auth.external_identities",
            title=f"{sum(external.values())} identity/identities use an external provider",
            detail=detail,
            phase=None,
            remedy=(
                "Migrate these users as email accounts and have them set a password, or "
                "wait for the provider surface. The scanner lists the providers so the "
                "decision can be made per provider rather than for all of them."
            ),
            items=[f"{provider}: {count}" for provider, count in sorted(external.items())],
        )
    ]


def _auth_passwords(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """The reset ADR-043 requires be reported in advance rather than discovered."""
    total = facts.auth_users.one("total", 0) or 0
    with_password = facts.auth_users.one("with_password", 0) or 0
    without = total - with_password
    if not total or not without:
        return []
    return [
        Finding(
            severity=WARNING,
            code="auth.no_password",
            title=f"{without} of {total} user(s) have no password to migrate",
            detail=(
                "These accounts were created through a provider or a magic link, so there "
                "is no password hash to carry across."
            ),
            remedy="They will need to set a password, or sign in again once their provider "
                   "is supported. Tell them before the cutover, not after.",
        )
    ]


def _foreign_tables(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    if not facts.foreign_tables.rows:
        return []
    return [
        Finding(
            severity=BLOCKER,
            code="schema.foreign_tables",
            title=f"{facts.foreign_tables.count} foreign table(s) point outside the database",
            detail=(
                "A foreign table is a pointer into another system, not data. The extensions "
                "that make one work (`postgres_fdw`, `dblink` and the rest) are refused on a "
                "shared node because they would reach other tenants -- see ADR-014 and "
                "specs/extension-allowlist.yaml."
            ),
            remedy="Replace the foreign tables with real ones, or keep that part of the "
                   "workload where it is.",
            items=[f"{row['schema']}.{row['name']} -> {row['server']}"
                   for row in facts.foreign_tables.rows],
        )
    ]


# Languages whose functions execute outside the database's own privilege model.
# ADR-045 refuses the extensions that provide them, so a function written in one
# cannot be created on the destination at all.
UNTRUSTED_LANGUAGES = frozenset({"plpython3u", "plpythonu", "plperlu", "pltclu", "c", "internal"})


def _untrusted_languages(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    offending = [
        f"{row['schema']}.{row['name']} ({row['language']})"
        for row in facts.functions.rows
        if row["language"] in UNTRUSTED_LANGUAGES
    ]
    if not offending:
        return []
    return [
        Finding(
            severity=BLOCKER,
            code="schema.untrusted_language",
            title=f"{len(offending)} function(s) use a language MaluDB does not allow",
            detail=(
                "Untrusted procedural languages execute as the database's operating-system "
                "user, which is the shortest path from one tenant to a shared node. "
                "specs/extension-allowlist.yaml refuses them by criterion 2."
            ),
            remedy="Rewrite them in `plpgsql` or `sql`, or run that logic in your "
                   "application.",
            items=offending,
        )
    ]


def _database_webhooks(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """The only part of Edge Functions visible from the database."""
    if not facts.function_hooks.rows:
        return []
    return [
        Finding(
            severity=BLOCKER,
            code="schema.database_webhooks",
            title=f"{facts.function_hooks.count} database webhook trigger(s) found",
            detail=(
                "These triggers call out to Supabase Edge Functions or arbitrary HTTP "
                "endpoints through `supabase_functions`/`net`. MaluDB has no Edge Function "
                "surface, and outbound HTTP from inside the database is refused on a shared "
                "node (specs/extension-allowlist.yaml, criterion 2)."
            ),
            remedy="Move the call into your application, or drop the trigger before "
                   "migrating.",
            items=[f"{row['schema']}.{row['table_name']}.{row['name']}"
                   for row in facts.function_hooks.rows],
        )
    ]


def _vault(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    total = facts.vault_secrets.one("total", 0) or 0
    if not total:
        return []
    return [
        Finding(
            severity=BLOCKER,
            code="vault.secrets",
            title=f"{total} Vault secret(s) have no equivalent on MaluDB",
            detail=(
                "Supabase Vault stores encrypted secrets inside the database, via "
                "`pgsodium`. MaluDB has no equivalent surface and the extension is not "
                "allowlisted, so the secrets cannot be decrypted on the destination."
            ),
            remedy="Move these secrets to your application's own configuration before "
                   "migrating. The scanner deliberately does not read their values.",
        )
    ]


def _realtime(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """Postgres Changes migrate; broadcast and presence are invisible here."""
    findings: list[Finding] = []
    published = facts.realtime_publication.count
    if published:
        findings.append(
            Finding(
                severity=WARNING,
                code="realtime.publication",
                title=f"{published} table(s) publish Postgres Changes",
                detail="Realtime is off by default on a new MaluDB project and these tables "
                       "will not publish until it is enabled.",
                remedy="Enable Realtime on the destination project after migrating.",
                items=[f"{row['schema']}.{row['name']}"
                       for row in facts.realtime_publication.rows][:50],
            )
        )
    findings.append(
        Finding(
            severity=WARNING,
            code="realtime.undetectable",
            title="Broadcast and presence cannot be detected from the database",
            detail=(
                "They are client-side Realtime features and leave no trace in the "
                "catalogue, so this scan cannot tell you whether you use them. Both are "
                "`deferred` in specs/compatibility-matrix.yaml."
            ),
            remedy="Check your application for `.on('broadcast')` and `.track(` before "
                   "cutting over.",
        )
    )
    return findings


def _edge_functions(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """Always emitted, because absence of evidence is not evidence of absence.

    Edge Functions live in Supabase's platform, not in the database, so no
    catalogue query can see them. Saying nothing would let a customer read a
    clean report as "everything I have migrates", which is the one misreading
    this scan must not permit.
    """
    return [
        Finding(
            severity=WARNING,
            code="edge_functions.undetectable",
            title="Edge Functions cannot be seen from the database",
            detail=(
                "They are deployed to Supabase's platform rather than stored in your "
                "project's database, so this scan cannot list them. MaluDB has no "
                "equivalent surface in any current phase."
            ),
            remedy="Run `supabase functions list` against the source project. Anything it "
                   "returns has to be rehosted before you can switch over.",
        )
    ]


# Roles a Supabase-shaped project exposes through its public API. A policy
# granting one of these unconditional access is a public read, whatever RLS says
# about the table.
PUBLIC_FACING_ROLES = frozenset({"anon", "authenticated", "public"})


def _rls_gaps(facts: SourceFacts, matrix: dict, allowlist: dict) -> list[Finding]:
    """Not a migration problem -- a "your data is about to be public" problem.

    Reported because the destination's grant posture is the same as Supabase's
    (ADR-018): `anon` holds the grant and RLS is what filters, so anything RLS
    does not cover is readable by whoever holds the publishable key. True on the
    source too, but a migration is when somebody is looking.

    **Three ways to be exposed, not one.** The first version of this rule
    checked `relrowsecurity` on ordinary tables and nothing else, which the
    security review showed is the smallest of the three and leaves the other two
    reporting clean:

    - a table with RLS disabled;
    - a **view** without `security_invoker`, which runs with its owner's
      privileges, so RLS on the tables underneath does not apply to the caller
      -- measured: a role that read 0 rows from the table read every row through
      the view. A materialized view is worse, because RLS cannot be enabled on
      one at all;
    - a table *with* RLS whose policy is `USING (true)` for a public-facing
      role, which is an enabled control that permits everything.
    """
    findings: list[Finding] = []

    tables = [
        f"{row['schema']}.{row['name']}"
        for row in facts.relations.rows
        if row["kind"] in ("r", "p") and not row["rls_enabled"]
    ]
    if tables:
        findings.append(
            Finding(
                severity=WARNING,
                code="schema.rls_disabled",
                title=f"{len(tables)} table(s) have row-level security disabled",
                detail=(
                    "MaluDB grants `anon` the same access Supabase does, so RLS is what "
                    "decides whether a public read returns rows or nothing. A table "
                    "without it is readable by anyone holding the project's publishable "
                    "key -- on your current project as much as on the migrated one."
                ),
                remedy="Enable RLS and add policies before cutting over, or confirm these "
                       "tables are meant to be public.",
                items=tables,
            )
        )

    definer_views = []
    for row in facts.relations.rows:
        if row["kind"] == "m":
            definer_views.append(f"{row['schema']}.{row['name']} (materialized view)")
        elif row["kind"] == "v" and "security_invoker=true" not in (row.get("options") or ""):
            definer_views.append(f"{row['schema']}.{row['name']} (view)")
    if definer_views:
        findings.append(
            Finding(
                severity=WARNING,
                code="schema.definer_rights_views",
                title=f"{len(definer_views)} view(s) read with their owner's privileges",
                detail=(
                    "A view without `security_invoker=true` runs as its owner, so "
                    "row-level security on the tables underneath does not apply to whoever "
                    "reads the view -- measured: a role that saw no rows in the table saw "
                    "every row through the view. Row-level security cannot be enabled on a "
                    "materialized view at all."
                ),
                remedy=(
                    "Set `security_invoker = true` on the views that read protected tables "
                    "(PostgreSQL 15+), or confirm the data behind them is meant to be "
                    "public. For a materialized view, revoke access from `anon` and "
                    "`authenticated` instead."
                ),
                items=definer_views,
            )
        )

    permissive = [
        f"{row['schema']}.{row['table_name']}: {row['name']} ({', '.join(row['roles'])})"
        for row in facts.policies.rows
        if row.get("permissive")
        and row.get("command") in ("r", "*")
        and _is_unconditional(row.get("using_expression"))
        and PUBLIC_FACING_ROLES & set(row.get("roles") or [])
    ]
    if permissive:
        findings.append(
            Finding(
                severity=WARNING,
                code="schema.policy_allows_all",
                title=f"{len(permissive)} policy/policies allow every row to a public role",
                detail=(
                    "Row-level security is enabled on these tables and the policy permits "
                    "everything, which reads the same as having no policy at all: anyone "
                    "holding the publishable key sees every row. Worth confirming rather "
                    "than assuming, because RLS being *on* is what usually gets checked."
                ),
                remedy="Narrow the policy, or confirm the table is meant to be public.",
                items=permissive,
            )
        )

    return findings


def _is_unconditional(expression: str | None) -> bool:
    """`USING (true)`, however it was spelled, and `USING` omitted entirely."""
    if expression is None:
        return True
    return expression.strip().strip("()").lower() == "true"


# -- summary ---------------------------------------------------------------


def _summarise(facts: SourceFacts) -> dict[str, Any]:
    tables = [r for r in facts.relations.rows if r["kind"] in ("r", "p")]
    # `reltuples` is -1 for a relation the planner has never analysed, which is
    # every table in a freshly restored database. Flooring that to zero would
    # report "0 rows" for a table nobody has counted -- the same mistake as
    # reporting unmetered usage as zero, and worse here because a customer sizing
    # a maintenance window would read it as "nothing to copy". Counted separately
    # and named instead.
    analysed = [row for row in tables if row["estimated_rows"] >= 0]
    return {
        "tables_never_analysed": len(tables) - len(analysed),
        "server_version": facts.server_version,
        "database_bytes": facts.database_bytes,
        "schemas": [row["name"] for row in facts.schemas.rows],
        "tables": len(tables),
        "views": len([r for r in facts.relations.rows if r["kind"] in ("v", "m")]),
        "estimated_rows": sum(row["estimated_rows"] for row in analysed),
        "policies": facts.policies.count,
        "functions": facts.functions.count,
        "triggers": facts.triggers.count,
        "extensions": [row["name"] for row in facts.extensions.rows],
        "auth_users": facts.auth_users.one("total", 0) or 0,
        "storage_objects": facts.storage_objects.one("total", 0) or 0,
        "storage_bytes": facts.storage_objects.one("bytes", 0) or 0,
        "largest_tables": [
            {"name": f"{row['schema']}.{row['name']}", "bytes": row["size_bytes"]}
            for row in facts.relations.rows[:5]
        ],
    }


def _phase_for(matrix: dict, surface: str, feature: str, *, default: int | None) -> int | None:
    """The phase the matrix says will carry a surface, so a blocker is a date."""
    try:
        return matrix["surfaces"][surface]["features"][feature].get("phase", default)
    except (KeyError, TypeError, AttributeError):
        return default


def _supported_auth_features(matrix: dict) -> set[str]:
    try:
        features = matrix["surfaces"]["auth"]["features"]
    except (KeyError, TypeError):
        return set()
    return {name for name, spec in features.items() if spec.get("status") == "supported"}
