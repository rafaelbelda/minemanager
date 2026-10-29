"""Eclipse Temurin runtimes from the Adoptium API.

The agent reports its platform; this resolves the newest GA JRE for a feature
release on it. Same contract as the server-jar providers: the agent gets a URL
pinned to the vendor's download host and a mandatory SHA-256.

Shape:
  GET /v3/assets/latest/{feature}/hotspot?os=&architecture=&image_type=jre&vendor=eclipse
      -> [{"binary": {"package": {"link","name","size","checksum"}, ...},
           "release_name": "jdk-21.0.5+11", "version": {"semver", ...}}]
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from minemanager_hub.providers.base import Download, ProviderError, check_url
from minemanager_hub.providers.http import get_json
from minemanager_shared.java import MANAGED_FEATURES

_API = "https://api.adoptium.net/v3/assets/latest"
# Release assets live on GitHub; its redirect to the asset CDN is followed by
# the agent's https-only opener.
_DOWNLOAD_DOMAINS = ("github.com",)

_PLATFORM_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


async def resolve_jre(feature: int, os_name: str, arch: str | None) -> Download:
    if feature not in MANAGED_FEATURES:
        raise ProviderError(f"Java {feature} is not one MineManager installs")
    if not arch:
        raise ProviderError("this node's CPU architecture has no Temurin builds")
    if not (_PLATFORM_RE.match(os_name) and _PLATFORM_RE.match(arch)):
        raise ProviderError(f"unrecognised platform {os_name}/{arch}")
    query = urlencode({
        "architecture": arch, "image_type": "jre", "os": os_name, "vendor": "eclipse",
    })
    assets = await get_json(f"{_API}/{feature}/hotspot?{query}", ttl=3600)
    pkg = None
    for asset in assets if isinstance(assets, list) else []:
        cand = (asset.get("binary") or {}).get("package") or {}
        if str(cand.get("name", "")).endswith(".tar.gz"):
            pkg, release = cand, asset
            break
    if pkg is None:
        raise ProviderError(f"Adoptium has no Temurin {feature} JRE for {os_name}/{arch}")
    checksum = str(pkg.get("checksum") or "")
    if not _SHA256_RE.match(checksum):
        raise ProviderError("Adoptium sent no SHA-256 for this runtime; refusing it")
    return Download(
        url=check_url(str(pkg.get("link", "")), _DOWNLOAD_DOMAINS),
        filename=str(pkg["name"]),
        version=str(release.get("release_name") or feature),
        size=pkg.get("size"),
        checksum=checksum.lower(),
        checksum_algo="sha256",
    )
