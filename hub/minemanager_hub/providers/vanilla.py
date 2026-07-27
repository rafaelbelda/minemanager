"""Vanilla provider — official Mojang server jars. No builds.

Shapes:
  version_manifest_v2.json
      -> {"latest": {...}, "versions": [{"id","type","url"}, ...]}   # newest first
  <per-version url>
      -> {"downloads": {"server": {"url","sha1","size"}}}            # may be absent
"""

from __future__ import annotations

from minemanager_hub.providers.base import Build, Download, Provider, ProviderError, Version
from minemanager_hub.providers.http import get_json

_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


class VanillaProvider(Provider):
    software = "vanilla"
    label = "Vanilla"
    has_builds = False

    async def _manifest(self) -> dict:
        return await get_json(_MANIFEST, ttl=600)

    async def versions(self) -> list[Version]:
        data = await self._manifest()
        return [
            Version(id=v["id"], label=v["id"], channel=v["type"])
            for v in data.get("versions", [])
            if v.get("type") == "release"        # releases only; skip snapshots
        ]

    async def builds(self, version: str) -> list[Build]:
        return []

    async def resolve(self, version: str, build: str | None) -> Download:
        data = await self._manifest()
        entry = next((v for v in data.get("versions", []) if v["id"] == version), None)
        if entry is None:
            raise ProviderError(f"unknown Minecraft version {version}")
        meta = await get_json(entry["url"], ttl=3600)  # per-version metadata is immutable
        server = (meta.get("downloads") or {}).get("server")
        if not server or "url" not in server:
            raise ProviderError(f"Mojang has no server jar for {version}")
        return Download(
            url=server["url"],
            filename="server.jar",
            version=version,
            build=None,
            size=server.get("size"),
            checksum=server.get("sha1"),
            checksum_algo="sha1",
        )
