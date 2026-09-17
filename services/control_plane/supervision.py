"""Starting and stopping one project's worker through systemd (ADR-027).

Split out of `workers` for ADR-083. The node's maintenance pass sleeps idle workers and
must not import `workers`, which reaches provisioning and tenant bootstrap: everything
here needs only a validated project ref and `systemctl`. `workers` re-exports every
name, so the gateway, the control plane and the tests are unchanged.
"""

from __future__ import annotations

import subprocess
from typing import Protocol

from services.control_plane import models

# The per-project units. Each worker module names its own as `SERVICE_TEMPLATE`, from here.
POSTGREST_TEMPLATE = "maludb-postgrest@{ref}.service"
GOTRUE_TEMPLATE = "maludb-gotrue@{ref}.service"
REALTIME_TEMPLATE = "maludb-realtime@{ref}.service"


class WorkerError(RuntimeError):
    """A worker could not be configured, started, or made ready."""


class Supervisor(Protocol):
    """Start and stop one project's worker. Narrow on purpose."""

    def start(self, project_ref: str) -> None: ...
    def stop(self, project_ref: str) -> None: ...
    def is_active(self, project_ref: str) -> bool: ...


class SystemdSupervisor:
    """ADR-027: workers are `maludb-postgrest@<ref>.service` template units.

    The control plane asks systemd rather than spawning children, so a control
    plane restart does not orphan every tenant's worker and an operator can
    inspect one with tools that predate this codebase.
    """

    def __init__(
        self,
        *,
        systemctl: str = "systemctl",
        use_sudo: bool = False,
        template: str = POSTGREST_TEMPLATE,
    ) -> None:
        self._prefix = ["sudo", "-n", systemctl] if use_sudo else [systemctl]
        self._template = template

    def unit_for(self, project_ref: str) -> str:
        """The unit name for a project, refusing an invalid ref.

        `AGENTS.md` requires identifiers generated from project metadata to be
        validated, and a systemd unit name is one: an unchecked ref could name a
        different unit entirely. Nothing here runs through a shell and arguments
        are passed as a list, so this is not command injection -- it is the
        weaker but real risk of acting on the wrong target.
        """
        if not models.is_valid_project_ref(project_ref):
            raise WorkerError(f"invalid project ref {project_ref!r}")
        return self._template.format(ref=project_ref)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # ruff S603: the executable is fixed, arguments are a list rather than a
        # shell string, and every project ref is validated by unit_for above.
        return subprocess.run(  # noqa: S603
            [*self._prefix, *args], capture_output=True, text=True, check=False
        )

    def start(self, project_ref: str) -> None:
        unit = self.unit_for(project_ref)
        result = self._run("start", unit)
        if result.returncode != 0:
            # systemd's stderr names the unit and the failure, and carries no
            # credential -- the secrets are in the config file, not the command.
            raise WorkerError(f"could not start {unit}: {result.stderr.strip()}")

    def stop(self, project_ref: str) -> None:
        unit = self.unit_for(project_ref)
        result = self._run("stop", unit)
        if result.returncode != 0:
            raise WorkerError(f"could not stop {unit}: {result.stderr.strip()}")

    def is_active(self, project_ref: str) -> bool:
        return self._run("is-active", self.unit_for(project_ref)).returncode == 0
