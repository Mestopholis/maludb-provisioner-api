"""Environment-aware configuration.

Plain module reading os.environ with explicit validation, matching the house
style in maludb-python-api-server. No settings framework.

Two rules from the ADRs are enforced here rather than left to deployment:

- ADR-024: FastAPI's documentation routes are configuration-gated, so they are
  never publicly reachable in production.
- ADR-023: the control plane fails closed when key material is unavailable. It
  refuses to start rather than running degraded, because a control plane that
  cannot decrypt cannot safely provision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ENVIRONMENTS = ("development", "test", "staging", "production")


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but unset")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _key_material(env_name: str, credential: str) -> bytes:
    """The KEK or pepper: the unit's systemd credential when it has one, else the file named by env_name.

    Every service that needs a key runs as its own user (`maludb-cp`,
    `maludb-provisioner`, `maludb-memory`, ...), and the file loader requires
    mode 600 -- so one root-owned file cannot be read by any of them, and a copy
    each would be several places to lose or rotate. `LoadCredential=` gives each
    unit a private copy readable by its user alone, in memory, gone when it stops.
    Found by the deployment rehearsal: following the runbook, both listeners
    failed with PermissionError on /etc/maludb/keys/kek.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if directory and (Path(directory) / credential).is_file():
        return _read_secret_file(str(Path(directory) / credential), env_name, systemd_credential=True)
    return _read_secret_file(_require(env_name), env_name)


def _read_secret_file(ref: str, label: str, *, systemd_credential: bool = False) -> bytes:
    """Load key material from a file reference.

    ADR-023 requires the loading path be a narrow interface with one
    implementation swappable for another. This file backend is the development
    implementation; a manager such as Vault replaces this function alone.

    A systemd credential is exempt from the mode check: systemd presents it as
    0440 root with an ACL granting the unit's user alone, inside a directory
    nothing else can enter, so the group bits describe the ACL mask, not a group.
    """
    path = Path(ref)
    if not path.is_file():
        raise ConfigError(f"{label}: no file at {ref}")
    mode = path.stat().st_mode & 0o077
    if mode and not systemd_credential:
        raise ConfigError(f"{label}: {ref} is group/world accessible (mode {oct(mode)}); chmod 600 it")
    material = path.read_bytes().strip()
    if len(material) < 32:
        raise ConfigError(f"{label}: {ref} holds {len(material)} bytes, need at least 32")
    return material


def redacted_dsn(dsn: str) -> str:
    """Render a connection string safe to log: host and database, no credentials."""
    parsed = urlsplit(dsn)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "?"
    user = f"{parsed.username}@" if parsed.username else ""
    return f"{parsed.scheme}://{user}{host}{port}/{database}"


@dataclass(frozen=True)
class Config:
    environment: str
    # repr=False on every field carrying credentials. database_url embeds a
    # password, so it is suppressed alongside the key material -- a security
    # review found it rendered in full while kek and token_pepper did not.
    database_url: str = field(repr=False)
    gateway_domain: str
    # Where a paid project's direct PostgreSQL connection points (ADR-047).
    # Deliberately a per-project name under a domain the platform controls,
    # never the node's own hostname: a node hostname in a customer's connection
    # string names which node they are on -- `docs/CONTROL-PLANE.md` already
    # treats one as something the audit trail must not publish -- and it breaks
    # the moment ADR-006's background move to another node happens, which is a
    # thing the customer's application would discover rather than be told.
    database_domain: str
    docs_enabled: bool
    kek: bytes = field(repr=False)
    token_pepper: bytes = field(repr=False)
    # The platform's own MaluMail key, used for every project on
    # sender_mode = 'platform_default' (ADR-029). repr=False for the same
    # reason as the rest: it sends mail as our verified domain.
    #
    # Optional rather than required, because a control plane that cannot send
    # email should still provision, serve and validate projects. The failure
    # surfaces where it matters -- a project on platform_default gets a refusal
    # naming the missing key, rather than the process refusing to start.
    malumail_api_key: str | None = field(default=None, repr=False)
    # Realtime is one instance per project (ADR-034), so there is no node-wide
    # port here any more: the port is allocated per project and read from the
    # project's row. What is node-wide is how those instances reach PostgreSQL
    # and which image they run.
    #
    # The **Realtime data address**: where a Realtime container reaches this
    # node's PostgreSQL. It must not be loopback, and that is a security
    # property rather than a preference -- a container that can reach the node's
    # loopback can reach every other worker on it, including tenants' PostgREST,
    # which serves anonymous reads to anything that can open its port. See
    # `realtime_workers`. None until a node has been prepared with one, which is
    # what makes an unprepared node refuse to start a Realtime worker rather
    # than start a badly contained one.
    realtime_db_host: str | None = None
    realtime_db_port: int = 5432
    # Pinned by ADR-033: latest dies with SIGILL in a precompiled Rust NIF on a
    # CPU with no AVX, which these nodes are.
    realtime_image: str = "docker.io/supabase/realtime:v2.110.0"
    # ADR-034 measured ~146 MB per instance. Well above it, so an ordinary
    # instance never meets the cap and a runaway one cannot take the node's
    # other tenants down with it.
    realtime_memory_max: str = "512m"

    # -- object storage (Phase 10) -------------------------------------------
    #
    # **One shared instance per node** (ADR-058), which is the opposite of
    # Realtime's answer and not in tension with it: ADR-034's reason was that
    # replication slot names are cluster-unique, and `storage-api` has no
    # equivalent. So unlike Realtime there *is* a node-wide port here, and the
    # per-project work is registering a tenant rather than starting a container.
    storage_port: int = 5000
    storage_admin_port: int = 5001
    storage_image: str = "docker.io/supabase/storage-api:v1.70.6"
    # Measured at 105.8 MB dedicated and 119.8 MB shared at eight tenants
    # (ADR-058). Well above both, for the reason the Realtime bound is: an
    # ordinary instance never meets the cap, and a runaway one cannot take the
    # node's other tenants with it.
    storage_memory_max: str = "1g"
    # Where the container reaches this node's PostgreSQL. Same property and same
    # reasoning as `realtime_db_host`, and it is a security property rather than
    # a preference: a container that can reach the node's loopback reaches every
    # other worker on it. None until a node has been prepared.
    storage_db_host: str | None = None
    storage_db_port: int = 5432
    # The largest object body the gateway will accept, Phase 10 slice 4.
    #
    # Its own setting rather than the gateway's 8 MiB `MAX_BODY_BYTES`, which is
    # sized for a PostgREST insert and would have capped every upload on the
    # platform at 8 MiB -- an incompatibility nobody decided, since Supabase's
    # own default is 50 MB. Configuration rather than a constant because it is a
    # limit, and AGENTS.md does not allow those to be hard-coded.
    #
    # The honest cost: the gateway buffers a request body in memory, so this
    # times a project's concurrency is what one project can occupy, and the
    # answer to wanting it much larger is streaming rather than a bigger number.
    # Resumable uploads are deferred for this phase, which is the other half of
    # why this is a ceiling rather than a chunk size.
    storage_max_upload_bytes: int = 50 * 1024 * 1024

    # The object store (ADR-055). S3 is the provider boundary, so all of this is
    # an endpoint and a credential rather than a driver -- changing provider is
    # changing these values plus a copy.
    #
    # The endpoint must not be loopback either, and for a second reason on top
    # of ADR-035's: an endpoint that only works from the node is an endpoint
    # that has quietly assumed co-location, which is exactly what ADR-055's exit
    # to dedicated hardware depends on nobody having done.
    storage_s3_endpoint: str | None = None
    # One bucket for the whole deployment (ADR-057). A customer "bucket" is a
    # row in their own `storage.buckets`; tenancy for objects lives in the key
    # prefix and the metadata, never in the object store.
    storage_s3_bucket: str = "maludb"
    storage_s3_region: str = "us-east-1"
    # Optional, and only meaningful for a store that has one. Phase 11 slice 4
    # reads a replication factor from it (ADR-069): the S3 endpoint answers
    # requests and says nothing about how many copies of a byte exist, so
    # durability has to be asked for somewhere else or taken on trust. Unset
    # means the platform records the store's durability as *undeclared* rather
    # than assuming it is fine -- an S3 service with no such endpoint is the
    # normal case, not a fault.
    storage_master_endpoint: str | None = None
    storage_s3_access_key: str | None = field(default=None, repr=False)
    storage_s3_secret_key: str | None = field(default=None, repr=False)

    # Phase 07 slice 5. The signup challenge, required from day one because
    # signup is public at launch: by the time farming shows up in the numbers
    # the accounts already exist, and cleaning up a farm is work nobody has
    # budgeted for.
    #
    # `captcha_required` is separate from having a secret on purpose. A
    # deployment that forgot to configure a provider must fail loudly at signup
    # rather than accept everybody because the verifier it built says yes to
    # everything -- which is what the development NullVerifier does.
    captcha_secret: str | None = field(default=None, repr=False)
    captcha_required: bool = False
    # What happens when the challenge service cannot be reached. False -- fail
    # closed -- because the cost of being wrong on signup is permanent
    # (accounts, projects, databases on shared nodes) and the cost of being
    # unavailable is a customer trying again later. See captcha.py.
    captcha_fail_open: bool = False

    # Phase 07 slice 4. Who platform mail comes from -- password resets and
    # anything else addressed to a *platform user* rather than to a project's
    # end users. It is the platform's own account on its own domain, not a
    # customer's sender: a reset for a MaluDB account has nothing to do with
    # whichever project the person happens to own, and sending it from their
    # project's address would be the platform impersonating a customer to that
    # customer. The MaluMail key is the platform key already in this file.
    #
    # Optional, and its absence is felt only where it matters: a control plane
    # that cannot send platform mail still serves every other route, and the
    # reset endpoint reports a platform problem rather than the process
    # refusing to start.
    # Free slice 2: the base URL a project's GoTrue posts its email hook to -- the control
    # plane's internal listener, e.g. http://10.0.0.10:8111 (ADR-037). Read by the gateway
    # on each node when it starts an Auth worker. Unset means no hook, which production
    # refuses rather than start a worker that silently sends nothing.
    email_hook_base_url: str | None = None
    platform_email_from: str | None = None
    platform_email_from_name: str = "MaluDB"
    # Where a reset link points. The dashboard's origin, not this API's: the
    # link goes to a page a person can type a new password into.
    dashboard_url: str = "https://app.maludb.org"

    # Phase 07 slice 0. What an anonymous caller may attempt, and how the caller
    # is identified. Starting values for a public launch rather than approved
    # numbers, and every one is overridable per deployment.
    signup_attempts: int = 5
    signup_window_seconds: int = 3600
    signin_attempts: int = 20
    signin_window_seconds: int = 300
    # Per account rather than per source, so a distributed attempt against one
    # account trips something. Generous enough that a person mistyping their
    # own password is unaffected.
    signin_account_attempts: int = 10
    signin_account_window_seconds: int = 300
    # Phase 09 slice 4, ADR-049. Stripe.
    #
    # Both are optional, and their absence is felt only where it matters: a
    # control plane that cannot take money still serves every other route, and
    # the billing routes answer 503 naming what is missing rather than the
    # process refusing to start. A deployment that is not selling yet is a real
    # deployment.
    #
    # There is no `livemode` setting. It is derived from the key's own prefix
    # (`stripe_api.Client.livemode`), which removes a way to be wrong: a
    # deployment cannot declare itself in test mode while holding a key that
    # charges people.
    stripe_secret_key: str | None = field(default=None, repr=False)
    # The endpoint signing secret, which is what authenticates a webhook.
    # Per endpoint rather than per account, so a deployment holds the one for
    # its own URL and a leaked staging secret cannot sign production events.
    stripe_webhook_secret: str | None = field(default=None, repr=False)
    # Overridable so a test can point at a transport that is not the internet.
    # Nothing in the suite sets it to a real host.
    stripe_api_base: str = "https://api.stripe.com"
    # Phase 09 slice 5, ADR-051. How long a failed payment is tolerated with
    # service entirely unchanged, before the subscription is cancelled and the
    # project reverts to the free tier.
    #
    # Configuration rather than a constant, and that is the development rule
    # against hard-coded plan limits rather than a preference: a grace period
    # *is* a plan limit. A deployment may lengthen it, and nothing in the code
    # may assume its value.
    #
    # It never shortens to zero by accident: `_count` honours zero, so a
    # deployment that wants no grace at all has to ask for it.
    billing_grace_days: int = 14

    # Whether `X-Forwarded-For` may name the client. **False by default, and
    # that default is the safe one**: trusting the header when nothing strips it
    # lets any caller forge the key its limit is counted against, which turns
    # every limit above into a no-op. Set it only where a proxy the platform
    # controls rewrites the header on the way in.
    trust_forwarded_for: bool = False

    # ADR-072. The gateway connects as its own PostgreSQL role, which cannot
    # read `nodes.admin_ciphertext` -- so a compromise of that internet-facing
    # process on a node cannot recover every *other* node's superuser DSN.
    #
    # Defaulted to empty rather than required, and it sits here at the end of
    # the dataclass, because tests construct `Config(...)` directly and a new
    # required field would break them. Empty means "fall back to the control
    # plane's own DSN", which is what development and the suite want and is
    # exactly the misconfiguration that must not reach production -- so the
    # check that matters is `gateway.main.assert_narrowed`, which asks the
    # database what this role can actually read rather than trusting this
    # variable to be set.
    gateway_database_url: str = field(default="", repr=False)
    # ADR-079 memory slice 6a. The query embedder's internal URL, which the gateway
    # asks for a search query's vector. Empty means search by text is not offered on
    # this node: the route answers 503 rather than guessing a host.
    memory_embedder_url: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def safe_database_dsn(self) -> str:
        """Credential-free form of database_url, for logs and diagnostics."""
        return redacted_dsn(self.database_url)


def _hook_base_url(raw: str) -> str | None:
    """The email hook's base URL, or None. Refuses anything but http(s) with a host and no path."""
    value = raw.strip().rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query:
        raise ConfigError(
            f"MALUDB_EMAIL_HOOK_BASE_URL={value!r} must be http(s)://host[:port] -- the control plane's "
            "internal listener, with no path"
        )
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        # GoTrue's own rule, checked here so it fails at gateway start rather than as a 503 on a
        # customer's first Auth request: "only localhost, 127.0.0.1, and ::1 are supported with http".
        raise ConfigError(
            f"MALUDB_EMAIL_HOOK_BASE_URL={value!r}: GoTrue accepts a plain-HTTP hook only on loopback. "
            "Point it at the node's relay (http://127.0.0.1:8119, deploy/maludb-email-hook-relay.socket) "
            "or use https"
        )
    return value


def staff_key_material() -> bytes:
    """The staff key (ADR-082): seals staff TOTP seeds, and must not be the KEK.

    Separate from `Config` on purpose. The admin process loads this and not the KEK,
    so it cannot be a field of the object every other process builds from `load()`.
    """
    return _key_material("MALUDB_STAFF_KEY_REF", "staff-key")


@dataclass(frozen=True)
class AdminConfig:
    """What the operator console's process is given (ADR-082), and deliberately nothing more.

    **No KEK and no platform pepper.** The staff key seals staff second factors and
    derives the staff session pepper; nothing here can open a node credential, a project
    secret, or verify a customer's token. `tests/test_admin_app.py` asserts the fields.

    `database_url` is the console's own role (`MALUDB_ADMIN_DATABASE_URL`), narrowed in
    slice 2, never the control plane's.
    """

    environment: str
    database_url: str = field(repr=False)
    staff_key: bytes = field(repr=False)
    docs_enabled: bool = False
    # The session cookie's Secure flag. On by default; off only where the listener is
    # plain HTTP inside the operator network, which a deployment must say out loud.
    cookie_secure: bool = True
    trust_forwarded_for: bool = False
    signin_attempts: int = 10
    signin_window_seconds: int = 300
    # The grace period the sales report counts down (ADR-051). The same variable the control
    # plane reads, so the two cannot disagree about when a failed payment bites.
    billing_grace_days: int = 14

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def safe_database_dsn(self) -> str:
        return redacted_dsn(self.database_url)


def load_admin() -> AdminConfig:
    """The operator console's configuration. Reads no KEK and no pepper, by construction."""
    environment = os.environ.get("MALUDB_ENV", "development").strip() or "development"
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"MALUDB_ENV must be one of {', '.join(ENVIRONMENTS)}, got {environment!r}")
    return AdminConfig(
        environment=environment,
        database_url=_require("MALUDB_ADMIN_DATABASE_URL"),
        staff_key=staff_key_material(),
        docs_enabled=_flag("MALUDB_ADMIN_DOCS_ENABLED", default=environment != "production"),
        cookie_secure=_flag("MALUDB_ADMIN_COOKIE_SECURE", default=True),
        trust_forwarded_for=_flag("MALUDB_ADMIN_TRUST_FORWARDED_FOR", default=False),
        signin_attempts=_count("MALUDB_ADMIN_SIGNIN_ATTEMPTS", 10),
        signin_window_seconds=_count("MALUDB_ADMIN_SIGNIN_WINDOW_SECONDS", 300),
        billing_grace_days=_count("MALUDB_BILLING_GRACE_DAYS", 14),
    )


def kek_material_if_configured() -> bytes | None:
    """The KEK where this process is configured with one, else None -- for comparing, not using.

    `cp-manage staff` reads it only to refuse a staff key that is the same material.
    """
    if not (os.environ.get("MALUDB_KEK_REF", "").strip() or os.environ.get("CREDENTIALS_DIRECTORY", "").strip()):
        return None
    try:
        return _key_material("MALUDB_KEK_REF", "kek")
    except ConfigError:
        return None


def load() -> Config:
    """Build configuration from the environment, or fail closed."""
    environment = os.environ.get("MALUDB_ENV", "development").strip() or "development"
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"MALUDB_ENV must be one of {', '.join(ENVIRONMENTS)}, got {environment!r}")

    # ADR-024: docs default on outside production, off in production. An
    # explicit override is honoured so staging can expose them deliberately.
    docs_enabled = _flag("MALUDB_DOCS_ENABLED", default=environment != "production")

    return Config(
        environment=environment,
        database_url=_require("MALUDB_CONTROL_PLANE_DATABASE_URL"),
        gateway_domain=os.environ.get("MALUDB_GATEWAY_DOMAIN", "maludb.local").strip(),
        database_domain=os.environ.get(
            "MALUDB_DATABASE_DOMAIN",
            f"db.{os.environ.get('MALUDB_GATEWAY_DOMAIN', 'maludb.local').strip()}",
        ).strip(),
        docs_enabled=docs_enabled,
        kek=_key_material("MALUDB_KEK_REF", "kek"),
        token_pepper=_key_material("MALUDB_TOKEN_PEPPER_REF", "pepper"),
        malumail_api_key=(os.environ.get("MALUMAIL_API", "").strip() or None),
        realtime_db_host=(os.environ.get("MALUDB_REALTIME_DB_HOST", "").strip() or None),
        realtime_db_port=_port("MALUDB_REALTIME_DB_PORT", 5432),
        realtime_image=(
            os.environ.get("MALUDB_REALTIME_IMAGE", "").strip()
            or "docker.io/supabase/realtime:v2.110.0"
        ),
        realtime_memory_max=(os.environ.get("MALUDB_REALTIME_MEMORY_MAX", "").strip() or "512m"),
        storage_port=_port("MALUDB_STORAGE_PORT", 5000),
        storage_admin_port=_port("MALUDB_STORAGE_ADMIN_PORT", 5001),
        storage_image=(
            os.environ.get("MALUDB_STORAGE_IMAGE", "").strip()
            or "docker.io/supabase/storage-api:v1.70.6"
        ),
        storage_memory_max=(os.environ.get("MALUDB_STORAGE_MEMORY_MAX", "").strip() or "1g"),
        storage_db_host=(os.environ.get("MALUDB_STORAGE_DB_HOST", "").strip() or None),
        storage_db_port=_port("MALUDB_STORAGE_DB_PORT", 5432),
        storage_max_upload_bytes=_count(
            "MALUDB_STORAGE_MAX_UPLOAD_BYTES", 50 * 1024 * 1024
        ),
        storage_s3_endpoint=(os.environ.get("MALUDB_STORAGE_S3_ENDPOINT", "").strip() or None),
        storage_s3_bucket=(os.environ.get("MALUDB_STORAGE_S3_BUCKET", "").strip() or "maludb"),
        storage_s3_region=(os.environ.get("MALUDB_STORAGE_S3_REGION", "").strip() or "us-east-1"),
        storage_master_endpoint=(
            os.environ.get("MALUDB_STORAGE_MASTER_ENDPOINT", "").strip() or None
        ),
        storage_s3_access_key=(
            os.environ.get("MALUDB_STORAGE_S3_ACCESS_KEY", "").strip() or None
        ),
        storage_s3_secret_key=(
            os.environ.get("MALUDB_STORAGE_S3_SECRET_KEY", "").strip() or None
        ),
        captcha_secret=(os.environ.get("MALUDB_CAPTCHA_SECRET", "").strip() or None),
        # Required by default in production, where signup faces the internet.
        captcha_required=_flag("MALUDB_CAPTCHA_REQUIRED", default=environment == "production"),
        captcha_fail_open=_flag("MALUDB_CAPTCHA_FAIL_OPEN", default=False),
        email_hook_base_url=_hook_base_url(os.environ.get("MALUDB_EMAIL_HOOK_BASE_URL", "")),
        platform_email_from=(os.environ.get("MALUDB_PLATFORM_EMAIL_FROM", "").strip() or None),
        platform_email_from_name=(
            os.environ.get("MALUDB_PLATFORM_EMAIL_FROM_NAME", "").strip() or "MaluDB"
        ),
        dashboard_url=(
            os.environ.get("MALUDB_DASHBOARD_URL", "").strip() or "https://app.maludb.org"
        ),
        signup_attempts=_count("MALUDB_SIGNUP_ATTEMPTS", 5),
        signup_window_seconds=_count("MALUDB_SIGNUP_WINDOW_SECONDS", 3600),
        signin_attempts=_count("MALUDB_SIGNIN_ATTEMPTS", 20),
        signin_window_seconds=_count("MALUDB_SIGNIN_WINDOW_SECONDS", 300),
        signin_account_attempts=_count("MALUDB_SIGNIN_ACCOUNT_ATTEMPTS", 10),
        signin_account_window_seconds=_count("MALUDB_SIGNIN_ACCOUNT_WINDOW_SECONDS", 300),
        trust_forwarded_for=_flag("MALUDB_TRUST_FORWARDED_FOR", default=False),
        gateway_database_url=os.environ.get("MALUDB_GATEWAY_DATABASE_URL", "").strip(),
        memory_embedder_url=_embedder_url(os.environ.get("MALUDB_MEMORY_EMBEDDER_URL", "")),
        stripe_secret_key=(os.environ.get("MALUDB_STRIPE_SECRET_KEY", "").strip() or None),
        stripe_webhook_secret=(
            os.environ.get("MALUDB_STRIPE_WEBHOOK_SECRET", "").strip() or None
        ),
        stripe_api_base=(
            os.environ.get("MALUDB_STRIPE_API_BASE", "").strip() or "https://api.stripe.com"
        ),
        billing_grace_days=_count("MALUDB_BILLING_GRACE_DAYS", 14),
    )


def _embedder_url(value: str) -> str:
    """The query embedder's URL, or refuse (ADR-079 memory slice 6a).

    The gateway sends it the customer's secret key with every search by text, so it
    must be an address literal on the private network or loopback: a hostname could
    resolve anywhere, and a public address would carry keys off the platform.
    """
    value = value.strip().rstrip("/")
    if not value:
        return ""
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    try:
        address = ipaddress.ip_address(parts.hostname or "")
        private = (address.is_private or address.is_loopback) and not address.is_unspecified
    except ValueError:
        private = False
    if parts.scheme not in ("http", "https") or not private or parts.path not in ("", "/") or parts.query:
        raise ConfigError("MALUDB_MEMORY_EMBEDDER_URL must be http(s)://<private or loopback address>:<port>, "
                          f"got {value!r}")
    return value


def _count(name: str, default: int) -> int:
    """A non-negative attempt count or window from the environment.

    Unusable values fall back to the default rather than raising, for the reason
    `_port` gives, with one difference that matters here: **zero is honoured**.
    A limit of zero attempts closes the route, which is a usable thing to want
    during an incident, so it must not be mistaken for "unset".
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _port(name: str, default: int) -> int:
    """A TCP port from the environment, or the default for anything unusable.

    Deliberately not fatal. A mistyped port here would stop the gateway serving
    every surface, not just Realtime, and a gateway that refuses to start is a
    worse outcome than one whose Realtime proxy cannot connect and says so.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default
