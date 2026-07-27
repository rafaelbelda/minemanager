"""Shared async HTTP client + a small TTL cache for upstream catalog fetches.

Version/build listings change rarely, so we cache them briefly to keep the UI
snappy and stay friendly to the upstream APIs.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from minemanager_hub.providers.base import ProviderError

_USER_AGENT = "minemanager/0.1 (+https://github.com/rafaelbelda/minemanager)"

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"user-agent": _USER_AGENT, "accept": "application/json"},
            follow_redirects=True,
        )
    return _client


class _TTLCache:
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if hit is None:
            return None
        expires, value = hit
        if time.monotonic() > expires:
            self._d.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._d[key] = (time.monotonic() + ttl, value)


_cache = _TTLCache()


async def get_json(url: str, *, ttl: float = 300.0) -> Any:
    """GET and parse JSON, caching the parsed body for ``ttl`` seconds."""
    cached = _cache.get(url)
    if cached is not None:
        return cached
    try:
        resp = await _get_client().get(url)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(f"upstream returned {exc.response.status_code} for {url}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"could not reach upstream: {exc}") from exc
    except ValueError as exc:
        raise ProviderError(f"upstream sent invalid JSON: {exc}") from exc
    _cache.set(url, data, ttl)
    return data


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
