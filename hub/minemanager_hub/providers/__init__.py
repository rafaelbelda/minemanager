"""Software-agnostic version/build providers.

Each supported server software supplies available versions, optional builds, and
a resolved download (URL + checksum + target filename) through a common
:class:`~minemanager_hub.providers.base.Provider` interface. The version REST
API and the updater consume that interface only — adding Purpur, Fabric, Forge,
etc. later is a new provider module and one line in the registry, with no change
to the API or the UI.
"""

from minemanager_hub.providers.base import (
    Build,
    Download,
    Provider,
    ProviderError,
    Version,
)
from minemanager_hub.providers.registry import get_provider, supported_software

__all__ = [
    "Build",
    "Download",
    "Provider",
    "ProviderError",
    "Version",
    "get_provider",
    "supported_software",
]
