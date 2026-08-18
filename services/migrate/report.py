"""Rendering a scan, and the number this deliberately does not make up.

Two outputs, both from the same `Scan`: text for a person deciding whether to
book a maintenance window, and JSON for slice 8's runbook and anything that
comes after it. Neither ever contains the source DSN.

**The freeze window.** ADR-044 commits to publishing one as a function of data
size, measured by Phase 08's validation runs. Those runs are slice 8. Until a
rate exists, this reports the measured size and says the window is not measured
yet -- it does not divide by a plausible-looking constant and print a figure
somebody would schedule an outage around. `api/usage.py` draws the same line
when it reports `metered: false` rather than a zero: a number nobody measured is
worse than an admission that nobody has.

`--throughput-mb-per-s` exists for the person who *has* measured, which after
slice 8 is this project. When it is supplied the window is arithmetic and is
labelled with the rate it came from.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from services.migrate.rules import BLOCKER, Scan

# Rendering only. A finding records every object it found; a terminal that
# printed forty thousand table names would bury the sentence above them.
MAX_ITEMS_SHOWN = 10

# And each one is bounded, because a PostgreSQL identifier may be 63 bytes but a
# bucket name or a provider is free text from the source database.
MAX_ITEM_LENGTH = 120

# **Everything a finding lists comes from the source database**: table, schema,
# trigger and extension names, and free-text columns like `storage.buckets.name`
# and `auth.identities.provider`. Printing those raw into a terminal lets
# whoever can write them repaint this report -- and this report is the artefact
# a customer reads to decide whether their cutover is safe, so spoofing it is
# the whole attack. C0 and C1 controls are stripped: ESC for cursor and colour
# sequences, and plain CR/LF, which forge report lines without needing ESC at
# all.
_CONTROL_CHARACTERS = dict.fromkeys(
    [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)], None
)


def sanitise(value: str) -> str:
    """What a terminal may be given. The JSON keeps the byte-accurate name.

    `json.dumps` escapes control bytes itself, so a runbook consuming the JSON
    still sees exactly what is in the customer's catalogue -- which is what it
    needs, since it is not painting a screen with it.
    """
    cleaned = str(value).translate(_CONTROL_CHARACTERS)
    if len(cleaned) > MAX_ITEM_LENGTH:
        cleaned = cleaned[:MAX_ITEM_LENGTH] + "..."
    return cleaned


def freeze_estimate(database_bytes: int, throughput_mb_per_s: float | None) -> dict[str, Any]:
    """The ADR-044 window, or an honest statement that nobody has measured it."""
    if not throughput_mb_per_s or throughput_mb_per_s <= 0:
        return {
            "database_bytes": database_bytes,
            "seconds": None,
            "measured": False,
            "note": (
                "Not measured yet. Phase 08's validation runs establish the rate "
                "(ADR-044); until then, pass --throughput-mb-per-s with a figure you have "
                "measured against your own data rather than trusting an estimate."
            ),
        }
    seconds = (database_bytes / (1024 * 1024)) / throughput_mb_per_s
    return {
        "database_bytes": database_bytes,
        "seconds": round(seconds),
        "measured": True,
        "note": (
            f"At {throughput_mb_per_s:g} MB/s over {_mb(database_bytes):.1f} MB. Copy time "
            "only: it excludes index rebuilds, the scan itself, and your own cutover steps."
        ),
    }


def as_json(scan: Scan, freeze: dict[str, Any]) -> str:
    return json.dumps(
        {
            "migratable": scan.migratable,
            "summary": scan.summary,
            "freeze": freeze,
            "findings": [asdict(finding) for finding in scan.findings],
        },
        indent=2,
        sort_keys=False,
    )


def as_text(scan: Scan, freeze: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = scan.summary

    lines.append("MaluDB migration scan")
    lines.append("=" * 60)
    lines.append(f"  source          {sanitise(summary.get('server_version', 'unknown'))}")
    lines.append(f"  size            {_mb(summary.get('database_bytes', 0)):.1f} MB")
    lines.append(
        f"  schema          {summary.get('tables', 0)} tables, "
        f"{summary.get('views', 0)} views, {summary.get('policies', 0)} policies, "
        f"{summary.get('functions', 0)} functions, {summary.get('triggers', 0)} triggers"
    )
    unanalysed = summary.get("tables_never_analysed", 0)
    rows_line = f"  rows (estimate) {summary.get('estimated_rows', 0):,}"
    if unanalysed:
        # Said rather than folded into the total: the planner has never counted
        # these, and a number that quietly excluded them would be read as
        # complete.
        rows_line += f"  ({unanalysed} table(s) never analysed, not counted)"
    lines.append(rows_line)
    lines.append(f"  auth users      {summary.get('auth_users', 0)}")
    if summary.get("storage_objects"):
        lines.append(
            f"  storage         {summary['storage_objects']} objects, "
            f"{_mb(summary.get('storage_bytes', 0)):.1f} MB"
        )
    lines.append("")

    if not scan.findings:
        lines.append("No findings. Every surface this project uses is supported.")
    for finding in scan.findings:
        marker = "BLOCKER" if finding.severity == BLOCKER else "warning"
        lines.append(f"[{marker}] {sanitise(finding.title)}")
        lines.extend(_wrap(finding.detail, "    "))
        for item in finding.items[:MAX_ITEMS_SHOWN]:
            lines.append(f"      - {sanitise(item)}")
        if len(finding.items) > MAX_ITEMS_SHOWN:
            lines.append(f"      ... and {len(finding.items) - MAX_ITEMS_SHOWN} more")
        if finding.phase:
            lines.append(f"    Arrives in phase {finding.phase}.")
        if finding.remedy:
            lines.extend(_wrap(f"What to do: {finding.remedy}", "    "))
        lines.append("")

    lines.append("-" * 60)
    if freeze["seconds"] is not None:
        lines.append(f"Expected write freeze: about {_duration(freeze['seconds'])}.")
    else:
        lines.append("Expected write freeze: not measured yet.")
    lines.extend(_wrap(freeze["note"], "  "))
    lines.append("")
    lines.extend(
        _wrap(
            "The freeze is yours to apply. MaluDB cannot stop writes to a Supabase project, "
            "and a migration that ran while writes continued produces a copy quietly missing "
            "rows -- so stop your application's writes before the final sync, and check the "
            "row counts in the validation report afterwards.",
            "  ",
        )
    )
    lines.append("")

    if scan.blockers:
        lines.append(
            f"NOT MIGRATABLE YET: {len(scan.blockers)} blocker(s), "
            f"{len(scan.warnings)} warning(s)."
        )
    else:
        lines.append(f"Ready to migrate. {len(scan.warnings)} warning(s) to read first.")
    return "\n".join(lines)


def _wrap(text: str, indent: str, width: int = 76) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _mb(byte_count: int) -> float:
    return byte_count / (1024 * 1024)


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"
