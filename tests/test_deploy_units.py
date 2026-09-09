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


@pytest.mark.parametrize("unit", [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT])
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


@pytest.mark.parametrize("unit", [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT])
def test_units_do_not_run_as_root(unit):
    text = _read(unit)
    users = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("User=")]
    assert users, f"{unit.name} sets no User=, so it runs as root"
    assert users[0] != "root", f"{unit.name} runs as root"


@pytest.mark.parametrize("unit", [PUBLIC_UNIT, INTERNAL_UNIT, GATEWAY_UNIT])
def test_units_carry_the_hardening_the_others_do(unit):
    """Matched against `maludb-provisioner.service`, which set the pattern."""
    text = _read(unit)
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "RestrictNamespaces=true",
    ):
        assert directive in text, f"{unit.name} is missing {directive}"
