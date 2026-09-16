"""Shared by the console frontend tests: every `${...}` in a source, at every depth."""

from __future__ import annotations

import re


def interpolations(source: str) -> list[str]:
    found = []
    for start in (m.end() for m in re.finditer(r"\$\{", source)):
        depth = 1
        i = start
        while depth and i < len(source):
            depth += {"{": 1, "}": -1}.get(source[i], 0)
            i += 1
        found.append(source[start:i - 1].strip())
    return found
