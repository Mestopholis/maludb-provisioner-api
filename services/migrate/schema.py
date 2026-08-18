r"""Getting a schema out of Supabase and into a shape the console can apply.

Phase 08 slice 6b. Three jobs, and each exists because of something measured
rather than assumed.

**`pg_dump` writes the DDL, not this module.** Reconstructing correct DDL from
catalogues means re-deriving dependency order, defaults, identity columns,
partitioning, constraint deferrability, policy expressions and function bodies
-- work PostgreSQL already does correctly and that a migration cannot afford to
do 95% right. The cost is an external binary on the customer's machine, which
ADR-042 already puts there, plus a version check: pg_dump refuses a server newer
than itself, and it does so *after* connecting, which during a cutover means
discovering it at the worst possible moment.

**A modern dump is not pure SQL.** pg_dump 17 wraps its output in `\restrict` /
`\unrestrict`, which are *psql* meta-commands. Piped into psql they are
invisible; sent to a SQL API they are `42601 syntax error at or near "\"` --
measured 2026-08-18, on the first line of the file. Anything applying a dump
through something other than psql has to strip them.

**Statements are split properly or not at all.** Splitting on `;` breaks the
first function body containing one, and pg_dump emits those dollar-quoted. A
mis-split does not fail loudly; it applies half a function. That is why the
splitter below is a state machine rather than a regular expression.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

# The console caps a request at 1,000,000 characters (`api/sql.py`). Batches are
# built well inside it: the cap is on the request, and a batch that only just
# fits leaves no room for the JSON envelope around it.
MAX_BATCH_BYTES = 700_000

_VERSION = re.compile(r"(\d+)(?:\.(\d+))?")


class SchemaError(RuntimeError):
    """The schema could not be dumped or prepared. Never carries the DSN."""


@dataclass
class Dump:
    """A dumped schema, ready to apply."""

    sql: str
    statements: list[str]

    @property
    def size(self) -> int:
        return len(self.sql)


def _pg_dump() -> str:
    """The absolute path, resolved once.

    Resolved rather than spelled `pg_dump` and left to `PATH` at exec time: an
    absolute path is what makes the "not installed" case a sentence a customer
    can act on instead of a `FileNotFoundError`, and it removes the ambiguity of
    which `pg_dump` on a machine that has several.
    """
    found = shutil.which("pg_dump")
    if not found:
        raise SchemaError(
            "pg_dump is not on PATH. It writes the schema this tool applies; install the "
            "PostgreSQL client tools at or above your Supabase project's server version."
        )
    return found


def pg_dump_version() -> tuple[int, int]:
    try:
        out = subprocess.run(  # noqa: S603 - resolved absolute path, fixed argv, no shell
            [_pg_dump(), "--version"], capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise SchemaError("pg_dump could not be run") from exc

    found = _VERSION.search(out)
    if not found:
        raise SchemaError(f"could not read a version out of pg_dump: {out.strip()!r}")
    return int(found.group(1)), int(found.group(2) or 0)


def check_version(server_version: str) -> None:
    """Refuse a pg_dump older than the source server, before the freeze.

    The scanner already knows the server version, so this is answerable the
    moment a customer runs `apply` rather than after pg_dump has connected.
    """
    major = pg_dump_version()[0]
    found = _VERSION.search(server_version or "")
    if not found:
        return  # unknown server version: let pg_dump make its own complaint
    server_major = int(found.group(1))
    if major < server_major:
        raise SchemaError(
            f"pg_dump is version {major} and the source server is {server_major}. "
            "pg_dump refuses a newer server; install client tools at or above "
            f"{server_major} and run this again."
        )


# Schemas the destination builds for itself. Supabase's own, plus the
# platform's bookkeeping: copying either would overwrite what provisioning made.
EXCLUDED_SCHEMAS = frozenset(
    {
        "auth", "storage", "realtime", "supabase_functions", "supabase_migrations",
        "graphql", "graphql_public", "extensions", "pgbouncer", "vault", "net",
        "pgsodium", "pgsodium_masks", "cron", "_realtime", "_analytics",
        "maludb_platform",
    }
)

# `pg_dump -n` takes a **pattern**, not a name: `*`, `?` and `[...]` are
# metacharacters, `.` separates database from schema, and supplying any `-n` at
# all stops pg_dump excluding system schemas. A schema name comes from the
# source catalogue, so on a hostile or merely odd source it is attacker-shaped
# input to a glob. Measured during the slice 6b security review: a source schema
# literally named `*` dumped `pg_catalog`, `auth`, `storage` and `vault` --
# defeating both the customer-schema boundary and the scan that only judged the
# schemas it enumerated.
#
# Refused rather than escaped. Escaping is possible but it is one more thing to
# get subtly right, and a schema whose name contains a glob character is
# vanishingly rare next to the cost of getting this wrong.
_PATTERN_METACHARACTERS = set('*?[]".\\')


def _reject_pattern_metacharacters(name: str) -> None:
    offending = sorted(set(name) & _PATTERN_METACHARACTERS)
    if offending or not name:
        raise SchemaError(
            f"the source schema {name!r} contains characters pg_dump would read as a "
            f"pattern ({''.join(offending) or 'it is empty'}). Rename it on the source, or "
            "migrate its objects by hand: a pattern here would silently widen what is "
            "dumped."
        )


def _libpq_env(dsn: str) -> dict[str, str]:
    """The connection, out of `argv` and into the environment.

    **`/proc/<pid>/cmdline` is world-readable** on every mainstream Linux unless
    the kernel was booted with `hidepid=`, so a DSN passed as an argument hands
    the customer's *Supabase production password* to every local account for the
    length of the dump -- verified on this project's own development host during
    the slice 6b security review. `/proc/<pid>/environ` is readable only by the
    process owner and root, which is the boundary that matters here: the threat
    is another unprivileged user on a shared or CI machine, not root.

    This module's own docstring already promised never to carry the DSN, and
    `cli.py` warns the customer that an argument is visible in `ps` -- and then
    the first version did exactly that with the DSN they had correctly supplied
    through the environment.
    """
    import psycopg.conninfo

    try:
        parts = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - psycopg raises its own type here
        # `from None`: the driver's message for a malformed conninfo can quote
        # the conninfo.
        raise SchemaError("the source connection string could not be parsed") from None

    env = dict(os.environ)
    for key, variable in (
        ("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
        ("dbname", "PGDATABASE"), ("sslmode", "PGSSLMODE"),
        ("password", "PGPASSWORD"),
    ):
        value = parts.get(key)
        if value is not None:
            env[variable] = str(value)
    return env


def dump(dsn: str, schemas: list[str]) -> Dump:
    """The customer's schemas, without their Supabase project's ownership.

    `--no-owner` and `--no-privileges` are not tidiness. The source's objects
    belong to Supabase's `postgres` role and carry its grants; the destination
    has no such role, and its grant posture is the platform's own (bootstrap 004
    and 008, ADR-018). A dump carrying `ALTER TABLE ... OWNER TO postgres` fails
    on the first statement, and one carrying the source's grants would quietly
    overwrite the posture the destination depends on. Measured: with both flags
    the dump contains no `OWNER TO`, no `GRANT` and no `SET SESSION
    AUTHORIZATION`, and still contains every `CREATE POLICY` -- RLS is not a
    privilege in pg_dump's sense.

    Extensions are deliberately absent: `-n` scopes the dump to schemas and
    `CREATE EXTENSION` is database-level. They are applied separately, from the
    allowlist, so nothing outside it is ever attempted.
    """
    if not schemas:
        raise SchemaError("no customer schemas to migrate")

    for name in schemas:
        _reject_pattern_metacharacters(name)

    argv = [
        _pg_dump(),
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--no-publications",
        "--no-subscriptions",
        "--no-security-labels",
        "--no-tablespaces",
        "--quote-all-identifiers",
    ]
    for name in schemas:
        argv += ["--schema", name]
    # Defence in depth behind the validation above: even a pattern that slipped
    # through cannot reach the schemas the destination builds for itself.
    for name in sorted(EXCLUDED_SCHEMAS):
        argv += ["--exclude-schema", name]

    try:
        finished = subprocess.run(  # noqa: S603 - resolved absolute path, fixed argv, no shell
            argv, capture_output=True, text=True, timeout=600, env=_libpq_env(dsn)
        )
    except subprocess.TimeoutExpired:
        # Never chained: `TimeoutExpired.cmd` is the argv, and while the DSN
        # is no longer in it, chaining an exception that renders a command line
        # is a habit worth not having.
        raise SchemaError("pg_dump did not finish within ten minutes") from None
    except (subprocess.SubprocessError, OSError) as exc:
        raise SchemaError("pg_dump could not be run") from exc

    if finished.returncode != 0:
        # pg_dump can echo the connection string in a failure, so its stderr is
        # summarised rather than passed through -- the rule the scanner already
        # follows for psycopg's connection errors.
        raise SchemaError(
            "pg_dump failed. Check that the connection string works and that your "
            "pg_dump is at least the server's version."
        )

    body = strip_meta_commands(finished.stdout)
    return Dump(sql=body, statements=split_statements(body))


# The only two psql directives pg_dump emits, and the only two removed. Matching
# "any line starting with a backslash" is what made the first version of this
# corrupt schemas -- see `strip_meta_commands`.
_META_COMMANDS = ("\\restrict", "\\unrestrict")

# ASCII tags only, which is every tag pg_dump emits: `appendStringLiteralDQ`
# picks `$$`, `$_$`, `$_X$` and so on. A hand-written non-ASCII tag can only
# appear *inside* one of those, where it is consumed as opaque body text.
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")

# `LANGUAGE sql BEGIN ATOMIC ... END` (PostgreSQL 14+) is the one function body
# pg_dump writes **unquoted**, so its internal semicolons are not hidden behind
# a dollar tag. Supabase runs 15 and 17, so a source project can contain one.
_BEGIN_ATOMIC = re.compile(r"\bBEGIN\s+ATOMIC\b", re.IGNORECASE)
_END = re.compile(r"\bEND\b", re.IGNORECASE)


@dataclass
class _Scan:
    """Where every statement ends, and which lines begin at top level.

    One pass and one state machine feeding both consumers. Two scanners would
    eventually disagree about what counts as "inside a string", and the
    disagreement would be a corrupted schema rather than an error.
    """

    ends: list[int]
    top_level_line_starts: set[int]


def _scan(sql: str) -> _Scan:
    ends: list[int] = []
    line_starts: set[int] = {0}
    length = len(sql)
    index = 0

    in_line_comment = False
    block_depth = 0
    in_single = in_double = False
    single_is_escapable = False
    dollar_tag: str | None = None
    paren_depth = 0
    atomic_depth = 0

    def quiet() -> bool:
        """True when nothing is open: the only place a `;` ends a statement."""
        return not (
            in_line_comment or block_depth or in_single or in_double
            or dollar_tag is not None
        )

    while index < length:
        char = sql[index]
        rest = sql[index:]

        if char == "\n":
            if quiet():
                line_starts.add(index + 1)
            in_line_comment = False
            index += 1
            continue

        if in_line_comment:
            index += 1
            continue

        if block_depth:
            if rest.startswith("/*"):
                block_depth += 1
                index += 2
                continue
            if rest.startswith("*/"):
                block_depth -= 1
                index += 2
                continue
            index += 1
            continue

        if dollar_tag is not None:
            if rest.startswith(dollar_tag):
                index += len(dollar_tag)
                dollar_tag = None
                continue
            index += 1
            continue

        if in_single:
            if single_is_escapable and char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == "'":
                if rest.startswith("''"):
                    index += 2
                    continue
                in_single = False
            index += 1
            continue

        if in_double:
            if char == '"':
                if rest.startswith('""'):
                    index += 2
                    continue
                in_double = False
            index += 1
            continue

        # -- outside every quoted or commented construct ------------------
        if rest.startswith("--"):
            in_line_comment = True
            index += 2
            continue
        if rest.startswith("/*"):
            block_depth = 1
            index += 2
            continue
        if char == "'":
            in_single = True
            # `E'...'` is the only form in which a backslash escapes, and
            # pg_dump emits it exactly when a value needs one.
            single_is_escapable = index > 0 and sql[index - 1] in "Ee"
            index += 1
            continue
        if char == '"':
            in_double = True
            index += 1
            continue

        if char == "$":
            found = _DOLLAR_TAG.match(sql, index)
            if found:
                dollar_tag = found.group(0)
                index += len(dollar_tag)
                continue

        # A `;` inside parentheses is not a boundary: `CREATE RULE ... DO
        # INSTEAD (stmt; stmt;)` puts two of them there, and pg_dump emits it
        # that way from `pg_get_ruledef`.
        if char == "(":
            paren_depth += 1
            index += 1
            continue
        if char == ")":
            paren_depth = max(0, paren_depth - 1)
            index += 1
            continue

        if atomic_depth == 0 and _BEGIN_ATOMIC.match(sql, index):
            atomic_depth = 1
            index += _BEGIN_ATOMIC.match(sql, index).end() - index
            continue
        if atomic_depth and _END.match(sql, index):
            atomic_depth = 0
            index += _END.match(sql, index).end() - index
            continue

        if char == ";" and not paren_depth and not atomic_depth:
            ends.append(index)
            index += 1
            continue

        index += 1

    return _Scan(ends=ends, top_level_line_starts=line_starts)


def strip_meta_commands(sql: str) -> str:
    r"""Remove psql's own directives, at top level only.

    **Two ways the obvious one-liner corrupts a schema silently**, both measured
    against a real dump during the slice 6b security review. It was:

        "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))

    - It is a line filter with no idea of lexical context, and `pg_dump` writes
      function bodies and string literals **verbatim**. Any line of the
      customer's own SQL beginning with a backslash was deleted along with
      pg_dump's own directives. The statement still parses and still applies;
      the destination just gets a different function body. `SET
      check_function_bodies = false` is in every dump, so plpgsql is not even
      parsed on the way in.
    - `str.splitlines()` splits on eight characters that are not `\n` -- `\v`,
      `\f`, 0x1C, 0x1D, 0x1E, U+0085, U+2028 and U+2029 -- and rejoining with
      `\n` rewrites every one of them. U+2028 and U+2029 arrive routinely in web
      application content, so a `DEFAULT` or a seed string carrying one migrated
      to a *different value*, invisibly.

    So this splits on `\n` alone and drops a directive only where the line
    begins outside every string, comment and dollar quote.
    """
    scan = _scan(sql)
    kept: list[str] = []
    offset = 0
    for line in sql.split("\n"):
        at_top_level = offset in scan.top_level_line_starts
        if not (at_top_level and line.lstrip().startswith(_META_COMMANDS)):
            kept.append(line)
        offset += len(line) + 1
    return "\n".join(kept)


def split_statements(sql: str) -> list[str]:
    """Split on statement boundaries, respecting everything that can hold a `;`.

    Tracked in one pass: `--` line comments; `/* */` block comments, which
    **nest** in PostgreSQL; `'...'` strings where `''` is an escaped quote;
    `E'...'` strings where a backslash escapes the next character; `"..."`
    identifiers, where a `;` inside is perfectly legal; `$tag$...$tag$` dollar
    quotes, which is how most function bodies arrive; parenthesised statement
    lists, which is how `CREATE RULE ... DO INSTEAD (stmt; stmt;)` arrives; and
    `BEGIN ATOMIC ... END`, the one body pg_dump writes unquoted.

    The last two were added after the slice 6b security review demonstrated both
    being cut in half. Neither corrupts silently -- the head of a split
    `BEGIN ATOMIC` is a syntax error -- but a batch boundary landing between the
    pieces abandons a migration part-applied, during a write freeze, citing a
    syntax error in SQL pg_dump wrote correctly.
    """
    scan = _scan(sql)
    statements: list[str] = []
    start = 0
    for end in scan.ends:
        statements.append(sql[start:end + 1].strip())
        start = end + 1
    trailing = sql[start:].strip()
    if trailing:
        statements.append(trailing)
    return [statement for statement in statements if statement and statement != ";"]



# The three roles a destination has that a source grant can be carried to.
# Anything the source granted to `postgres`, `supabase_admin` or a role of the
# customer's own has no equivalent here and is not invented.
CARRIABLE_ROLES = ("anon", "authenticated", "service_role")


def privilege_statements(functions: list[dict]) -> list[str]:
    """Re-emit the restrictions `--no-privileges` threw away.

    **The finding this exists for.** `pg_dump --no-privileges` suppresses the
    whole ACL restore, and an ACL restore is `REVOKE` *then* `GRANT` -- so it
    drops restrictions as well as grants. `SECURITY DEFINER` is a function
    property rather than a privilege, so it survives. The destination then
    creates the function fresh, and a fresh PostgreSQL function carries the
    default ACL: **`EXECUTE` to `PUBLIC`**.

    Measured end to end during the slice 6b security review: a source function
    with `REVOKE ALL ... FROM PUBLIC` and `SECURITY DEFINER` -- the careful
    developer's pattern -- arrived on the destination executable by `anon`,
    which PostgREST exposes as `/rpc/<name>` to anyone holding the publishable
    key, running with the tenant admin's rights. That is ADR-018's finding
    reintroduced for customer code, by the migration tool, silently.

    Nothing here *tightens* beyond the source: a function the source left open
    stays open, because a migration that quietly locked down a working RPC would
    break the customer's application in the other direction. What it does is
    make the destination's permissions match the source's, for the roles a
    destination has.
    """
    statements: list[str] = []
    for function in functions:
        acl = function.get("acl")
        signature = function.get("signature")
        if not signature or acl is None:
            # A null ACL is PostgreSQL's default, which is what the destination
            # already has. Nothing to carry.
            continue

        granted = _execute_grantees(acl)
        if "" in granted:
            continue  # the source grants PUBLIC too; the default already matches

        statements.append(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
        for role in CARRIABLE_ROLES:
            if role in granted:
                statements.append(f"GRANT EXECUTE ON FUNCTION {signature} TO {role};")
    return statements


def statements_for(dumped: Dump, functions: list[dict]) -> list[str]:
    """Everything a migration applies, in order: the schema, then its permissions.

    One function so the CLI and the tests cannot drift. The first version of the
    end-to-end test built its own list and quietly left the permission
    statements out, which meant it asserted the very property the fix exists for
    while never applying the fix.
    """
    return dumped.statements + privilege_statements(functions)


def _execute_grantees(acl: list[str]) -> set[str]:
    """Who the source grants EXECUTE to. `""` is PUBLIC, as PostgreSQL spells it.

    An aclitem renders as `grantee=privileges/grantor`, and PUBLIC's grantee
    half is empty -- `=X/postgres`. `X` is EXECUTE.
    """
    grantees: set[str] = set()
    for item in acl or []:
        grantee, _, rest = str(item).partition("=")
        privileges = rest.split("/", 1)[0]
        if "X" in privileges:
            grantees.add(grantee.strip('"'))
    return grantees


def batches(statements: list[str], max_bytes: int = MAX_BATCH_BYTES) -> list[str]:
    """Group statements into as few requests as the size cap allows.

    The console accepts a multi-statement request and answers with a result set
    per statement, which is what makes this practical: measured, a forty-table
    schema with eighty policies is 254 statements and 37 KB -- one request. Sent
    one statement at a time it would be 254 requests against a plan that allows
    one per eight seconds, which is half an hour of maintenance window for a
    small schema.

    A statement larger than the cap on its own is passed through rather than
    split: it cannot be divided without changing its meaning, and the console
    refuses it with an error naming the size, which is a better failure than a
    silent truncation here.
    """
    grouped: list[str] = []
    current: list[str] = []
    size = 0
    for statement in statements:
        addition = len(statement) + 1
        if current and size + addition > max_bytes:
            grouped.append("\n".join(current))
            current, size = [], 0
        current.append(statement)
        size += addition
    if current:
        grouped.append("\n".join(current))
    return grouped
