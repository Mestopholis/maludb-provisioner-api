"""The systemd units, asserted rather than reviewed.

ADR-037 splits the control plane into two applications and says in its own
consequences that `deploy/` must describe the second listener. For several
phases it did not, and the property that matters -- the internal application is
not reachable from the internet -- was carried by prose and by whoever was
typing. `AGENTS.md` records what happens to controls held that way: twice a
security review was carried by a checklist, and twice it was skipped.

So these tests exist to fail when someone "simplifies" a unit file. They are
cheap and they are not clever; the mistakes they catch are the realistic ones:
copying the public unit to make the internal one and forgetting to change the
factory, or pasting `0.0.0.0` into the internal unit to make a connection work.

**A unit file is not a security control.** A bind address stops the process
listening everywhere; it does not stop a reverse proxy, a published container
port or a permissive security group putting the listener back on the internet.
`docs/DEPLOYMENT.md` verifies the property from outside the host, which is the
only place it is real. These tests defend the file, which is the part a future
diff can change without anyone noticing.
"""

from __future__ import annotations

import pathlib

import pytest

DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"

PUBLIC_UNIT = DEPLOY / "maludb-control-plane-public.service"
INTERNAL_UNIT = DEPLOY / "maludb-control-plane-internal.service"
GATEWAY_UNIT = DEPLOY / "maludb-gateway.service"
MEMORY_UNIT = DEPLOY / "maludb-memory-worker.service"
EGRESS_UNIT = DEPLOY / "maludb-egress-proxy.service"
MEMORY_ENV = DEPLOY / "memory-worker.env.example"


def _read(unit: pathlib.Path) -> str:
    assert unit.exists(), f"{unit.name} is missing; docs/DEPLOYMENT.md installs it"
    return unit.read_text()


def _exec_start(unit: pathlib.Path) -> str:
    """ExecStart, with systemd's line continuations folded out."""
    text = _read(unit).replace("\\\n", " ")
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            return " ".join(line.split())
    raise AssertionError(f"{unit.name} has no ExecStart")


# -- the factory, which is the whole difference between the two units ------


def test_the_public_unit_serves_only_the_public_application():
    """`create_app` here would put the email hook on the internet."""
    exec_start = _exec_start(PUBLIC_UNIT)
    assert "services.control_plane.main:create_public_app" in exec_start, exec_start
    assert "main:create_app " not in exec_start, (
        "the public unit runs the INTERNAL factory: create_app mounts every "
        "router, including /internal/hooks/email. See ADR-037."
    )


def test_the_internal_unit_serves_the_internal_application():
    exec_start = _exec_start(INTERNAL_UNIT)
    assert "services.control_plane.main:create_app" in exec_start, exec_start
    assert "create_public_app" not in exec_start, (
        "the internal unit runs the public factory, so the routes it exists to "
        "serve -- the email hook -- are not mounted at all"
    )


# -- the bind address ------------------------------------------------------


@pytest.mark.parametrize("unit", [INTERNAL_UNIT, PUBLIC_UNIT])
def test_neither_control_plane_unit_binds_every_interface(unit):
    """0.0.0.0 in either of these is wrong, for different reasons.

    In the internal unit it publishes routes whose only other protection is a
    signature. In the public unit it exposes an API carrying bearer tokens with
    nothing terminating TLS -- that unit binds loopback because a reverse proxy
    is in front.
    """
    exec_start = _exec_start(unit)
    # noqa S104 flags the literal as "possible binding to all interfaces". Here
    # it is the assertion that the string is *absent*, which is the inverse.
    assert "0.0.0.0" not in exec_start, (  # noqa: S104
        f"{unit.name} binds every interface: {exec_start}"
    )
    assert "--host" in exec_start, f"{unit.name} does not pass an explicit --host"


def test_the_internal_bind_address_comes_from_configuration():
    """Hard-coding a private address here would be a lie on most hosts.

    A fixed 10.x is wrong wherever the private range differs, and the resulting
    edit -- by somebody making the service start -- is exactly where 0.0.0.0
    gets pasted in. The variable makes the correct value a deployment decision
    with a documented constraint beside it.
    """
    exec_start = _exec_start(INTERNAL_UNIT)
    assert "${MALUDB_INTERNAL_BIND}" in exec_start, exec_start
    example = (DEPLOY / "control-plane.env.example").read_text()
    assert "MALUDB_INTERNAL_BIND=" in example
    assert "Not 0.0.0.0" in example, (
        "the example does not say what the value must not be, which is the only "
        "part of it that stops a mistake"
    )


# -- the gateway -----------------------------------------------------------


def test_the_gateway_reads_its_own_environment_file():
    """ADR-072: the narrowed DSN must not share a file with the wide one.

    A gateway that inherited control-plane.env would be one line away from
    connecting as the control plane and recovering every node's superuser DSN.
    """
    text = _read(GATEWAY_UNIT)
    assert "EnvironmentFile=/etc/maludb/gateway.env" in text
    assert "control-plane.env" not in text, (
        "the gateway unit reads the control plane's environment file, which "
        "carries the unnarrowed database URL (ADR-072)"
    )


def test_the_gateway_environment_names_its_own_role():
    example = (DEPLOY / "gateway.env.example").read_text()
    assert "MALUDB_GATEWAY_DATABASE_URL=" in example
    assert "cp-manage gateway grant" in example, (
        "the example does not say how to create the narrowed role, so the "
        "obvious thing to do is paste in the control plane's DSN"
    )


# -- conventions the other five units already follow -----------------------


@pytest.mark.parametrize("unit", [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT, MEMORY_UNIT])
def test_units_keep_secrets_out_of_systemctl_show(unit):
    """`Environment=` renders in `systemctl show`; `EnvironmentFile=` does not.

    The existing five units all use the file form for this reason, and a
    database password or a KEK path in process metadata is a gift to anything
    that can read it.
    """
    text = _read(unit)
    assert "EnvironmentFile=" in text, f"{unit.name} does not use an environment file"
    leaked = [
        line
        for line in text.splitlines()
        if line.startswith("Environment=")
        and any(k in line for k in ("PASSWORD", "DATABASE_URL", "SECRET", "KEK"))
    ]
    assert not leaked, f"{unit.name} puts a secret in systemctl show: {leaked}"


@pytest.mark.parametrize("unit", [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT, MEMORY_UNIT])
def test_units_do_not_run_as_root(unit):
    text = _read(unit)
    users = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("User=")]
    assert users, f"{unit.name} sets no User=, so it runs as root"
    assert users[0] != "root", f"{unit.name} runs as root"


@pytest.mark.parametrize("unit", [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT, MEMORY_UNIT])
def test_units_carry_the_hardening_the_others_do(unit):
    """Matched against `maludb-provisioner.service`, which set the pattern."""
    text = _read(unit)
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "PrivateTmp=true",
        "RestrictNamespaces=true",
    ):
        assert directive in text, f"{unit.name} is missing {directive}"



KEYED_UNITS = [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT, MEMORY_UNIT, DEPLOY / "maludb-provisioner.service",
               DEPLOY / "maludb-memory-embedder.service"]


@pytest.mark.parametrize("unit", KEYED_UNITS, ids=lambda u: u.name)
def test_units_that_hold_the_kek_load_it_as_a_credential(unit):
    """Found by the deployment rehearsal. Each runs as its own user and the loader requires
    mode 600, so a root-owned key file is readable by none of them: both listeners failed
    with PermissionError on /etc/maludb/keys/kek. A credential is a private copy per unit."""
    text = _read(unit)
    assert "LoadCredential=kek:/etc/maludb/keys/kek" in text, f"{unit.name} cannot read the KEK"
    assert "LoadCredential=pepper:/etc/maludb/keys/pepper" in text, f"{unit.name} cannot read the pepper"


def test_every_unit_that_runs_platform_code_with_a_key_is_listed():
    for unit in DEPLOY.glob("*.service"):
        if "LoadCredential=kek:" in unit.read_text():
            assert unit in KEYED_UNITS, unit.name
    assert "LoadCredential" not in _read(EGRESS_UNIT), "the egress proxy holds nothing"


@pytest.mark.parametrize("unit", sorted(DEPLOY.glob("*.service")), ids=lambda u: u.name)
def test_units_that_reach_postgresql_through_libpq_hide_home_without_denying_it(unit):
    """Found by the deployment rehearsal: the gateway's pool never initialised against the
    control plane's database with `sslmode=require`, because `ProtectHome=true` turns libpq's
    client-certificate lookup into "Permission denied". Units whose code never opens a
    PostgreSQL connection (Realtime, Storage, the egress proxy) are not affected."""
    text = _read(unit)
    if not any(entry in text for entry in ("/opt/maludb/.venv/bin/", "/usr/local/bin/postgrest")):
        return
    if "egress_proxy" in text:
        return
    assert "ProtectHome=true" not in text, f"{unit.name}: TLS connections to PostgreSQL fail under ProtectHome=true"
    assert "ProtectHome=tmpfs" in text, f"{unit.name} no longer hides /home"

# -- the memory worker (ADR-079, memory slice 5a) -----------------------------


def test_the_memory_worker_is_not_the_provisioner_and_holds_none_of_its_file():
    """Decision 6: a compromised memory worker reaches memory, not the fleet. The
    provisioner's environment carries node superuser credentials; sharing its file
    or its user would hand them over."""
    text = _read(MEMORY_UNIT)
    assert "services.control_plane.memory_worker" in text
    assert "provisioner.env" not in text and "control-plane.env" not in text
    users = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("User=")]
    assert users == ["maludb-memory"], users


def test_the_memory_worker_has_no_route_to_the_internet():
    """It writes to tenant databases on private addresses and reaches model providers
    only through the egress proxy on loopback (slice 5b). Widening this line for a
    provider would bypass the only place the three hosts are enforced."""
    text = _read(MEMORY_UNIT)
    assert "IPAddressDeny=any" in text
    allowed = " ".join(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("IPAddressAllow="))
    for entry in allowed.split():
        assert entry in ("localhost", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"), entry



# -- the egress proxy (ADR-079, memory slice 5b) --------------------------------


def test_the_worker_is_pointed_at_the_proxy_and_starts_after_it():
    assert "MALUDB_MEMORY_EGRESS_PROXY=http://127.0.0.1:" in _read(MEMORY_ENV)
    assert "maludb-egress-proxy.service" in _read(MEMORY_UNIT)


def test_the_egress_proxy_holds_nothing_and_runs_as_its_own_user():
    """The only unit with internet access carries no database URL and no KEK: a
    compromised proxy reaches three provider hosts and no secret."""
    text = _read(EGRESS_UNIT)
    assert "services.control_plane.egress_proxy" in text
    assert "EnvironmentFile" not in text, "the proxy reads no environment file, so it cannot be handed a secret"
    assert "InaccessiblePaths=/etc/maludb" in text
    users = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("User=")]
    assert users == ["maludb-egress"], users


def test_the_egress_proxy_listens_on_loopback_and_cannot_reach_private_ranges():
    """The code refuses a private address; the unit refuses it again, so a mistake in
    one still leaves the node's network out of reach."""
    text = _read(EGRESS_UNIT)
    listen = [line.split("=", 2)[2] for line in text.splitlines()
              if line.startswith("Environment=MALUDB_EGRESS_PROXY_LISTEN=")]
    assert listen and listen[0].startswith("127.0.0.1:"), listen
    denied = " ".join(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("IPAddressDeny="))
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7"):
        assert cidr in denied.split(), cidr
    assert "IPAddressAllow=" not in text, "an allow line would override the private-range denial"


# -- the query embedder (ADR-079, memory slice 6a) --------------------------------

EMBEDDER_UNIT = DEPLOY / "maludb-memory-embedder.service"
EMBEDDER_ENV = DEPLOY / "memory-embedder.env.example"


def test_the_embedder_has_its_own_user_file_and_no_route_to_the_internet():
    """It holds provider keys and verifies customers' keys: not the worker's file, not the
    control plane's, and providers only through the proxy on loopback."""
    text = _read(EMBEDDER_UNIT)
    assert "services.control_plane.memory_embedder" in text
    assert "memory-embedder.env" in text and "memory-worker.env" not in text and "control-plane.env" not in text
    users = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("User=")]
    assert users == ["maludb-embedder"], users
    assert "IPAddressDeny=any" in text
    allowed = " ".join(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("IPAddressAllow="))
    for entry in allowed.split():
        assert entry in ("localhost", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"), entry
    env = _read(EMBEDDER_ENV)
    assert "MALUDB_MEMORY_EGRESS_PROXY=http://127.0.0.1:" in env
    assert "memembed:" in env, "the example must show the embedder's own role, not the control plane's"
