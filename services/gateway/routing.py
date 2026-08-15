"""Resolving a request's project from its hostname (ADR-008).

`docs/API-GATEWAY.md`: *do not trust `Host` without allowed-domain validation*.

The Host header is attacker-controlled on every request. Treating it as a
project identifier without checking the domain means anyone can name any
project, and combined with a key check that trusted the same header it would
mean naming any project *they hold a key for* — which is every project, given
one free account. So the domain is verified before the label is even looked at,
and the label is validated by the same rule that guards generated SQL
identifiers.
"""

from __future__ import annotations

from services.control_plane import models


class RoutingError(ValueError):
    """The request cannot be attributed to a project."""


def project_ref_from_host(host: str | None, *, gateway_domain: str) -> str:
    """Extract the project ref from `<ref>.<gateway_domain>`.

    Raises `RoutingError` for anything else. Every rejection is deliberately
    the same exception with the same shape, so a caller cannot accidentally
    turn "wrong domain" into a different HTTP response from "no such project"
    and hand out a way to enumerate refs.
    """
    if not host:
        raise RoutingError("no Host header")

    # A Host may carry a port, and IPv6 literals carry brackets. Neither is a
    # valid project hostname, but stripping the port first means `ref.example
    # .com:443` resolves rather than being rejected for a reason the client
    # cannot see.
    candidate = host.strip().lower()
    if candidate.startswith("["):
        raise RoutingError("IPv6 literal is not a project hostname")
    candidate = candidate.split(":", 1)[0].rstrip(".")

    domain = gateway_domain.strip().lower().rstrip(".")
    if not domain:
        raise RoutingError("no gateway domain configured")

    suffix = f".{domain}"
    if not candidate.endswith(suffix):
        raise RoutingError(f"host {candidate!r} is not under {domain!r}")

    label = candidate[: -len(suffix)]
    if not label:
        raise RoutingError("no project label in host")
    # Exactly one label. `a.b.maludb.com` must not resolve to project `b` or to
    # project `a.b`: a wildcard certificate covers one level, and accepting more
    # invites a name that means different things to the proxy and to us.
    if "." in label:
        raise RoutingError(f"host {candidate!r} has more than one label under {domain!r}")

    # The same validation that guards generated SQL identifiers. A ref reaching
    # this far is used to look up a project and, on a wake, to name a systemd
    # unit.
    if not models.is_valid_project_ref(label):
        raise RoutingError(f"{label!r} is not a valid project ref")
    return label
