"""The Minecraft/Velocity -> Java table and the pick rule."""

from __future__ import annotations

import pytest

from minemanager_shared import java


@pytest.mark.parametrize("software,version,minimum,recommended", [
    ("vanilla", "1.5.2", 8, 8),
    ("paper", "1.7.10", 8, 8),
    ("paper", "1.16.5", 8, 8),
    ("paper", "1.17", 16, 17),
    ("paper", "1.17.1", 16, 17),
    ("vanilla", "1.18", 17, 17),
    ("paper", "1.20.4", 17, 17),
    ("paper", "1.20.5", 21, 21),
    ("vanilla", "1.21.11", 21, 21),
    ("vanilla", "1.21.11-pre3", 21, 21),
    ("paper", "26.1", 25, 25),
    ("paper", "26.3", 25, 25),
    ("velocity", "1.1.9", 8, 8),
    ("velocity", "3.1.1", 11, 17),
    ("velocity", "3.4.0", 17, 17),
    ("velocity", "3.5.1", 21, 21),
    ("velocity", "4.3.1", 25, 25),
])
def test_requirement(software, version, minimum, recommended):
    req = java.requirement(software, version)
    assert (req.minimum, req.recommended) == (minimum, recommended)


def test_every_recommendation_is_installable():
    for rules in java.RULES.values():
        for rule in rules:
            assert rule.recommended in java.MANAGED_FEATURES
            assert rule.recommended >= rule.minimum


@pytest.mark.parametrize("software,version", [("paper", None), ("paper", "24w14a"), ("forge", "1.20")])
def test_unknown_is_none(software, version):
    assert java.requirement(software, version) is None


def test_legacy_flag():
    assert java.requirement("paper", "1.12.2").legacy
    assert not java.requirement("paper", "1.18").legacy


def test_pick_prefers_recommended_then_lowest_satisfying():
    req = java.requirement("paper", "1.20.4")          # min 17
    assert java.pick(req, [8, 17, 21]) == 17
    assert java.pick(req, [8, 25, 21]) == 21
    assert java.pick(req, [8, 11]) is None
    legacy = java.requirement("paper", "1.12.2")
    assert java.pick(legacy, [21, 17]) == 17
