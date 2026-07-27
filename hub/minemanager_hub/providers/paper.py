"""PaperMC provider — serves both Paper and Velocity via the v3 "Fill" API.

Paper and Velocity share an identical catalog shape on ``fill.papermc.io``, so a
single provider parameterised by project covers both. Both expose builds.

Shapes (v3):
  GET /v3/projects/{project}
      -> {"versions": {"<minor>": ["<version>", ...], ...}}   # grouped, newest first
  GET /v3/projects/{project}/versions/{version}/builds
      -> [{"id": <int>, "channel": "STABLE", "downloads":
             {"server:default": {"name","size","url","checksums":{"sha256"}}}}, ...]
"""

from __future__ import annotations

from minemanager_hub.providers.base import Build, Download, Provider, ProviderError, Version
from minemanager_hub.providers.http import get_json

_BASE = "https://fill.papermc.io/v3/projects"


def _is_stable_version(v: str) -> bool:
    # Skip Minecraft pre-releases / RCs / snapshots and Velocity -SNAPSHOTs.
    return "-" not in v


class PaperProvider(Provider):
    has_builds = True

    def __init__(self, project: str, software: str, label: str) -> None:
        self.project = project
        self.software = software
        self.label = label

    async def versions(self) -> list[Version]:
        data = await get_json(f"{_BASE}/{self.project}", ttl=600)
        groups = data.get("versions", {})
        out: list[Version] = []
        for arr in groups.values():          # dict is newest-group first
            for v in arr:                    # newest within group first
                if _is_stable_version(v):
                    out.append(Version(id=v, label=v))
        return out

    async def _raw_builds(self, version: str) -> list[dict]:
        data = await get_json(f"{_BASE}/{self.project}/versions/{version}/builds", ttl=120)
        if not isinstance(data, list):
            raise ProviderError(f"no builds for {self.label} {version}")
        return data

    async def builds(self, version: str) -> list[Build]:
        out: list[Build] = []
        for b in await self._raw_builds(version):   # newest first
            bid = str(b["id"])
            out.append(Build(id=bid, label=f"Build {bid}", channel=b.get("channel")))
        return out

    async def resolve(self, version: str, build: str | None) -> Download:
        raw = await self._raw_builds(version)
        if not raw:
            raise ProviderError(f"no builds available for {self.label} {version}")
        if build:
            chosen = next((b for b in raw if str(b["id"]) == str(build)), None)
            if chosen is None:
                raise ProviderError(f"build {build} not found for {self.label} {version}")
        else:
            chosen = raw[0]  # newest

        downloads = chosen.get("downloads", {})
        dl = downloads.get("server:default") or (next(iter(downloads.values()), None))
        if not dl or "url" not in dl:
            raise ProviderError(f"no downloadable jar for {self.label} {version} build {chosen['id']}")
        return Download(
            url=dl["url"],
            filename=dl.get("name", f"{self.project}-{version}-{chosen['id']}.jar"),
            version=version,
            build=str(chosen["id"]),
            size=dl.get("size"),
            checksum=(dl.get("checksums") or {}).get("sha256"),
            checksum_algo="sha256",
        )
