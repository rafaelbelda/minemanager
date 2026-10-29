"""Which Java each server version needs, and which Java MineManager installs.

Shared so the hub (auto-pick at creation, the requirement API) and the UI's
data come from one table.

Sources, checked 2026-10: Mojang's per-version ``javaVersion.majorVersion``
(piston-meta) and PaperMC Fill v3's ``java.version.minimum``. Both agree for
Minecraft; Velocity has its own line. 1.6.1-1.7.1 carry no data upstream and
Paper 1.7.10 claims 6; Java 8 runs all of them, so the table starts there.

``minimum`` is a hard floor: the server will not start below it. ``recommended``
is the managed release to install for it, which differs only where upstream
asks for a non-LTS release (16) or one Temurin no longer ships for servers (11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: The Temurin feature releases MineManager can install. Between them they
#: cover every row below.
MANAGED_FEATURES: tuple[int, ...] = (8, 17, 21, 25)


@dataclass(frozen=True)
class Rule:
    since: str          # first version this rule applies to
    minimum: int
    recommended: int


# Ascending by ``since``. A version takes the last rule it is at or above.
_MINECRAFT: tuple[Rule, ...] = (
    Rule("1.0", 8, 8),
    Rule("1.17", 16, 17),
    Rule("1.18", 17, 17),
    Rule("1.20.5", 21, 21),
    Rule("26.1", 25, 25),
)

_VELOCITY: tuple[Rule, ...] = (
    Rule("1.0", 8, 8),
    Rule("3.0", 11, 17),
    Rule("3.3", 17, 17),
    Rule("3.5", 21, 21),
    Rule("4.0", 25, 25),
)

RULES: dict[str, tuple[Rule, ...]] = {
    "vanilla": _MINECRAFT,
    "paper": _MINECRAFT,
    "velocity": _VELOCITY,
}

# Old servers were written for 8. Newer Java usually runs vanilla, but old
# plugins and mods often break on it, so it is worth a warning, not a refusal.
LEGACY_MAX = 8

# The whole id, or up to a pre-release suffix: "24w14a" is a snapshot, not 24.
_NUM_RE = re.compile(r"^\d+(?:\.\d+)*(?=$|[-\s])")


def parse_version(version: str | None) -> Optional[tuple[int, ...]]:
    """``"1.20.4"`` -> ``(1, 20, 4)``; ``"1.21.11-pre3"`` -> ``(1, 21, 11)``.

    None for anything without a leading dotted number (snapshots like
    ``24w14a``, empty, None).
    """
    m = _NUM_RE.match((version or "").strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(0).split("."))


@dataclass(frozen=True)
class Requirement:
    minimum: int
    recommended: int
    #: Built for Java 8; newer Java may break old plugins and mods.
    legacy: bool


def requirement(software: str, version: str | None) -> Optional[Requirement]:
    """What ``software`` at ``version`` needs, or None when it cannot be told."""
    rules = RULES.get(software)
    parsed = parse_version(version)
    if rules is None or parsed is None:
        return None
    match: Rule | None = None
    for rule in rules:
        if parsed >= parse_version(rule.since):
            match = rule
    if match is None:            # older than the table: treat like its first row
        match = rules[0]
    return Requirement(match.minimum, match.recommended, legacy=match.minimum <= LEGACY_MAX)


def satisfies(req: Requirement, major: int) -> bool:
    return major >= req.minimum


def pick(req: Requirement, majors: list[int]) -> Optional[int]:
    """Best of the available ``majors`` for ``req``: the recommended release if
    present, else the lowest one that satisfies it (closest to what the server
    was built for). None if none does.
    """
    ok = sorted(m for m in set(majors) if satisfies(req, m))
    if not ok:
        return None
    return req.recommended if req.recommended in ok else ok[0]


def table() -> dict[str, list[dict]]:
    """The rules as plain data, for the UI."""
    return {
        software: [
            {"since": r.since, "minimum": r.minimum, "recommended": r.recommended}
            for r in rules
        ]
        for software, rules in RULES.items()
    }
