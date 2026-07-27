"""Map a server software name to its provider.

Adding a new software (Purpur, Fabric, Forge, …) is a new provider module plus
one entry here — nothing else changes.
"""

from __future__ import annotations

from minemanager_hub.providers.base import Provider
from minemanager_hub.providers.paper import PaperProvider
from minemanager_hub.providers.vanilla import VanillaProvider


def _build_registry() -> dict[str, Provider]:
    return {
        "vanilla": VanillaProvider(),
        "paper": PaperProvider(project="paper", software="paper", label="Paper"),
        "velocity": PaperProvider(project="velocity", software="velocity", label="Velocity"),
    }


_registry = _build_registry()


def get_provider(software: str) -> Provider | None:
    return _registry.get(software)


def supported_software() -> list[str]:
    return list(_registry)
