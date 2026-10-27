"""Untrusted upstream metadata.

The catalog decides which URL the agent fetches, inside the node's network,
and which checksum it accepts. A tampered or broken catalog must not aim that
fetch off the vendor's domains, downgrade it to plain http, or smuggle a path
into the next upstream request.
"""

from __future__ import annotations

import httpx
import pytest

from minemanager_hub.providers import ProviderError, http
from minemanager_hub.providers.base import check_id, check_url
from minemanager_hub.providers.paper import PaperProvider
from minemanager_hub.providers.vanilla import VanillaProvider


class TestCheckUrl:
    @pytest.mark.parametrize("url", [
        "https://fill-data.papermc.io/v1/objects/abc/paper.jar",
        "https://papermc.io/x.jar",
    ])
    def test_vendor_https_urls_pass(self, url):
        assert check_url(url, ("papermc.io",)) == url

    @pytest.mark.parametrize("url", [
        "http://fill-data.papermc.io/paper.jar",            # downgrade
        "https://169.254.169.254/latest/meta-data/",          # SSRF into the node's net
        "https://papermc.io.evil.com/paper.jar",              # suffix trick
        "https://evilpapermc.io/paper.jar",                   # no dot boundary
        "file:///etc/passwd",
        "https://user@evil.com/papermc.io",
    ])
    def test_anything_else_is_refused(self, url):
        with pytest.raises(ProviderError):
            check_url(url, ("papermc.io",))


class TestCheckId:
    @pytest.mark.parametrize("value", ["1.21.8", "3.4.0", "60", "1.20.1-rc1"])
    def test_real_ids_pass(self, value):
        assert check_id("version", value) == value

    @pytest.mark.parametrize("value", ["..", "../velocity", "1.21/../x", "", "a?b", "a b"])
    def test_path_tricks_are_refused(self, value):
        with pytest.raises(ProviderError):
            check_id("version", value)


def _patch_json(monkeypatch, module, responses: dict):
    async def fake_get_json(url, *, ttl=0):
        return responses[url]
    monkeypatch.setattr(module, "get_json", fake_get_json)


class TestResolveRefusesBadCatalogs:
    async def test_paper_download_off_domain(self, monkeypatch):
        from minemanager_hub.providers import paper
        url = "https://fill.papermc.io/v3/projects/paper/versions/1.21.8/builds"
        _patch_json(monkeypatch, paper, {url: [{"id": 1, "downloads": {"server:default": {
            "url": "https://attacker.example/paper.jar", "checksums": {"sha256": "00"}}}}]})
        with pytest.raises(ProviderError, match="outside"):
            await PaperProvider("paper", "paper", "Paper").resolve("1.21.8", None)

    async def test_paper_version_cannot_walk_the_upstream_path(self):
        with pytest.raises(ProviderError):
            await PaperProvider("paper", "paper", "Paper").resolve("../../velocity", None)

    async def test_vanilla_metadata_url_off_domain(self, monkeypatch):
        from minemanager_hub.providers import vanilla
        manifest = {"versions": [{"id": "1.21.8", "type": "release",
                                  "url": "http://piston-meta.mojang.com/v1/1.21.8.json"}]}
        _patch_json(monkeypatch, vanilla, {vanilla._MANIFEST: manifest})
        with pytest.raises(ProviderError, match="non-https"):
            await VanillaProvider().resolve("1.21.8", None)


class TestCatalogTransport:
    async def test_a_redirect_off_https_is_refused(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.scheme == "https":
                return httpx.Response(302, headers={"location": "http://fill.papermc.io/x"})
            return httpx.Response(200, json={"checksums": {"sha256": "attacker"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        monkeypatch.setattr(http, "_client", client)
        monkeypatch.setattr(http, "_cache", http._TTLCache())
        with pytest.raises(ProviderError, match="off https"):
            await http.get_json("https://fill.papermc.io/x")
        await client.aclose()
